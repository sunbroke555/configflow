"""Connection IP writes use the bound profile, preserve source format and avoid broadening rules."""
from copy import deepcopy

import pytest
import yaml

from backend.common.agent_manager import get_agent_manager
from backend.test_client_tools import agent, rule_setup
from backend.test_oxidns_integration import oxidns_app
from backend.utils import connection_rules
from backend.utils.rule_utils import rule_cache_read_path


def setup(oxidns_app, monkeypatch, behavior='classical', content='# Keep this comment\nDOMAIN,existing.test\n'):
    app, repo = oxidns_app
    rule_setup(repo, monkeypatch)
    repo.update_shared_transaction(lambda shared: shared['rule_library'][0].update(behavior=behavior, content=content))
    aid = agent()
    return app.test_client(), repo, aid, f'/api/agents/{aid}/mihomo/rule-targets'


def body(value='192.0.2.8', rule_type='IP-CIDR', target='direct'):
    return dict(profile_id='office', ruleset_id='lib', target=target, value=value, rule_type=rule_type)


@pytest.mark.parametrize('behavior,content,value,rule_type,line', [
    ('classical', '# Keep this comment\nDOMAIN,existing.test', '192.0.2.8', 'IP-CIDR', 'IP-CIDR,192.0.2.8/32,no-resolve'),
    ('classical', '', '2001:0db8:0::8', 'IP-CIDR6', 'IP-CIDR6,2001:db8::8/128,no-resolve'),
    ('ipcidr', '# Keep this comment\n192.0.2.4/32\n', '192.0.2.8', 'IP-CIDR', '192.0.2.8/32'),
    ('ipcidr', '', '2001:db8::8', 'IP-CIDR6', '2001:db8::8/128'),
    ('classical', '', '::ffff:192.0.2.8', 'IP-CIDR', 'IP-CIDR,192.0.2.8/32,no-resolve'),
])
def test_single_address_append_preserves_content_and_cache(oxidns_app, monkeypatch, behavior, content, value, rule_type, line):
    client, repo, aid, url = setup(oxidns_app, monkeypatch, behavior, content)
    default = deepcopy(repo.get_profile('default'))
    result = client.post(url + '/apply?profile=default', json=body(value, rule_type))
    assert result.status_code == 200, result.get_json()
    assert len(result.get_json()['added']) == 1
    item = repo.get_shared()['rule_library'][0]
    assert item['content'].startswith(content)
    assert item['content'].splitlines()[-1] == line
    assert item['content'].count(line) == 1
    with open(rule_cache_read_path(item)) as handle:
        assert handle.read() == item['content']
    assert client.post(url + '/apply', json=body(value, rule_type)).get_json()['skipped'][0]['reason'] == 'exists'
    assert repo.get_profile('default') == default
    # IPs must not enter domain-discovery history/reprobe/undo flows.
    assert not repo.get_profile('office')['domain_discovery'].get('history')


@pytest.mark.parametrize('content', [
    '# Keep this comment\npayload:\n  - "IP-CIDR,192.0.2.1/32,no-resolve"\nother: yes\n',
    '# Keep this comment\npayload: ["IP-CIDR,192.0.2.1/32,no-resolve"]\n',
    'payload: ["IP-CIDR,192.0.2.1/32,no-resolve",]\n',
    '# Keep this comment\npayload: []\n',
    'payload:\n- "IP-CIDR,192.0.2.1/32,no-resolve"',
])
def test_yaml_payload_append_remains_valid_and_keeps_comments(oxidns_app, monkeypatch, content):
    client, repo, _, url = setup(oxidns_app, monkeypatch, content=content)
    result = client.post(url + '/apply', json=body())
    assert result.status_code == 200, result.get_json()
    saved = repo.get_shared()['rule_library'][0]['content']
    assert saved.startswith('# Keep this comment') == content.startswith('# Keep this comment')
    expected = yaml.safe_load(content)
    expected['payload'].append('IP-CIDR,192.0.2.8/32,no-resolve')
    assert yaml.safe_load(saved) == expected


@pytest.mark.parametrize('content,value,rule_type', [
    ('IP-CIDR,192.0.2.0/24,no-resolve', '192.0.2.8', 'IP-CIDR'),
    ('192.0.2.0/24', '192.0.2.8', 'IP-CIDR'),
    ('payload: ["IP-CIDR6,2001:db8::/64,no-resolve"]', '2001:db8::8', 'IP-CIDR6'),
])
def test_existing_covering_network_is_not_duplicated(oxidns_app, monkeypatch, content, value, rule_type):
    client, repo, _, url = setup(oxidns_app, monkeypatch, content=content)
    result = client.post(url + '/apply', json=body(value, rule_type)).get_json()
    assert not result['added'] and result['skipped'][0]['reason'] == 'exists'
    assert repo.get_shared()['rule_library'][0]['content'] == content


