"""Router snapshot reuse, single-flight reads and current saved client data."""
import copy
import threading
from concurrent.futures import ThreadPoolExecutor

from backend.common import client_discovery
from backend.common import client_discovery_cache as cache_module
from backend.common.client_discovery_cache import DiscoveryCache, Snapshot
from backend.routers.lan_clients import Reading, Sighting

ROUTER = {'id': 'r1', 'type': 'ikuai', 'endpoint': 'http://192.0.2.1', 'password': 'secret'}


def snapshot(host='TV', error=''):
    reading = Reading(sightings={'192.0.2.10': Sighting('192.0.2.10', mac='AA:BB:CC:DD:EE:FF', hostname=host)})
    return Snapshot([(None if error else reading, error)], host)


def test_cached_snapshot_is_immediate_and_stale_requests_background_refresh(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(cache_module.time, 'monotonic', lambda: clock[0])
    cache, calls = DiscoveryCache(), []

    def load():
        calls.append(True)
        return snapshot()
    _, first = cache.read('office', [ROUTER], load)
    assert first == {'cached': False, 'stale': False, 'refresh_error': ''}
    clock[0] += 59
    _, fresh = cache.read('office', [ROUTER], load)
    assert fresh['cached'] and not fresh['stale'] and len(calls) == 1
    clock[0] += 1
    _, stale = cache.read('office', [ROUTER], load)
    assert stale['cached'] and stale['stale'] and len(calls) == 1
    _, refreshed = cache.read('office', [ROUTER], load, refresh=True)
    assert not refreshed['cached'] and not refreshed['stale'] and len(calls) == 2


def test_refresh_failure_keeps_last_useful_snapshot_and_throttles_retry():
    cache = DiscoveryCache()
    cache.read('office', [ROUTER], lambda: snapshot('old'))
    value, meta = cache.read('office', [ROUTER], lambda: snapshot('failed', '认证失败'), refresh=True)
    assert value.checked_at == 'old' and value.readings[0][0] is not None
    assert meta == {'cached': True, 'stale': False, 'refresh_error': '认证失败'}
    _, again = cache.read('office', [ROUTER], lambda: 1 / 0)
    assert again == meta


def test_profiles_and_router_settings_are_isolated_and_mutation_cannot_corrupt_cache():
    cache = DiscoveryCache()
    first, _ = cache.read('office', [ROUTER], lambda: snapshot('office'))
    first.readings[0][0].sightings.clear()
    again, _ = cache.read('office', [ROUTER], lambda: 1 / 0)
    assert again.readings[0][0].sightings
    other, _ = cache.read('home', [ROUTER], lambda: snapshot('home'))
    assert other.checked_at == 'home'
    changed, meta = cache.read('office', [{**ROUTER, 'password': 'new'}], lambda: snapshot('changed'))
    assert changed.checked_at == 'changed' and not meta['cached']
    cache.invalidate('office')
    new, _ = cache.read('office', [{**ROUTER, 'password': 'new'}], lambda: snapshot('new'))
    assert new.checked_at == 'new'


def test_overlapping_cold_reads_and_refreshes_share_one_router_read(monkeypatch):
    cache = DiscoveryCache()
    for refresh in (False, True):
        started, release, joined = threading.Event(), threading.Event(), threading.Event()
        calls = []
        original = cache_module.Future.result

        def result(future, *args, **kwargs):
            joined.set()
            return original(future, *args, **kwargs)

        def load():
            calls.append(True)
            started.set()
            assert release.wait(3)
            return snapshot()

        with monkeypatch.context() as patch:
            patch.setattr(cache_module.Future, 'result', result)
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(cache.read, 'office', [ROUTER], load, refresh)
                try:
                    assert started.wait(3)
                    second = pool.submit(cache.read, 'office', [ROUTER], load, refresh)
                    assert joined.wait(3)
                finally:
                    release.set()
                assert first.result()[0].checked_at == second.result()[0].checked_at
        assert len(calls) == 1


def test_cached_router_readings_join_latest_names_policies_and_infrastructure(monkeypatch):
    cache, reads = DiscoveryCache(), []
    config = {'routers': {'items': [ROUTER]}}
    devices = []
    monkeypatch.setattr(client_discovery, 'list_devices', lambda _: {'devices': copy.deepcopy(devices)})

    def read_all(routers, *_args):
        reads.append(True)
        return [(routers[0], snapshot().readings[0][0], '')]
    monkeypatch.setattr(client_discovery, '_read_all', read_all)
    first = client_discovery.discover(config, cache=cache, profile_id='office')
    assert first['discovered'][0]['ip'] == '192.0.2.10'
    devices.append({'ip': '192.0.2.10', 'name': 'new name', 'mode': 'direct'})
    second = client_discovery.discover(config, cache=cache, profile_id='office')
    assert second['devices'][0]['name'] == 'new name' and second['devices'][0]['mode'] == 'direct'
    assert second['discovered'] == [] and len(reads) == 1
    devices.clear()
    excluded = client_discovery.discover(config, ['192.0.2.10'], cache=cache, profile_id='office')
    assert excluded['discovered'] == [] and len(reads) == 1
    # Removing and re-adding a router must not revive an earlier snapshot.
    client_discovery.discover({}, cache=cache, profile_id='office')
    client_discovery.discover(config, cache=cache, profile_id='office')
    assert len(reads) == 2


def test_invalidated_inflight_read_cannot_overwrite_new_snapshot():
    cache = DiscoveryCache()
    started, release = threading.Event(), threading.Event()

    def load():
        started.set()
        assert release.wait(3)
        return snapshot('old')
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(cache.read, 'office', [ROUTER], load)
        try:
            assert started.wait(3)
            cache.invalidate('office')
            cache.read('office', [ROUTER], lambda: snapshot('new'))
        finally:
            release.set()
        pending.result()
    value, _ = cache.read('office', [ROUTER], lambda: 1 / 0)
    assert value.checked_at == 'new'
