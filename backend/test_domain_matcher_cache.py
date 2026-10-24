"""Domain discovery cache correctness under edits and concurrent refreshes."""
import copy
import os
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.utils import domain_matcher_cache as cache


@pytest.fixture
def rules(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, 'get_rules_dir', lambda: str(tmp_path))
    cache.clear_cache()
    config = {
        'rule_configs': [
            {'id': 'first', 'itemType': 'rule', 'rule_type': 'DOMAIN-SUFFIX',
             'value': 'first.example', 'policy': 'DIRECT'},
            {'id': 'set', 'itemType': 'ruleset', 'library_rule_id': 'lib', 'name': 'domains', 'policy': 'PROXY'},
        ],
        'rule_library': [{'id': 'lib', 'name': 'domains', 'source_type': 'content', 'behavior': 'classical',
                          'content': 'DOMAIN-SUFFIX,matched.example\n'}],
    }
    yield config, tmp_path
    cache.clear_cache()


def test_reuses_positive_and_negative_matches_without_caching_traffic(rules, monkeypatch):
    config, _ = rules
    calls = []
    original = cache.RuleConfigMatcher.match
    def match(self, host):
        calls.append(host)
        return original(self, host)
    monkeypatch.setattr(cache.RuleConfigMatcher, 'match', match)
    first = cache.local_matcher(config, 'default')
    result = first.match('www.matched.example')
    result['policy'] = 'corrupted'
    assert first.match('missing.example') is None
    changed = copy.deepcopy(config)
    changed['agents'] = [{'last_heartbeat': 'new'}]
    changed['domain_discovery'] = {'ignored': ['ignored.example']}
    second = cache.local_matcher(changed, 'default')
    assert second is first
    assert second.match('www.matched.example')['policy'] == 'PROXY'
    assert second.match('missing.example') is None
    assert calls == ['www.matched.example', 'missing.example']


@pytest.mark.parametrize('edit', ['inline', 'policy', 'enabled', 'order'])
def test_config_edits_invalidate_immediately(rules, edit):
    config, _ = rules
    config['rule_library'][0]['content'] += 'DOMAIN-SUFFIX,first.example\n'
    old = cache.local_matcher(config, 'default')
    assert old.match('new.example') is None
    assert old.match('first.example')['policy'] == 'DIRECT'
    changed = copy.deepcopy(config)
    if edit == 'inline':
        changed['rule_library'][0]['content'] += 'DOMAIN,new.example\n'
    elif edit == 'policy':
        changed['rule_configs'][0]['policy'] = 'PROXY'
    elif edit == 'enabled':
        changed['rule_configs'][0]['enabled'] = False
    else:
        changed['rule_configs'].reverse()
    new = cache.local_matcher(changed, 'default')
    assert new is not old
    assert new.match('new.example' if edit == 'inline' else 'first.example')['policy'] == 'PROXY'


def test_file_creation_replacement_and_deletion_invalidate_negative_matches(rules):
    config, root = rules
    old = cache.local_matcher(config, 'default')
    assert old.match('remote.example') is None
    path = root / 'domains.list'
    path.write_text('DOMAIN,remote.example\n')
    created = cache.local_matcher(config, 'default')
    assert created is not old
    assert created.match('remote.example')['policy'] == 'PROXY'
    assert created.match('newone.example') is None
    stat = path.stat()
    replacement = root / 'replacement'
    replacement.write_text('DOMAIN,newone.example\n')
    os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    replacement.replace(path)
    replaced = cache.local_matcher(config, 'default')
    assert replaced is not created
    assert replaced.match('remote.example') is None
    assert replaced.match('newone.example')['policy'] == 'PROXY'
    path.unlink()
    removed = cache.local_matcher(config, 'default')
    assert removed.match('newone.example') is None
    assert removed.match('matched.example')['policy'] == 'PROXY'


