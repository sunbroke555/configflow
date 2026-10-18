"""Domain format repairs preserve matching scope and every unaffected byte."""
from copy import deepcopy

import pytest
import yaml

from backend.agents.deployment_bundle import convert_mosdns_rules
from backend.converters.mosdns import convert_rule_content_for_mosdns
from backend.test_oxidns_integration import oxidns_app  # noqa: F401
from backend.test_client_tools import agent, rule_setup
from backend.utils import domain_discovery_service as discovery
from backend.utils.ruleset_content import inspect_domain_content, read_rule_payload, remove_rule_payload, append_rule_payload
from backend.utils.rule_matcher import parse_rule_line, match_domain


@pytest.mark.parametrize('source', [
    '# domains\r\nDOMAIN-SUFFIX,x.ai\r\nDOMAIN,api.telegram.org\r\n+.grok.com\r\nexact.test\r\n',
    "# domains\npayload:\n  - 'DOMAIN-SUFFIX,x.ai' # suffix\n  - DOMAIN,api.telegram.org\n  - '+.grok.com'\n  - exact.test\n",
    'payload: ["DOMAIN-SUFFIX,x.ai", "DOMAIN,api.telegram.org", "+.grok.com", "exact.test",]\n',
])
def test_repairs_preserve_text_and_matching_scope_across_delivery(source):
    checked = inspect_domain_content(source, 'domain')
    assert not checked['errors']
    assert [change['after'] for change in checked['changes']] == ['+.x.ai', 'api.telegram.org']
    expected = source.replace("'DOMAIN-SUFFIX,x.ai'", '"+.x.ai"').replace('"DOMAIN-SUFFIX,x.ai"', '"+.x.ai"')
    expected = expected.replace('DOMAIN-SUFFIX,x.ai', '+.x.ai')
    expected = expected.replace('"DOMAIN,api.telegram.org"', '"api.telegram.org"')
    expected = expected.replace('DOMAIN,api.telegram.org', '"api.telegram.org"' if 'payload:' in source else 'api.telegram.org')
    assert checked['content'] == expected
    assert inspect_domain_content(expected, 'domain') == {'content': expected, 'changes': [], 'errors': []}
    converted = 'domain:x.ai\nfull:api.telegram.org\ndomain:grok.com\nfull:exact.test'
    assert convert_rule_content_for_mosdns(expected, 'domain') == converted
    assert convert_mosdns_rules(expected.encode()).decode() == converted
    for domain, matched in [('x.ai', True), ('api.x.ai', True), ('deep.api.x.ai', True),
                            ('api.telegram.org', True), ('sub.api.telegram.org', False), ('other.test', False)]:
        rules = ['+.x.ai', 'api.telegram.org', '+.grok.com', 'exact.test']
        assert any(match_domain(domain, *parse_rule_line(rule)) for rule in rules) == matched


@pytest.mark.parametrize('source', ['DOMAIN-KEYWORD,google', 'IP-CIDR,192.0.2.0/24', 'DOMAIN-SUFFIX,x.ai,DIRECT',
                                  '+.x.ai\ninvalid/host', '+.x.ai\nfoo..test', 'payload: [true]',
                                  'payload: ["DOMAIN,x.ai",', 'a: &rules ["DOMAIN,x.ai"]\npayload: *rules',
                                  'payload: [&rule "DOMAIN,x.ai", *rule]', 'payload: []\npayload: []'])
def test_unsupported_or_ambiguous_input_is_not_silently_dropped(source):
    assert inspect_domain_content(source, 'domain')['errors']


def test_plain_and_wildcard_mixture_needs_no_repair():
    source = '# unchanged\n+.x.ai\napi.telegram.org\n*.example.test\n.example.org\n\n'
    assert inspect_domain_content(source, 'domain') == {'content': source, 'changes': [], 'errors': []}
    assert not inspect_domain_content('DOMAIN-KEYWORD,google', 'classical')['errors']


@pytest.mark.parametrize('content', [
    'payload: ["first.test", "last.test",]\n',
    'payload:\n  - first.test\n  - last.test\n',
    '{"payload": ["first.test", "last.test"], "other": "preserved"}',
])
def test_yaml_removal_can_empty_and_refill_the_same_payload(content):
    for value in ('first.test', 'last.test'):
        content, removed = remove_rule_payload(content, lambda line: line == value)
        assert removed == [value]
        assert value not in yaml.safe_load(content)['payload']
    _, payload = read_rule_payload(content)
    content = append_rule_payload(content, '+.new.test', payload)
    assert yaml.safe_load(content)['payload'] == ['+.new.test']
    if 'other' in content:
        assert yaml.safe_load(content)['other'] == 'preserved'


def test_check_content_requires_auth_when_authentication_is_enabled(oxidns_app, monkeypatch):
    app, repo = oxidns_app
    monkeypatch.setattr('backend.common.auth.is_auth_enabled', lambda: True)
    before = deepcopy(repo.export_all())
    assert app.test_client().post('/api/rule-library/check-content', json={'behavior': 'domain', 'content': 'x.ai'}).status_code == 401
    assert repo.export_all() == before


