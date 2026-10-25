"""One-form rule creation and per-rule OxiDNS DNS over the rules API."""
import re

import pytest
import yaml
from flask import Flask

from backend.common import config as config_module
from backend.common.config_repository import ProfileRepository
from backend.common.oxidns_common_routing import read_routing
from backend.routes import register_blueprints

HEADERS = {'X-ConfigFlow-Profile': 'other'}
LIBRARY = [
    {'id': 'ads', 'name': 'Ads', 'source_type': 'content', 'content': 'ads.test', 'behavior': 'domain', 'enabled': True},
    {'id': 'mixed', 'name': 'Mixed', 'source_type': 'content', 'content': 'DOMAIN,m.test\nIP-CIDR,198.51.100.0/24',
     'behavior': 'classical', 'enabled': True},
    {'id': 'cn', 'name': 'CN', 'source_type': 'content', 'content': 'cn.test', 'behavior': 'domain', 'enabled': True},
]
RULES = [
    {'id': 'set', 'itemType': 'ruleset', 'library_rule_id': 'cn', 'policy': 'DIRECT', 'enabled': True},
    {'id': 'inline', 'itemType': 'rule', 'rule_type': 'DOMAIN', 'value': 'local.test', 'policy': 'DIRECT', 'enabled': True},
    {'id': 'proxied', 'itemType': 'rule', 'rule_type': 'DOMAIN-SUFFIX', 'value': 'proxied.test', 'policy': 'Proxy', 'enabled': True},
    {'id': 'off', 'itemType': 'rule', 'rule_type': 'DOMAIN-SUFFIX', 'value': 'off.test', 'policy': 'DIRECT', 'enabled': False},
    {'id': 'geo', 'itemType': 'rule', 'rule_type': 'GEOIP', 'value': 'CN', 'policy': 'DIRECT', 'enabled': True},
]


@pytest.fixture
def space(tmp_path):
    """(client, repository): profile 'other' in template mode, cloned from default."""
    repository = ProfileRepository(tmp_path)
    repository.save_shared({'rule_library': [dict(item) for item in LIBRARY]})
    repository.save_profile('default', {
        'proxy_groups': [{'id': 'group', 'name': 'Proxy', 'type': 'select', 'manual_nodes': ['DIRECT']}],
        'rule_configs': [dict(item) for item in RULES],
    })
    repository.create_profile({'id': 'other', 'name': 'Other'}, clone_from='default')
    previous = config_module._repository
    config_module.set_repository(repository)
    app = Flask(__name__)
    register_blueprints(app)
    yield app.test_client(), repository
    config_module.set_repository(previous)
    config_module.reset_config_context()


def make_native(client):
    settings = client.get('/api/oxidns/settings', headers=HEADERS).get_json()
    native = client.post('/api/oxidns/editor', headers=HEADERS, json={'settings': settings}).get_json()['settings']
    response = client.post('/api/oxidns/settings', headers=HEADERS, json=native)
    assert response.status_code == 200, response.get_json()


def set_dns(client, routes, **body):
    return client.put('/api/rules/dns', headers=HEADERS, json={'routes': routes, **body})


def oxidns(repository):
    return repository.get_profile('other')['oxidns']


def native_values(repository):
    config = repository.get_compat_config('other')
    document = yaml.safe_load(config['oxidns']['native_config'])
    return read_routing(document, config['oxidns'], config)


def lock_native(client, repository):
    """A native draft whose blackhole helper was edited by hand."""
    make_native(client)
    assert set_dns(client, {'proxied': 'blackhole'}).status_code == 200
    settings = client.get('/api/oxidns/settings', headers=HEADERS).get_json()
    assert "mode: 'null'" in settings['native_config']
    settings['native_config'] = settings['native_config'].replace("mode: 'null'", 'mode: nxdomain')
    assert client.post('/api/oxidns/settings', headers=HEADERS, json=settings).status_code == 200
    assert native_values(repository)['capabilities']['direct_rules']['editable'] is False


def compose(client, source, **body):
    rule = body.pop('rule', {})
    return client.post('/api/rules/compose', headers=HEADERS,
                       json={'source': source, 'rule': {'policy': 'DIRECT', **rule}, **body})


def values(text, rule_type='DOMAIN-SUFFIX'):
    return {'kind': 'values', 'rule_type': rule_type, 'text': text}


