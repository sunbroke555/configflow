"""Rules share sources while composition, group references and MosDNS stay local."""
import pytest
import yaml
from flask import Flask

from backend.common import config as config_module
from backend.common.config_repository import ProfileRepository
from backend.routes import register_blueprints


@pytest.fixture
def compositions(tmp_path):
    repository = ProfileRepository(tmp_path)
    repository.save_shared({
        'subscriptions': [{'id': 'subscription', 'name': 'Feed', 'url': 'https://example.test/sub'}],
        'subscription_aggregations': [{'id': 'aggregation', 'name': 'Aggregate', 'subscriptions': ['subscription'], 'nodes': []}],
        'rule_library': [{'id': 'library', 'name': 'Shared Rules', 'source_type': 'content', 'content': 'DOMAIN,example.test', 'behavior': 'classical', 'enabled': True}],
    })
    repository.save_profile('default', {
        'proxy_groups': [
            {'id': 'group', 'name': 'Proxy', 'type': 'select', 'aggregations': ['aggregation'], 'manual_nodes': ['DIRECT']},
            {'id': 'following', 'name': 'Following', 'type': 'select', 'follow_group': 'group'},
            {'id': 'including', 'name': 'Including', 'type': 'select', 'include_groups': ['group'], 'proxies_order': [{'type': 'strategy', 'id': 'group'}]},
        ],
        'rule_configs': [
            {'id': 'set', 'itemType': 'ruleset', 'library_rule_id': 'library', 'policy': 'Proxy', 'enabled': True},
            {'id': 'inline', 'itemType': 'rule', 'rule_type': 'DOMAIN', 'value': 'local.test', 'policy': 'DIRECT', 'enabled': True},
        ],
    })
    repository.create_profile({'id': 'other', 'name': 'Other'}, clone_from='default')
    config_module.set_repository(repository)
    app = Flask(__name__)
    register_blueprints(app)
    return app.test_client(), repository, {'X-ConfigFlow-Profile': 'other'}


def test_source_fields_cannot_override_shared_library_and_reorder_only_moves_ids(compositions):
    client, repository, headers = compositions
    before = repository.get_profile('default')
    response = client.put('/api/rules/set', headers=headers, json={
        'policy': 'REJECT', 'name': 'Override', 'url': 'https://attacker.invalid',
        'content': 'override', 'behavior': 'ipcidr', 'source_type': 'url',
    })
    assert response.status_code == 200
    item = next(item for item in client.get('/api/rules', headers=headers).json if item['id'] == 'set')
    assert item['name'] == 'Shared Rules'
    assert item['behavior'] == 'classical'
    assert item['content'] == 'DOMAIN,example.test'
    assert item['policy'] == 'REJECT'
    assert client.post('/api/rules/reorder', headers=headers, json={'rule_configs': [{'id': 'inline'}, {'id': 'set', 'policy': 'DIRECT'}]}).status_code == 200
    profile = repository.get_profile('other')
    assert [item['id'] for item in profile['rule_configs']] == ['inline', 'set']
    assert profile['rule_configs'][1]['policy'] == 'REJECT'
    assert 'content' not in profile['rule_configs'][1]
    assert 'url' not in profile['rule_configs'][1]
    assert repository.get_profile('default') == before


def test_missing_library_is_rejected_atomically(compositions):
    client, repository, headers = compositions
    before = repository.export_all()
    response = client.post('/api/rule-sets', headers=headers, json={'id': 'bad', 'name': 'Private', 'url': 'https://example.test', 'policy': 'DIRECT'})
    assert response.status_code == 400
    assert repository.export_all() == before


