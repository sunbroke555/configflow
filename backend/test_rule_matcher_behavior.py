"""Routing tests must use the selected provider behavior, not import fallbacks."""
from unittest.mock import Mock

import pytest

from backend.utils.rule_matcher import RuleConfigMatcher, is_valid_domain, parse_rule_line


def matcher(source_text, behavior='classical', **overrides):
    return RuleConfigMatcher([{'id': 'set', 'itemType': 'ruleset', 'behavior': behavior, **overrides}],
                             [], lambda *_: source_text)


@pytest.mark.parametrize('behavior,content,query', [
    ('ipcidr', '+.tmdb.org', 'api.tmdb.org'),
    ('ipcidr', 'DOMAIN-SUFFIX,tmdb.org', 'tmdb.org'),
    ('ipcidr', '127.0.0.1', '127.0.0.1'),
    ('ipcidr', '::1', '::1'),
    ('ipcidr', 'IP-CIDR,127.0.0.0/8,no-resolve', '127.0.0.1'),
    ('ipcidr', '127.0.0.1/255.0.0.0', '127.0.0.1'),
    ('ipcidr', '127.0.0.1/33', '127.0.0.1'),
    ('ipcidr', '127.0.0.1/008', '127.0.0.1'),
    ('domain', 'DOMAIN-SUFFIX,tmdb.org', 'tmdb.org'),
    ('domain', 'domain-shuffix,tmdb.org', 'tmdb.org'),
    ('domain', 'full:tmdb.org', 'tmdb.org'),
    ('domain', 'domain:tmdb.org', 'tmdb.org'),
    ('domain', '127.0.0.0/8', '127.0.0.1'),
    ('domain', '127.0.0.1', '127.0.0.1'),
    ('domain', 'IP-CIDR,127.0.0.0/8', '127.0.0.1'),
    ('classical', '*.tmdb.org', 'api.tmdb.org'),
    ('classical', '.tmdb.org', 'api.tmdb.org'),
    ('classical', 'full:tmdb.org', 'tmdb.org'),
    ('classical', 'domain:tmdb.org', 'tmdb.org'),
    ('classical', 'IP-CIDR,127.0.0.1', '127.0.0.1'),
    ('classical', 'IP-CIDR,127.0.0.1/008', '127.0.0.1'),
    ('classical', '127.0.0.1/008', '127.0.0.1'),
    ('classical', 'IP,127.0.0.1', '127.0.0.1'),
    ('classical', 'REGEX,.*', 'tmdb.org'),
    ('classical', 'DOMAIN-SHUFFIX,tmdb.org', 'tmdb.org'),
    ('classical', 'DOMAIN-SUFFIX,', 'tmdb.org'),
    ('classical', 'DOMAIN-REGEX,[', 'tmdb.org'),
    ('classical', 'MATCH', 'match'),
    ('classical', 'DOMAIN', 'domain'),
])
def test_invalid_or_cross_format_lines_do_not_claim_a_match(behavior, content, query):
    assert matcher(content, behavior).match(query) is None


@pytest.mark.parametrize('content,query', [
    ('127.0.0.0/8', '127.0.0.1'), ('127.0.0.1/8', '127.0.0.2'),
    ('2001:db8::/32', '2001:db8::1'), ('::1/128', '::1'),
])
def test_ipcidr_accepts_only_cidr_and_keeps_network_semantics(content, query):
    assert matcher(content, 'ipcidr').match(query)['matched_line'] == content


@pytest.mark.parametrize('pattern,matches,misses', [
    ('tmdb.org', ['tmdb.org'], ['api.tmdb.org']),
    ('+.tmdb.org', ['tmdb.org', 'api.tmdb.org', 'api.deep.tmdb.org'], ['other.org']),
    ('.tmdb.org', ['api.tmdb.org', 'api.deep.tmdb.org'], ['tmdb.org']),
    ('*.tmdb.org', ['api.tmdb.org'], ['tmdb.org', 'api.deep.tmdb.org']),
    ('api.*.ORG', ['api.tmdb.org', 'api.other.org'], ['api.org', 'api.deep.tmdb.org']),
    ('+.*.tmdb.org', ['api.tmdb.org', 'api.deep.tmdb.org'], ['tmdb.org']),
    ('.api.*.org', ['sub.api.tmdb.org', 'deep.sub.api.tmdb.org'], ['api.tmdb.org']),
    ('*', ['localhost'], ['tmdb.org']),
])
def test_domain_provider_wildcards_follow_label_boundaries(pattern, matches, misses):
    subject = matcher(pattern, 'domain')
    for query in matches:
        assert subject.match(query)['matched_line'] == pattern
    for query in misses:
        assert subject.match(query) is None


@pytest.mark.parametrize('content,query', [
    ('+.tmdb.org', 'api.tmdb.org'), ('tmdb.org', 'tmdb.org'),
    ('127.0.0.1', '127.0.0.1'), ('127.0.0.1/8', '127.0.0.2'), ('::1', '::1'),
    ('DOMAIN-SUFFIX,tmdb.org', 'api.tmdb.org'),
    ('domain-suffix,tmdb.org', 'api.tmdb.org'),
    ('IP-CIDR6,127.0.0.0/8,no-resolve', '127.0.0.1'),
    ('IP-CIDR,::1/128,no-resolve', '::1'),
    (r'DOMAIN-REGEX,^node[0-9]{1,3}\.org$', 'node123.org'),
    (r'DOMAIN-REGEX,^\D+\.org$', 'text.org'),
    (r'DOMAIN-REGEX,^EXAMPLE\.org$', 'example.org'),
])
def test_classical_safe_bare_repairs_and_typed_rules_match(content, query):
    assert matcher(content).match(query)['matched_line'] == content


