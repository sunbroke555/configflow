"""Small status snapshots never retain full traffic histories or stale files."""
from collections import OrderedDict
from datetime import datetime
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from backend.agents import traffic_store
from backend.agents.traffic_store import TrafficStore
from backend.utils import json_store


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch):
    monkeypatch.setattr(traffic_store, '_STATUS_CACHE', OrderedDict())


def write_data(store, agent='a', status=None):
    data = {'status': status if status is not None else {'value': 'running', 'at': 'now', 'mihomo_version': '1'},
            'days': {'today': {'hosts': {'synthetic.example': {'large': list(range(100))}}}}}
    store._write_json(agent, data)
    return data


def track_reads(store, monkeypatch):
    calls = []
    original = store._read_json

    def read(agent):
        calls.append(agent)
        return original(agent)

    monkeypatch.setattr(store, '_read_json', read)
    return calls


def wait_for_changed_ctime(path, previous):
    # In-place invalidation relies on ctime; older Linux filesystems may keep
    # it unchanged within a clock tick even though Python exposes nanoseconds.
    deadline = time.monotonic() + 2
    while path.stat().st_ctime_ns == previous.st_ctime_ns and time.monotonic() < deadline:
        time.sleep(0.01)
        os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    assert path.stat().st_ctime_ns != previous.st_ctime_ns


def test_status_hit_reuses_only_small_independent_metadata(tmp_path, monkeypatch):
    store = TrafficStore(tmp_path)
    write_data(store)
    calls = track_reads(store, monkeypatch)
    first = store.load_status('a')
    first['value'] = 'mutated'
    assert store.load_status('a')['value'] == 'running'
    assert calls == ['a']
    assert list(traffic_store._STATUS_CACHE.values())[0][1] == {
        'value': 'running', 'at': 'now', 'mihomo_version': '1'}


