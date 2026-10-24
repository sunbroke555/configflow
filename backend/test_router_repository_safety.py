"""All configuration writers preserve live RouterOS ownership and publication locks."""
import copy
from concurrent.futures import ThreadPoolExecutor
import multiprocessing

import pytest

from backend.common import config as config_module
from backend.common.config_repository import ProfileInUse, ProfileRepository, ProfileValidationError
from backend.routers.rule_sync_store import job_lock
from backend.routes import routers as routes_module
from backend.test_ros_api_safety import (
    _hold_connection_job_lock, publish, router_api, snapshot,
)
from backend.test_agent_upgrade_auth import authenticated_app  # noqa: F401


CONNECTION = {'id': 'ros', 'name': 'ROS', 'type': 'routeros',
              'endpoint': 'http://192.168.0.1', 'username': 'admin',
              'password': 'secret', 'verify_tls': True}
MANIFEST = {'owner': 'configflow:test', 'resources': [
    {'path': '/ip/firewall/mangle', 'id': '*1', 'fields': {'comment': 'configflow:test'}},
]}


@pytest.fixture
def repository(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, 'DATA_DIR', str(tmp_path))
    repository = ProfileRepository(tmp_path)
    repository.create_profile({'id': 'live', 'name': 'Live'})
    repository.save_profile('live', {'routers': {'items': [CONNECTION]}})
    return repository


def persist(repository, manifest=MANIFEST):
    router = repository.get_profile('live')['routers']['items'][0]
    with job_lock('live', router['id'], router) as acquired:
        assert acquired
        repository.save_router_publication('live', router, manifest=manifest)
    return repository.get_profile('live')['routers']['items'][0]


def replace_routers(repository, writer, routers):
    if writer == 'profile-import':
        repository.import_profile('live', {'routers': routers})
    elif writer == 'whole-import':
        document = repository.export_all()
        document['profiles']['live']['routers'] = routers
        repository.import_all(document)
    elif writer == 'fields':
        repository.update_profile_fields('live', {'routers': routers})
    elif writer == 'save':
        repository.save_profile('live', {'routers': routers})
    else:
        def mutate(config):
            config['routers'] = routers
        repository.update_profile_transaction('live', mutate)


@pytest.mark.parametrize('writer', ['profile-import', 'whole-import', 'fields', 'save', 'transaction'])
@pytest.mark.parametrize('change', ['remove-router', 'remove-manifest', 'replace-manifest', 'id',
                                    'type', 'endpoint'])
def test_every_writer_rejects_live_ownership_loss(repository, writer, change):
    persist(repository)
    before = repository.export_all()
    raw = repository._store.fingerprint()
    routers = copy.deepcopy(before['profiles']['live']['routers'])
    router = routers['items'][0]
    if change == 'remove-router':
        routers['items'] = []
    elif change == 'remove-manifest':
        router.pop('ros_policy_state')
    elif change == 'replace-manifest':
        router['ros_policy_state']['resources'] = []
    else:
        router[change] = False if change == 'verify_tls' else 'changed'
    with pytest.raises(ProfileInUse):
        replace_routers(repository, writer, routers)
    assert repository._store.fingerprint() == raw
    assert repository.export_all() == before


@pytest.mark.parametrize('writer', ['profile-import', 'whole-import', 'fields', 'save', 'transaction'])
@pytest.mark.parametrize('field', ['username', 'password', 'verify_tls'])
def test_credential_repair_preserves_ownership_in_every_writer(repository, writer, field):
    persist(repository)
    before = repository.export_all()
    routers = copy.deepcopy(before['profiles']['live']['routers'])
    routers['items'][0][field] = False if field == 'verify_tls' else 'replacement'
    replace_routers(repository, writer, routers)
    saved = repository.get_profile('live')['routers']['items'][0]
    assert saved == routers['items'][0]
    assert saved['ros_policy_state'] == MANIFEST