def test_group_rename_preserves_id_references_and_profile_isolation(compositions):
    client, repository, headers = compositions
    before = repository.get_profile('default')
    assert client.put('/api/proxy-groups/group', headers=headers, json={'name': 'Renamed'}).status_code == 200
    profile = repository.get_profile('other')
    assert profile['rule_configs'][0]['policy'] == 'Renamed'
    assert profile['rule_configs'][1]['policy'] == 'DIRECT'
    assert profile['proxy_groups'][1]['follow_group'] == 'group'
    assert profile['proxy_groups'][2]['include_groups'] == ['group']
    assert profile['proxy_groups'][2]['proxies_order'] == [{'type': 'strategy', 'id': 'group'}]
    assert repository.get_profile('default') == before
    snapshot = repository.export_all()
    assert client.delete('/api/proxy-groups/group', headers=headers).status_code == 409
    assert repository.export_all() == snapshot


def test_any_profile_can_reference_shared_aggregation_sources_directly(compositions):
    client, repository, headers = compositions
    default = repository.get_profile('default')
    shared = repository.get_shared()
    response = client.put('/api/proxy-groups/group', headers=headers, json={'subscriptions': ['subscription']})
    assert response.status_code == 200, response.get_data(as_text=True)
    assert repository.get_profile('other')['proxy_groups'][0]['subscriptions'] == ['subscription']
    assert repository.get_compat_config('other')['subscriptions'][0]['id'] == 'subscription'
    assert repository.get_profile('default') == default
    assert repository.get_shared() == shared
    assert 'resource_refs' not in repository.get_profile('other')



def test_dns_selection_is_cleaned_atomically_when_rule_is_deleted(compositions):
    client, repository, headers = compositions
    before = repository.get_profile('default')
    response = client.post('/api/mosdns/rulesets', headers=headers, json={'direct_rulesets': ['set'], 'direct_rules': ['inline']})
    assert response.status_code == 200
    for rule_id in ('set', 'inline'):
        response = client.delete('/api/rules/' + rule_id, headers=headers)
        assert response.status_code == 200
        current = repository.get_profile('other')
        assert all(item['id'] != rule_id for item in current['rule_configs'])
        assert rule_id not in current['mosdns']['direct_rulesets']
        assert rule_id not in current['mosdns']['direct_rules']
    assert repository.get_profile('default') == before


def test_selected_dns_rule_cannot_change_to_an_unconvertible_type(compositions):
    client, repository, headers = compositions
    assert client.post('/api/mosdns/rulesets', headers=headers, json={'proxy_rules': ['inline']}).status_code == 200
    before = repository.get_profile('other')
    response = client.put('/api/rules/inline', headers=headers, json={'rule_type': 'GEOIP', 'value': 'CN'})
    assert response.status_code == 400
    assert 'MosDNS 代理规则' in response.get_json()['message']
    assert repository.get_profile('other') == before
    # Convertible types and unrelated edits still save.
    assert client.put('/api/rules/inline', headers=headers, json={'rule_type': 'IP-CIDR', 'value': '10.0.0.0/8'}).status_code == 200
    assert client.put('/api/rules/inline', headers=headers, json={'policy': 'Proxy'}).status_code == 200
    # Unselected rules may use any type.
    assert client.put('/api/rules/inline', headers={'X-ConfigFlow-Profile': 'default'}, json={'rule_type': 'GEOIP'}).status_code == 200


def test_native_oxidns_type_guard_follows_the_routing_document(compositions):
    """Native drafts keep stale selectors as a catalog; the YAML decides what is selected."""
    client, repository, headers = compositions
    def save(settings):
        assert client.post('/api/oxidns/settings', headers=headers, json=settings).status_code == 200
    def apply(settings, proxy_rules):
        response = client.post('/api/oxidns/common-settings/apply', headers=headers,
                               json={'settings': settings, 'changes': {'proxy_rules': proxy_rules}})
        assert response.status_code == 200, response.get_json()
        return response.get_json()['settings']
    def retype(rule_type, value):
        return client.put('/api/rules/inline', headers=headers, json={'rule_type': rule_type, 'value': value})
    settings = client.get('/api/oxidns/settings', headers=headers).get_json()
    native = client.post('/api/oxidns/editor', headers=headers, json={'settings': settings}).get_json()['settings']
    save(apply(native, ['inline']))
    response = retype('GEOIP', 'CN')
    assert response.status_code == 400 and 'OxiDNS DNS 处理「代理 DNS」' in response.get_json()['message']
    unselected = apply(client.get('/api/oxidns/settings', headers=headers).get_json(), [])
    save(unselected)
    stored = client.get('/api/oxidns/settings', headers=headers).get_json()
    assert stored['config_mode'] == 'native' and 'inline' in stored['proxy_rules']
    assert retype('GEOIP', 'CN').status_code == 200