def test_parallel_refreshes_compute_each_host_once(rules, monkeypatch):
    config, _ = rules
    original = cache.RuleConfigMatcher.match
    entered, release = threading.Event(), threading.Event()
    calls = []
    def match(self, host):
        calls.append(host)
        entered.set()
        assert release.wait(3)
        return original(self, host)
    monkeypatch.setattr(cache.RuleConfigMatcher, 'match', match)
    def refresh():
        matcher = cache.local_matcher(copy.deepcopy(config), 'default')
        return matcher.match('matched.example'), matcher.match('missing.example')
    with ThreadPoolExecutor(max_workers=8) as pool:
        tasks = [pool.submit(refresh) for _ in range(8)]
        try:
            assert entered.wait(3)
        finally:
            release.set()
        results = [task.result(timeout=3) for task in tasks]
    assert all(result == results[0] for result in results)
    assert calls == ['matched.example', 'missing.example']


def test_cache_is_bounded_and_profiles_are_isolated(rules, monkeypatch):
    config, _ = rules
    monkeypatch.setattr(cache, 'MAX_MATCHERS', 2)
    monkeypatch.setattr(cache, 'MAX_HOST_RESULTS', 2)
    first = cache.local_matcher(config, 'first')
    second = cache.local_matcher(config, 'second')
    assert first is not second
    cache.local_matcher(config, 'third')
    assert len(cache._CACHE) == 2
    assert cache.local_matcher(config, 'first') is not first
    for host in ['a.example', 'b.example', 'c.example']:
        first.match(host)
    assert list(first._results) == ['b.example', 'c.example']


def test_healthy_indexes_survive_multiple_automatic_probe_rounds(rules, monkeypatch):
    config, root = rules
    (root / 'domains.list').write_text('DOMAIN-SUFFIX,matched.example\n')
    now = [1.0]
    monkeypatch.setattr(cache.time, 'monotonic', lambda: now[0])
    reads = []
    original = cache._LocalSource.__call__
    def read(self, item, library):
        reads.append(library['id'])
        return original(self, item, library)
    monkeypatch.setattr(cache._LocalSource, '__call__', read)
    first = cache.local_matcher(config, 'default')
    assert first.match('matched.example')
    indexes = list(first._matcher._indexes.values())
    for _ in range(4):
        now[0] += 600
        current = cache.local_matcher(config, 'default')
        assert current is first
        assert current.match(f"host{now[0]:.0f}.matched.example")
        assert list(current._matcher._indexes.values()) == indexes
    assert reads == ['lib']


def test_idle_eviction_releases_indexes_and_existing_handles_remain_usable(rules, monkeypatch):
    config, _ = rules
    now = [1.0]
    monkeypatch.setattr(cache.time, 'monotonic', lambda: now[0])
    first = cache.local_matcher(config, 'first')
    first.match('matched.example')
    owner = weakref.ref(first)
    index = weakref.ref(next(iter(first._matcher._indexes.values())))
    kept = cache.local_matcher(config, 'kept')
    del first
    now[0] += cache.MATCHER_IDLE_SECONDS
    with cache._LOCK:
        cache._evict_idle_locked(now[0])
    assert not cache._CACHE
    # No cyclic GC is needed to release a cache with no outstanding users.
    assert owner() is None
    assert index() is None
    assert kept.match('matched.example')['policy'] == 'PROXY'


def test_background_cleanup_runs_without_another_request(rules, monkeypatch):
    config, _ = rules
    monkeypatch.setattr(cache, 'MATCHER_IDLE_SECONDS', 0.025)
    monkeypatch.setattr(cache, 'CLEANUP_INTERVAL_SECONDS', 0.01)
    matcher = cache.local_matcher(config, 'default')
    matcher.match('matched.example')
    owner = weakref.ref(matcher)
    del matcher
    deadline = time.monotonic() + 2
    while owner() is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert owner() is None
    with cache._LOCK:
        assert not cache._CACHE
        assert cache._CLEANUP_TIMER is None


def test_clear_cancels_cleanup_and_stale_callbacks_cannot_reschedule(rules):
    config, _ = rules
    cache.local_matcher(config, 'default')
    generation = cache._CLEANUP_GENERATION
    timer = cache._CLEANUP_TIMER
    cache.clear_cache()
    timer.join(timeout=1)
    assert not timer.is_alive()
    current = cache.local_matcher(config, 'default')
    current_timer = cache._CLEANUP_TIMER
    cache._cleanup_idle(generation)
    assert cache._CLEANUP_TIMER is current_timer
    assert cache.local_matcher(config, 'default') is current


