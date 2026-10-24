"""Atomic profile drafts and explicit, guarded DNS/ROS publication previews."""
import copy
import pytest
from backend.test_agent_upgrade_auth import authenticated_app  # noqa: F401
from backend.common.config import get_repository
from backend.common.oxidns_device_policy import revision
from backend.routers.policy_workspace import draft_revision


@pytest.fixture
def workspace(authenticated_app):
    client, manager, agents, admin, _ = authenticated_app
    dns = manager.register_agent({'name': 'DNS one', 'host': '192.0.2.53', 'port': 1234,
        'service_type': 'oxidns', 'version': '1.8.14-go', 'deployment_method': 'shell'})
    payload = {'name': 'ROS', 'type': 'routeros', 'endpoint': 'http://192.0.2.1',
        'username': 'admin', 'password': 'private-password', 'proxy_gateway_agent_id': agents[0]['id'],
        'ros_policy_rule_sets': [], 'ip_rule_sets': []}
    created = client.post('/api/routers', headers=admin, json=payload)
    assert created.status_code == 200
    path = '/api/routers/' + created.get_json()['router']['id']
    return client, manager, dns, admin, path


def read(workspace):
    client, _, _, admin, path = workspace
    response = client.get(path + '/policy-settings', headers=admin)
    assert response.status_code == 200, response.get_json()
    return response.get_json()['workspace']


def body_for(state):
    return {'revision': state['revision'], 'names_revision': state['names_revision'], 'base_url': '',
        'router': {key: state['router'].get(key) for key in ('ip_rule_sets', 'ros_policy_rule_sets', 'proxy_gateway_agent_id')},
        'policies': state['sources']['policies']}


def test_profile_sources_read_without_contacting_router_and_no_secrets(workspace, monkeypatch):
    from backend.routes import routers
    monkeypatch.setattr(routers, '_driver', lambda *_: pytest.fail('draft reads must not contact ROS'))
    state = read(workspace)
    assert state['profile_id'] == 'default' and state['sources']['editable']
    assert state['source_agents'][0]['id'] == workspace[2]['id']
    assert 'password' not in state['router']
    assert 'private-password' not in str(state)
    assert workspace[2]['token'] not in str(state)


def test_saves_sources_and_router_atomically_preserving_unrelated_settings(workspace):
    client, _, _, admin, path = workspace
    state = read(workspace)
    before = copy.deepcopy(get_repository().get_profile('default'))
    body = body_for(state)
    body['policies'] = [{'ip': '192.0.2.105', 'mode': 'smart', 'name': 'NAS'}, {'ip': '192.0.2.51', 'mode': 'direct', 'name': 'direct'}]
    body['router']['proxy_gateway_agent_id'] = ''
    saved = client.put(path + '/policy-settings', headers=admin, json=body)
    assert saved.status_code == 200, saved.get_json()
    updated = saved.get_json()['workspace']
    assert updated['sources']['policies'] == body['policies']
    assert updated['router']['proxy_gateway_agent_id'] == ''
    assert updated['revision'] != state['revision']
    after = get_repository().get_profile('default')
    assert after['device_notes'] == {'192.0.2.105': 'NAS', '192.0.2.51': 'direct'}
    for key in before:
        if key not in ('oxidns', 'routers', 'device_notes', '_revision', 'updated_at'):
            assert after[key] == before[key]
    assert after['routers']['items'][0]['password'] == 'private-password'