def test_compose_values_creates_rules_and_dns_atomically(space):
    client, repository = space
    before = repository.get_profile('other')['_revision']
    response = compose(client, values('a.test\nb.test'), dns='follow')
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    ids = [item['id'] for item in body['data']['rules']]
    profile = repository.get_profile('other')
    assert [item['id'] for item in profile['rule_configs'][:2]] == ids
    assert [item['value'] for item in profile['rule_configs'][:2]] == ['a.test', 'b.test']
    assert profile['_revision'] == before + 1
    assert profile['oxidns']['rule_dns_follow'] == ids
    assert all(identity in profile['oxidns']['direct_rules'] for identity in ids)
    assert body['dns'] == {identity: {'state': 'follow', 'route': 'direct', 'follow_route': 'direct'} for identity in ids}
    assert body['draft'] is True and body['skipped'] == [] and body['data']['library'] is None
    # No auto-syncing OxiDNS device: nothing is withheld from automatic publication.
    assert body['pinned_withheld'] is False
    # The default profile is untouched.
    assert len(repository.get_profile('default')['rule_configs']) == len(RULES)


def test_compose_multiline_prefix_types_comments_wildcards_and_limit(space):
    client, repository = space
    text = '\n'.join(['# comment', '', '+.plus.test', '*.star.test', 'full:full.test', 'DOMAIN,api.test,Proxy',
                      'IP-CIDR,192.0.2.0/24,no-resolve', 'bare.test', 'bare.test'])
    response = compose(client, values(text, 'DOMAIN-KEYWORD'), rule={'position': 'bottom'})
    assert response.status_code == 200, response.get_json()
    created = response.get_json()['data']['rules']
    assert [(item['rule_type'], item['value']) for item in created] == [
        ('DOMAIN-SUFFIX', 'plus.test'), ('DOMAIN-SUFFIX', 'star.test'), ('DOMAIN', 'full.test'),
        ('DOMAIN', 'api.test'), ('IP-CIDR', '192.0.2.0/24'), ('DOMAIN-KEYWORD', 'bare.test')]
    assert created[4]['no_resolve'] is True and created[0]['no_resolve'] is False
    assert [item['id'] for item in repository.get_profile('other')['rule_configs'][-6:]] == [item['id'] for item in created]
    warnings = response.get_json()['warnings']
    assert '忽略第 6 行行尾策略 Proxy' in warnings
    assert '*.star.test 将按 DOMAIN-SUFFIX 添加，也包含 star.test 本身' in warnings
    before = repository.export_all()
    response = compose(client, values('ok.test\nbad_domain!\nIP-CIDR,999.0.0.0/8'))
    assert response.status_code == 400
    assert response.get_json()['message'].startswith('第 2 行：不是有效域名')
    assert [error['line'] for error in response.get_json()['field_errors']] == [2, 3]
    response = compose(client, values('\n'.join(f'x{index}.test' for index in range(201))))
    assert response.status_code == 400 and '一次最多添加 200 条' in response.get_json()['message']
    assert repository.export_all() == before


def test_compose_mixed_paste_applies_dns_ip(space):
    client, repository = space
    response = compose(client, values('ads.test\nIP-CIDR,203.0.113.0/24'), dns='blackhole', dns_ip='default')
    assert response.status_code == 200, response.get_json()
    domain, ip = response.get_json()['data']['rules']
    assert oxidns(repository)['rule_actions'] == {domain['id']: {'action': 'blackhole'}}
    assert response.get_json()['dns'][ip['id']] == {'state': 'default', 'route': None}
    # Without dns_ip every line takes dns; the first IP group is announced.
    response = compose(client, values('IP-CIDR,198.51.100.0/24\nnx.test'), dns='nxdomain')
    first, second = response.get_json()['data']['rules']
    assert {first['id'], second['id']} <= set(oxidns(repository)['rule_actions'])
    assert response.get_json()['warnings'] == ['#1 开始参与 DNS 分流：所有未命中域名规则的查询将先向直连 DNS 探测一次']


def test_compose_skips_existing_duplicates_and_reports_disabled(space):
    client, repository = space
    response = compose(client, values('off.test\nnew.test\nDOMAIN,local.test'))
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert [item['value'] for item in body['data']['rules']] == ['new.test']
    assert body['skipped'] == [
        {'line': 1, 'value': 'off.test', 'existing_id': 'off', 'index': 4, 'enabled': False},
        {'line': 3, 'value': 'local.test', 'existing_id': 'inline', 'index': 2, 'enabled': True}]


