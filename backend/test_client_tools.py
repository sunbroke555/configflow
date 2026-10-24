"""Shared names and connection rule writes stay inside the Agent's profile."""
from copy import deepcopy

import pytest

from backend.common.agent_manager import get_agent_manager
from backend.common.device_notes import read_notes, set_note
from backend.common.oxidns_device_policy import apply_policies
from backend.test_oxidns_integration import oxidns_app
from backend.utils import domain_discovery_service as service


def agent(kind='mihomo', profile='office'):
    return get_agent_manager().register_agent({'name': kind, 'host': '127.0.0.1',
        'port': 18000, 'service_type': kind, 'profile_id': profile})['id']


def test_notes_shared_with_dns_without_changing_native_rules(oxidns_app):
    app, repo = oxidns_app
    aid, dns = agent(), agent('oxidns')
    def setup(config):
        config['oxidns'] = apply_policies(config['oxidns'], config, [
            {'ip': '192.0.2.5', 'name': '旧电脑', 'mode': 'direct'}])
    repo.update_profile_transaction('office', setup)
    original = deepcopy(repo.get_profile('office')['oxidns'])
    default = deepcopy(repo.get_profile('default'))
    client = app.test_client()
    url = f'/api/agents/{aid}/client-notes?profile=default'
    assert client.get(url).get_json()['notes'] == {'192.0.2.5': '旧电脑'}
    body = {'profile_id': 'office', 'ip': '::ffff:192.0.2.5', 'name': '工作电脑', 'previous_name': '旧电脑'}
    saved = client.put(url, json=body)
    assert saved.status_code == 200, saved.get_json()
    assert saved.get_json()['notes'] == {'192.0.2.5': '工作电脑'}
    assert client.get(f'/api/agents/{dns}/client-notes').get_json()['notes'] == saved.get_json()['notes']
    state = client.get(f'/api/agents/{dns}/oxidns/device-policies').get_json()
    assert state['policies'][0]['name'] == '工作电脑'
    # A rename is display-only: the OxiDNS settings and their revisions stay unchanged.
    assert repo.get_profile('office')['oxidns'] == original
    assert repo.get_profile('default') == default
    assert client.put(url, json=body).status_code == 409
    body.update(name='', previous_name='工作电脑')
    # Clearing hides the name the policy row saved before names moved to device_notes.
    assert client.put(url, json=body).get_json()['notes'].get('192.0.2.5', '') == ''
    assert client.get(f'/api/agents/{dns}/oxidns/device-policies').get_json()['policies'][0]['name'] == ''
    assert repo.get_profile('office')['oxidns'] == original


def test_dns_save_reuses_names_and_keeps_notes_after_policy_removal(oxidns_app):
    app, repo = oxidns_app
    aid, dns = agent(), agent('oxidns')
    client = app.test_client()
    url = f'/api/agents/{aid}/client-notes'
    assert client.put(url, json={'profile_id': 'office', 'ip': '192.0.2.6/32', 'name': 'NAS', 'previous_name': ''}).status_code == 200
    assert not repo.get_profile('office')['oxidns'].get('device_policies')
    policy_url = f'/api/agents/{dns}/oxidns/device-policies'
    state = client.get(policy_url).get_json()
    saved = client.put(policy_url, json={'profile_id': 'office', 'revision': state['revision'], 'notes_revision': state['notes_revision'],
        'policies': [{'ip': '192.0.2.6', 'mode': 'smart', 'name': ''}]}).get_json()
    assert saved['policies'][0]['name'] == 'NAS'
    saved['policies'][0]['name'] = '群晖'
    renamed = client.put(policy_url, json={key: saved[key] for key in ('profile_id', 'revision', 'notes_revision', 'policies')}).get_json()
    assert client.get(url).get_json()['notes']['192.0.2.6'] == '群晖'
    assert client.put(policy_url, json={'profile_id': 'office', 'revision': renamed['revision'], 'notes_revision': renamed['notes_revision'], 'policies': []}).status_code == 200
    assert client.get(url).get_json()['notes']['192.0.2.6'] == '群晖'