def test_renaming_sources_keeps_oxidns_and_rejects_stale_workspace(workspace):
    client, manager, dns, admin, path = workspace
    body = body_for(read(workspace))
    body['policies'] = [{'ip': '192.0.2.105', 'mode': 'smart', 'name': 'NAS'}]
    assert client.put(path + '/policy-settings', headers=admin, json=body).status_code == 200
    state = read(workspace)
    oxidns = copy.deepcopy(get_repository().get_profile('default')['oxidns'])
    body = body_for(state)
    body['policies'][0]['name'] = '群晖'
    renamed = client.put(path + '/policy-settings', headers=admin, json=body)
    assert renamed.status_code == 200, renamed.get_json()
    assert get_repository().get_profile('default')['oxidns'] == oxidns
    assert renamed.get_json()['workspace']['oxidns_revision'] == state['oxidns_revision']
    assert get_repository().get_profile('default')['device_notes'] == {'192.0.2.105': '群晖'}
    stale = body_for(renamed.get_json()['workspace'])
    notes = {'profile_id': 'default', 'ip': '192.0.2.105', 'name': '存储', 'previous_name': '群晖'}
    assert client.put(f'/api/agents/{dns["id"]}/client-notes', headers=admin, json=notes).status_code == 200
    # A rename elsewhere does not invalidate previews, only saving stale names. It still advances
    # the profile revision every DNS push checks, which the panel rechecks before publishing.
    assert read(workspace)['revision'] == stale['revision']
    assert read(workspace)['config_revision'] == renamed.get_json()['workspace']['config_revision'] + 1
    assert client.put(path + '/policy-settings', headers=admin, json=stale).status_code == 409
    assert read(workspace)['sources']['policies'][0]['name'] == '存储'


def test_router_only_saves_leave_sources_alone_and_ignore_renames(workspace):
    client, _, dns, admin, path = workspace
    body = body_for(read(workspace))
    body['policies'] = [{'ip': '192.0.2.105', 'mode': 'smart', 'name': 'NAS'}]
    assert client.put(path + '/policy-settings', headers=admin, json=body).status_code == 200
    state = read(workspace)
    notes = {'profile_id': 'default', 'ip': '192.0.2.105', 'name': '群晖', 'previous_name': 'NAS'}
    assert client.put(f'/api/agents/{dns["id"]}/client-notes', headers=admin, json=notes).status_code == 200
    before = copy.deepcopy(get_repository().get_profile('default'))
    router_only = {'revision': state['revision'], 'base_url': '',
                   'router': {**body_for(state)['router'], 'proxy_gateway_agent_id': ''}}
    saved = client.put(path + '/policy-settings', headers=admin, json=router_only)
    assert saved.status_code == 200, saved.get_json()
    after = get_repository().get_profile('default')
    assert after['oxidns'] == before['oxidns'] and after['device_notes'] == before['device_notes']
    assert after['routers']['items'][0]['proxy_gateway_agent_id'] == ''


@pytest.mark.parametrize('failure', ['bad_ip', 'duplicate', 'bad_router', 'extra_field'])
def test_bad_source_or_router_change_does_not_partially_save(workspace, failure):
    client, _, _, admin, path = workspace
    body = body_for(read(workspace)); before = copy.deepcopy(get_repository().get_profile('default'))
    body['router']['proxy_gateway_agent_id'] = 'different'
    body['policies'] = [{'ip': '192.0.2.105', 'mode': 'smart'}]
    if failure == 'bad_ip': body['policies'][0]['ip'] = 'bad-ip'
    if failure == 'duplicate': body['policies'] *= 2
    if failure == 'bad_router': body['router']['ip_rule_sets'] = 'not-a-list'
    if failure == 'extra_field': body['router']['password'] = 'overwrite'
    response = client.put(path + '/policy-settings', headers=admin, json=body)
    assert response.status_code == 400, response.get_json()
    assert get_repository().get_profile('default') == before


def test_conflicting_editor_change_cannot_be_overwritten(workspace):
    client, _, _, admin, path = workspace
    body = body_for(read(workspace))
    assert client.put(path, headers=admin, json={'name': 'Changed elsewhere'}).status_code == 200
    before = copy.deepcopy(get_repository().get_profile('default'))
    response = client.put(path + '/policy-settings', headers=admin, json=body)
    assert response.status_code == 409
    assert get_repository().get_profile('default') == before


