from copy import deepcopy

import pytest
import yaml

from backend.common.agent_manager import get_agent_manager
from backend.common.device_group_workspace import DRAFT_FIELDS
from backend.common.oxidns_editor import prepare_editor
from backend.test_oxidns_integration import oxidns_app

PATH = '/api/profiles/office/device-groups'


def load(client):
    response = client.get(PATH)
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def proposed(snapshot):
    body = {key: deepcopy(snapshot[key]) for key in DRAFT_FIELDS}
    body.update(revision=snapshot['revision'], base_url='http://config.test')
    body['device_groups']['groups'].insert(0, {'id': 'dg_a123456789abcdef', 'name': '直连设备',
        'mode': 'direct', 'members': ['192.168.60.0/24'], 'keep_blocking': False, 'dns': 'follow'})
    body['policy_routing_sources'] = {'include': ['192.168.60.0/24'], 'exclude_groups': ['dg_a123456789abcdef']}
    return body


def review(client, body):
    response = client.post(PATH + '/preview', json=body)
    assert response.status_code == 200, response.get_json()
    preview = response.get_json()
    assert preview['blockers'] == [], preview['blockers']
    return preview


def save(client, body):
    checked = review(client, body)
    response = client.put(PATH, json={**body, 'preview_token': checked['preview_token']})
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def test_preview_is_read_only_complete_and_saves_only_explicit_draft(oxidns_app):
    app, repository = oxidns_app
    client = app.test_client()
    before = repository.path.read_bytes()
    snapshot = load(client)
    assert not snapshot['migrated'] and not snapshot['migration']['available']
    assert snapshot['device_groups']['groups'][-1]['id'] == 'default'
    body = proposed(snapshot)
    checked = review(client, body)
    assert {row['service_type'] for row in checked['generated_changes']} == {'oxidns', 'mihomo'}
    assert all(row['before'] != row['after'] for row in checked['generated_changes'])
    assert repository.path.read_bytes() == before
    saved = client.put(PATH, json={**body, 'preview_token': checked['preview_token']})
    assert saved.status_code == 200, saved.get_json()
    assert saved.get_json()['migrated']
    assert repository.get_profile('office')['device_groups']['groups'][0]['id'] == 'dg_a123456789abcdef'
    assert repository.get_profile('default').get('device_groups') is None
    assert repository.get_profile('office')['oxidns'].get('config_mode', 'template') == 'template'


@pytest.mark.parametrize('change', ['draft', 'shared', 'agent', 'base_url'])
def test_preview_cas_rejects_changed_content_shared_binding_or_url(oxidns_app, change):
    app, repository = oxidns_app
    client = app.test_client()
    body = proposed(load(client))
    checked = review(client, body)
    if change == 'draft':
        body['device_groups']['groups'][0]['members'] = ['192.168.90.0/24']
    elif change == 'shared':
        repository.save_shared({'rule_library': repository.get_shared()['rule_library']})
    elif change == 'agent':
        get_agent_manager().register_agent({'name': 'DNS', 'host': '192.168.60.20', 'port': 18000,
                                           'service_type': 'oxidns', 'profile_id': 'office'})
    else:
        body['base_url'] = 'http://changed.test'
    before = repository.path.read_bytes()
    response = client.put(PATH, json={**body, 'preview_token': checked['preview_token']})
    assert response.status_code == 409, response.get_json()
    assert repository.path.read_bytes() == before


def test_native_save_updates_groups_via_trusted_repository_gate(oxidns_app):
    app, repository = oxidns_app
    config = repository.get_compat_config('office')
    repository.save_profile('office', {'oxidns': prepare_editor(config['oxidns'], config)})
    client = app.test_client()
    saved = save(client, proposed(load(client)))
    body = {key: deepcopy(saved[key]) for key in DRAFT_FIELDS}
    body.update(revision=saved['revision'], base_url='http://config.test')
    body['device_groups']['groups'][0]['members'] = ['192.168.90.0/24']
    save(client, body)
    settings = repository.get_profile('office')['oxidns']
    document = yaml.safe_load(settings['native_config'])
    source = next(plugin for plugin in document['plugins'] if plugin['tag'] == 'cf_dg_set_a123456789abcdef')
    assert source['args']['ips'] == ['192.168.90.0/24']
    assert settings['_device_groups_patch']