def test_compose_existing_library_multiple_ids(space):
    client, repository = space
    response = compose(client, {'kind': 'library', 'library_rule_ids': ['ads', 'mixed']}, dns='proxy',
                       rule={'policy': 'Proxy', 'remark': 'note'})
    assert response.status_code == 200, response.get_json()
    created = response.get_json()['data']['rules']
    assert [item['library_rule_id'] for item in created] == ['ads', 'mixed']
    stored = repository.get_profile('other')['rule_configs'][:2]
    assert stored == created and all('url' not in item and 'behavior' not in item for item in stored)
    assert stored[0]['remark'] == 'note'
    assert oxidns(repository)['proxy_rulesets'] == [item['id'] for item in created]
    # Proxy like the proxy fallback: the IP part cannot change an answer, so no probe starts.
    assert response.get_json()['warnings'] == []


def test_compose_library_already_referenced_409_code_in_usages(space):
    client, repository = space
    before = repository.export_all()
    response = compose(client, {'kind': 'library', 'library_rule_ids': ['ads', 'cn']})
    assert response.status_code == 409
    body = response.get_json()
    assert body['message'] == '该规则集已在策略规则 #1'
    assert body['usages'][0]['code'] == 'ruleset_already_used' and body['usages'][0]['id'] == 'set'
    response = compose(client, {'kind': 'library', 'library_rule_ids': ['missing']})
    assert response.status_code == 404 and response.get_json()['message'] == '规则集必须引用存在的共享规则库条目'
    assert repository.export_all() == before


def test_compose_new_library_url_and_content_one_commit(space, monkeypatch):
    client, repository = space
    cached = []
    monkeypatch.setattr('backend.utils.rule_utils.save_rule_to_local', cached.append)
    monkeypatch.setattr('backend.utils.rule_utils.read_rule_source_content', lambda _: 'example.test')
    monkeypatch.setattr('backend.utils.rule_utils.write_validated_rule_cache', lambda rule, content: cached.append(rule))
    shared, profile = repository.get_shared()['_revision'], repository.get_profile('other')['_revision']
    response = compose(client, {'kind': 'new_library', 'library': {
        'id': 'chosen', 'name': '广告', 'source_type': 'url', 'url': 'https://example.test/ads.list',
        'content': 'dropped', 'behavior': 'domain', 'format': 'text', 'enabled': True}}, dns='nodata')
    assert response.status_code == 200, response.get_json()
    library = response.get_json()['data']['library']
    assert re.fullmatch(r'lib_[0-9a-f]{12}', library['id']) and 'content' not in library
    assert repository.get_shared()['rule_library'][-1] == library and cached == [library]
    reference = response.get_json()['data']['rules'][0]
    assert reference['library_rule_id'] == library['id']
    assert oxidns(repository)['rule_actions'] == {reference['id']: {'action': 'nodata'}}
    assert repository.get_shared()['_revision'] == shared + 1
    assert repository.get_profile('other')['_revision'] == profile + 1
    content = compose(client, {'kind': 'new_library', 'library': {
        'name': '本地', 'source_type': 'content', 'content': 'local.test', 'url': 'dropped', 'behavior': 'domain'}})
    assert content.status_code == 200
    assert 'url' not in content.get_json()['data']['library']
    # API and MCP callers may leave out enabled; the library page needs it spelled out.
    assert content.get_json()['data']['library']['enabled'] is True
    assert repository.get_shared()['rule_library'][-1]['enabled'] is True
    disabled = compose(client, {'kind': 'new_library', 'library': {
        'name': '停用', 'source_type': 'content', 'content': 'off.test', 'behavior': 'domain', 'enabled': False}})
    assert disabled.status_code == 200 and disabled.get_json()['data']['library']['enabled'] is False
    duplicate = compose(client, {'kind': 'new_library', 'library': {
        'name': '广告', 'source_type': 'content', 'content': 'x.test', 'behavior': 'domain'}})
    assert duplicate.status_code == 400 and duplicate.get_json()['message'] == '规则库已有同名规则集「广告」'


def test_compose_library_cache_failure_is_warning(space, monkeypatch):
    client, repository = space

    def fail(_):
        raise OSError('disk full')

    monkeypatch.setattr('backend.utils.rule_utils.save_rule_to_local', fail)
    response = compose(client, {'kind': 'new_library', 'library': {
        'name': 'Cached', 'source_type': 'content', 'content': 'c.test', 'behavior': 'domain'}})
    assert response.status_code == 200
    assert response.get_json()['warnings'] == ['规则集已保存，但本地缓存失败，发布时会重新获取']
    assert any(item['name'] == 'Cached' for item in repository.get_shared()['rule_library'])