@pytest.mark.parametrize('field', ['username', 'password', 'verify_tls'])
def test_live_publication_still_locks_credential_repairs(repository, field):
    router = persist(repository)
    before = repository.export_all()
    replacement = {**router, field: False if field == 'verify_tls' else 'replacement'}
    with job_lock('live', router['id'], router) as acquired:
        assert acquired
        with pytest.raises(ProfileInUse):
            replace_routers(repository, 'profile-import', {'items': [replacement]})
    assert repository.export_all() == before


@pytest.mark.parametrize('operation', ['delete-profile', 'reset'])
def test_delete_and_reset_keep_live_manifest_and_profile_artifacts(repository, operation):
    persist(repository)
    artifact = repository.profile_path('live', 'generated/owned.txt')
    artifact.parent.mkdir(parents=True)
    artifact.write_text('retain me')
    before = repository.export_all()
    with pytest.raises(ProfileInUse):
        if operation == 'delete-profile':
            repository.delete_profile('live')
        else:
            repository.reset_all()
    assert repository.export_all() == before
    assert artifact.read_text() == 'retain me'


def test_duplicate_ids_cannot_shadow_the_active_manifest(repository):
    persist(repository)
    before = repository.export_all()
    routers = copy.deepcopy(before['profiles']['live']['routers'])
    routers['items'].insert(0, copy.deepcopy(CONNECTION))
    with pytest.raises(ProfileValidationError):
        repository.import_profile('live', {'routers': routers})
    assert repository.export_all() == before


def test_verified_publication_and_withdrawal_preserve_concurrent_metadata(repository):
    original = repository.get_profile('live')['routers']['items'][0]
    with job_lock('live', original['id'], original) as acquired:
        assert acquired
        repository.update_profile_transaction('live',
            lambda config: config['routers']['items'][0].update(name='Concurrent edit'))
        repository.save_router_publication('live', original, manifest=MANIFEST,
                                           last_release={'ok': True})
        # Restored RouterOS rows may receive new IDs; the rollback readback is
        # authoritative even when the caller still has the original snapshot.
        restored = copy.deepcopy(MANIFEST)
        restored['resources'][0]['id'] = '*2'
        repository.save_router_publication('live', original, manifest=restored)
        saved = repository.get_profile('live')['routers']['items'][0]
        assert saved['ros_policy_state'] == restored
        assert saved['name'] == 'Concurrent edit' and saved['last_release'] == {'ok': True}
        repository.save_router_publication('live', original, manifest=None)
    repository.import_profile('live', {'routers': {'items': []}})
    assert repository.get_profile('live')['routers']['items'] == []


def test_rule_sync_persists_rollback_even_when_it_matches_initial_manifest(repository, monkeypatch):
    from types import SimpleNamespace
    from backend.routers.rule_sync import _save_manifest

    router = persist(repository)
    monkeypatch.setattr(config_module, '_repository', repository)
    changed = copy.deepcopy(MANIFEST)
    changed['resources'][0]['id'] = '*2'
    with job_lock('live', router['id'], router) as acquired:
        assert acquired
        _save_manifest('live', router, SimpleNamespace(ros_policy_manifest=changed))
        assert repository.get_profile('live')['routers']['items'][0]['ros_policy_state'] == changed
        _save_manifest('live', router, SimpleNamespace(ros_policy_manifest=MANIFEST))
        assert repository.get_profile('live')['routers']['items'][0]['ros_policy_state'] == MANIFEST


@pytest.mark.parametrize('change', ['endpoint', 'username', 'password', 'verify_tls'])
def test_publication_cannot_persist_against_a_different_connection(repository, change):
    stale = copy.deepcopy(CONNECTION)
    stale[change] = False if change == 'verify_tls' else 'other'
    before = repository.export_all()
    with job_lock('live', CONNECTION['id'], CONNECTION) as acquired:
        assert acquired
        with pytest.raises(ProfileInUse):
            repository.save_router_publication('live', stale, manifest=MANIFEST)
    assert repository.export_all() == before


def test_settings_commit_does_not_reacquire_held_job_lock(repository):
    with job_lock('live', CONNECTION['id'], CONNECTION) as acquired:
        assert acquired
        changed = {**CONNECTION, 'password': 'new password'}
        repository.save_router_settings('live', CONNECTION, changed)
        repository.save_router_settings('live', changed, None)
    assert repository.get_profile('live')['routers']['items'] == []