def test_deleting_a_rule_removes_it_from_native_oxidns_routing(compositions):
    """A deleted ruleset must not leave providers or downloads no publication can fill."""
    import yaml
    from backend.common.oxidns_common_routing import read_routing
    from backend.converters.oxidns import _rule_path, _tag, get_oxidns_ruleset_downloads
    from backend.common.oxidns_native import relative_resource, resource_references
    client, repository, headers = compositions
    settings = client.get('/api/oxidns/settings', headers=headers).get_json()
    native = client.post('/api/oxidns/editor', headers=headers, json={'settings': settings}).get_json()['settings']
    response = client.post('/api/oxidns/common-settings/apply', headers=headers, json={
        'settings': native, 'changes': {'direct_rulesets': ['set'], 'proxy_rules': ['inline']}})
    assert response.status_code == 200, response.get_json()
    assert client.post('/api/oxidns/settings', headers=headers, json=response.get_json()['settings']).status_code == 200
    assert _rule_path({'id': 'set'}, 'ip').removeprefix('./') in client.get('/api/oxidns/settings', headers=headers).get_json()['native_config']

    assert client.delete('/api/rules/set', headers=headers).status_code == 200
    config = repository.get_compat_config('other')
    document = yaml.safe_load(config['oxidns']['native_config'])
    # Providers, sequence matches and rules_download entries all name the generated file.
    assert _tag('rule', 'set') not in config['oxidns']['native_config']
    referenced = {relative_resource(path) for path in resource_references(document)} - {None}
    downloads = {item['local_path'].removeprefix('./') for item in get_oxidns_ruleset_downloads(config)}
    assert not {path for path in referenced if path.startswith('rules/rule_')} - downloads
    routing = read_routing(document, config['oxidns'], config)
    assert routing['values']['proxy_rules'] == ['inline']
    assert all(routing['capabilities'][key]['editable']
               for key in ('direct_rulesets', 'proxy_rulesets', 'direct_rules', 'proxy_rules'))
    # The other profile's composition is untouched.
    assert any(item['id'] == 'set' for item in repository.get_profile('default')['rule_configs'])


def test_rule_delete_is_refused_while_unrecognized_native_routing_uses_it(compositions):
    client, repository, headers = compositions
    settings = client.get('/api/oxidns/settings', headers=headers).get_json()
    native = client.post('/api/oxidns/editor', headers=headers, json={'settings': settings}).get_json()['settings']
    applied = client.post('/api/oxidns/common-settings/apply', headers=headers, json={
        'settings': native, 'changes': {'direct_rulesets': ['set']}}).get_json()['settings']
    # A hand-written provider reusing the generated file makes routing unrecognizable.
    from backend.converters.oxidns import _rule_path
    applied['native_config'] = applied['native_config'].replace(
        'plugins:\n', 'plugins:\n- tag: manual_set\n  type: domain_set\n  args:\n    files:\n'
        f'    - {_rule_path({"id": "set"}, "domain")}\n', 1)
    assert client.post('/api/oxidns/settings', headers=headers, json=applied).status_code == 200
    response = client.delete('/api/rules/set', headers=headers)
    assert response.status_code == 400 and 'YAML 中删掉' in response.get_json()['message']
    assert any(item['id'] == 'set' for item in repository.get_profile('other')['rule_configs'])


