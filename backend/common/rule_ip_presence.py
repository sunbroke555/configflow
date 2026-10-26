"""Whether a classical rule set really has IP lines.

OxiDNS can only match IP rules against an answer, so every IP group makes each
query that no domain rule claimed go to the local upstream first. A classical
list such as TikTok.list has no IP lines at all and must not cause that.

The answer comes from the content OxiDNS would download: the validated library
cache for remote sources (never the network) or the inline content, split by the
same function rule-proxy serves. Unknown content returns None, and callers then
keep the previous assumption that a classical set has IP lines.
"""
import hashlib
import os
import threading

_MEMO = {}
_MEMO_LIMIT = 1024
_LOCK = threading.Lock()


def _has_ip_lines(text):
    from backend.converters.mosdns import RULE_PART_IP, convert_rule_content_for_mosdns
    converted = convert_rule_content_for_mosdns(text, RULE_PART_IP)
    # An empty part comes back as a "# no ip rules" placeholder comment.
    return any(line.strip() and not line.lstrip().startswith('#') for line in converted.splitlines())


def _remember(key, value):
    with _LOCK:
        if len(_MEMO) >= _MEMO_LIMIT:
            _MEMO.clear()
        _MEMO[key] = value
    return value


def _declaration(item, config):
    """The library entry a profile rule set points at, as rule-proxy resolves it."""
    library_id = item.get('library_rule_id')
    if library_id is not None:
        for entry in config.get('rule_library') or []:
            if isinstance(entry, dict) and entry.get('id') == library_id:
                return entry
    return item


def classical_ip_lines(item, config):
    """True or False for a classical set whose content is known, else None."""
    item = _declaration(item, config)
    source_type = item.get('source_type', 'url')
    if source_type == 'content':
        text = item.get('content')
        if not isinstance(text, str):
            return None
        key = ('content', hashlib.sha256(text.encode()).hexdigest())
        if key in _MEMO:
            return _MEMO[key]
        return _remember(key, _has_ip_lines(text))
    if source_type != 'url' or not item.get('url'):
        return None
    from backend.converters.common import ruleset_format
    from backend.utils.rule_utils import read_validated_rule_cache, rule_cache_read_path
    try:
        if ruleset_format(item, config) == 'mrs':
            return None
        path = rule_cache_read_path(item, config.get('rule_library'))
        stat = os.stat(path)
    except (OSError, TypeError, ValueError):
        return None
    key = ('cache', path, stat.st_mtime_ns, stat.st_size)
    if key in _MEMO:
        return _MEMO[key]
    data = read_validated_rule_cache(item, config)
    if data is None:
        return None
    try:
        text = data.decode('utf-8-sig') if isinstance(data, bytes) else data
    except UnicodeDecodeError:
        return None
    return _remember(key, _has_ip_lines(text))