@pytest.mark.parametrize('value,rule_type,canonical', [
    ('192.0.2.123/24', 'IP-CIDR', '192.0.2.0/24'),
    ('192.0.3.123/23', 'IP-CIDR', '192.0.2.0/23'),
    ('2001:db8:1234:5678::abcd/64', 'IP-CIDR6', '2001:db8:1234:5678::/64'),
    ('2001:db8:1234:5678::abcd/56', 'IP-CIDR6', '2001:db8:1234:5600::/56'),
])
def test_subnet_is_canonicalized_and_only_full_coverage_is_skipped(oxidns_app, monkeypatch, value, rule_type, canonical):
    host = value.split('/')[0]
    suffix = 32 if rule_type == 'IP-CIDR' else 128
    content = f'{rule_type},{host}/{suffix},no-resolve\n'
    client, repo, _, url = setup(oxidns_app, monkeypatch, content=content)
    result = client.post(url + '/apply', json=body(value, rule_type))
    assert result.status_code == 200, result.get_json()
    assert len(result.get_json()['added']) == 1  # An existing host must not hide the broader subnet.
    assert repo.get_shared()['rule_library'][0]['content'] == content + f'{rule_type},{canonical},no-resolve\n'
    duplicate = client.post(url + '/apply', json=body(value, rule_type)).get_json()
    assert not duplicate['added'] and duplicate['skipped'][0]['reason'] == 'exists'


@pytest.mark.parametrize('value,rule_type', [
    ('new.test', 'IP-CIDR'), ('192.0.2.8/33', 'IP-CIDR'), ('192.0.2.8\nMATCH,DIRECT', 'IP-CIDR'),
    ('192.0.2.8', 'IP-CIDR6'), ('2001:db8::8', 'IP-CIDR'), ('fe80::1%eth0', 'IP-CIDR6'),
    (None, 'IP-CIDR'), ('224.0.0.1', 'IP-CIDR'), ('0.0.0.0', 'IP-CIDR'),
])
def test_invalid_ip_requests_do_not_change_rules(oxidns_app, monkeypatch, value, rule_type):
    client, repo, _, url = setup(oxidns_app, monkeypatch)
    before = deepcopy(repo.get_shared()['rule_library'])
    assert client.post(url + '/apply', json=body(value, rule_type)).status_code == 400
    assert repo.get_shared()['rule_library'] == before


@pytest.mark.parametrize('change', [dict(behavior='domain'), dict(source_type='url'), dict(content='payload: [broken')])
def test_incompatible_sources_are_not_overwritten(oxidns_app, monkeypatch, change):
    client, repo, _, url = setup(oxidns_app, monkeypatch)
    repo.update_shared_transaction(lambda shared: shared['rule_library'][0].update(change))
    before = deepcopy(repo.get_shared()['rule_library'])
    assert client.post(url + '/apply', json=body()).status_code == 400
    assert repo.get_shared()['rule_library'] == before


def test_current_profile_policy_filters_targets_and_is_enforced_on_write(oxidns_app, monkeypatch):
    client, repo, _, url = setup(oxidns_app, monkeypatch, behavior='ipcidr', content='')
    state = client.get(url + '?profile=default').get_json()
    assert state['profile_id'] == 'office'
    assert state['ip_rulesets'][0]['targets'] == ['direct']
    assert client.post(url + '/apply', json=body(target='proxy')).status_code == 400
    assert repo.get_shared()['rule_library'][0]['content'] == ''


@pytest.mark.parametrize('race', ['binding', 'format', 'policy'])
def test_under_lock_changes_cannot_redirect_ip_writes(oxidns_app, monkeypatch, race):
    client, repo, aid, url = setup(oxidns_app, monkeypatch)
    before = repo.get_shared()['rule_library'][0]['content']
    original = connection_rules.update_shared_config_transaction
    def change_then_write(updater):
        if race == 'binding':
            get_agent_manager().update_agent(aid, {'profile_id': 'default'})
        elif race == 'format':
            repo.update_shared_transaction(lambda shared: shared['rule_library'][0].update(behavior='domain'))
        else:
            repo.update_profile_transaction('office', lambda profile: profile['rule_configs'][0].update(policy='PROXY'))
        return original(updater)
    monkeypatch.setattr(connection_rules, 'update_shared_config_transaction', change_then_write)
    assert client.post(url + '/apply', json=body()).status_code in (400, 409)
    assert repo.get_shared()['rule_library'][0]['content'] == before
