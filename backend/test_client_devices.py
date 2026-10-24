"""The client devices page edits names and DNS policies of one profile, one row at a time."""
from copy import deepcopy

import yaml

from backend.common.agent_manager import get_agent_manager
from backend.common.oxidns_device_policy import DIRECT_TAG, apply_policies, revision
from backend.test_oxidns_integration import oxidns_app  # noqa: F401

URL = '/api/client-devices?profile=office'


def agent(kind='oxidns', profile='office', name=None):
    return get_agent_manager().register_agent({'name': name or kind, 'host': '127.0.0.1',
        'port': 18000, 'service_type': kind, 'profile_id': profile})['id']


def change(ip, name, mode, previous_name='', previous_mode=''):
    return {'profile_id': 'office', 'ip': ip, 'name': name, 'mode': mode,
            'previous_name': previous_name, 'previous_mode': previous_mode, 'base_url': ''}


def seed(repo):
    def setup(config):
        config['oxidns'] = apply_policies(config['oxidns'], config, [{'ip': '192.0.2.5', 'name': '电视', 'mode': 'direct'}])
        config['device_notes'] = {'192.0.2.5': '电视', '192.0.2.9': '手机'}
    repo.update_profile_transaction('office', setup)


def test_lists_named_and_policy_devices_with_the_profiles_oxidns_agents(oxidns_app):
    app, repo = oxidns_app
    seed(repo)
    dns, other = agent(name='dns'), agent(profile='default', name='other')
    agent('mihomo')
    state = app.test_client().get(URL).get_json()
    assert state['profile_id'] == 'office' and state['editable']
    assert state['devices'] == [{'ip': '192.0.2.5', 'name': '电视', 'mode': 'direct'},
                                {'ip': '192.0.2.9', 'name': '手机', 'mode': ''}]
    assert [item['id'] for item in state['agents']] == [dns]
    assert other not in str(state)
    assert state['oxidns_revision'] == revision(repo.get_profile('office')['oxidns'])


def test_renaming_never_touches_the_oxidns_settings(oxidns_app):
    app, repo = oxidns_app
    seed(repo)
    oxidns = deepcopy(repo.get_profile('office')['oxidns'])
    client = app.test_client()
    for body in (change('192.0.2.5', '客厅电视', 'direct', '电视', 'direct'), change('192.0.2.9', '', '', '手机')):
        saved = client.put(URL, json=body)
        assert saved.status_code == 200, saved.get_json()
    assert repo.get_profile('office')['oxidns'] == oxidns
    assert saved.get_json()['devices'] == [{'ip': '192.0.2.5', 'name': '客厅电视', 'mode': 'direct'}]


def test_setting_and_clearing_a_policy_keeps_the_name(oxidns_app):
    app, repo = oxidns_app
    seed(repo)
    client = app.test_client()
    before = client.get(URL).get_json()['oxidns_revision']
    added = client.put(URL, json=change('192.0.2.9', '手机', 'smart', '手机'))
    assert added.status_code == 200, added.get_json()
    assert added.get_json()['oxidns_revision'] != before
    assert repo.get_profile('office')['oxidns']['device_policies'][-1] == {'ip': '192.0.2.9', 'name': '手机', 'mode': 'smart'}
    cleared = client.put(URL, json=change('192.0.2.5', '', '', '电视', 'direct')).get_json()
    assert {row['ip'] for row in repo.get_profile('office')['oxidns']['device_policies']} == {'192.0.2.9'}
    # Clearing the name together with the policy leaves nothing behind.
    assert '192.0.2.5' not in repo.get_profile('office')['device_notes']
    assert cleared['devices'] == [{'ip': '192.0.2.9', 'name': '手机', 'mode': 'smart'}]
    native = yaml.safe_load(repo.get_profile('office')['oxidns']['native_config'])
    assert all(plugin['tag'] != DIRECT_TAG for plugin in native['plugins'])


def test_a_new_address_is_normalized_and_can_start_with_a_policy(oxidns_app):
    app, repo = oxidns_app
    saved = app.test_client().put(URL, json=change('192.0.2.0/24', '', 'direct')).get_json()
    assert saved['devices'] == [{'ip': '192.0.2.0/24', 'name': '', 'mode': 'direct'}]
    assert repo.get_profile('office')['device_notes'] == {}


def test_a_row_changed_elsewhere_is_not_overwritten(oxidns_app):
    app, repo = oxidns_app
    seed(repo)
    before = deepcopy(repo.get_profile('office'))
    client = app.test_client()
    for body in (change('192.0.2.5', '新名', 'direct', '旧名', 'direct'), change('192.0.2.5', '电视', 'smart', '电视', 'smart'),
                 change('192.0.2.7', '新设备', '', '已有')):
        response = client.put(URL, json=body)
        assert response.status_code == 409 and '其他页面' in response.get_json()['message']
    assert repo.get_profile('office') == before


def test_invalid_requests_change_nothing(oxidns_app):
    app, repo = oxidns_app
    seed(repo)
    before = deepcopy(repo.get_profile('office'))
    client = app.test_client()
    for body, status in ((change('hostname.test', 'x', ''), 400), (change('192.0.2.5', 'a\nb', 'direct', '电视', 'direct'), 400),
                         (change('192.0.2.5', '电视', 'proxy', '电视', 'direct'), 400), ({**change('192.0.2.1', '', ''), 'extra': 1}, 400),
                         ({**change('192.0.2.1', 'x', ''), 'profile_id': 'default'}, 409)):
        assert client.put(URL, json=body).status_code == status
    assert repo.get_profile('office') == before


def test_a_hand_edited_policy_plugin_blocks_policy_changes_but_not_renames(oxidns_app):
    app, repo = oxidns_app
    seed(repo)

    def hand_edit(config):
        document = yaml.safe_load(config['oxidns']['native_config'])
        next(p for p in document['plugins'] if p['tag'] == DIRECT_TAG)['args'][0] = {'exec': '$forward_remote'}
        config['oxidns']['native_config'] = yaml.safe_dump(document)
    repo.update_profile_transaction('office', hand_edit)
    client = app.test_client()
    state = client.get(URL).get_json()
    assert not state['editable'] and state['reason']
    assert client.put(URL, json=change('192.0.2.9', '手机', 'direct', '手机')).status_code == 400
    assert client.put(URL, json=change('192.0.2.5', '电视机', 'direct', '电视', 'direct')).status_code == 200


def test_client_devices_require_admin(oxidns_app, monkeypatch):
    from backend.common import auth
    app, _ = oxidns_app
    monkeypatch.setattr(auth, 'ADMIN_USERNAME', 'admin')
    monkeypatch.setattr(auth, 'ADMIN_PASSWORD', 'password')
    client = app.test_client()
    assert client.get(URL).status_code == 401
    assert client.put(URL, json=change('192.0.2.1', 'x', '')).status_code == 401