@pytest.mark.parametrize('source', [
    '\ufeff+.tmdb.org\n',
    'payload: ["+.tmdb.org", "127.0.0.1"]',
    '"payload": ["+.tmdb.org", "127.0.0.1"]',
    '\ufeffpayload:\n  - "+.tmdb.org" # suffix\n  - "127.0.0.1"\n',
])
def test_payload_yaml_quoted_keys_and_bom_are_decoded(source):
    subject = matcher(source, format='yaml' if 'payload' in source else 'text')
    assert subject.match('api.tmdb.org') is not None
    if '127.0.0.1' in source:
        assert subject.match('127.0.0.1') is not None


@pytest.mark.parametrize('source', [
    'payload: ["+.tmdb.org", true]', 'payload: {x: "+.tmdb.org"}',
    'payload: ["+.tmdb.org"', 'payload: ["+.tmdb.org"]\npayload: ["other.org"]',
])
def test_invalid_payload_does_not_fall_back_to_matching_document_lines(source):
    assert matcher(source, format='yaml').match('api.tmdb.org') is None


@pytest.mark.parametrize('rule_format,source', [
    ('yaml', 'DOMAIN,tmdb.org\n'),
    ('text', 'payload:\n  - DOMAIN,tmdb.org\n'),
    ('text', 'payload: ["DOMAIN,tmdb.org"]\n'),
    ('mrs', 'DOMAIN,tmdb.org\n'),
])
def test_declared_format_mismatch_does_not_claim_a_match(rule_format, source):
    assert matcher(source, format=rule_format).match('tmdb.org') is None


def test_source_indexes_do_not_mix_formats():
    content = 'payload: ["DOMAIN,tmdb.org"]'
    rules = [{'id': fmt, 'itemType': 'ruleset', 'behavior': 'classical', 'format': fmt}
             for fmt in ('text', 'yaml')]
    load = Mock(return_value=content)
    subject = RuleConfigMatcher(rules, [], load, index_key=lambda *_: 'shared')
    assert subject.match('tmdb.org')['rule_id'] == 'yaml'
    assert load.call_count == 2
    assert subject.invalidate_rulesets(lambda item, _: item['format'] == 'yaml') == 1
    assert subject.match('tmdb.org')['rule_id'] == 'yaml'
    assert load.call_count == 3


@pytest.mark.parametrize('metadata', [
    {'source_type': 'url', 'url': 'https://rules.test/list.yaml'},
    {'source_type': 'content', 'content': 'payload: ["DOMAIN,tmdb.org"]'},
])
def test_undeclared_format_uses_shared_converter_inference(metadata):
    subject = matcher('payload: ["DOMAIN,tmdb.org"]', **metadata)
    assert subject.match('tmdb.org') is not None


def test_library_format_is_used_over_reference_format():
    subject = RuleConfigMatcher(
        [{'id': 'set', 'itemType': 'ruleset', 'library_rule_id': 'lib', 'behavior': 'classical', 'format': 'text'}],
        [{'id': 'lib', 'format': 'yaml'}], lambda *_: 'payload: ["DOMAIN,tmdb.org"]')
    assert subject.match('tmdb.org') is not None


def test_source_reuse_includes_behavior_and_invalidates_only_selected_index():
    rules = [{'id': behavior, 'itemType': 'ruleset', 'behavior': behavior, 'library_rule_id': 'shared'}
             for behavior in ('ipcidr', 'domain', 'classical')]
    library = [{'id': 'shared', 'behavior': 'classical'}]
    load = Mock(return_value='+.tmdb.org\nIP-CIDR,127.0.0.0/8,no-resolve')
    subject = RuleConfigMatcher(rules, library, load, index_key=lambda *_: 'shared')
    assert subject.match('api.tmdb.org')['rule_id'] == 'domain'
    assert subject.match('127.0.0.1')['rule_id'] == 'classical'
    assert load.call_count == 3
    assert subject.invalidate_rulesets(lambda item, _: item['id'] == 'domain') == 1
    assert subject.match('127.0.0.1')['rule_id'] == 'classical'
    assert load.call_count == 4


def test_library_behavior_is_used_when_reference_has_not_been_hydrated():
    subject = RuleConfigMatcher([{'id': 'set', 'itemType': 'ruleset', 'library_rule_id': 'lib'}],
                                [{'id': 'lib', 'behavior': 'ipcidr'}], lambda *_: '+.tmdb.org')
    assert subject.match('api.tmdb.org') is None


def test_missing_behavior_defaults_to_domain_like_generated_providers():
    subject = RuleConfigMatcher([{'id': 'set', 'itemType': 'ruleset'}], [],
                                lambda *_: '127.0.0.1\nDOMAIN,typed.org\n+.tmdb.org')
    assert subject.match('127.0.0.1') is None
    assert subject.match('typed.org') is None
    assert subject.match('api.tmdb.org')['behavior'] == 'domain'


def test_generic_parser_stays_available_to_cross_format_import_consumers():
    assert parse_rule_line('full:tmdb.org') == ('DOMAIN', 'tmdb.org')
    assert parse_rule_line('domain:tmdb.org') == ('DOMAIN-SUFFIX', 'tmdb.org')
    assert parse_rule_line('*.tmdb.org') == ('DIRECT-SUBDOMAIN', 'tmdb.org')


@pytest.mark.parametrize('query,valid', [('999.999.999.999', False), ('300.1.1.1', False),
                                      ('123.456', False), ('3com.org', True), ('123.example', True)])
def test_invalid_dotted_ips_are_not_domains(query, valid):
    assert is_valid_domain(query) is valid