def test_lookup_uses_longest_prefix_and_explicit_default_exceptions(oxidns_app):
    app, _ = oxidns_app
    client = app.test_client()
    body = proposed(load(client))
    body['device_groups']['groups'][-1]['members'] = ['192.168.60.128/25']
    body['device_groups']['groups'][0]['members'].append('192.168.60.192/26')
    save(client, body)
    for address, group, included in [('192.168.60.10', 'dg_a123456789abcdef', False),
                                     ('192.168.60.150', 'default', True),
                                     ('192.168.60.200', 'dg_a123456789abcdef', False)]:
        response = client.get(PATH + '/lookup', query_string={'ip': address})
        assert response.status_code == 200, response.get_json()
        result = response.get_json()
        assert (result['group_id'], result['policy_routing_included']) == (group, included)


def test_leaving_source_warning_and_group_delete_reference_cleanup(oxidns_app, monkeypatch):
    app, repository = oxidns_app
    client = app.test_client()
    body = proposed(load(client))
    body['policy_routing_sources']['exclude_groups'] = []
    saved = save(client, body)
    samples = [{'ip': '192.168.1.1', 'kind': 'router', 'count': 10, 'share': .5}]
    monkeypatch.setattr('backend.agents.device_group_activity.activity', lambda *_args, **_kwargs:
        {'clients': [{'ip': '192.168.60.10', 'count': 10}], 'infrastructure_samples': samples})
    next_body = {key: deepcopy(saved[key]) for key in DRAFT_FIELDS}
    next_body.update(revision=saved['revision'], base_url='http://config.test')
    next_body['policy_routing_sources']['exclude_groups'] = ['dg_a123456789abcdef']
    checked = review(client, next_body)
    assert checked['routing_changes']['removed'] == ['192.168.60.0/24']
    assert any(row['code'] == 'leaving_policy_routing' for row in checked['warnings'])
    assert checked['routing_changes']['affected_clients'] == ['192.168.60.10']
    assert checked['infrastructure_samples'] == samples
    saved = save(client, next_body)
    next_body = {key: deepcopy(saved[key]) for key in DRAFT_FIELDS}
    next_body.update(revision=saved['revision'], base_url='http://config.test')
    next_body['device_groups']['groups'] = [next_body['device_groups']['groups'][-1]]
    save(client, next_body)
    assert repository.get_profile('office')['policy_routing_sources']['exclude_groups'] == []


def test_legacy_interface_projects_readonly_and_rejects_put_after_enable(oxidns_app):
    app, _ = oxidns_app
    aid = get_agent_manager().register_agent({'name': 'DNS', 'host': '192.168.1.1', 'port': 18000,
        'service_type': 'oxidns', 'profile_id': 'office'})['id']
    client = app.test_client()
    save(client, proposed(load(client)))
    old_path = f'/api/agents/{aid}/oxidns/device-policies'
    result = client.get(old_path).get_json()
    assert result['migrated'] and result['editable'] is False
    assert result['policies'][0]['mode'] == 'direct'
    response = client.put(old_path, json={'profile_id': 'office', 'revision': result['revision'], 'policies': []})
    assert response.status_code == 409 and '设备组' in response.get_json()['message']


def test_old_policy_space_cannot_skip_explicit_upgrade_preview(oxidns_app):
    app, repository = oxidns_app
    settings = repository.get_profile('office')['oxidns']
    settings['device_policies'] = [{'ip': '192.168.1.20', 'mode': 'direct'}]
    repository.save_profile('office', {'oxidns': settings})
    client = app.test_client()
    snapshot = load(client)
    assert snapshot['migration']['available']
    response = client.post(PATH + '/preview', json=proposed(snapshot))
    assert response.status_code == 400 and '升级为设备组' in response.get_json()['message']


