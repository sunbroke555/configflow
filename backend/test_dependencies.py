"""内置 Sub-Store 的版本选择、模式判定与在线更新流程。"""
import hashlib
import os
from types import SimpleNamespace

import pytest

from backend.utils import dependencies as deps


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    builtin = tmp_path / 'opt'
    runtime = tmp_path / 'data' / 'sub-store' / 'runtime'
    builtin.mkdir()
    (builtin / deps.SUB_STORE_ASSET).write_text('builtin')
    (builtin / 'VERSION').write_text('2.40.0\n')
    monkeypatch.setattr(deps, 'SUB_STORE_BUILTIN_DIR', str(builtin))
    monkeypatch.setattr(deps, 'sub_store_runtime_dir', lambda: str(runtime))
    monkeypatch.delenv('SUB_STORE_URL', raising=False)
    monkeypatch.delenv('SUB_STORE_BACKEND_API_PORT', raising=False)
    monkeypatch.setattr('backend.common.config.get_system_config', lambda: {'system_config': {}})
    from backend.utils.sub_store_runtime import SubStoreRuntime
    manager = SubStoreRuntime(tmp_path / 'locks')
    monkeypatch.setattr(manager, '_ready', lambda: True)
    monkeypatch.setattr(deps, 'runtime', manager)
    deps._update_state.update(state='idle', message='', target_version='', finished_at=0.0)
    return builtin, runtime


def _write_runtime(runtime, version):
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / deps.SUB_STORE_ASSET).write_text('runtime')
    (runtime / 'VERSION').write_text(version + '\n')


def test_version_compare():
    assert deps.is_newer('2.42.3', '2.42.2')
    assert deps.is_newer('v2.43.0', '2.42.10')
    assert not deps.is_newer('2.42.3', '2.42.3')
    assert not deps.is_newer('bad', '2.42.3')
    assert deps.is_newer('2.42.3', '')


def test_active_bundle_prefers_newer_runtime(dirs):
    builtin, runtime = dirs
    assert deps.active_bundle() == (str(builtin), '2.40.0')
    _write_runtime(runtime, '2.41.0')
    assert deps.active_bundle() == (str(runtime), '2.41.0')


def test_image_upgrade_overrides_older_online_update(dirs):
    builtin, runtime = dirs
    _write_runtime(runtime, '2.39.9')
    assert deps.active_bundle() == (str(builtin), '2.40.0')


def test_base_url_always_builtin_when_bundled(dirs, monkeypatch):
    from backend.utils import sub_store_client
    monkeypatch.setenv('SUB_STORE_URL', 'http://sub-store:3001')
    monkeypatch.setattr('backend.common.config.get_system_config',
                        lambda: {'system_config': {'sub_store_url': 'http://10.0.0.2:3001'}})
    assert sub_store_client._get_base_url() == 'http://127.0.0.1:3001'


def test_custom_builtin_port_is_shared_by_client_readiness_and_update(dirs, monkeypatch):
    from backend.utils import sub_store_client
    from backend.utils.sub_store_runtime import SubStoreRuntime
    monkeypatch.setenv('SUB_STORE_BACKEND_API_PORT', '15802')
    # An external URL must not divert the bundled process or its conversions.
    monkeypatch.setenv('SUB_STORE_URL', 'http://unrelated-service:3001')
    expected = 'http://127.0.0.1:15802'
    assert sub_store_client._get_base_url() == expected
    requested = []
    def version(base_url, timeout=5):
        requested.append(base_url)
        return '2.40.0'
    monkeypatch.setattr(deps, 'fetch_running_version', version)
    assert SubStoreRuntime()._ready()
    assert deps._wait_for_version('2.40.0') == '2.40.0'
    monkeypatch.setattr(deps, 'fetch_latest_release', lambda **_: ({'version': '2.40.0'}, ''))
    assert deps.start_sub_store_update()[0] is False
    assert requested == [expected, expected, expected]


@pytest.mark.parametrize('value', ['', '0', '65536', '-1', '+3001', '30.01', '3001/path', '３００１'])
def test_invalid_builtin_port_never_falls_back_to_an_unrelated_service(dirs, monkeypatch, value):
    from backend.utils import sub_store_client
    from backend.utils.sub_store_runtime import SubStoreRuntime
    monkeypatch.setenv('SUB_STORE_BACKEND_API_PORT', value)
    calls = []
    monkeypatch.setattr(deps.requests, 'get', lambda *args, **kwargs: calls.append(args))
    for action in (sub_store_client._get_base_url, SubStoreRuntime()._ready):
        with pytest.raises(ValueError, match='SUB_STORE_BACKEND_API_PORT'):
            action()
    assert calls == []


