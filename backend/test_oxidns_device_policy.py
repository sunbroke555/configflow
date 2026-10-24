from copy import deepcopy

import pytest
import yaml

from backend.common.oxidns_device_policy import (DIRECT_TAG, HOOK, POLICY_TAG,
    apply_policies, normalize_client_range, normalize_policies, read_policies)
from backend.common.oxidns_editor import prepare_editor
from backend.common.oxidns_settings import OxidnsSettingsError
from backend.common.oxidns_common import apply_common_settings, read_common_settings
from backend.converters.oxidns import generate_oxidns_config
from backend.common.agent_manager import get_agent_manager
from backend.test_oxidns_integration import oxidns_app

ROWS = [{'ip': '192.168.50.50', 'name': '电脑', 'mode': 'smart'},
        {'ip': '192.168.50.51', 'name': '电视', 'mode': 'direct'}]


def plugin(document, tag):
    return next(p for p in document['plugins'] if p['tag'] == tag)


def test_policy_roundtrip_preserves_native_content_and_removes_only_owned_rules():
    config = {'oxidns': {'query_recorder': {'enabled': True}, 'custom_hosts': 'example.test 1.2.3.4'}}
    settings = prepare_editor(config['oxidns'], config)
    before = yaml.safe_load(settings['native_config'])
    before['custom_extension'] = {'keep': 'untouched'}
    settings['native_config'] = yaml.safe_dump(before)
    settings['additional_files'] = [{'path': 'custom.txt', 'content': 'keep'}]
    updated = apply_policies(settings, config, ROWS)
    document = yaml.safe_load(updated['native_config'])
    main = plugin(document, 'sequence_main')['args']
    assert main.index(HOOK) > main.index({'exec': '$query_recorder'})
    assert main.index(HOOK) > main.index({'exec': '$custom_hosts'})
    assert main.index(HOOK) < len(main) - 1
    assert plugin(document, POLICY_TAG)['args'][0]['matches'] == ['client_ip 192.168.50.51']
    assert plugin(document, DIRECT_TAG)['args'][0] == {'exec': '$forward_local'}
    assert read_policies(updated, config)['editable']
    assert updated['additional_files'] == settings['additional_files']
    cleared = apply_policies(updated, config, [])
    assert yaml.safe_load(cleared['native_config']) == before
    assert cleared['device_policies'] == []
    assert read_policies(cleared, config)['editable']


@pytest.mark.parametrize('rows', [None, {}, [{'ip': '192.168.0.1/33', 'mode': 'direct'}],
    [{'ip': '1.2.3.4\nexec reject', 'mode': 'direct'}], [{'ip': '::', 'mode': 'direct'}],
    [{'ip': '1.2.3.4', 'mode': 'proxy'}], [{'ip': '1.2.3.4', 'mode': 'direct', 'name': 1}],
    [{'ip': '2001:db8::1', 'mode': 'smart'}, {'ip': '2001:0db8::1', 'mode': 'direct'}],
    [{'ip': '192.168.0.1', 'mode': 'smart'}, {'ip': '::ffff:192.168.0.1', 'mode': 'direct'}]])
def test_reject_invalid_and_duplicate_devices(rows):
    with pytest.raises(OxidnsSettingsError):
        normalize_policies(rows)


@pytest.mark.parametrize(('raw', 'expected'), [
    ('192.168.50.51/24', '192.168.50.0/24'), (' 192.168.50.51/32 ', '192.168.50.51'),
    ('2001:0DB8:abcd::1234/48', '2001:db8:abcd::/48'), ('2001:db8::1/128', '2001:db8::1'),
    ('::ffff:192.168.50.51/120', '192.168.50.0/24'), ('::ffff:c0a8:3233', '192.168.50.51'),
    ('::ffff:192.168.50.51/128', '192.168.50.51'), ('::ffff:192.168.50.51/96', '0.0.0.0/0'),
    ('0.0.0.0/0', '0.0.0.0/0'), ('::/0', '::/0'), ('192.168.0.255/31', '192.168.0.254/31'),
    ('2001:db8::ffff/127', '2001:db8::fffe/127'), ('::1/64', '::/64'),
])
def test_canonicalize_client_hosts_and_networks(raw, expected):
    assert normalize_client_range(raw) == expected


@pytest.mark.parametrize('raw', ['example.com/24', '192.168.0.1/-1', '192.168.0.1/33',
    '192.168.0.1/24/1', '192.168.0.1/255.255.255.0', '192.168.0.1/',
    '2001:db8::1/129', 'fe80::1%eth0/64', '::ffff:192.168.0.1/95',
    '192.168.001.1/24', '224.0.0.0/4', 'ff00::/8', '0.0.0.0/32', '::/128',
    '192.168.0.1/２４', '192.168.0.1/24\nexec accept'])
def test_reject_invalid_client_ranges(raw):
    with pytest.raises(OxidnsSettingsError):
        normalize_client_range(raw)