@pytest.mark.parametrize('field', ['type', 'endpoint', 'username', 'password', 'verify_tls'])
def test_ordinary_writer_cannot_change_connection_during_first_publication(repository, field):
    before = repository.export_all()
    routers = copy.deepcopy(before['profiles']['live']['routers'])
    routers['items'][0][field] = False if field == 'verify_tls' else 'changed'
    with job_lock('live', CONNECTION['id'], CONNECTION) as acquired:
        assert acquired
        with pytest.raises(ProfileInUse):
            repository.import_profile('live', {'routers': routers})
        assert repository.export_all() == before
        repository.save_router_publication('live', CONNECTION, manifest=MANIFEST)
    assert repository.get_profile('live')['routers']['items'][0]['ros_policy_state'] == MANIFEST


def test_batch_removal_deduplicates_device_connection_locks(repository):
    repository.save_profile('live', {'routers': {'items': [
        CONNECTION, {**CONNECTION, 'id': 'same-device'},
    ]}})
    repository.delete_profile('live')
    assert [profile['id'] for profile in repository.list_profiles()] == ['default']


@pytest.mark.parametrize('writer', ['profile-import', 'whole-import', 'fields', 'transaction',
                                    'reset', 'delete-profile'])
def test_first_publication_blocks_batch_replacement_across_processes(repository, writer):
    before = repository.export_all()
    artifact = repository.profile_path('live', 'generated/first.txt')
    artifact.parent.mkdir(parents=True)
    artifact.write_text('still publishing')
    context = multiprocessing.get_context('spawn')
    receiver, sender = context.Pipe(duplex=False)
    release = context.Event()
    process = context.Process(target=_hold_connection_job_lock,
                              args=(str(repository.data_dir), CONNECTION, sender, release))
    process.start()
    sender.close()
    try:
        assert receiver.poll(20), 'Child did not acquire the publication lock'
        assert receiver.recv() is True
        with pytest.raises(ProfileInUse):
            if writer == 'reset':
                repository.reset_all()
            elif writer == 'delete-profile':
                repository.delete_profile('live')
            else:
                replace_routers(repository, writer, {'items': []})
        assert repository.export_all() == before
        assert artifact.read_text() == 'still publishing'
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(10)
        receiver.close()
    assert process.exitcode == 0
    # Rejection must release the document lock so the publisher can save its
    # first manifest, and must not leak the process lock afterward.
    assert persist(repository)['ros_policy_state'] == MANIFEST


def test_replacement_holds_publication_lock_through_database_commit(repository, monkeypatch):
    original = repository._store._commit
    observations = []

    def commit(connection):
        with job_lock('other', 'other-router', CONNECTION) as acquired:
            observations.append(acquired)
        return original(connection)

    monkeypatch.setattr(repository._store, '_commit', commit)
    repository.import_profile('live', {'routers': {'items': []}})
    assert observations == [False]
    with job_lock('other', 'other-router', CONNECTION) as acquired:
        assert acquired


def batch_request(client, admin, operation):
    if operation == 'profile-import':
        return client.post('/api/profiles/default/import', headers=admin,
                           json={'routers': {'items': []}})
    if operation == 'whole-import':
        document = config_module.get_repository().export_all()
        document['profiles']['default']['routers']['items'] = []
        return client.post('/api/config/import', headers=admin, json=document)
    if operation == 'reset':
        return client.post('/api/config/reset', headers=admin)
    return client.delete('/api/profiles/default?new_default=spare', headers=admin)


def permit_default_deletion():
    repository = config_module.get_repository()
    repository.create_profile({'id': 'spare', 'name': 'Spare'})

    def rebind(system):
        for agent in system['agents']:
            agent['profile_id'] = 'spare'

    repository.update_system_transaction(rebind)