def test_compose_rolls_back_shared_and_profile_on_dns_error(space, monkeypatch):
    client, repository = space
    monkeypatch.setattr('backend.utils.rule_utils.save_rule_to_local', lambda _: None)
    before = repository.export_all()
    response = compose(client, {'kind': 'new_library', 'library': {
        'name': 'Pinned', 'source_type': 'content', 'content': 'p.test', 'behavior': 'domain'}},
        dns='upstream:000000000000')
    assert response.status_code == 400
    assert response.get_json()['message'] == '所选上游已不在 OxiDNS 上游设置中，请刷新后重选'
    assert repository.export_all() == before
    response = compose(client, values('x.test'), dns='drop')
    assert response.status_code == 400 and response.get_json()['message'] == '不支持的 DNS 处理：drop'


def test_compose_custom_native_409_nothing_written(space, monkeypatch):
    client, repository = space
    monkeypatch.setattr('backend.utils.rule_utils.save_rule_to_local', lambda _: None)
    lock_native(client, repository)
    before = repository.export_all()
    response = compose(client, {'kind': 'new_library', 'library': {
        'name': 'Locked', 'source_type': 'content', 'content': 'l.test', 'behavior': 'domain'}}, dns='refused')
    assert response.status_code == 409
    usage = response.get_json()['usages'][0]
    assert usage['code'] == 'oxidns_routing_custom' and '黑洞插件 cf_dns_blackhole 已被手动修改' in usage['reason']
    assert repository.export_all() == before


def test_compose_default_dns_works_on_custom_native(space):
    client, repository = space
    lock_native(client, repository)
    native = oxidns(repository)['native_config']
    response = compose(client, values('plain.test'))
    assert response.status_code == 200, response.get_json()
    assert response.get_json()['warnings'] == []
    assert oxidns(repository)['native_config'] == native


def test_compose_native_writes_routing_in_the_same_commit(space):
    client, repository = space
    make_native(client)
    response = compose(client, values('blocked.test'), dns='blackhole', rule={'policy': 'REJECT'})
    assert response.status_code == 200, response.get_json()
    identity = response.get_json()['data']['rules'][0]['id']
    result = native_values(repository)
    assert result['values']['rule_actions'] == {identity: {'action': 'blackhole'}}
    assert 'cf_dns_blackhole' in oxidns(repository)['native_config']
    assert response.get_json()['dns'][identity] == {'state': 'explicit', 'route': 'blackhole'}


def test_compose_rejects_dns_on_unconvertible_line(space):
    client, repository = space
    before = repository.export_all()
    response = compose(client, values('ok.test\nGEOIP,CN'), dns='blackhole')
    assert response.status_code == 400
    assert response.get_json()['message'] == '第 2 行 GEOIP 规则不参与 DNS 分流，请把 DNS 设为默认上游'
    assert compose(client, values('GEOIP,US')).status_code == 200
    assert repository.get_profile('other')['rule_configs'][0]['value'] == 'US'
    assert len(repository.export_all()['profiles']['other']['rule_configs']) == len(before['profiles']['other']['rule_configs']) + 1


def test_compose_rejects_unknown_policy(space):
    client, repository = space
    before = repository.export_all()
    response = compose(client, values('x.test'), rule={'policy': 'Foo'})
    assert response.status_code == 400 and response.get_json()['message'] == '策略不存在：Foo'
    assert repository.export_all() == before
    assert compose(client, values('x.test'), rule={'policy': 'Proxy'}).status_code == 200


def test_compose_server_generates_ids(space):
    client, repository = space
    response = compose(client, values('one.test\ntwo.test'), rule={'id': 'client-id'})
    rules = response.get_json()['data']['rules']
    assert all(re.fullmatch(r'rule_[0-9a-f]{12}', item['id']) for item in rules)
    assert len({item['id'] for item in rules}) == 2
    library = compose(client, {'kind': 'library', 'library_rule_ids': ['ads']}).get_json()['data']['rules'][0]
    assert re.fullmatch(r'ruleset_[0-9a-f]{12}', library['id'])