def test_match_touches_idle_time_and_active_match_is_never_expired(rules, monkeypatch):
    config, _ = rules
    now = [1.0]
    monkeypatch.setattr(cache.time, 'monotonic', lambda: now[0])
    matcher = cache.local_matcher(config, 'default')
    now[0] += cache.MATCHER_IDLE_SECONDS - 1
    assert matcher.match('matched.example')
    now[0] += 2
    with cache._LOCK:
        cache._evict_idle_locked(now[0])
    assert cache._CACHE
    entered, release = threading.Event(), threading.Event()
    original = matcher._matcher.match
    def blocking(host):
        entered.set()
        assert release.wait(3)
        return original(host)
    monkeypatch.setattr(matcher._matcher, 'match', blocking)
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(matcher.match, 'new.matched.example')
        try:
            assert entered.wait(3)
            now[0] += cache.MATCHER_IDLE_SECONDS + 1
            with cache._LOCK:
                cache._evict_idle_locked(now[0])
                assert matcher.active == 1
                assert cache._CACHE
            # Registry acquisition must not wait for the active index lock.
            assert cache.local_matcher(config, 'default') is matcher
        finally:
            release.set()
        assert task.result(timeout=3)
    assert matcher.active == 0
    assert matcher.last_used == now[0]


def test_failed_file_retries_without_rebuilding_healthy_indexes(rules, monkeypatch):
    config, root = rules
    now = [1.0]
    monkeypatch.setattr(cache.time, 'monotonic', lambda: now[0])
    config['rule_configs'] = [
        {'id': 'broken', 'itemType': 'ruleset', 'library_rule_id': 'broken', 'policy': 'FIRST'},
        config['rule_configs'][1],
    ]
    config['rule_library'].append({'id': 'broken', 'name': 'broken', 'source_type': 'content', 'behavior': 'classical',
                                   'content': 'DOMAIN,fallback.example'})
    (root / 'broken.list').write_text('DOMAIN,matched.example\nDOMAIN,recovered.example')
    (root / 'domains.list').write_text('DOMAIN,matched.example')
    reads, fail = [], [True]
    real_open = open
    def opening(path, *args, **kwargs):
        reads.append(os.path.basename(path))
        if fail[0] and os.path.basename(path) == 'broken.list':
            raise PermissionError('temporarily unavailable')
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(cache, 'open', opening, raising=False)
    matcher = cache.local_matcher(config, 'default')
    assert matcher.match('fallback.example')['policy'] == 'FIRST'
    assert matcher.match('matched.example')['policy'] == 'PROXY'
    assert matcher.match('recovered.example') is None
    healthy = matcher._matcher._indexes[('source', 'lib', 'classical', 'text')]
    fail[0] = False
    now[0] += cache.READ_RETRY_SECONDS - 1
    assert matcher.match('matched.example')['policy'] == 'PROXY'
    now[0] += 1
    assert matcher.match('matched.example')['policy'] == 'FIRST'
    assert matcher.match('recovered.example')['policy'] == 'FIRST'
    assert matcher.match('fallback.example') is None
    assert matcher._matcher._indexes[('source', 'lib', 'classical', 'text')] is healthy
    assert reads == ['broken.list', 'domains.list', 'broken.list']
    assert matcher._source.retry_at is None


def test_legitimate_empty_file_is_reused_and_change_is_detected(rules, monkeypatch):
    config, root = rules
    path = root / 'domains.list'
    path.write_text('')
    now = [1.0]
    monkeypatch.setattr(cache.time, 'monotonic', lambda: now[0])
    matcher = cache.local_matcher(config, 'default')
    assert matcher.match('matched.example') is None
    assert matcher._source.retry_at is None
    indexes = list(matcher._matcher._indexes.values())
    now[0] += 600
    assert cache.local_matcher(config, 'default') is matcher
    assert matcher.match('still.missing.example') is None
    assert list(matcher._matcher._indexes.values()) == indexes
    path.write_text('DOMAIN,matched.example')
    assert cache.local_matcher(config, 'default').match('matched.example')