def test_preview_is_read_only_pins_full_revision_and_rejects_foreign_targets(workspace, monkeypatch):
    from backend.routes import routers
    client, manager, dns, admin, path = workspace
    state = read(workspace); closed = []
    class Driver:
        def close(self): closed.append(True)
    monkeypatch.setattr(routers, '_driver', lambda *_: Driver())
    monkeypatch.setattr(routers, '_inspect', lambda *_: (None, {}, {}, {'return_routing': {}}))
    monkeypatch.setattr(routers, 'build_plan', lambda *_: {'fingerprint': 'pinned', 'blockers': [], 'warnings': [], 'stages': [], 'empty': True})
    monkeypatch.setattr(manager, 'push_config_to_agent', lambda *_: pytest.fail('preview must not publish'))
    before = copy.deepcopy(get_repository().get_profile('default'))
    body = {'revision': state['revision'], 'targets': [dns['id']], 'base_url': ''}
    response = client.post(path + '/policy-settings/preview', headers=admin, json=body)
    assert response.status_code == 200, response.get_json()
    preview = response.get_json()['preview']
    assert preview['config_revision'] == get_repository().config_revision('default')
    assert preview['oxidns_revision'] == revision(before['oxidns'])
    assert 'plugins:' in preview['dns_content'] and preview['targets'][0]['id'] == dns['id']
    assert get_repository().get_profile('default') == before and closed == [True]
    response = client.post(path + '/policy-settings/preview', headers=admin, json={**body, 'targets': ['other-profile-device']})
    assert response.status_code == 409


def test_preview_rejects_stale_workspace(workspace):
    client, _, _, admin, path = workspace
    response = client.post(path + '/policy-settings/preview', headers=admin, json={'revision': 'stale', 'targets': []})
    assert response.status_code == 409


def test_dns_publish_rejects_full_config_changes_before_preparation(workspace, monkeypatch):
    from backend.routes import agents
    client, manager, dns, admin, _ = workspace
    state = read(workspace)
    monkeypatch.setattr(agents, '_prepare_transactional_content', lambda *_args, **_kwargs: pytest.fail('stale configuration must not be prepared'))
    response = client.post('/api/agents/' + dns['id'] + '/push-config', headers=admin, json={
        'expected_profile_id': 'default', 'expected_oxidns_revision': state['oxidns_revision'],
        'expected_config_revision': get_repository().config_revision('default') - 1})
    assert response.status_code == 409, response.get_json()


@pytest.fixture
def preview_stub(workspace, monkeypatch):
    from backend.routes import routers
    closed = []
    class Driver:
        def close(self): closed.append(True)
    monkeypatch.setattr(routers, '_driver', lambda *_: Driver())
    monkeypatch.setattr(routers, '_inspect', lambda *_: (None, {}, {}, {'return_routing': {}}))
    monkeypatch.setattr(routers, 'build_plan', lambda *_: {
        'fingerprint': 'pinned', 'blockers': [], 'warnings': [], 'stages': [], 'empty': True})
    monkeypatch.setattr(workspace[1], 'push_config_to_agent',
                        lambda *_a, **_kw: pytest.fail('preview must not publish'))
    return closed


@pytest.mark.parametrize(('method', 'suffix'), [('get', '/policy-settings'),
    ('put', '/policy-settings'), ('post', '/policy-settings/preview')])
def test_workspace_endpoints_require_authentication(workspace, method, suffix):
    client, _, _, _, path = workspace
    assert getattr(client, method)(path + suffix).status_code == 401


def test_workspace_reads_and_saves_cannot_cross_configuration_spaces(workspace):
    client, manager, dns, admin, path = workspace
    repository = get_repository()
    repository.create_profile({'id': 'other', 'name': 'Other'})
    other_dns = manager.register_agent({'name': 'Foreign DNS', 'host': '192.0.2.54', 'port': 1234,
        'service_type': 'oxidns', 'profile_id': 'other'})
    state = read(workspace)
    assert [agent['id'] for agent in state['source_agents']] == [dns['id']]
    assert other_dns['id'] not in str(state)
    before = copy.deepcopy(repository.get_profile('default'))
    scoped = {**admin, 'X-ConfigFlow-Profile': 'other'}
    assert client.get(path + '/policy-settings', headers=scoped).status_code == 404
    assert client.put(path + '/policy-settings', headers=scoped, json=body_for(state)).status_code == 404
    assert client.post(path + '/policy-settings/preview', headers=scoped,
                       json={'revision': state['revision'], 'targets': []}).status_code == 404
    assert repository.get_profile('default') == before