@pytest.mark.parametrize('native', [False, True])
def test_migration_preview_is_readonly_and_explicit_atomic_upgrade(oxidns_app, native):
    app, repository = oxidns_app
    config = repository.get_compat_config('office')
    config['oxidns']['device_policies'] = [
        {'ip': '192.168.60.0/24', 'mode': 'smart'},
        {'ip': '192.168.60.128/25', 'mode': 'direct'},
        {'ip': '192.168.60.160/27', 'mode': 'smart'}]
    if native:
        config['oxidns'] = prepare_editor(config['oxidns'], config)
    repository.save_profile('office', {'oxidns': config['oxidns']})
    client = app.test_client()
    before = repository.path.read_bytes()
    body = {'revision': load(client)['revision'], 'base_url': 'http://config.test'}
    response = client.post(PATH + '/migration/preview', json=body)
    assert response.status_code == 200, response.get_json()
    checked = response.get_json()
    assert checked['blockers'] == []
    assert repository.path.read_bytes() == before
    assert len(checked['generated_changes']) == 2
    assert all(row['before'] != row['after'] for row in checked['generated_changes'])
    groups = checked['draft']['device_groups']['groups']
    assert [group['name'] for group in groups] == ['直连设备', '默认组']
    assert groups[-1]['members'] == ['192.168.60.160/27']
    response = client.post(PATH + '/migration', json={**body, 'preview_token': checked['preview_token']})
    assert response.status_code == 200, response.get_json()
    settings = repository.get_profile('office')['oxidns']
    assert settings['device_policies'] == []
    assert not settings.get('template_settings', {}).get('device_policies')
    assert settings.get('config_mode', 'template') == ('native' if native else 'template')
    if native:
        document = yaml.safe_load(settings['native_config'])
        tags = [plugin['tag'] for plugin in document['plugins']]
        assert 'cf_dg_dispatch' in tags and 'cf_device_policy' not in tags
    repeat = client.post(PATH + '/migration', json={**body, 'revision': response.get_json()['revision'], 'preview_token': checked['preview_token']})
    assert repeat.status_code == 409


def test_migration_invalid_legacy_member_is_reviewable_and_never_written(oxidns_app):
    app, repository = oxidns_app
    settings = repository.get_profile('office')['oxidns']
    settings['device_policies'] = [{'ip': '127.0.0.2', 'mode': 'direct'}]
    repository.save_profile('office', {'oxidns': settings})
    client = app.test_client()
    body = {'revision': load(client)['revision']}
    before = repository.path.read_bytes()
    checked = client.post(PATH + '/migration/preview', json=body)
    assert checked.status_code == 200, checked.get_json()
    assert checked.get_json()['blockers']
    rejected = client.post(PATH + '/migration', json={**body, 'preview_token': ''})
    assert rejected.status_code == 400
    assert repository.path.read_bytes() == before


@pytest.mark.parametrize('service', ['mihomo', 'surge', 'loon', 'mosdns'])
def test_generation_warnings_are_outside_configuration_bytes(oxidns_app, service):
    from flask import Response
    from urllib.parse import unquote
    import json
    from backend.routes.generate import _download_response, _preview_response
    app, _ = oxidns_app
    with app.app_context():
        original = '固定配置\n'
        assert _preview_response(original, {}, service).get_json() == {'content': original}
        config = {'device_groups': {'version': 1, 'groups': [
            {'id': 'dg_abcdef', 'name': '直连设备', 'mode': 'direct', 'members': ['192.168.60.10']},
            {'id': 'default', 'name': '默认组', 'members': []}]}}
        preview = _preview_response(original, config, service).get_json()
        assert preview['content'] == original and preview['warnings']
        download = _download_response(Response(original), config, service)
        assert download.get_data() == original.encode()
        assert json.loads(unquote(download.headers['X-ConfigFlow-Warnings'])) == preview['warnings']


def test_noop_groups_do_not_claim_a_new_mihomo_version_requirement():
    from backend.common.device_group_warnings import generation_warnings
    config = {'device_groups': {'version': 1, 'groups': [
        {'id': 'default', 'name': '默认组', 'members': []}]}}
    assert generation_warnings(config, 'mihomo') == []


def test_full_tun_does_not_get_selective_gateway_source_warning():
    from backend.common.device_group_workspace import warnings
    config = {'proxy_groups': [{'id': 'pg', 'name': '代理出口'}],
        'device_groups': {'version': 1, 'groups': [
            {'id': 'dg_abcdef', 'name': '代理设备', 'members': ['192.168.60.0/24'], 'mode': 'proxy', 'policy': '代理出口'},
            {'id': 'default', 'name': '默认组', 'members': []}]},
        'routers': {'items': [{'id': 'r', 'endpoint': 'http://192.168.1.1'}]},
        'policy_routing_sources': {'include': [], 'exclude_groups': []},
        'mihomo': {'custom_config': 'tun:\n  enable: true\n'}}
    assert {'selective_gateway', 'proxy_outside_sources'}.isdisjoint(row['code'] for row in warnings(config))
    config['mihomo'] = {'linux_gateway': {'enabled': True}}
    assert {'selective_gateway', 'proxy_outside_sources'}.issubset(row['code'] for row in warnings(config))
