"""Independent RouterOS sources, workspace compatibility and refresh guards."""
from copy import deepcopy

import pytest

from backend.common.config import get_repository
from backend.common.device_group_migration import plan_migration
from backend.common.device_groups import source_revision
from backend.routers.policy_workspace import draft_revision
from backend.routers.ros_policy import inspect_desired
from backend.routers import rule_sync
from backend.test_agent_upgrade_auth import authenticated_app  # noqa: F401
from backend.test_policy_workspace import workspace, preview_stub, read, body_for  # noqa: F401
from backend.test_ros_policy import GATEWAY, MemoryROS, desired_for, inspect, release
from backend.test_router_rule_sync import environment, publish, NEW  # noqa: F401


def enable():
    get_repository().update_profile_fields('default', {
        'device_groups': {'version': 1, 'groups': [
            {'id': 'dg_123456', 'name': '直连设备', 'members': ['172.16.0.0/25'], 'mode': 'direct'},
            {'id': 'default', 'name': '默认组', 'members': []}]},
        'policy_routing_sources': {'include': ['172.16.0.0/24'], 'exclude_groups': ['dg_123456']}})


def modern_body(state):
    body = body_for(state)
    body.pop('policies')
    body['policy_routing_sources'] = state['sources']['policy_routing_sources']
    return body


def test_new_source_workspace_is_read_only_and_saves_without_rewriting_oxidns(workspace, monkeypatch):
    from backend.routes import routers
    enable()
    monkeypatch.setattr(routers, '_driver', lambda *_: pytest.fail('draft editing must not contact RouterOS'))
    state = read(workspace)
    assert state['sources']['kind'] == 'device_groups' and state['sources']['policies'] == []
    assert state['sources']['groups'] == [{'id': 'dg_123456', 'name': '直连设备'}, {'id': 'default', 'name': '默认组'}]
    before = get_repository().get_profile('default')
    body = modern_body(state)
    body['policy_routing_sources']['include'] = ['172.16.2.0/24', 'fd00::/64']
    client, _, _, admin, path = workspace
    response = client.put(path + '/policy-settings', headers=admin, json=body)
    assert response.status_code == 200, response.get_json()
    after = get_repository().get_profile('default')
    assert after['oxidns'] == before['oxidns']
    assert after['policy_routing_sources']['include'] == body['policy_routing_sources']['include']


@pytest.mark.parametrize('value', [None, [], {'include': ['172.16.0.50', '172.16.0.50/32'], 'exclude_groups': []},
                                  {'include': [], 'exclude_groups': ['dg_deadbe']}])
def test_bad_independent_sources_never_partially_change_the_router(workspace, value):
    enable()
    client, _, _, admin, path = workspace
    before = get_repository().get_profile('default')
    body = modern_body(read(workspace))
    body['router']['proxy_gateway_agent_id'] = ''
    body['policy_routing_sources'] = value
    response = client.put(path + '/policy-settings', headers=admin, json=body)
    assert response.status_code == 400, response.get_json()
    assert get_repository().get_profile('default') == before


def test_legacy_workspace_can_change_router_only_but_cannot_edit_projected_policies(workspace):
    enable()
    client, _, _, admin, path = workspace
    body = body_for(read(workspace))
    body['router']['proxy_gateway_agent_id'] = ''
    response = client.put(path + '/policy-settings', headers=admin, json=body)
    assert response.status_code == 200, response.get_json()
    body = body_for(read(workspace))
    body['policies'] = [{'ip': '172.16.0.50', 'mode': 'smart'}]
    before = get_repository().get_profile('default')
    response = client.put(path + '/policy-settings', headers=admin, json=body)
    assert response.status_code == 409 and '来源编辑器' in response.get_json()['message']
    assert get_repository().get_profile('default') == before