def test_legacy_policy_names_are_a_fallback_and_can_be_hidden():
    config = {'oxidns': {'device_policies': [
        {'ip': '192.0.2.1', 'name': '旧名', 'mode': 'smart'},
        {'ip': '192.0.2.2', 'name': '保留', 'mode': 'direct'},
        {'ip': '192.0.2.3', 'name': '', 'mode': 'smart'}]},
        'device_notes': {'192.0.2.1': '新名', '192.0.2.3': 'NAS'}}
    assert read_notes(config) == {'192.0.2.1': '新名', '192.0.2.2': '保留', '192.0.2.3': 'NAS'}
    set_note(config, '192.0.2.2', '')
    set_note(config, '192.0.2.3', '')
    assert config['device_notes'] == {'192.0.2.1': '新名', '192.0.2.2': ''}
    assert read_notes(config) == {'192.0.2.1': '新名'}
    assert config['oxidns']['device_policies'][1]['name'] == '保留'


def test_policy_name_edits_keep_template_settings_and_revisions(oxidns_app):
    app, repo = oxidns_app
    dns = agent('oxidns')
    repo.update_profile_transaction('office', lambda config: config['oxidns'].update(
        device_policies=[{'ip': '192.0.2.7', 'name': '旧名', 'mode': 'direct'}]))
    original = deepcopy(repo.get_profile('office')['oxidns'])
    assert original.get('config_mode', 'template') == 'template' and not original.get('native_config')
    client = app.test_client()
    url = f'/api/agents/{dns}/oxidns/device-policies'
    state = client.get(url).get_json()
    body = {key: state[key] for key in ('profile_id', 'revision', 'notes_revision', 'policies')}
    body['policies'][0]['name'] = '客厅电视'
    saved = client.put(url, json=body)
    assert saved.status_code == 200, saved.get_json()
    # Only device_notes changed: no native conversion and no new OxiDNS revision to publish.
    assert repo.get_profile('office')['oxidns'] == original
    assert saved.get_json()['revision'] == state['revision']
    assert saved.get_json()['policies'][0]['name'] == '客厅电视'
    assert repo.get_profile('office')['device_notes'] == {'192.0.2.7': '客厅电视'}
    body['policies'][0]['mode'] = 'smart'
    body['notes_revision'] = saved.get_json()['notes_revision']
    changed = client.put(url, json=body)
    assert changed.status_code == 200, changed.get_json()
    assert changed.get_json()['revision'] != state['revision']
    assert repo.get_profile('office')['oxidns']['device_policies'] == [{'ip': '192.0.2.7', 'name': '客厅电视', 'mode': 'smart'}]


def test_policy_editor_rejects_names_renamed_elsewhere(oxidns_app):
    app, _ = oxidns_app
    mihomo, dns = agent(), agent('oxidns')
    client = app.test_client()
    url = f'/api/agents/{dns}/oxidns/device-policies'
    notes = f'/api/agents/{mihomo}/client-notes'
    state = client.get(url).get_json()
    created = client.put(url, json={'profile_id': 'office', 'revision': state['revision'], 'notes_revision': state['notes_revision'],
                                    'policies': [{'ip': '192.0.2.8', 'name': 'NAS', 'mode': 'smart'}]}).get_json()
    body = {key: created[key] for key in ('profile_id', 'revision', 'notes_revision', 'policies')}
    # Naming a device without a policy does not disturb the open editor.
    assert client.put(notes, json={'profile_id': 'office', 'ip': '192.0.2.9', 'name': '手机', 'previous_name': ''}).status_code == 200
    body['policies'][0]['mode'] = 'direct'
    assert client.put(url, json=body).status_code == 200
    latest = client.get(url).get_json()
    body = {key: latest[key] for key in ('profile_id', 'revision', 'notes_revision', 'policies')}
    assert client.put(notes, json={'profile_id': 'office', 'ip': '192.0.2.8', 'name': '群晖', 'previous_name': 'NAS'}).status_code == 200
    assert client.get(url).get_json()['revision'] == latest['revision']
    body['policies'][0]['mode'] = 'smart'
    assert client.put(url, json=body).status_code == 409
    assert client.get(notes).get_json()['notes']['192.0.2.8'] == '群晖'