def test_preview_does_not_write_and_save_requires_explicit_repaired_content(oxidns_app):
    app, repo = oxidns_app
    client = app.test_client()
    before = deepcopy(repo.export_all())
    body = {'behavior': 'domain', 'content': 'DOMAIN-SUFFIX,x.ai\napi.telegram.org', 'append_lines': ['+.grok.com']}
    preview = client.post('/api/rule-library/check-content', json=body)
    assert preview.status_code == 200
    assert repo.export_all() == before
    rule = {'id': 'new', 'name': 'New', 'source_type': 'content', 'behavior': 'domain', 'content': body['content']}
    rejected = client.post('/api/rule-library', json=rule)
    assert rejected.status_code == 400
    assert repo.export_all() == before
    rule['content'] = preview.json['content']
    assert client.post('/api/rule-library', json=rule).status_code == 200
    assert client.get('/api/rules/local/New').data.decode() == '+.x.ai\napi.telegram.org\n+.grok.com\n'
    shared = deepcopy(repo.get_shared())
    assert client.put('/api/rule-library/new', json={**rule, 'content': 'DOMAIN-KEYWORD,google'}).status_code == 400
    assert repo.get_shared() == shared
    state = client.get('/api/rule-library/new/draft-content').json
    assert client.put('/api/rule-library/new/draft-content', json={'revision': state['revision'], 'content': 'DOMAIN,x.ai'}).status_code == 400
    assert repo.get_shared() == shared
    for name, profile in before['profiles'].items():
        assert repo.get_profile(name) == profile


@pytest.mark.parametrize('content', ['payload:\n  - exact.test # keep\n', 'payload: ["exact.test",]\n'])
def test_add_preview_appends_inside_yaml_and_retains_comments(oxidns_app, content):
    app, repo = oxidns_app
    before = deepcopy(repo.export_all())
    result = app.test_client().post('/api/rule-library/check-content', json={
        'behavior': 'domain', 'content': content, 'append_lines': ['+.x.ai', 'api.telegram.org']})
    assert result.status_code == 200 and not result.json['errors']
    assert yaml.safe_load(result.json['content'])['payload'] == ['exact.test', '+.x.ai', 'api.telegram.org']
    if '# keep' in content:
        assert '# keep' in result.json['content']
    assert repo.export_all() == before


def test_old_invalid_source_can_still_be_disabled_without_rewriting(oxidns_app):
    app, repo = oxidns_app
    rule = repo.get_shared()['rule_library'][0]
    assert app.test_client().put('/api/rule-library/lib', json={**rule, 'enabled': False}).status_code == 200
    assert repo.get_shared()['rule_library'][0]['content'] == rule['content']


@pytest.mark.parametrize('content', ['+.existing.test\n', 'payload: ["+.existing.test",]\n'])
def test_connection_add_saves_correct_domain_syntax_and_preserves_payload(oxidns_app, monkeypatch, content):
    app, repo = oxidns_app
    rule_setup(repo, monkeypatch)
    ruleset_id = discovery.read_settings(repo.get_compat_config('office'))['proxy_ruleset']
    def setup(shared):
        next(rule for rule in shared['rule_library'] if rule['id'] == ruleset_id).update(behavior='domain', content=content)
    repo.update_shared_transaction(setup)
    path = f'/api/agents/{agent()}/mihomo/rule-targets/apply'
    for value, kind in [('exact.test', 'DOMAIN'), ('suffix.test', 'DOMAIN-SUFFIX')]:
        result = app.test_client().post(path, json={'profile_id': 'office', 'ruleset_id': ruleset_id,
            'target': 'proxy', 'value': value, 'rule_type': kind})
        assert result.status_code == 200, result.json
    actual = next(rule for rule in repo.get_shared()['rule_library'] if rule['id'] == ruleset_id)['content']
    assert convert_rule_content_for_mosdns(actual, 'domain') == 'domain:existing.test\nfull:exact.test\ndomain:suffix.test'
    assert discovery.undo_domain('office', 'exact.test', 'proxy')['removed'] == ['exact.test']
    actual = next(rule for rule in repo.get_shared()['rule_library'] if rule['id'] == ruleset_id)['content']
    assert convert_rule_content_for_mosdns(actual, 'domain') == 'domain:existing.test\ndomain:suffix.test'


def test_connection_add_rejects_old_invalid_prefix_instead_of_reporting_false_coverage(oxidns_app, monkeypatch):
    app, repo = oxidns_app
    rule_setup(repo, monkeypatch)
    ruleset_id = discovery.read_settings(repo.get_compat_config('office'))['proxy_ruleset']
    def setup(shared):
        next(rule for rule in shared['rule_library'] if rule['id'] == ruleset_id).update(behavior='domain', content='DOMAIN-SUFFIX,example.test')
    repo.update_shared_transaction(setup)
    aid = agent()
    before = deepcopy(repo.export_all())
    response = app.test_client().post(f'/api/agents/{aid}/mihomo/rule-targets/apply', json={
        'profile_id': 'office', 'ruleset_id': ruleset_id, 'target': 'proxy', 'value': 'example.test', 'rule_type': 'DOMAIN'})
    assert response.status_code == 400
    assert repo.export_all() == before