@pytest.mark.parametrize('operation', ['profile-import', 'whole-import', 'reset', 'delete-profile'])
def test_batch_http_mutations_cannot_abandon_published_policy(router_api, operation):
    client, admin, path, driver, constructed, closed = router_api
    permit_default_deletion()
    publish(client, admin, path)
    before = config_module.get_repository().export_all()
    rows, writes = copy.deepcopy(driver.rows), copy.deepcopy(driver.writes)
    constructed.clear()
    closed.clear()
    response = batch_request(client, admin, operation)
    assert response.status_code == 409, response.get_json()
    assert config_module.get_repository().export_all() == before
    assert driver.rows == rows and driver.writes == writes
    assert not constructed and not closed


@pytest.mark.parametrize('operation', ['profile-import', 'whole-import', 'reset', 'delete-profile'])
def test_batch_http_mutation_during_first_readback_cannot_erase_router(router_api, monkeypatch, operation):
    client, admin, path, driver, _, _ = router_api
    permit_default_deletion()
    original = routes_module.execute
    responses = []

    def execute(*args):
        result = original(*args)
        assert result['ok'] and driver.ros_policy_manifest['resources']
        assert not snapshot()['routers']['items'][0].get('ros_policy_state')
        before = config_module.get_repository().export_all()
        with ThreadPoolExecutor(max_workers=1) as pool:
            response = pool.submit(batch_request, client.application.test_client(), admin, operation).result(10)
        responses.append(response.status_code)
        assert response.status_code == 409, response.get_json()
        assert config_module.get_repository().export_all() == before
        return result

    monkeypatch.setattr(routes_module, 'execute', execute)
    publish(client, admin, path)
    assert responses == [409]
    stored = snapshot()['routers']['items'][0]
    assert stored['ros_policy_state'] == driver.ros_policy_manifest
    assert stored['last_release']['ok']


@pytest.mark.parametrize('field', ['username', 'password', 'verify_tls'])
def test_router_settings_allow_credential_repair_without_device_writes(router_api, field):
    client, admin, path, driver, constructed, closed = router_api
    publish(client, admin, path)
    before = snapshot()
    manifest = copy.deepcopy(before['routers']['items'][0]['ros_policy_state'])
    rows, writes = copy.deepcopy(driver.rows), copy.deepcopy(driver.writes)
    constructed.clear()
    closed.clear()
    response = client.put(path, headers=admin,
                          json={field: False if field == 'verify_tls' else 'changed'})
    assert response.status_code == 200, response.get_json()
    saved = snapshot()['routers']['items'][0]
    assert saved[field] == (False if field == 'verify_tls' else 'changed')
    assert saved['ros_policy_state'] == manifest
    assert driver.rows == rows and driver.writes == writes
    assert not constructed and not closed


def test_rotated_router_password_can_be_repaired_then_policy_withdrawn(router_api, monkeypatch):
    from backend.routers.base import RouterError
    client, admin, path, driver, _, _ = router_api
    publish(client, admin, path)
    manifest = copy.deepcopy(snapshot()['routers']['items'][0]['ros_policy_state'])
    rows = copy.deepcopy(driver.rows)
    authenticated = [False]
    original_request = driver._request

    def require_new_password(endpoint, username, password, **kwargs):
        authenticated[0] = password == 'new-password'
        return driver

    def authenticated_request(*args, **kwargs):
        if not authenticated[0]:
            raise RouterError('RouterOS 认证失败')
        return original_request(*args, **kwargs)

    monkeypatch.setitem(routes_module.DRIVERS, 'routeros', require_new_password)
    monkeypatch.setattr(driver, '_request', authenticated_request)
    assert client.post(path + '/plan', headers=admin).status_code == 502
    response = client.put(path, headers=admin, json={'password': 'new-password'})
    assert response.status_code == 200, response.get_json()
    assert snapshot()['routers']['items'][0]['ros_policy_state'] == manifest
    assert driver.rows == rows
    response = client.put(path, headers=admin, json={'ros_policy_rule_sets': []})
    assert response.status_code == 200, response.get_json()
    publish(client, admin, path)
    assert not snapshot()['routers']['items'][0].get('ros_policy_state')
    assert not driver.rows['/ip/firewall/mangle']