def test_oxidns_edit_invalidates_joint_save_without_changing_router(workspace):
    client, _, _, admin, path = workspace
    body = body_for(read(workspace))
    repository = get_repository()
    settings = repository.get_profile('default')['oxidns']
    settings['worker_threads'] = 3
    repository.save_profile('default', {'oxidns': settings})
    before = copy.deepcopy(repository.get_profile('default'))
    body['router']['proxy_gateway_agent_id'] = ''
    assert client.put(path + '/policy-settings', headers=admin, json=body).status_code == 409
    assert repository.get_profile('default') == before


@pytest.mark.parametrize('change', ['profile', 'shared'])
def test_change_while_building_joint_preview_is_rejected(workspace, preview_stub, monkeypatch, change):
    from backend.routes import routers
    client, _, dns, admin, path = workspace
    state = read(workspace)
    repository = get_repository()
    def inspect(*_args):
        if change == 'profile':
            repository.save_profile('default', {'mihomo': {'custom_config': 'mode: direct'}})
        else:
            repository.save_shared({'rule_library': []})
        return None, {}, {}, {'return_routing': {}}
    monkeypatch.setattr(routers, '_inspect', inspect)
    response = client.post(path + '/policy-settings/preview', headers=admin,
        json={'revision': state['revision'], 'targets': [dns['id']]})
    assert response.status_code == 409
    assert '生成预览期间配置已更新' in response.json['message']
    assert 'preview' not in response.json and preview_stub == [True]


def test_joint_preview_uses_one_atomic_config_snapshot(workspace, preview_stub, monkeypatch):
    from backend.routes import routers
    client, _, dns, admin, path = workspace
    state = read(workspace)
    monkeypatch.setattr(routers, 'get_config',
                        lambda *_a, **_kw: pytest.fail('must not pair an old cached config with a new revision'))
    response = client.post(path + '/policy-settings/preview', headers=admin,
        json={'revision': state['revision'], 'targets': [dns['id']]})
    assert response.status_code == 200, response.json
    assert response.json['preview']['config_revision'] == get_repository().config_revision('default')


def test_invalid_dns_configuration_blocks_selected_dns_preview(workspace, preview_stub, monkeypatch):
    client, _, dns, admin, path = workspace
    state = read(workspace)
    monkeypatch.setattr('backend.converters.oxidns.generate_oxidns_config', lambda *_a, **_kw:
        'plugins:\n- tag: upstream\n  type: forward\n  args: {}\n')
    response = client.post(path + '/policy-settings/preview', headers=admin,
        json={'revision': state['revision'], 'targets': [dns['id']]})
    assert response.status_code == 400, response.json
    assert not response.json['success'] and 'preview' not in response.json
    assert preview_stub == [True]


def test_explicit_router_only_preview_skips_dns_rendering(workspace, preview_stub, monkeypatch):
    client, _, _, admin, path = workspace
    state = read(workspace)
    monkeypatch.setattr('backend.converters.oxidns.generate_oxidns_config',
                        lambda *_a, **_kw: pytest.fail('Router-only preview must not prepare DNS'))
    response = client.post(path + '/policy-settings/preview', headers=admin,
        json={'revision': state['revision'], 'targets': []})
    assert response.status_code == 200, response.json
    assert response.json['preview']['targets'] == []
    assert response.json['preview']['dns_content'] == ''


def test_router_read_failure_does_not_produce_publishable_preview(workspace, preview_stub, monkeypatch):
    from backend.routes import routers
    from backend.routers.base import RouterError
    client, _, dns, admin, path = workspace
    state = read(workspace)
    def inspect(*_args):
        raise RouterError('router offline')
    monkeypatch.setattr(routers, '_inspect', inspect)
    response = client.post(path + '/policy-settings/preview', headers=admin,
        json={'revision': state['revision'], 'targets': [dns['id']]})
    assert response.status_code == 400
    assert 'preview' not in response.json and preview_stub == [True]