def test_base_url_falls_back_to_env_without_bundle(dirs, monkeypatch):
    from backend.utils import sub_store_client
    builtin, _runtime = dirs
    (builtin / deps.SUB_STORE_ASSET).unlink()
    monkeypatch.setenv('SUB_STORE_URL', 'http://dev-sub-store:3001/')
    assert sub_store_client._get_base_url() == 'http://dev-sub-store:3001'


def test_update_refused_without_bundle(dirs):
    builtin, _runtime = dirs
    (builtin / deps.SUB_STORE_ASSET).unlink()
    ok, message = deps.start_sub_store_update()
    assert not ok and '内置' in message


def _release(content, version='2.41.0'):
    return {'version': version, 'download_url': 'https://github.com/x/sub-store.bundle.js',
            'sha256': hashlib.sha256(content).hexdigest(), 'published_at': '', 'release_url': '',
            'notes': '', 'size': len(content)}


class _FakeResp:
    def __init__(self, content):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        yield self.content


def _patch_io(monkeypatch, content, running_versions):
    monkeypatch.setattr(deps, '_MIN_BUNDLE_SIZE', 1)
    monkeypatch.setattr(deps.requests, 'get', lambda *a, **k: _FakeResp(content))
    monkeypatch.setattr(deps.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout='', stderr=''))
    restarts = []
    monkeypatch.setattr(deps, '_restart_sub_store', lambda: restarts.append(1))
    versions = iter(running_versions)
    monkeypatch.setattr(deps, '_wait_for_version', lambda expected, timeout=40: next(versions))
    return restarts


def test_update_success_writes_runtime_bundle(dirs, monkeypatch):
    _builtin, runtime = dirs
    content = b'console.log("sub-store")'
    restarts = _patch_io(monkeypatch, content, ['2.41.0'])
    deps._update_lock.acquire()
    deps._run_update(_release(content))

    assert deps.get_update_state()['state'] == 'success'
    assert (runtime / deps.SUB_STORE_ASSET).read_bytes() == content
    assert (runtime / 'VERSION').read_text().strip() == '2.41.0'
    assert restarts == [1]
    assert not deps._update_lock.locked()


def test_update_rolls_back_when_new_version_does_not_start(dirs, monkeypatch):
    builtin, runtime = dirs
    _write_runtime(runtime, '2.40.5')
    content = b'broken bundle'
    restarts = _patch_io(monkeypatch, content, ['', '2.40.5'])
    deps._update_lock.acquire()
    deps._run_update(_release(content))

    state = deps.get_update_state()
    assert state['state'] == 'failed' and '回滚' in state['message']
    assert (runtime / 'VERSION').read_text().strip() == '2.40.5'
    assert (runtime / deps.SUB_STORE_ASSET).read_text() == 'runtime'
    assert len(restarts) == 2
    assert not os.path.exists(str(runtime) + '.bak')


def test_update_rejects_checksum_mismatch(dirs, monkeypatch):
    _builtin, runtime = dirs
    restarts = _patch_io(monkeypatch, b'tampered', ['2.41.0'])
    release = _release(b'original')
    deps._update_lock.acquire()
    deps._run_update(release)

    state = deps.get_update_state()
    assert state['state'] == 'failed' and 'SHA256' in state['message']
    assert not (runtime / deps.SUB_STORE_ASSET).exists()
    assert restarts == []


def test_update_restores_previous_bundle_when_restart_raises(dirs, monkeypatch):
    _, runtime = dirs
    _write_runtime(runtime, '2.40.5')
    content = b'new bundle'
    _patch_io(monkeypatch, content, ['2.40.5'])
    attempts = []
    def restart():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError('restart failed')
    monkeypatch.setattr(deps, '_restart_sub_store', restart)
    deps._update_lock.acquire()
    deps._run_update(_release(content))
    assert len(attempts) == 2
    assert '已回滚' in deps.get_update_state()['message']
    assert (runtime / deps.SUB_STORE_ASSET).read_text() == 'runtime'
    assert (runtime / 'VERSION').read_text().strip() == '2.40.5'