@pytest.mark.parametrize(('first', 'second'), [
    ('192.168.50.0/24', '192.168.50.51/24'), ('192.168.50.51', '192.168.50.51/32'),
    ('2001:db8::/64', '2001:0db8::1234/64'), ('2001:db8::1', '2001:db8::1/128'),
    ('192.168.50.0/24', '::ffff:192.168.50.51/120'),
])
def test_reject_equivalent_hosts_and_networks(first, second):
    with pytest.raises(OxidnsSettingsError, match='重复'):
        normalize_policies([{'ip': first, 'mode': 'smart'}, {'ip': second, 'mode': 'direct'}])


def test_specific_ranges_and_smart_exceptions_win_independent_of_input_order():
    rows = [{'ip': '192.168.0.0/16', 'mode': 'direct'},
            {'ip': '192.168.50.0/24', 'mode': 'smart'},
            {'ip': '192.168.50.128/25', 'mode': 'direct'},
            {'ip': '192.168.50.150', 'mode': 'smart'},
            {'ip': '192.168.50.51', 'mode': 'direct'}]
    first = apply_policies({}, {}, rows)
    second = apply_policies({}, {}, rows[::-1])
    policy = plugin(yaml.safe_load(first['native_config']), POLICY_TAG)
    assert policy == plugin(yaml.safe_load(second['native_config']), POLICY_TAG)
    assert policy['args'] == [
        {'matches': ['client_ip 192.168.50.51'], 'exec': f'goto {DIRECT_TAG}'},
        {'matches': ['client_ip 192.168.50.150'], 'exec': 'return'},
        {'matches': ['client_ip 192.168.50.128/25'], 'exec': f'goto {DIRECT_TAG}'},
        {'matches': ['client_ip 192.168.50.0/24'], 'exec': 'return'},
        {'matches': ['client_ip 192.168.0.0/16'], 'exec': f'goto {DIRECT_TAG}'},
        {'exec': 'return'},
    ]
    assert read_policies(first, {})['editable']
    changed = apply_common_settings(first, {'query_recorder.enabled': True}, {})['settings']
    assert read_policies(changed, {})['editable']
    assert plugin(yaml.safe_load(changed['native_config']), POLICY_TAG) == policy


def test_legacy_host_rules_can_add_remove_networks_without_losing_native_changes():
    settings = apply_policies({}, {}, ROWS)
    original = yaml.safe_load(settings['native_config'])
    original['custom_extension'] = {'keep': True}
    settings['native_config'] = yaml.safe_dump(original)
    extended = apply_policies(settings, {}, ROWS + [{'ip': '192.168.0.0/16', 'mode': 'direct'}])
    assert read_policies(extended, {})['editable']
    restored = apply_policies(extended, {}, ROWS)
    assert yaml.safe_load(restored['native_config']) == original
    empty = apply_policies(extended, {}, [{'ip': '192.168.0.0/16', 'mode': 'smart'}])
    assert all(p['tag'] not in (POLICY_TAG, DIRECT_TAG) for p in yaml.safe_load(empty['native_config'])['plugins'])
    assert read_policies(empty, {})['editable']


def test_manually_reordered_cidr_rules_are_not_silently_overwritten():
    rows = [{'ip': '192.168.0.0/16', 'mode': 'direct'}, {'ip': '192.168.0.1', 'mode': 'smart'}]
    settings = apply_policies({}, {}, rows)
    document = yaml.safe_load(settings['native_config'])
    args = plugin(document, POLICY_TAG)['args']
    args[0], args[1] = args[1], args[0]
    settings['native_config'] = yaml.safe_dump(document)
    assert not read_policies(settings, {})['editable']
    with pytest.raises(OxidnsSettingsError):
        apply_policies(settings, {}, [])


def test_template_keeps_network_rules_and_roundtrips_to_editor():
    rows = [{'ip': '192.168.50.51/24', 'mode': 'direct'}, {'ip': '192.168.50.50', 'mode': 'smart'}]
    config = {'oxidns': {'device_policies': rows}}
    document = yaml.safe_load(generate_oxidns_config(config))
    assert plugin(document, POLICY_TAG)['args'][1]['matches'] == ['client_ip 192.168.50.0/24']
    assert read_policies(prepare_editor(config['oxidns'], config), config)['editable']


def test_template_and_other_editors_keep_policies():
    config = {'oxidns': {'device_policies': ROWS}}
    document = yaml.safe_load(generate_oxidns_config(config))
    assert HOOK in plugin(document, 'sequence_main')['args']
    settings = prepare_editor(config['oxidns'], config)
    state = read_common_settings(settings, config)
    assert state['capabilities']['custom_matches']['editable']
    assert state['values']['custom_matches'] == []
    result = apply_common_settings(settings, {'default_forward': 'forward_local', 'query_recorder.enabled': True}, config)
    assert read_policies(result['settings'], config)['editable']
    changed = yaml.safe_load(result['settings']['native_config'])
    main = plugin(changed, 'sequence_main')['args']
    assert main.index({'exec': '$query_recorder'}) < main.index(HOOK)