def test_put_rules_dns_quick_switch_and_group(space):
    client, repository = space
    response = set_dns(client, {'inline': 'refused', 'proxied': 'follow', 'set': 'direct'})
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body['rules'] == {'inline': {'state': 'explicit', 'route': 'refused'},
                             'proxied': {'state': 'follow', 'route': 'proxy', 'follow_route': 'proxy'},
                             'set': {'state': 'explicit', 'route': 'direct'}}
    assert body['pinned_withheld'] is False
    stored = oxidns(repository)
    assert stored['rule_actions'] == {'inline': {'action': 'refused'}}
    assert stored['proxy_rules'] == ['proxied'] and stored['direct_rulesets'] == ['set']
    assert stored['rule_dns_follow'] == ['proxied']
    # Switching back to default removes every holder.
    response = set_dns(client, {'inline': 'default', 'proxied': 'default'})
    assert response.get_json()['rules']['inline'] == {'state': 'default', 'route': None}
    stored = oxidns(repository)
    assert stored['rule_actions'] == {} and stored['proxy_rules'] == [] and stored['rule_dns_follow'] == []
    # Native drafts get the same choices written into the YAML.
    make_native(client)
    response = set_dns(client, {'inline': 'nxdomain', 'proxied': 'blackhole'})
    assert response.status_code == 200, response.get_json()
    assert native_values(repository)['values']['rule_actions'] == {
        'inline': {'action': 'nxdomain'}, 'proxied': {'action': 'blackhole'}}
    assert set_dns(client, {'x': 'proxy'}).status_code == 400
    assert set_dns(client, {f'r{index}': 'proxy' for index in range(201)}).status_code == 400


def test_put_rules_dns_skip_unsupported(space):
    client, repository = space
    response = set_dns(client, {'geo': 'blackhole', 'inline': 'blackhole'})
    assert response.status_code == 400
    assert response.get_json()['message'] == 'GEOIP 规则不参与 DNS 分流，请把 DNS 设为默认上游'
    response = set_dns(client, {'geo': 'blackhole', 'inline': 'blackhole'}, skip_unsupported=True)
    assert response.status_code == 200
    assert response.get_json()['skipped'] == ['geo']
    assert oxidns(repository)['rule_actions'] == {'inline': {'action': 'blackhole'}}


def test_put_rules_dns_custom_native_is_409(space):
    client, repository = space
    lock_native(client, repository)
    before = repository.export_all()
    response = set_dns(client, {'inline': 'refused'})
    assert response.status_code == 409
    assert response.get_json()['usages'][0]['code'] == 'oxidns_routing_custom'
    assert repository.export_all() == before


def test_upstream_key_unknown_or_linked_rejected(space):
    client, repository = space
    state = client.get('/api/rules/dns', headers=HEADERS).get_json()
    local = next(item for item in state['upstreams'] if item['source'] == 'local')
    assert local['label'] == '直连 · ' + local['addr'].removeprefix('udp://') and local['linked'] is False
    assert 'entry' not in local
    response = set_dns(client, {'inline': 'upstream:' + local['key']})
    assert response.status_code == 200, response.get_json()
    action = oxidns(repository)['rule_actions']['inline']
    assert action['action'] == 'upstream' and action['source'] == 'local' and action['upstream']['addr'] == local['addr']
    assert response.get_json()['rules']['inline']['upstream_label'] == local['label']
    assert set_dns(client, {'proxied': 'upstream:0123456789ab'}).get_json()['message'] == \
        '所选上游已不在 OxiDNS 上游设置中，请刷新后重选'
    # A DNS-follow binding of an OxiDNS agent on this profile makes the upstream linked.
    repository.update_system_transaction(lambda system: system['agents'].append({
        'id': 'dns', 'name': 'DNS', 'service_type': 'oxidns', 'enabled': True, 'profile_id': 'other',
        'dns_follow': {'upstream_tag': 'forward_local', 'upstream_index': 0}}))
    state = client.get('/api/rules/dns', headers=HEADERS).get_json()
    assert next(item for item in state['upstreams'] if item['key'] == local['key'])['linked'] is True
    response = set_dns(client, {'proxied': 'upstream:' + local['key']})
    assert response.status_code == 400 and response.get_json()['message'] == '已绑定 DNS 联动，请改选直连 DNS'
    # The rule already pinned to it keeps its copy.
    assert set_dns(client, {'inline': 'upstream:' + local['key']}).status_code == 200


