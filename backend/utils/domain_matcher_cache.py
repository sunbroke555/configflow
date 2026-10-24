"""Bounded, idle-evicted local rule indexes; never cache Agent/traffic status."""

import atexit
import copy
import hashlib
import json
import os
from backend.common.config_repository import ProfileValidationError
import threading
import time
from collections import OrderedDict

from backend.utils.rule_matcher import RuleConfigMatcher
from backend.utils.rule_utils import get_rules_dir, rule_cache_read_path
from backend.utils.logger import get_logger

MAX_MATCHERS = 4
MAX_HOST_RESULTS = 10000
MATCHER_IDLE_SECONDS = 1800  # Reuse across the automatic probe's 10-minute rounds.
READ_RETRY_SECONDS = 30
CLEANUP_INTERVAL_SECONDS = 60
_CACHE = OrderedDict()
_LOCK = threading.Lock()
_CLEANUP_TIMER = None
_CLEANUP_GENERATION = 0
logger = get_logger(__name__)


def _file_stamp(stat):
    return stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _library_key(item, library_rule):
    # Opt in only when loading is wholly determined by the shared library entry.
    # Missing names fall back to item.name, so those must keep per-item indexes.
    if library_rule and library_rule.get('id') and library_rule.get('name'):
        return library_rule['id']
    return None


def _read_key(item, library_rule):
    key = _library_key(item, library_rule)
    return ('library', key) if key is not None else ('item', id(item))


class _LocalSource:
    """Local-file/inline loader with retry state and no reference back to its owner."""

    def __init__(self, rules_dir, stamps, library, paths):
        self.rules_dir = rules_dir
        self.stamps = stamps
        self.library = library
        self.paths = paths
        self.failed = set()
        self.retry_at = None

    def _retry(self, item, library_rule):
        self.failed.add(_read_key(item, library_rule))
        if self.retry_at is None:
            self.retry_at = time.monotonic() + READ_RETRY_SECONDS

    def __call__(self, item, library_rule):
        name = (library_rule or {}).get('name') or item.get('name')
        if name:
            path = rule_cache_read_path(library_rule or item, self.library, rules_dir=self.rules_dir)
            key = _read_key(item, library_rule)
            if self.paths.get(key, path) != path:
                self._retry(item, library_rule)
            self.paths[key] = path
            try:
                with open(path, 'r', encoding='utf-8') as handle:
                    before = _file_stamp(os.fstat(handle.fileno()))
                    content = handle.read()
                    after = _file_stamp(os.fstat(handle.fileno()))
                # A lazy read may happen after the signature was taken. Do not
                # retain a raced result indefinitely, even through an old handle.
                current = _file_stamp(os.stat(path))
                if before != after or after != self.stamps.get(path) or current != after:
                    self._retry(item, library_rule)
                if before == after == current:
                    # Keep an old handle's retry bounded after a stable read.
                    # The registry signature still detects edits on acquisition.
                    self.stamps[path] = after
                if (library_rule or item).get('source_type', 'url') == 'url':
                    from backend.utils.ruleset_content import validate_remote_rule_content
                    validate_remote_rule_content(content, library_rule or item)
                return content
            except FileNotFoundError:
                if self.stamps.get(path) is not None:
                    self._retry(item, library_rule)
            except (OSError, UnicodeError, ProfileValidationError):
                self._retry(item, library_rule)
        if library_rule and library_rule.get('source_type', 'url') == 'content':
            return library_rule.get('content', '')
        # Unavailable URL sources never fetch from the network on this path.
        self._retry(item, library_rule)
        return ''


class _CachedMatcher:
    def __init__(self, config, rules_dir, stamps, now):
        self.last_used = now
        self.active = 0
        self._lock = threading.Lock()
        self._results = OrderedDict()
        self._source = _LocalSource(rules_dir, stamps, config.get('cache_library', config.get('rule_library', [])), config.get('_source_paths', {}))
        self._matcher = RuleConfigMatcher(
            config.get('rule_configs', []), config.get('rule_library', []),
            self._source, index_key=_library_key,
        )

    def match(self, host):
        # Never hold the registry lock while waiting for an index or doing I/O.
        with _LOCK:
            self.active += 1
            self.last_used = time.monotonic()
        try:
            with self._lock:
                if self._source.retry_at is not None and time.monotonic() >= self._source.retry_at:
                    failed = self._source.failed
                    removed = self._matcher.invalidate_rulesets(
                        lambda item, library: _read_key(item, library) in failed)
                    self._source.failed = set()
                    self._source.retry_at = None
                    # A failed earlier rule can change both positive and negative
                    # results, but unrelated parsed indexes remain reusable.
                    self._results.clear()
                    logger.debug('Rule index cache retried %d unavailable sources', removed)
                if host not in self._results:
                    self._results[host] = self._matcher.match(host)
                    if len(self._results) > MAX_HOST_RESULTS:
                        self._results.popitem(last=False)
                self._results.move_to_end(host)
                return copy.deepcopy(self._results[host])
        finally:
            with _LOCK:
                self.active -= 1
                self.last_used = time.monotonic()