def test_source_preview_has_no_dns_publish_targets_or_generated_oxidns(workspace, preview_stub, monkeypatch):
    enable()
    monkeypatch.setattr('backend.common.oxidns_validation.require_valid_document', lambda *_: pytest.fail('independent sources must not render DNS'))
    client, _, dns, admin, path = workspace
    state = read(workspace)
    response = client.post(path + '/policy-settings/preview', headers=admin,
                           json={'revision': state['revision'], 'targets': [dns['id']], 'base_url': ''})
    assert response.status_code == 200, response.get_json()
    preview = response.get_json()['preview']
    assert preview['targets'] == [] and preview['dns_content'] == ''
    assert '无需发布 OxiDNS' in str(preview['warnings'])


def test_new_sources_do_not_depend_on_the_legacy_native_policy_chain():
    cfg = {'oxidns': {'config_mode': 'native', 'native_config': 'plugins: []'},
           'policy_routing_sources': {'include': ['192.168.0.101'], 'exclude_groups': []},
           'rule_configs': [{'id': 'set', 'itemType': 'ruleset', 'library_rule_id': 'lib', 'policy': 'PROXY'}],
           'rule_library': [{'id': 'lib', 'name': 'IP', 'behavior': 'ipcidr', 'source_type': 'content', 'content': '1.1.1.0/24'}]}
    router = {'id': 'ros', 'ros_policy_rule_sets': ['set']}
    result = inspect_desired(cfg, router, {'proxy_gateway': GATEWAY}, content_loader=lambda *_: '1.1.1.0/24')
    assert not result['error'] and result['sources'] == ['192.168.0.101/32']
    assert result['source_kind'] == 'device_groups'
    assert result['source_revision'] == source_revision(cfg)


def test_new_workspace_revision_ignores_dns_but_detects_source_or_gateway_changes():
    cfg = {'policy_routing_sources': {'include': ['192.168.0.101'], 'exclude_groups': []},
           'oxidns': {'local_dns': 'old'}, 'agents': [{'id': 'gw', 'host': GATEWAY, 'service_type': 'mihomo', 'profile_id': 'default'}]}
    router = {'id': 'ros', 'proxy_gateway_agent_id': 'gw'}
    before = draft_revision(cfg, router)
    cfg['oxidns']['local_dns'] = 'new'
    assert draft_revision(cfg, router) == before
    cfg['agents'][0]['host'] = '192.168.0.4'
    assert draft_revision(cfg, router) != before
    cfg['agents'][0]['host'] = GATEWAY
    cfg['policy_routing_sources']['include'] = ['192.168.0.102']
    assert draft_revision(cfg, router) != before


def test_published_source_withdrawal_is_explicitly_warned():
    driver = MemoryROS()
    release(driver)
    desired = desired_for(driver)
    desired['ros_policy'].update(sources=['192.168.0.102/32'], source_kind='device_groups',
                                 previous=deepcopy(driver.ros_policy_manifest))
    report, plan = inspect(driver, desired)
    assert not plan['blockers']
    assert '离开策略路由' in str(plan['warnings'])
    assert '192.168.0.101/32' in str(plan['warnings'])


def test_automatic_sync_detects_new_sources_but_ignores_unrelated_dns_edits(environment):
    repository, driver = environment
    cfg = repository.get_compat_config('default')
    migration = plan_migration(cfg)
    assert not migration['blockers']
    repository.update_profile_transaction('default', lambda current: current.update({
        key: migration['config'][key] for key in ('device_groups', 'policy_routing_sources', 'device_notes', 'oxidns')}),
        allow_device_group_migration=True)
    publish(environment)
    cfg = rule_sync.config_for('default')
    router = cfg['routers']['items'][0]
    before = rule_sync.controls(cfg, router)
    cfg['oxidns']['local_dns'] = 'udp://8.8.4.4'
    assert rule_sync.controls(cfg, router) == before
    repository.update_profile_fields('default', {'policy_routing_sources': {'include': ['192.168.0.102'], 'exclude_groups': []}})
    result = rule_sync.sync('default', 'ros', lambda *_: NEW)
    assert result['status'] == 'paused'
    assert not driver.writes