def test_get_rules_dns_engine_agents_auto_sync_and_in_use(space):
    client, repository = space
    body = client.get('/api/rules/dns', headers=HEADERS).get_json()
    assert body['success'] is True
    engine = body['engine']
    assert engine['mode'] == 'template' and engine['editable'] is True and engine['oxidns_in_use'] is False
    assert engine['local_probe_active'] is False and engine['agents'] == [] and engine['warnings'] == []
    assert body['rules'] == {'off': {'state': 'disabled', 'route': None},
                             'geo': {'state': 'unsupported', 'reason': 'GEOIP 规则不参与 DNS 分流'}}
    repository.update_system_transaction(lambda system: system['agents'].extend([
        {'id': 'a1', 'name': 'OxiDNS 主路由', 'service_type': 'oxidns', 'enabled': True, 'profile_id': 'other',
         'rule_sync': {'settings': {'library': True}}},
        {'id': 'a2', 'name': 'Elsewhere', 'service_type': 'mihomo', 'enabled': True, 'profile_id': 'default'},
        {'id': 'a3', 'name': 'Off', 'service_type': 'oxidns', 'enabled': False, 'profile_id': 'other'}]))
    engine = client.get('/api/rules/dns', headers=HEADERS).get_json()['engine']
    assert engine['oxidns_in_use'] is True
    assert engine['agents'] == [{'id': 'a1', 'name': 'OxiDNS 主路由', 'service_type': 'oxidns',
                                 'auto_sync': {'library': True, 'rules': False}}]
    # A routed IP-capable ruleset turns the local probe on; MosDNS choices are reported for comparison.
    assert client.post('/api/mosdns/rulesets', headers=HEADERS, json={'direct_rules': ['inline']}).status_code == 200
    compose(client, {'kind': 'library', 'library_rule_ids': ['mixed']}, dns='direct')
    body = client.get('/api/rules/dns', headers=HEADERS).get_json()
    assert body['engine']['local_probe_active'] is True
    assert body['mosdns'] == {'inline': 'direct'}
    make_native(client)
    body = client.get('/api/rules/dns', headers=HEADERS).get_json()
    assert body['engine']['mode'] == 'native' and body['engine']['editable'] is True
    assert body['engine']['default_forward'] == 'forward_remote' and body['engine']['prefer_ipv4'] is True


def test_import_with_malformed_rule_action_is_refused(space):
    client, repository = space
    settings = dict(oxidns(repository), rule_actions={'inline': {'kind': 'blackhole'}})
    response = client.post('/api/profiles/other/import', headers=HEADERS, json={'oxidns': settings})
    assert response.status_code == 400, response.get_json()
    assert oxidns(repository).get('rule_actions', {}) == {}
    assert client.get('/api/rules/dns', headers=HEADERS).status_code == 200
    assert client.put('/api/rules/inline', headers=HEADERS, json={'enabled': False}).status_code == 200


def test_saves_report_pinned_withheld_for_their_rules(space, monkeypatch):
    from backend.agents import rule_sync
    client, _ = space
    calls = []
    def withheld(repository, profile_id, rule_ids):
        calls.append((profile_id, list(rule_ids)))
        return True
    monkeypatch.setattr(rule_sync, 'pinned_withheld', withheld)
    body = compose(client, values('pin.test')).get_json()
    created = body['data']['rules'][0]['id']
    assert body['pinned_withheld'] is True and calls.pop() == ('other', [created])
    body = set_dns(client, {'inline': 'direct', 'geo': 'direct'}, skip_unsupported=True).get_json()
    assert body['pinned_withheld'] is True and calls.pop() == ('other', ['inline'])
    response = client.put('/api/rules/inline', headers=HEADERS, json={'remark': 'x'})
    assert response.get_json()['pinned_withheld'] is True and calls.pop() == ('other', ['inline'])
    response = client.put('/api/rule-sets/set', headers=HEADERS, json={'remark': 'x'})
    assert response.get_json()['pinned_withheld'] is True and calls.pop() == ('other', ['set'])
    assert 'pinned_withheld' not in client.delete('/api/rules/inline', headers=HEADERS).get_json()
    assert calls == []
    # A failed check never fails the committed save; the field is just left out.
    monkeypatch.setattr(rule_sync, 'pinned_withheld', lambda *_a: 1 / 0)
    response = client.put('/api/rules/proxied', headers=HEADERS, json={'remark': 'y'})
    assert response.status_code == 200 and 'pinned_withheld' not in response.get_json()