def test_manual_edits_are_detected_without_overwriting():
    settings = apply_policies({}, {}, ROWS)
    document = yaml.safe_load(settings['native_config'])
    plugin(document, DIRECT_TAG)['args'][0] = {'exec': '$forward_remote'}
    settings['native_config'] = yaml.safe_dump(document)
    before = deepcopy(settings)
    assert not read_policies(settings, {})['editable']
    with pytest.raises(OxidnsSettingsError):
        apply_policies(settings, {}, [])
    assert settings == before


def test_api_binding_shared_scope_and_stale_write(oxidns_app):
    app, repository = oxidns_app
    manager = get_agent_manager()
    aid = manager.register_agent({'name': 'DNS', 'host': '127.0.0.1', 'port': 18000,
                                  'service_type': 'oxidns', 'profile_id': 'office'})['id']
    peer = manager.register_agent({'name': 'DNS2', 'host': '127.0.0.2', 'port': 18000,
                                   'service_type': 'oxidns', 'profile_id': 'office'})['id']
    client = app.test_client()
    url = f'/api/agents/{aid}/oxidns/device-policies?profile=default'
    state = client.get(url).get_json()
    assert state['profile_id'] == 'office'
    assert {a['id'] for a in state['agents']} == {aid, peer}
    default = deepcopy(repository.get_profile('default'))
    body = {'profile_id': 'office', 'revision': state['revision'], 'notes_revision': state['notes_revision'], 'policies': ROWS}
    saved = client.put(url, json=body)
    assert saved.status_code == 200, saved.get_json()
    assert saved.headers['Cache-Control'] == 'no-store'
    assert saved.get_json()['policies'] == ROWS
    assert repository.get_profile('default') == default
    assert 'token' not in saved.get_data(as_text=True)
    assert client.put(url, json=body).status_code == 409
    body['revision'] = saved.get_json()['revision']
    body['notes_revision'] = saved.get_json()['notes_revision']
    manager.update_agent(aid, {'profile_id': 'default'})
    assert client.put(url, json=body).status_code == 409


def test_publish_rejects_stale_device_policy_snapshot_before_contacting_agent(oxidns_app, monkeypatch):
    app, repository = oxidns_app
    manager = get_agent_manager()
    aid = manager.register_agent({'name': 'DNS', 'host': '127.0.0.1', 'port': 18000,
                                  'service_type': 'oxidns', 'profile_id': 'office'})['id']
    client = app.test_client()
    state = client.get(f'/api/agents/{aid}/oxidns/device-policies').get_json()
    monkeypatch.setattr(manager, 'begin_deployment', lambda *args: pytest.fail('Stale draft must not begin publication'))
    body = {'expected_profile_id': 'default', 'expected_oxidns_revision': state['revision']}
    assert client.post(f'/api/agents/{aid}/push-config', json=body).status_code == 409
    body['expected_profile_id'] = 'office'
    repository.update_profile_transaction('office', lambda config: config['oxidns'].update(worker_threads=8))
    assert client.post(f'/api/agents/{aid}/push-config', json=body).status_code == 409


def test_api_saves_canonical_networks_and_rejects_duplicate_ranges_without_changes(oxidns_app):
    app, repository = oxidns_app
    aid = get_agent_manager().register_agent({'name': 'DNS', 'host': '127.0.0.1',
        'port': 18000, 'service_type': 'oxidns', 'profile_id': 'office'})['id']
    client = app.test_client()
    url = f'/api/agents/{aid}/oxidns/device-policies'
    state = client.get(url).get_json()
    body = {'profile_id': 'office', 'revision': state['revision'], 'notes_revision': state['notes_revision'], 'policies': [
        {'ip': '192.168.50.51/24', 'mode': 'direct'}, {'ip': '192.168.50.50', 'mode': 'smart'}]}
    saved = client.put(url, json=body)
    assert saved.status_code == 200, saved.get_json()
    assert [p['ip'] for p in saved.get_json()['policies']] == ['192.168.50.0/24', '192.168.50.50']
    assert client.get(url).get_json()['editable']
    before = deepcopy(repository.get_profile('office'))
    body['revision'] = saved.get_json()['revision']
    body['notes_revision'] = saved.get_json()['notes_revision']
    body['policies'].append({'ip': '192.168.50.0/24', 'mode': 'smart'})
    assert client.put(url, json=body).status_code == 400
    assert repository.get_profile('office') == before


def test_api_requires_admin_and_only_accepts_oxidns(oxidns_app, monkeypatch):
    from backend.common import auth
    app, _ = oxidns_app
    monkeypatch.setattr(auth, 'ADMIN_USERNAME', 'admin')
    monkeypatch.setattr(auth, 'ADMIN_PASSWORD', 'password')
    agent = get_agent_manager().register_agent({'name': 'proxy', 'host': '127.0.0.1',
                                               'port': 18000, 'service_type': 'mihomo'})
    url = f'/api/agents/{agent["id"]}/oxidns/device-policies'
    client = app.test_client()
    for method in (client.get, client.put):
        assert method(url).status_code == 401
        assert method(url, headers={'Authorization': 'Bearer ' + agent['token']}).status_code == 401
    assert client.get(url, headers={'Authorization': 'Bearer ' + auth.generate_token('admin')}).status_code == 400