def _signature(config, rules_dir):
    rules = config.get('rule_configs', [])
    library = {item.get('id'): item for item in config.get('rule_library', [])}
    referenced = []
    stamps = {}
    paths = config['_source_paths'] = {}
    for item in rules:
        if (item.get('itemType') != 'ruleset' or not item.get('enabled', True)
                or not item.get('library_enabled', True)):
            continue
        source = library.get(item.get('library_rule_id'))
        referenced.append(source)
        name = (source or {}).get('name') or item.get('name')
        if not name:
            continue
        path = rule_cache_read_path(source or item, config.get('cache_library', config.get('rule_library', [])), rules_dir=rules_dir)
        paths[_read_key(item, source)] = path
        try:
            stamps[path] = _file_stamp(os.stat(path))
        except OSError:
            stamps[path] = None
    payload = json.dumps([rules, referenced, stamps], sort_keys=True, ensure_ascii=False).encode('utf8')
    return hashlib.sha256(payload).digest(), stamps


def _evict_idle_locked(now):
    stale = [slot for slot, (_, matcher) in _CACHE.items()
             if not matcher.active and now - matcher.last_used >= MATCHER_IDLE_SECONDS]
    for slot in stale:
        # Only release registry ownership. In-flight/previously returned handles
        # remain valid, and no timer retains a bound method on the large matcher.
        del _CACHE[slot]
    if stale:
        logger.debug('Rule index cache evicted %d idle matchers', len(stale))


def _schedule_cleanup_locked():
    global _CLEANUP_TIMER
    if _CACHE and _CLEANUP_TIMER is None:
        timer = threading.Timer(CLEANUP_INTERVAL_SECONDS, _cleanup_idle, args=(_CLEANUP_GENERATION,))
        timer.daemon = True
        _CLEANUP_TIMER = timer
        timer.start()


def _cleanup_idle(generation):
    global _CLEANUP_TIMER
    with _LOCK:
        if generation != _CLEANUP_GENERATION:
            return
        _CLEANUP_TIMER = None
        _evict_idle_locked(time.monotonic())
        _schedule_cleanup_locked()


def clear_cache():
    """Release cache ownership and cancel its timer (shutdown/test isolation)."""
    global _CLEANUP_TIMER, _CLEANUP_GENERATION
    with _LOCK:
        _CLEANUP_GENERATION += 1
        if _CLEANUP_TIMER is not None:
            _CLEANUP_TIMER.cancel()
        _CLEANUP_TIMER = None
        _CACHE.clear()


def local_matcher(config, profile_id):
    rules_dir = os.path.realpath(get_rules_dir())
    # A request may mutate its config after returning this handle. Keep only
    # matcher inputs, using the same snapshot for the signature and lazy loads.
    rules = config.get('rule_configs', [])
    referenced = {item.get('library_rule_id') for item in rules if item.get('itemType') == 'ruleset'}
    snapshot = copy.deepcopy({'rule_configs': rules, 'rule_library': [
        item for item in config.get('rule_library', []) if item.get('id') in referenced]})
    # Only names/IDs are needed to disambiguate old caches, not unreferenced content.
    snapshot['cache_library'] = [{'id': item.get('id'), 'name': item.get('name', '')}
                                 for item in config.get('rule_library', [])]
    signature, stamps = _signature(snapshot, rules_dir)
    slot = (rules_dir, profile_id)
    now = time.monotonic()
    with _LOCK:
        _evict_idle_locked(now)
        cached = _CACHE.get(slot)
        if cached is None or cached[0] != signature:
            reason = 'changed' if cached is not None else 'missing'
            cached = (signature, _CachedMatcher(snapshot, rules_dir, stamps, now))
            _CACHE[slot] = cached
            logger.debug('Rule index cache created matcher (%s)', reason)
        cached[1].last_used = now
        _CACHE.move_to_end(slot)
        while len(_CACHE) > MAX_MATCHERS:
            _CACHE.popitem(last=False)
        _schedule_cleanup_locked()
        return cached[1]


atexit.register(clear_cache)