def test_scoped_content_rule_proxy_avoids_network(compositions, monkeypatch):
    client, repository, headers = compositions
    def unexpected_network(*args, **kwargs):
        raise AssertionError('Local shared content must not perform HTTP self-requests')
    monkeypatch.setattr('requests.get', unexpected_network)
    query = {'url': '/api/profiles/other/rule-library/content/library'}
    assert client.get('/api/profiles/other/mosdns/rule-proxy', query_string=query, headers=headers).status_code == 401
    query['token'] = repository.get_system()['system_config']['rule_proxy_token']
    response = client.get('/api/profiles/other/mosdns/rule-proxy', query_string=query, headers=headers)
    assert response.status_code == 200
    assert response.get_data(as_text=True).strip() == 'full:example.test'


# ---- Per-rule OxiDNS DNS on rule writes -----------------------------------

from backend.test_rule_compose_route import (  # noqa: E402
    HEADERS, lock_native, make_native, native_values, oxidns, set_dns, space,
)


def put_rule(client, rule_id, **fields):
    return client.put('/api/rules/' + rule_id, headers=HEADERS, json=fields)


@pytest.mark.parametrize('native', [False, True], ids=['template', 'native'])
def test_policy_change_moves_follow_rule_template_and_native(space, native):
    client, repository = space
    if native:
        make_native(client)
    assert set_dns(client, {'inline': 'follow', 'set': 'follow'}).status_code == 200
    response = put_rule(client, 'inline', policy='Proxy')
    assert response.status_code == 200, response.get_json()
    state = client.get('/api/rules/dns', headers=HEADERS).get_json()['rules']
    assert state['inline'] == {'state': 'follow', 'route': 'proxy', 'follow_route': 'proxy'}
    assert state['set'] == {'state': 'follow', 'route': 'direct', 'follow_route': 'direct'}
    values = native_values(repository)['values'] if native else oxidns(repository)
    assert 'inline' in values['proxy_rules'] and 'inline' not in values['direct_rules']
    assert oxidns(repository)['rule_dns_follow'] == ['inline', 'set']


def test_policy_change_keeps_explicit_dns(space):
    client, repository = space
    assert set_dns(client, {'inline': 'direct', 'proxied': 'blackhole'}).status_code == 200
    for rule_id in ('inline', 'proxied'):
        assert put_rule(client, rule_id, policy='REJECT').status_code == 200
    stored = oxidns(repository)
    assert stored['direct_rules'] == ['inline'] and stored['rule_actions'] == {'proxied': {'action': 'blackhole'}}
    assert stored['rule_dns_follow'] == []


def test_policy_reject_with_follow_removes_route(space):
    client, repository = space
    make_native(client)
    assert set_dns(client, {'proxied': 'follow'}).status_code == 200
    assert put_rule(client, 'proxied', policy='REJECT').status_code == 200
    assert native_values(repository)['values']['proxy_rules'] == []
    assert client.get('/api/rules/dns', headers=HEADERS).get_json()['rules']['proxied'] == {
        'state': 'follow', 'route': None, 'follow_route': None}


def test_policy_change_on_custom_native_saves_with_warning(space):
    client, repository = space
    lock_native(client, repository)
    native = oxidns(repository)['native_config']
    response = put_rule(client, 'inline', policy='Proxy')
    assert response.status_code == 200
    assert response.get_json()['warnings'][0].startswith('OxiDNS 分流含自定义结构，DNS 未修改：黑洞插件')
    assert repository.get_profile('other')['rule_configs'][1]['policy'] == 'Proxy'
    assert oxidns(repository)['native_config'] == native
    # A remark edit does not touch routing and says nothing.
    response = put_rule(client, 'inline', remark='note')
    assert response.status_code == 200 and 'warnings' not in response.get_json()