def test_removing_a_legacy_named_policy_keeps_its_name(oxidns_app):
    app, repo = oxidns_app
    dns = agent('oxidns')
    def setup(config):
        config['oxidns'] = apply_policies(config['oxidns'], config, [
            {'ip': '192.0.2.1', 'name': '旧电视', 'mode': 'direct'},
            {'ip': '192.0.2.2', 'name': '旧音箱', 'mode': 'direct'},
            {'ip': '192.0.2.3', 'name': '', 'mode': 'smart'}])
        config['device_notes'] = {'192.0.2.2': ''}
    repo.update_profile_transaction('office', setup)
    client = app.test_client()
    url = f'/api/agents/{dns}/oxidns/device-policies'
    state = client.get(url).get_json()
    body = {key: state[key] for key in ('profile_id', 'revision', 'notes_revision')}
    saved = client.put(url, json={**body, 'policies': [state['policies'][2]]})
    assert saved.status_code == 200, saved.get_json()
    # The removed row's legacy name survives; a name hidden before stays hidden.
    assert saved.get_json()['device_notes'] == {'192.0.2.1': '旧电视'}


def test_policy_editor_requires_the_names_revision(oxidns_app):
    app, repo = oxidns_app
    dns = agent('oxidns')
    client = app.test_client()
    url = f'/api/agents/{dns}/oxidns/device-policies'
    state = client.get(url).get_json()
    before = deepcopy(repo.get_profile('office'))
    body = {'profile_id': 'office', 'revision': state['revision'], 'policies': [{'ip': '192.0.2.4', 'name': 'NAS', 'mode': 'smart'}]}
    assert client.put(url, json=body).status_code == 409
    assert repo.get_profile('office') == before


def test_new_policy_row_without_a_name_keeps_a_rename_made_elsewhere(oxidns_app):
    app, _ = oxidns_app
    mihomo, dns = agent(), agent('oxidns')
    client = app.test_client()
    notes = f'/api/agents/{mihomo}/client-notes'
    url = f'/api/agents/{dns}/oxidns/device-policies'
    assert client.put(notes, json={'profile_id': 'office', 'ip': '192.0.2.9', 'name': '手机', 'previous_name': ''}).status_code == 200
    state = client.get(url).get_json()
    assert client.put(notes, json={'profile_id': 'office', 'ip': '192.0.2.9', 'name': '小米手机', 'previous_name': '手机'}).status_code == 200
    saved = client.put(url, json={**{key: state[key] for key in ('profile_id', 'revision', 'notes_revision')},
                                  'policies': [{'ip': '192.0.2.9', 'name': '', 'mode': 'smart'}]})
    assert saved.status_code == 200, saved.get_json()
    assert saved.get_json()['policies'][0]['name'] == '小米手机'
    assert client.get(notes).get_json()['notes'] == {'192.0.2.9': '小米手机'}


def test_notes_bound_profile_validation_and_conflict(oxidns_app):
    app, repo = oxidns_app
    aid = agent()
    client = app.test_client()
    url = f'/api/agents/{aid}/client-notes'
    body = {'profile_id': 'office', 'ip': '192.0.2.5', 'name': 'NAS', 'previous_name': ''}
    for key, value in [('ip', 'hostname.test'), ('name', 'a\nb'), ('previous_name', None)]:
        assert client.put(url, json={**body, key: value}).status_code == 400
    before = deepcopy(repo.get_profile('office'))
    get_agent_manager().update_agent(aid, {'profile_id': 'default'})
    assert client.put(url, json=body).status_code == 409
    assert repo.get_profile('office') == before


def test_note_profile_clone_and_export_roundtrip(oxidns_app):
    _, repo = oxidns_app
    repo.update_profile_transaction('office', lambda config: config.update(device_notes={'192.0.2.1': '电脑'}))
    repo.create_profile({'id': 'clone', 'name': 'Clone'}, clone_from='office')
    assert repo.get_profile('clone')['device_notes'] == {'192.0.2.1': '电脑'}
    doc = repo.export_all()
    doc['profiles']['default'].pop('device_notes', None)
    repo.import_all(doc)
    assert read_notes(repo.get_compat_config('default')) == {}
    assert read_notes(repo.get_compat_config('office')) == {'192.0.2.1': '电脑'}