def test_missing_url_source_stays_local_and_recovers_when_file_appears(rules, monkeypatch):
    config, root = rules
    config['rule_library'][0].update(source_type='url', url='https://must-not-fetch.invalid/rules')
    import requests
    def network(*args, **kwargs):
        pytest.fail('local matching must not fetch remote rules')
    monkeypatch.setattr(requests, 'get', network)
    old = cache.local_matcher(config, 'default')
    assert old.match('matched.example') is None
    assert old._source.retry_at is not None
    from backend.utils.rule_utils import rule_cache_filename
    path = root / rule_cache_filename(config['rule_library'][0])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('DOMAIN,matched.example')
    current = cache.local_matcher(config, 'default')
    assert current is not old
    assert current.match('matched.example')


def test_lazy_load_uses_frozen_config_and_repository(rules, monkeypatch, tmp_path):
    config, root = rules
    (root / 'domains.list').write_text('DOMAIN,original.example')
    matcher = cache.local_matcher(config, 'default')
    other = root / 'other'
    other.mkdir()
    (other / 'domains.list').write_text('DOMAIN,changed.example')
    config['rule_configs'][1]['policy'] = 'CHANGED'
    config['rule_library'][0]['content'] = 'DOMAIN,changed.example'
    monkeypatch.setattr(cache, 'get_rules_dir', lambda: str(other))
    assert matcher.match('original.example')['policy'] == 'PROXY'
    assert matcher.match('changed.example') is None
    current = cache.local_matcher(config, 'default')
    assert current is not matcher
    assert current.match('changed.example')['policy'] == 'CHANGED'


def test_duplicate_library_references_share_index_but_keep_first_rule_metadata(rules):
    config, _ = rules
    first = config['rule_configs'][1]
    config['rule_configs'].append(dict(first, id='later', policy='LATER'))
    matcher = cache.local_matcher(config, 'default')
    assert matcher.match('missing.example') is None  # Visit both references.
    assert len(matcher._matcher._indexes) == 1
    hit = matcher.match('matched.example')
    assert hit['rule_id'] == 'set'
    assert hit['policy'] == 'PROXY'
    assert hit['priority'] == 2


def test_replaced_file_during_lazy_read_is_retried_on_same_handle(rules, monkeypatch):
    config, root = rules
    path = root / 'domains.list'
    path.write_text('DOMAIN,old.example')
    now = [1.0]
    monkeypatch.setattr(cache.time, 'monotonic', lambda: now[0])
    matcher = cache.local_matcher(config, 'default')
    original_stamp = cache._file_stamp
    calls = [0]
    def stamp(stat):
        calls[0] += 1
        # After fstat-before/read/fstat-after, replace the path atomically.
        if calls[0] == 2:
            replacement = root / 'replacement'
            replacement.write_text('DOMAIN,new.example')
            replacement.replace(path)
        return original_stamp(stat)
    monkeypatch.setattr(cache, '_file_stamp', stamp)
    assert matcher.match('old.example')
    assert matcher.match('new.example') is None
    assert matcher._source.retry_at is not None
    now[0] += cache.READ_RETRY_SECONDS
    assert matcher.match('new.example')
    assert matcher.match('old.example') is None


def test_missing_expected_file_retries_inline_fallback_on_existing_handle(rules, monkeypatch):
    config, root = rules
    path = root / 'domains.list'
    path.write_text('DOMAIN,file.example')
    now = [1.0]
    monkeypatch.setattr(cache.time, 'monotonic', lambda: now[0])
    matcher = cache.local_matcher(config, 'default')
    path.unlink()
    assert matcher.match('matched.example')
    assert matcher.match('file.example') is None
    assert matcher._source.retry_at is not None
    path.write_text('DOMAIN,file.example')
    now[0] += cache.READ_RETRY_SECONDS
    assert matcher.match('file.example')
    assert matcher.match('matched.example') is None
    # One subsequent stable read acknowledges the changed stamp; it must not
    # cause all later requests to re-read the now healthy source indefinitely.
    now[0] += cache.READ_RETRY_SECONDS
    assert matcher.match('file.example')
    index = next(iter(matcher._matcher._indexes.values()))
    assert matcher._source.retry_at is None
    now[0] += cache.READ_RETRY_SECONDS
    assert matcher.match('another.example') is None
    assert next(iter(matcher._matcher._indexes.values())) is index