def test_put_dns_field_popped_from_rule_item(space):
    client, repository = space
    response = put_rule(client, 'inline', policy='DIRECT', dns='nxdomain')
    assert response.status_code == 200, response.get_json()
    assert 'dns' not in response.get_json()['data']
    assert 'dns' not in next(item for item in repository.get_profile('other')['rule_configs'] if item['id'] == 'inline')
    assert oxidns(repository)['rule_actions'] == {'inline': {'action': 'nxdomain'}}
    assert put_rule(client, 'inline', dns='drop').status_code == 400


def test_toggle_with_whole_item_is_not_strict(space):
    client, repository = space
    lock_native(client, repository)
    item = next(item for item in client.get('/api/rules', headers=HEADERS).json if item['id'] == 'proxied')
    response = put_rule(client, 'proxied', **{**item, 'enabled': False})
    assert response.status_code == 200
    assert response.get_json()['warnings'][0].startswith('OxiDNS 分流含自定义结构')
    # A request naming dns on the same locked draft is refused as a whole.
    before = repository.export_all()
    response = put_rule(client, 'inline', policy='Proxy', dns='refused')
    assert response.status_code == 409 and response.get_json()['usages'][0]['code'] == 'oxidns_routing_custom'
    assert repository.export_all() == before


def test_type_change_blocked_by_rule_action_unless_dns_default(space):
    client, repository = space
    assert set_dns(client, {'inline': 'blackhole', 'proxied': 'follow'}).status_code == 200
    before = repository.export_all()
    response = put_rule(client, 'inline', rule_type='DST-PORT', value='443')
    assert response.status_code == 400
    assert response.get_json()['message'] == ('此规则已用于 OxiDNS DNS 处理「黑洞」，DST-PORT 类型不参与 DNS 分流。'
                                              '请同时把 DNS 设为默认上游，或新建一条规则')
    assert repository.export_all() == before
    response = put_rule(client, 'inline', rule_type='DST-PORT', value='443', dns='default')
    assert response.status_code == 200
    assert oxidns(repository)['rule_actions'] == {}
    # A follow rule simply loses its route.
    assert put_rule(client, 'proxied', rule_type='GEOIP', value='US').status_code == 200
    assert oxidns(repository)['proxy_rules'] == []


def test_delete_removes_actions_follow_parked_and_native_route(space):
    client, repository = space
    make_native(client)
    assert set_dns(client, {'inline': 'blackhole', 'proxied': 'follow', 'off': 'refused'}).status_code == 200
    assert oxidns(repository)['rule_dns_parked'] == {'off': {'action': 'refused'}}
    for rule_id in ('inline', 'proxied', 'off'):
        assert client.delete('/api/rules/' + rule_id, headers=HEADERS).status_code == 200
    stored = oxidns(repository)
    assert (stored['rule_actions'], stored['rule_dns_follow'], stored['rule_dns_parked']) == ({}, [], {})
    assert 'cf_dns_blackhole' not in stored['native_config']
    assert native_values(repository)['capabilities']['direct_rules']['editable'] is True


def test_reorder_syncs_native_routing(space):
    client, repository = space
    make_native(client)
    assert set_dns(client, {'inline': 'refused', 'proxied': 'blackhole'}).status_code == 200
    response = client.post('/api/rules/reorder', headers=HEADERS, json={'ids': ['proxied'], 'position': 'top'})
    assert response.status_code == 200 and 'warnings' not in response.get_json()
    result = native_values(repository)
    assert result['capabilities']['direct_rules']['editable'] is True
    assert list(result['values']['rule_actions']) == ['proxied', 'inline']