def rule_setup(repo, monkeypatch):
    repo.update_profile_transaction('office', lambda config: config.update(domain_discovery={'direct_ruleset': 'lib', 'proxy_ruleset': 'lib'}))
    monkeypatch.setattr(service, 'save_rule_to_local', lambda item: None)


def test_connection_rules_use_agent_defaults_and_skip_duplicates(oxidns_app, monkeypatch):
    app, repo = oxidns_app
    aid = agent()
    rule_setup(repo, monkeypatch)
    # A writable Domain provider uses domain syntax; invalid legacy prefixes
    # have a separate regression covering the explicit repair requirement.
    repo.update_shared_transaction(lambda shared: shared['rule_library'][0].update(content='+.example.test\n'))
    client = app.test_client()
    url = f'/api/agents/{aid}/mihomo/rule-targets'
    state = client.get(url + '?profile=default').get_json()
    assert state['profile_id'] == 'office'
    assert state['direct']['id'] == 'lib'
    body = {'profile_id': 'office', 'ruleset_id': 'lib', 'target': 'direct', 'value': 'new.test', 'rule_type': 'DOMAIN'}
    result = client.post(url + '/apply', json=body)
    assert result.status_code == 200, result.get_json()
    assert len(result.get_json()['added']) == 1
    assert client.post(url + '/apply', json=body).get_json()['skipped'][0]['reason'] == 'exists'
    assert repo.get_compat_config('office')['rule_library'][0]['content'].count('new.test') == 1
    assert not repo.get_profile('default').get('domain_discovery', {}).get('history')


@pytest.mark.parametrize('value', ['192.0.2.1', '198.18.0.1', '::1', '*.example.test', '', 'a.test\nMATCH,DIRECT'])
def test_connection_rule_rejects_non_domains(oxidns_app, monkeypatch, value):
    app, repo = oxidns_app
    rule_setup(repo, monkeypatch)
    before = deepcopy(repo.export_all())
    aid = agent()
    # Registration changes shared state; compare only the rule library.
    result = app.test_client().post(f'/api/agents/{aid}/mihomo/rule-targets/apply', json={
        'profile_id': 'office', 'ruleset_id': 'lib', 'target': 'direct', 'value': value, 'rule_type': 'DOMAIN'})
    assert result.status_code == 400
    assert repo.get_compat_config('office')['rule_library'] == before['shared']['rule_library']


def test_rule_default_changes_under_write_lock_are_rejected(oxidns_app, monkeypatch):
    app, repo = oxidns_app
    rule_setup(repo, monkeypatch)
    aid = agent()
    original = service.update_shared_config_transaction
    def race(updater):
        repo.update_profile_transaction('office', lambda config: config['domain_discovery'].update(direct_ruleset=None))
        return original(updater)
    monkeypatch.setattr(service, 'update_shared_config_transaction', race)
    before = deepcopy(repo.get_compat_config('office')['rule_library'])
    result = app.test_client().post(f'/api/agents/{aid}/mihomo/rule-targets/apply', json={
        'profile_id': 'office', 'ruleset_id': 'lib', 'target': 'direct', 'value': 'new.test', 'rule_type': 'DOMAIN'})
    assert result.status_code == 409, result.get_json()
    assert repo.get_compat_config('office')['rule_library'] == before


def test_client_tools_require_admin(oxidns_app, monkeypatch):
    from backend.common import auth
    app, _ = oxidns_app
    aid = agent()
    monkeypatch.setattr(auth, 'ADMIN_USERNAME', 'admin')
    monkeypatch.setattr(auth, 'ADMIN_PASSWORD', 'password')
    client = app.test_client()
    for path in ('client-notes', 'mihomo/rule-targets', 'mihomo/rule-targets/apply'):
        response = client.open(f'/api/agents/{aid}/{path}', method='POST' if path.endswith('apply') else 'GET')
        assert response.status_code == 401
