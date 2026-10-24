"""Ruleset behavior validation rejects known mistakes without changing drafts."""
from copy import deepcopy

import pytest
import yaml

from backend.common.config_repository import ProfileValidationError
from backend.utils.rule_syntax import RULE_TYPES, cidr_error, rule_value_error, split_rule_payload
from backend.utils.ruleset_content import (
    classical_line_error, inspect_domain_content, normalize_classical_lines, validate_domain_content,
)


@pytest.mark.parametrize('kind,value', [
    ('DOMAIN', 'api.example.test'), ('DOMAIN-SUFFIX', 'example.test'),
    ('DOMAIN-KEYWORD', 'example'), ('DOMAIN-WILDCARD', '*.example.?om'),
    ('DOMAIN-REGEX', r'(?<host>\p{Han}+)\.test$'),
    ('DOMAIN-REGEX', r'^node[]a,b]$'), ('DOMAIN-REGEX', r'^node[^]a,b]$'),
    ('DOMAIN-REGEX', r'^node[[:alpha:](]$'),
    ('DOMAIN-REGEX', r'^node{foo$'), ('DOMAIN-REGEX', r'^node{1$'),
    ('DOMAIN-REGEX', r'\Q[(literal)\E'),
    ('IP-CIDR', '2001:db8::/32'), ('IP-CIDR6', '192.0.2.0/24'),
    ('SRC-IP-CIDR', '192.0.2.1/32'), ('IP-SUFFIX', '192.0.2.1/16'),
    ('SRC-IP-SUFFIX', '2001:db8::1/64'),
    ('SRC-PORT', '0/80/443/8000-9000/65535'), ('DST-PORT', '443'), ('IN-PORT', '1-65535'),
    ('DSCP', '0/12-18/63'), ('DSCP', '*'), ('UID', '0-4294967295'),
    ('IP-ASN', '65535'), ('SRC-IP-ASN', '0'), ('GEOIP', 'CN'), ('SRC-GEOIP', 'CN'),
    ('GEOSITE', 'cn@ads'), ('PROCESS-NAME', 'app'), ('PROCESS-PATH', '/usr/bin/app'),
    ('PROCESS-NAME-REGEX', r'^app[0-9]{1,3}$'), ('PROCESS-PATH-REGEX', r'/apps/(a,b)'),
    ('PROCESS-NAME-WILDCARD', 'app*'), ('PROCESS-PATH-WILDCARD', '/apps/*'),
    ('NETWORK', 'tcp'), ('IN-TYPE', 'HTTP'), ('IN-USER', 'user'), ('IN-NAME', 'listener'),
    ('REMATCH-NAME', 'example'), ('SUB-RULE', '(NETWORK,tcp)'), ('RULE-SET', 'Example'),
    ('AND', '((DOMAIN,example.test),(OR,((NETWORK,tcp),(DST-PORT,80/443))))'),
    ('OR', '((DOMAIN-REGEX,^node{1,3}$),(DOMAIN,example.test))'),
    ('NOT', '((DOMAIN,example.test))'), ('MATCH', ''),
])
def test_known_valid_rule_values_and_go_regex_features(kind, value):
    assert kind in RULE_TYPES
    assert rule_value_error(kind, value) is None


@pytest.mark.parametrize('kind,value', [
    ('DOMAIN-SHUFFIX', 'example.test'), ('FUTURE-TYPE', 'x'), ('', 'x'), (None, 'x'),
    ('DOMAIN', ''), ('DOMAIN', '999.999.999.999'), ('DOMAIN-SUFFIX', '192.0.2.1'),
    ('DOMAIN', '-bad.test'), ('DOMAIN', 'bad..test'), ('DOMAIN', 'bad.test\nOTHER'),
    ('IP-CIDR', '192.0.2.1'), ('IP-CIDR', '192.0.2.0/33'),
    ('IP-CIDR6', '2001:db8::/129'), ('IP-CIDR', '192.0.2.0/255.255.255.0'),
    ('IP-CIDR', 'fe80::1%eth0/64'), ('IP-CIDR', '999.2.3.4/8'),
    ('IP-CIDR', '192.0.2.0/024'), ('IP-CIDR6', '2001:db8::/032'),
    ('DOMAIN-REGEX', '[abc'), ('DOMAIN-REGEX', '(abc'), ('DOMAIN-REGEX', 'abc)'),
    ('DOMAIN-REGEX', 'abc\\'), ('DOMAIN-REGEX', 'a{3,2}'), ('DOMAIN-REGEX', r'\p{Han}['),
    ('PROCESS-NAME-REGEX', '*app'),
    ('DST-PORT', '65536'), ('SRC-PORT', '-1'), ('IN-PORT', '443-80'),
    ('DST-PORT', '80/abc'), ('DSCP', '64'), ('UID', '4294967296'),
    ('IP-ASN', 'not-asn'), ('NETWORK', 'quic'), ('MATCH', 'example.test'),
    ('AND', '(DOMAIN,example.test)'), ('AND', '((DOMAIN,example.test)'),
    ('AND', '((DOMAIN-SHUFFIX,example.test))'),
    ('NOT', '((DOMAIN,one.test),(DOMAIN,two.test))'), ('AND', '((MATCH,))'),
])
def test_rule_value_errors_are_explicit(kind, value):
    assert rule_value_error(kind, value)