def test_toggle_parks_and_restores_native_dns(space):
    client, repository = space
    make_native(client)
    assert set_dns(client, {'inline': 'nodata', 'set': 'proxy'}).status_code == 200
    for rule_id in ('inline', 'set'):
        assert put_rule(client, rule_id, enabled=False).status_code == 200
    stored = oxidns(repository)
    assert stored['rule_dns_parked'] == {'inline': {'action': 'nodata'}, 'set': {'action': 'proxy'}}
    assert native_values(repository)['values']['rule_actions'] == {}
    assert client.get('/api/rules/dns', headers=HEADERS).get_json()['rules']['inline'] == {
        'state': 'disabled', 'route': 'nodata'}
    for rule_id in ('inline', 'set'):
        assert put_rule(client, rule_id, enabled=True).status_code == 200
    assert oxidns(repository)['rule_dns_parked'] == {}
    values = native_values(repository)['values']
    assert values['rule_actions'] == {'inline': {'action': 'nodata'}} and values['proxy_rulesets'] == ['set']


def test_native_write_defaults_base_url_to_request_root(space):
    client, repository = space
    repository.update_system_transaction(lambda system: system['system_config'].update(server_domain=''))
    # Scheduled updates need a routed ruleset before the draft becomes native.
    assert set_dns(client, {'set': 'direct'}).status_code == 200
    settings = client.get('/api/oxidns/settings', headers=HEADERS).get_json()
    settings['rule_update'] = {**settings['rule_update'], 'enabled': True}
    assert client.post('/api/oxidns/settings', headers=HEADERS, json=settings).status_code == 200
    make_native(client)
    response = client.post('/api/rules/compose', headers=HEADERS, json={
        'source': {'kind': 'library', 'library_rule_ids': ['ads']}, 'rule': {'policy': 'DIRECT'}, 'dns': 'refused'})
    assert response.status_code == 200, response.get_json()
    document = yaml.safe_load(oxidns(repository)['native_config'])
    urls = [item['url'] for plugin in document['plugins'] if isinstance(plugin.get('args'), dict)
            for item in plugin['args'].get('downloads') or [] if 'content%2Fads' in item.get('url', '')]
    assert urls and all(url.startswith('http://localhost/') for url in urls)


def test_library_toggle_resyncs_native_profiles_in_one_transaction(space):
    client, repository = space
    make_native(client)
    assert set_dns(client, {'set': 'blackhole'}).status_code == 200
    revisions = {pid: repository.get_profile(pid)['_revision'] for pid in ('default', 'other')}
    library = next(item for item in client.get('/api/rule-library', headers=HEADERS).get_json() if item['id'] == 'cn')
    assert library['has_ip_part'] is False
    response = client.put('/api/rule-library/cn', headers=HEADERS, json={**library, 'enabled': False})
    assert response.status_code == 200 and response.get_json()['warnings'] == []
    assert 'has_ip_part' not in next(item for item in repository.get_shared()['rule_library'] if item['id'] == 'cn')
    assert oxidns(repository)['rule_dns_parked'] == {'set': {'action': 'blackhole'}}
    assert 'cf_dns_blackhole' not in oxidns(repository)['native_config']
    # Only the native profile referencing the entry was rewritten.
    assert repository.get_profile('other')['_revision'] == revisions['other'] + 1
    assert repository.get_profile('default')['_revision'] == revisions['default']
    response = client.put('/api/rule-library/cn', headers=HEADERS, json={**library, 'enabled': True})
    assert response.status_code == 200
    assert oxidns(repository)['rule_dns_parked'] == {}
    assert native_values(repository)['values']['rule_actions'] == {'set': {'action': 'blackhole'}}


# ---- Inline policy changes (PUT /rules/policy) ------------------------------

def put_policy(client, **body):
    return client.put('/api/rules/policy', headers=HEADERS, json=body)