@pytest.mark.parametrize('atomic', [False, True])
def test_external_same_size_same_mtime_change_invalidates(tmp_path, atomic):
    store = TrafficStore(tmp_path)
    data = write_data(store)
    assert store.load_status('a')['value'] == 'running'
    path = Path(store._path('a'))
    previous = path.stat()
    data['status']['value'] = 'unknown'
    destination = path.with_suffix('.new') if atomic else path
    destination.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')))
    os.utime(destination, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    if atomic:
        os.replace(destination, path)
    else:
        wait_for_changed_ctime(path, previous)
    assert path.stat().st_size == previous.st_size
    assert path.stat().st_mtime_ns == previous.st_mtime_ns
    assert store.load_status('a')['value'] == 'unknown'


def test_replacement_during_read_does_not_label_old_status_with_new_signature(tmp_path, monkeypatch):
    store = TrafficStore(tmp_path)
    write_data(store)
    original = store._read_json
    replaced = False

    def read(agent):
        nonlocal replaced
        data = original(agent)
        if not replaced:
            replaced = True
            write_data(store, status={'value': 'unreachable'})
        return data

    monkeypatch.setattr(store, '_read_json', read)
    assert store.load_status('a')['value'] == 'running'
    assert not traffic_store._STATUS_CACHE
    assert store.load_status('a')['value'] == 'unreachable'


def test_report_populates_status_without_redecoding_history_and_delete_invalidates(tmp_path, monkeypatch):
    store = TrafficStore(tmp_path)
    calls = track_reads(store, monkeypatch)
    store.add_report('a', {'status': 'running', 'items': []})
    assert store.load_status('a')['value'] == 'running'
    returned = store.add_report('a', {'status': 'unreachable', 'items': []})
    returned['value'] = 'caller mutation'
    assert store.load_status('a')['value'] == 'unreachable'
    assert calls == ['a', 'a'], 'Only the two report merges need a history decode'
    store.delete('a')
    assert not traffic_store._STATUS_CACHE
    assert store.load_status('a') is None


def test_full_history_read_populates_independent_status_for_other_store_instances(tmp_path, monkeypatch):
    first, second = TrafficStore(tmp_path), TrafficStore(tmp_path)
    write_data(first)
    loaded = first.load('a')
    loaded['status']['value'] = 'caller mutation'
    calls = track_reads(second, monkeypatch)
    assert second.load_status('a')['value'] == 'running'
    assert calls == []


@pytest.mark.parametrize('after_replace', [False, True])
def test_failed_report_write_never_caches_uncommitted_status(tmp_path, monkeypatch, after_replace):
    store = TrafficStore(tmp_path)
    store.add_report('a', {'status': 'running', 'items': []})
    original = store._write_json

    def fail(agent, data):
        if after_replace:
            original(agent, data)
        raise OSError('write failed')

    monkeypatch.setattr(store, '_write_json', fail)
    with pytest.raises(OSError, match='write failed'):
        store.add_report('a', {'status': 'unreachable', 'items': []})
    assert not traffic_store._STATUS_CACHE
    calls = track_reads(store, monkeypatch)
    assert store.load_status('a')['value'] == ('unreachable' if after_replace else 'running')
    assert calls == ['a']


@pytest.mark.parametrize('atomic', [False, True])
def test_external_change_after_report_write_cannot_acquire_written_status(tmp_path, monkeypatch, atomic):
    store = TrafficStore(tmp_path)
    original = store._write_json

    def replace_after_write(agent, data):
        written = original(agent, data)
        path = Path(store._path(agent))
        previous = path.stat()
        changed = dict(data, status=dict(data['status'], value='unknown'))
        destination = path.with_suffix('.new') if atomic else path
        destination.write_text(json.dumps(changed, ensure_ascii=False, separators=(',', ':')))
        os.utime(destination, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        if atomic:
            os.replace(destination, path)
        else:
            wait_for_changed_ctime(path, previous)
        assert path.stat().st_size == previous.st_size
        assert path.stat().st_mtime_ns == previous.st_mtime_ns
        return written

    monkeypatch.setattr(store, '_write_json', replace_after_write)
    store.add_report('a', {'status': 'running', 'items': []})
    assert not traffic_store._STATUS_CACHE
    assert store.load_status('a')['value'] == 'unknown'


def test_external_replacement_during_atomic_write_does_not_change_written_identity(tmp_path, monkeypatch):
    store = TrafficStore(tmp_path)
    replace = os.replace

    def replace_then_external(source, destination):
        replace(source, destination)
        external = Path(destination).with_suffix('.external')
        external.write_text('{"status":{"value":"unreachable"},"days":{}}')
        replace(external, destination)

    monkeypatch.setattr(json_store.os, 'replace', replace_then_external)
    store.add_report('a', {'status': 'running', 'items': []})
    assert not traffic_store._STATUS_CACHE
    assert store.load_status('a')['value'] == 'unreachable'


@pytest.mark.parametrize('preserve_mtime', [False, True])
def test_external_in_place_edit_during_atomic_write_is_not_cached(tmp_path, monkeypatch, preserve_mtime):
    store = TrafficStore(tmp_path)
    replace = os.replace

    def replace_then_edit(source, destination):
        replace(source, destination)
        path = Path(destination)
        previous = path.stat()
        data = json.loads(path.read_text())
        data['status']['value'] = 'unknown'
        path.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')))
        modified = path.stat()
        timestamp = previous.st_mtime_ns if preserve_mtime else modified.st_mtime_ns + 1_000_000_000
        os.utime(path, ns=(previous.st_atime_ns, timestamp))
        assert path.stat().st_size == previous.st_size

    monkeypatch.setattr(json_store.os, 'replace', replace_then_edit)
    store.add_report('a', {'status': 'running', 'items': []})
    assert not traffic_store._STATUS_CACHE
    assert store.load_status('a')['value'] == 'unknown'


def test_other_json_stores_keep_close_before_replace_without_digest(tmp_path, monkeypatch):
    store = json_store.JsonFileStore(tmp_path)
    opened = []
    builtin_open, replace = open, os.replace

    def track_open(*args, **kwargs):
        handle = builtin_open(*args, **kwargs)
        opened.append(handle)
        return handle

    def replace_closed(source, destination):
        assert opened and all(handle.closed for handle in opened)
        replace(source, destination)

    def unexpected_digest(handle):
        raise AssertionError('Only traffic status caching needs content verification')

    monkeypatch.setattr(json_store, 'open', track_open, raising=False)
    monkeypatch.setattr(json_store.os, 'replace', replace_closed)
    monkeypatch.setattr(json_store, '_file_digest', unexpected_digest)
    assert store._write_json('a', {'hosts': {'example.test': {'value': 'unchanged'}}}) is None
    assert store._read_json('a') == {'hosts': {'example.test': {'value': 'unchanged'}}}


def test_windows_writer_closes_file_before_replacement_and_returns_no_signature(tmp_path, monkeypatch):
    store = TrafficStore(tmp_path)
    opened = []
    builtin_open = open

    def track_open(*args, **kwargs):
        handle = builtin_open(*args, **kwargs)
        opened.append(handle)
        return handle

    def replace_closed(source, destination):
        assert opened and all(handle.closed for handle in opened)
        os.replace(source, destination)

    monkeypatch.setattr(json_store, 'open', track_open, raising=False)
    monkeypatch.setattr(json_store, 'os', SimpleNamespace(
        name='nt', makedirs=os.makedirs, path=os.path, replace=replace_closed))
    assert store._write_json('a', {'status': {'value': 'running'}, 'days': {}}) is None
    assert store._read_json('a')['status']['value'] == 'running'


def test_report_merges_external_history_and_keeps_version_and_retention(tmp_path):
    store = TrafficStore(tmp_path)
    store.add_report('a', {'status': 'running', 'mihomo_version': 'v1', 'items': []},
                     now=datetime(2026, 10, 1))
    data = store.load('a', now=datetime(2026, 10, 1))
    data['days']['2026-10-10'] = {'ip_only_conns': 12, 'dropped': 3, 'hosts': {}}
    store._write_json('a', data)
    store.add_report('a', {'status': 'unreachable', 'ip_only_conns': 5, 'items': []},
                     now=datetime(2026, 10, 10))
    saved = store.load('a', now=datetime(2026, 10, 10))
    assert set(saved['days']) == {'2026-10-10'}
    assert saved['days']['2026-10-10'] == {'ip_only_conns': 17, 'dropped': 3, 'hosts': {}}
    assert store.load_status('a') == {
        'value': 'unreachable', 'at': '2026-10-10T00:00:00', 'mihomo_version': 'v1'}


@pytest.mark.parametrize('content', ['{', '[]', '{"status":{"value":"running"},"days":[]}'])
def test_report_recovers_invalid_history_without_caching_old_status(tmp_path, content):
    store = TrafficStore(tmp_path)
    store.add_report('a', {'status': 'running', 'items': []})
    Path(store._path('a')).write_text(content)
    store.add_report('a', {'status': 'unreachable', 'items': []})
    assert store.load_status('a')['value'] == 'unreachable'
    assert len(store.load('a')['days']) == 1


@pytest.mark.parametrize('content', [None, '{', '[]', '{"status":{"value":"running"},"days":[]}'])
def test_read_errors_and_invalid_documents_are_not_cached(tmp_path, monkeypatch, content):
    store = TrafficStore(tmp_path)
    if content is not None:
        Path(store._path('a')).write_text(content)
    calls = track_reads(store, monkeypatch)
    assert store.load_status('a') is None
    assert store.load_status('a') is None
    assert calls == ['a', 'a']
    assert not traffic_store._STATUS_CACHE


@pytest.mark.parametrize('status', [
    {'value': 'x' * 1025},
    {'value': '界' * 400},
    {'value': {'nested': []}},
    {'value': 'running', 'unexpected': 'extra'},
])
def test_oversized_or_unexpected_status_is_returned_without_caching(tmp_path, monkeypatch, status):
    store = TrafficStore(tmp_path)
    write_data(store, status=status)
    calls = track_reads(store, monkeypatch)
    assert store.load_status('a') == status
    assert store.load_status('a') == status
    assert calls == ['a', 'a']
    assert not traffic_store._STATUS_CACHE


def test_status_cache_is_lru_bounded_and_scoped_by_path(tmp_path, monkeypatch):
    monkeypatch.setattr(traffic_store, '_STATUS_CACHE_LIMIT', 2)
    first, second = TrafficStore(tmp_path / 'one'), TrafficStore(tmp_path / 'two')
    write_data(first, 'a', {'value': 'running'})
    write_data(second, 'a', {'value': 'unreachable'})
    write_data(first, 'b', {'value': 'no_controller'})
    assert first.load_status('a')['value'] == 'running'
    assert second.load_status('a')['value'] == 'unreachable'
    first.load_status('a')
    first.load_status('b')
    assert len(traffic_store._STATUS_CACHE) == 2
    assert str(Path(second._path('a')).absolute()) not in traffic_store._STATUS_CACHE


def test_windows_disables_stat_cache(monkeypatch):
    def unexpected_stat(path):
        raise AssertionError('must not use Windows ctime')

    monkeypatch.setattr(traffic_store, 'os', SimpleNamespace(name='nt', stat=unexpected_stat))
    assert traffic_store._status_signature('unused') is None