def test_rule_parser_matches_classical_payload_comma_rules():
    assert split_rule_payload('DOMAIN,example.test,DIRECT,no-resolve') == ('DOMAIN', 'example.test', ['DIRECT', 'no-resolve'])
    assert split_rule_payload('DOMAIN-REGEX,^a{1,3}$,DIRECT') == ('DOMAIN-REGEX', '^a{1,3}$,DIRECT', [])
    assert split_rule_payload('PROCESS-PATH-REGEX,/apps/(a,b)') == ('PROCESS-PATH-REGEX', '/apps/(a,b)', [])
    assert classical_line_error('DOMAIN,example.test,DIRECT,no-resolve') is None


@pytest.mark.parametrize('value,mask', [('192.0.2.1', '32'), ('2001:db8::1', '128')])
def test_ipcidr_bare_address_error_explains_the_required_prefix(value, mask):
    assert f'{value}/{mask}' in cidr_error(value)


@pytest.mark.parametrize('source', [
    '+.tmdb.org', 'api.tmdb.org', 'DOMAIN-SUFFIX,tmdb.org', 'DOMAIN,tmdb.org',
    'IP-CIDR,192.0.2.0/24', '192.0.2.1', '2001:db8::1', '192.0.2.0/33',
    '2001:db8::/129', '999.2.3.4/8', '192.0.2.0/255.255.255.0',
])
@pytest.mark.parametrize('yaml_payload', [False, True])
def test_ipcidr_rejects_domains_typed_rules_and_invalid_networks(source, yaml_payload):
    if yaml_payload:
        source = yaml.safe_dump({'payload': [source]})
    checked = inspect_domain_content(source, 'ipcidr')
    assert checked['errors'] and checked['content'] == source and not checked['changes']
    with pytest.raises(ProfileValidationError):
        validate_domain_content({'source_type': 'content', 'behavior': 'ipcidr', 'content': source})


@pytest.mark.parametrize('source', [
    '# networks\n192.0.2.8/24\n2001:db8::8/32\n// keep\n',
    'payload: ["192.0.2.1/32", "2001:db8::1/128"]\n',
])
def test_ipcidr_preserves_valid_networks_without_conversion(source):
    assert inspect_domain_content(source, 'ipcidr') == {'content': source, 'changes': [], 'errors': []}
    validate_domain_content({'source_type': 'content', 'behavior': 'ipcidr', 'content': source})


@pytest.mark.parametrize('source', [
    '192.0.2.1', '192.0.2.0/24', '2001:db8::1', '999.999.999.999',
    '+.999.999.999.999', '*.192.0.2.1', 'IP-CIDR,192.0.2.0/24', 'DOMAIN,999.2.3.4',
])
def test_domain_rejects_ip_like_and_non_domain_rules(source):
    assert inspect_domain_content(source, 'domain')['errors']


@pytest.mark.parametrize('source', [
    'DOMAIN-SHUFFIX,tmdb.org', 'DOMAIN,', 'DOMAIN,tmdb.org,', 'IP-CIDR,192.0.2.1/33',
    'IP-CIDR,192.0.2.1', 'IP-CIDR6,2001:db8::/129', 'DOMAIN-REGEX,[broken',
    'DST-PORT,99999', 'MATCH', 'RULE-SET,Other', 'SUB-RULE,(NETWORK,tcp)', 'DOMAIN',
    'UNKNOWN,a,b', 'full:foreign.example,extra',
])
def test_classical_explicit_errors_reject_saves_but_delivery_keeps_original(source):
    checked = inspect_domain_content(source, 'classical')
    assert checked['errors'] and checked['content'] == source
    with pytest.raises(ProfileValidationError):
        validate_domain_content({'source_type': 'content', 'behavior': 'classical', 'content': source})
    normalized, warnings = normalize_classical_lines(source + '\n+.valid.test')
    assert normalized == source + '\nDOMAIN-SUFFIX,valid.test'
    assert warnings[0]['line'] == 1 and warnings[0]['before'] == source