def test_group_policy_change_is_one_write_moving_follow_and_keeping_explicit_and_labels(space):
    client, repository = space
    assert client.post('/api/rules/group-name', headers=HEADERS, json={'ids': ['set', 'inline'], 'group_name': 'Home'}).status_code == 200
    assert set_dns(client, {'set': 'follow', 'inline': 'direct'}).status_code == 200
    default = repository.get_profile('default')
    revision = repository.config_revision('other')
    response = put_policy(client, ids=['set', 'inline'], policy='Proxy')
    assert response.status_code == 200, response.get_json()
    assert repository.config_revision('other') == revision + 1
    rules = {item['id']: item for item in repository.get_profile('other')['rule_configs']}
    assert [rules[key]['policy'] for key in ('set', 'inline')] == ['Proxy', 'Proxy']
    assert [rules[key]['group_name'] for key in ('set', 'inline')] == ['Home', 'Home']
    state = client.get('/api/rules/dns', headers=HEADERS).get_json()['rules']
    assert state['set'] == {'state': 'follow', 'route': 'proxy', 'follow_route': 'proxy'}
    assert state['inline']['state'] == 'explicit' and state['inline']['route'] == 'direct'
    assert repository.get_profile('default') == default


def test_row_policy_change_clears_its_group_label(space):
    client, repository = space
    assert client.post('/api/rules/group-name', headers=HEADERS, json={'ids': ['set', 'inline'], 'group_name': 'Home'}).status_code == 200
    assert put_policy(client, ids=['inline'], policy='REJECT', group_name='').status_code == 200
    rules = {item['id']: item for item in repository.get_profile('other')['rule_configs']}
    assert (rules['inline']['policy'], rules['inline']['group_name']) == ('REJECT', '')
    assert (rules['set']['policy'], rules['set']['group_name']) == ('DIRECT', 'Home')


def test_policy_change_rebuilds_native_routing(space):
    client, repository = space
    make_native(client)
    assert set_dns(client, {'inline': 'follow'}).status_code == 200
    assert put_policy(client, ids=['inline'], policy='Proxy').status_code == 200
    values = native_values(repository)['values']
    assert 'inline' in values['proxy_rules'] and 'inline' not in values['direct_rules']


def test_policy_change_on_custom_native_saves_rules_with_warning(space):
    client, repository = space
    lock_native(client, repository)
    native = oxidns(repository)['native_config']
    response = put_policy(client, ids=['inline', 'off'], policy='Proxy')
    assert response.status_code == 200
    assert response.get_json()['warnings'][0].startswith('OxiDNS 分流含自定义结构，DNS 未修改')
    assert oxidns(repository)['native_config'] == native
    assert [item['policy'] for item in repository.get_profile('other')['rule_configs'] if item['id'] in ('inline', 'off')] == ['Proxy', 'Proxy']


def test_policy_change_has_no_batch_cap_for_large_groups(space):
    client, repository = space
    response = client.post('/api/rules/batch', headers=HEADERS, json={
        'rule_type': 'DOMAIN-SUFFIX', 'domains': [f'd{index}.test' for index in range(250)], 'policy': 'DIRECT'})
    assert response.status_code == 200
    ids = [item['id'] for item in response.get_json()['rules']]
    assert put_policy(client, ids=ids, policy='Proxy').status_code == 200
    rules = {item['id']: item for item in repository.get_profile('other')['rule_configs']}
    assert {rules[key]['policy'] for key in ids} == {'Proxy'}


@pytest.mark.parametrize('body, status', [
    ({'ids': ['inline'], 'policy': 'Missing'}, 400),
    ({'ids': ['inline', 'deleted'], 'policy': 'Proxy'}, 404),
    ({'ids': [], 'policy': 'Proxy'}, 400),
    ({'ids': ['inline', 'inline'], 'policy': 'Proxy'}, 400),
    ({'ids': 'inline', 'policy': 'Proxy'}, 400),
    ({'ids': ['inline'], 'policy': ''}, 400),
    ({'ids': ['inline'], 'policy': 'Proxy', 'group_name': 1}, 400),
])
def test_policy_change_rejects_bad_requests_without_changes(space, body, status):
    client, repository = space
    before = repository.get_profile('other')
    assert put_policy(client, **body).status_code == status
    assert repository.get_profile('other') == before