def test_classical_safe_bare_content_and_ambiguous_warnings_still_save_unchanged():
    source = '+.tmdb.org\napi.tmdb.org\n192.0.2.1\n2001:db8::1\n*.manual.test\nfull:foreign.test'
    rule = {'source_type': 'content', 'behavior': 'classical', 'content': source}
    before = deepcopy(rule)
    validate_domain_content(rule)
    assert rule == before
    checked = inspect_domain_content(source, 'classical')
    assert not checked['errors'] and len(checked['warnings']) == 2


@pytest.mark.parametrize('behavior,rule_format', [
    ('unknown', 'text'), ('DOMAIN', 'text'), (None, 'text'), ('domain', 'json'),
    ('classical', 'mrs'), ('ipcidr', 'mrs'), ('domain', 'mrs'),
])
def test_bad_behavior_format_and_handwritten_mrs_are_rejected(behavior, rule_format):
    checked = inspect_domain_content('', behavior, rule_format)
    assert checked['errors']
    with pytest.raises(ProfileValidationError):
        validate_domain_content({'source_type': 'content', 'behavior': behavior, 'format': rule_format, 'content': ''})


def test_url_metadata_is_validated_without_reading_remote_content():
    validate_domain_content({'source_type': 'url', 'behavior': 'domain', 'format': 'mrs'})
    validate_domain_content({'source_type': 'url', 'behavior': 'ipcidr', 'format': 'mrs'})
    for override in ({'behavior': 'typo'}, {'format': 'binary'}, {'behavior': 'classical', 'format': 'mrs'}):
        with pytest.raises(ProfileValidationError):
            validate_domain_content({'source_type': 'url', 'behavior': 'domain', 'format': 'text', **override})


def test_unchanged_legacy_invalid_sources_can_still_be_renamed_or_disabled():
    old = {'name': 'Old', 'source_type': 'content', 'behavior': 'typo', 'format': 'broken', 'content': 'DOMAIN-SHUFFIX,x'}
    validate_domain_content({**old, 'name': 'New'}, old)
    validate_domain_content({**old, 'enabled': False}, old)
    with pytest.raises(ProfileValidationError):
        validate_domain_content({**old, 'content': 'changed'}, old)


@pytest.mark.parametrize('rule_format', [None, ''])
def test_unspecified_legacy_format_stays_editable(rule_format):
    checked = inspect_domain_content('+.tmdb.org', 'classical', rule_format)
    assert not checked['errors']
    validate_domain_content({'source_type': 'content', 'behavior': 'classical', 'format': rule_format, 'content': '+.tmdb.org'})


@pytest.mark.parametrize('source_type', ['inline', 'unknown', None])
def test_invalid_source_types_are_rejected(source_type):
    with pytest.raises(ProfileValidationError):
        validate_domain_content({'source_type': source_type, 'behavior': 'domain', 'content': 'example.test'})


@pytest.mark.parametrize('source', ['192.0.2.0/024', '2001:db8::/032', '0.0.0.0/00'])
def test_bare_prefixes_with_leading_zeroes_are_not_converted_into_invalid_rules(source):
    normalized, warnings = normalize_classical_lines(source)
    assert normalized == source and warnings


@pytest.mark.parametrize('kind', ['DST-PORT', 'DSCP', 'UID'])
def test_extremely_long_integer_values_return_validation_error(kind):
    assert rule_value_error(kind, '9' * 5000)


def test_extremely_nested_regex_returns_validation_error():
    assert rule_value_error('DOMAIN-REGEX', '(' * 2000 + 'x' + ')' * 2000)


@pytest.mark.parametrize('source', ['192.0.2.1/33', '999.999.999.999', ':::/64', '2001:db8::/129', '192.0.2.0/024'])
def test_classical_malformed_bare_ip_is_an_error_but_never_rewritten(source):
    checked = inspect_domain_content(source, 'classical')
    assert checked['errors'] and checked['content'] == source
    assert not checked['warnings']
    with pytest.raises(ProfileValidationError):
        validate_domain_content({'source_type': 'content', 'behavior': 'classical', 'content': source})
    normalized, warnings = normalize_classical_lines(source)
    assert normalized == source and warnings


@pytest.mark.parametrize('source', ['.example.test', '*.example.test', 'full:example.test', 'domain:example.test', 'ip:192.0.2.0/24'])
def test_classical_foreign_or_ambiguous_syntax_remains_a_nonblocking_warning(source):
    checked = inspect_domain_content(source, 'classical')
    assert checked['warnings'] and not checked['errors']
    assert checked['content'] == source
    validate_domain_content({'source_type': 'content', 'behavior': 'classical', 'content': source})


@pytest.mark.parametrize('expression', [
    '(?x)^example # (', '(?x)^example # [', '(?x)^example # ' + '\\',
    '(?#literal comment [)^example', '(?ix:^ example )', '(?-i:x)(?x)^example # [',
])
@pytest.mark.parametrize('yaml_payload', [False, True])
def test_native_valid_verbose_and_comment_regexes_are_left_to_the_core(expression, yaml_payload):
    # Mihomo v1.19.32 loads and matches the comment cases; bracket counting
    # without understanding regexp2's comments incorrectly rejected them.
    assert rule_value_error('DOMAIN-REGEX', expression) is None
    source = 'DOMAIN-REGEX,' + expression
    if yaml_payload:
        source = yaml.safe_dump({'payload': [source]})
    checked = inspect_domain_content(source, 'classical')
    assert not checked['errors'] and not checked['warnings']
    assert checked['content'] == source
    validate_domain_content({'source_type': 'content', 'behavior': 'classical', 'content': source})


@pytest.mark.parametrize('behavior,value', [
    ('domain', 'example.test'), ('ipcidr', '192.0.2.0/24'), ('classical', '+.example.test'),
])
@pytest.mark.parametrize('key', ['payload', '"payload"', "'payload'"])
def test_explicit_text_format_rejects_yaml_payload_without_rewriting(behavior, value, key):
    source = f'{key}:\n  - "{value}"\n'
    checked = inspect_domain_content(source, behavior, 'text')
    assert checked['content'] == source and checked['changes'] == []
    assert checked['errors'] == [{'line': 0, 'message': '内容是 YAML payload，请选择 YAML 格式后保存。'}]
    rule = {'source_type': 'content', 'behavior': behavior, 'format': 'text', 'content': source}
    before = deepcopy(rule)
    with pytest.raises(ProfileValidationError, match='请选择.*YAML|请.*选择 YAML'):
        validate_domain_content(rule)
    assert rule == before


@pytest.mark.parametrize('behavior,value', [
    ('domain', 'example.test'), ('ipcidr', '192.0.2.0/24'), ('classical', '+.example.test'),
])
@pytest.mark.parametrize('rule_format', [None, '', 'yaml'])
def test_yaml_and_unspecified_formats_accept_payload_without_changing_saved_source(behavior, value, rule_format):
    source = f'payload: ["{value}"]\n'
    checked = inspect_domain_content(source, behavior, rule_format)
    assert not checked['errors']
    rule = {'source_type': 'content', 'behavior': behavior, 'format': rule_format, 'content': source}
    before = deepcopy(rule)
    validate_domain_content(rule)
    assert rule == before


@pytest.mark.parametrize('kind,value', [
    ('DST-PORT', '80,443'), ('DOMAIN-KEYWORD', 'foo,bar'), ('PROCESS-NAME', 'app,DIRECT'),
    ('RULE-SET', 'Example,DIRECT'), ('DOMAIN-WILDCARD', '*.example.test,DIRECT'),
])
def test_non_regex_rule_values_cannot_inject_extra_config_fields(kind, value):
    assert '逗号' in rule_value_error(kind, value)


def test_classical_existing_parameter_fields_remain_accepted():
    assert classical_line_error('DOMAIN-KEYWORD,foo,DIRECT') is None
    assert classical_line_error('DST-PORT,80,no-resolve') is None
    assert rule_value_error('DOMAIN-REGEX', '^foo,bar$') is None
    assert rule_value_error('AND', '((DOMAIN,example.test),(DST-PORT,80/443))') is None
    assert rule_value_error('SUB-RULE', '(NETWORK,tcp)') is None
