"""Classical compatibility only rewrites unambiguous bare domain/IP entries."""
import ipaddress
from copy import deepcopy

import pytest
import yaml

from backend.common.config_repository import ProfileValidationError
from backend.utils.rule_matcher import match_query, parse_rule_line
from backend.utils.ruleset_content import (
    inspect_domain_content,
    normalize_classical_lines,
    read_rule_payload,
    validate_domain_content,
)


@pytest.mark.parametrize(('source', 'expected'), [
    ('+.tmdb.org', 'DOMAIN-SUFFIX,tmdb.org'),
    ('+.x', 'DOMAIN-SUFFIX,x'),
    ('api.tmdb.org', 'DOMAIN,api.tmdb.org'),
    ('x', 'DOMAIN,x'),
    ('API.Example.COM', 'DOMAIN,API.Example.COM'),
    ('xn--fsqu00a.xn--0zwm56d', 'DOMAIN,xn--fsqu00a.xn--0zwm56d'),
    ('192.0.2.1', 'IP-CIDR,192.0.2.1/32,no-resolve'),
    ('192.0.2.9/24', 'IP-CIDR,192.0.2.9/24,no-resolve'),
    ('0.0.0.0/0', 'IP-CIDR,0.0.0.0/0,no-resolve'),
    ('2001:db8::1', 'IP-CIDR6,2001:db8::1/128,no-resolve'),
    ('2001:DB8::9/32', 'IP-CIDR6,2001:DB8::9/32,no-resolve'),
    ('::1', 'IP-CIDR6,::1/128,no-resolve'),
    ('::/0', 'IP-CIDR6,::/0,no-resolve'),
    ('::ffff:192.0.2.1', 'IP-CIDR6,::ffff:192.0.2.1/128,no-resolve'),
])
def test_normalize_safe_bare_entries(source, expected):
    assert normalize_classical_lines(source) == (expected, [])
    assert normalize_classical_lines(expected) == (expected, [])


@pytest.mark.parametrize(('source', 'expected'), [
    ('\ufeff+.tmdb.org\r\n', '\ufeffDOMAIN-SUFFIX,tmdb.org\r\n'),
    ('\ufeff  example.com\n', '\ufeff  DOMAIN,example.com\n'),
    ('\ufeff# comment\n+.tmdb.org', '\ufeff# comment\nDOMAIN-SUFFIX,tmdb.org'),
    ('\ufeffpayload: ["+.tmdb.org"]\n', '\ufeffpayload: ["DOMAIN-SUFFIX,tmdb.org"]\n'),
    ('\ufeff{"payload":["example.com"]}', '\ufeff{"payload":["DOMAIN,example.com"]}'),
    ('\ufeff', '\ufeff'),
])
def test_utf8_bom_is_preserved_without_hiding_the_first_entry(source, expected):
    assert normalize_classical_lines(source) == (expected, [])
    assert normalize_classical_lines(expected) == (expected, [])


@pytest.mark.parametrize('source', [
    '', '\n \n', '# comment\n// comment\n',
    'DOMAIN-SUFFIX,tmdb.org\nDOMAIN,example.com\nIP-CIDR,192.0.2.0/24',
    'DOMAIN-KEYWORD,google\nIP-CIDR6,2001:db8::/32,no-resolve',
    '  DOMAIN,example.com \r\n# +.tmdb.org\r\n\t// 192.0.2.1\r\n',
    'UNKNOWN,a,b\nfull:foreign.example,extra',
])
def test_unchanged_lines_are_byte_preserved(source):
    assert normalize_classical_lines(source) == (source, [])


@pytest.mark.parametrize('source', [
    '.example.com', '*.example.com', 'full:example.com', 'domain:example.com',
    'regexp:^example', 'ip:192.0.2.1', '+.', '+.bad..test', '+.192.0.2.1',
    'foo..test', 'bad-.test', '-bad.test', 'https://example.com', 'example.com/path',
    '192.0.2.999', '192.0.2.1/33', '2001:db8::/129', '192.0.2.0/255.255.255.0',
    'fe80::1%eth0', 'example.com # inline text', 'a' * 64 + '.example',
])
def test_ambiguous_or_invalid_entries_are_preserved_with_line_warning(source):
    actual, warnings = normalize_classical_lines(source)
    assert actual == source
    assert len(warnings) == 1
    assert warnings[0]['line'] == 1
    assert warnings[0]['before'] == source
    assert warnings[0]['message']


def test_mixed_text_keeps_order_indentation_newlines_and_warnings():
    source = '# Header\r\n  +.tmdb.org \r\n\r\napi.test\r\n192.0.2.1\r\nDOMAIN,x\r\n  *.test\r\n// End'
    expected = '# Header\r\n  DOMAIN-SUFFIX,tmdb.org \r\n\r\nDOMAIN,api.test\r\nIP-CIDR,192.0.2.1/32,no-resolve\r\nDOMAIN,x\r\n  *.test\r\n// End'
    normalized, warnings = normalize_classical_lines(source)
    assert normalized == expected
    assert [warning['line'] for warning in warnings] == [7]
    assert normalize_classical_lines(normalized) == (normalized, warnings)
    assert normalize_classical_lines(source) == (normalized, warnings)


@pytest.mark.parametrize('source', [
    '# Header\npayload:\n  - "+.tmdb.org" # suffix\n  - api.test\n  - 192.0.2.1\n  - "2001:db8::/32"\n  - "DOMAIN,old.test"\n  - "*.other.test"\n  - "# embedded comment"\nother: preserved\n',
    'payload: ["+.tmdb.org", api.test, 192.0.2.1, "2001:db8::/32", "DOMAIN,old.test", "*.other.test", "# embedded comment",]\nother: preserved\n',
    '{"payload":["+.tmdb.org", "api.test", "192.0.2.1", "2001:db8::/32", "DOMAIN,old.test", "*.other.test", "# embedded comment"],"other":"preserved"}',
])
def test_yaml_payload_rewrite_is_lossless_stable_and_valid(source):
    normalized, warnings = normalize_classical_lines(source)
    parsed = yaml.safe_load(normalized)
    assert parsed == {'payload': [
        'DOMAIN-SUFFIX,tmdb.org', 'DOMAIN,api.test', 'IP-CIDR,192.0.2.1/32,no-resolve',
        'IP-CIDR6,2001:db8::/32,no-resolve', 'DOMAIN,old.test', '*.other.test', '# embedded comment',
    ], 'other': 'preserved'}
    assert len(warnings) == 1 and warnings[0]['before'] == '*.other.test'
    if '# suffix' in source:
        assert '# suffix' in normalized
    # Every existing typed rule and non-payload value remains untouched.
    assert '"DOMAIN,old.test"' in normalized
    assert source.endswith('\n') == normalized.endswith('\n')
    assert normalize_classical_lines(normalized) == (normalized, warnings)
    assert normalize_classical_lines(source) == (normalized, warnings)


@pytest.mark.parametrize('source', [
    'payload: []\n', 'payload: ["DOMAIN,x", "", "// note"]\n',
    'payload:\n  - "DOMAIN,x" # existing\nother: api.test\n',
])
def test_unchanged_yaml_does_not_get_reserialized(source):
    assert normalize_classical_lines(source) == (source, [])


@pytest.mark.parametrize('source', [
    'payload: ["+.example.com",', 'payload: true', 'payload: [true]',
    'payload: []\npayload: []', 'a: &rules ["+.example.com"]\npayload: *rules',
    'payload: [&rule "+.example.com", *rule]', 'payload:\n  - |\n    example.com\n',
])
def test_invalid_or_ambiguous_yaml_is_never_partially_rewritten(source):
    normalized, warnings = normalize_classical_lines(source)
    assert normalized == source and warnings
    checked = inspect_domain_content(source, 'classical')
    assert checked['content'] == source and checked['errors']
    assert not checked['changes']


def test_inspection_proposes_explicit_changes_and_keeps_warnings_separate():
    source = '+.tmdb.org\n192.0.2.1\n*.example.com'
    result = inspect_domain_content(source, 'classical')
    assert result['changes'] == [
        {'line': 1, 'before': '+.tmdb.org', 'after': 'DOMAIN-SUFFIX,tmdb.org'},
        {'line': 2, 'before': '192.0.2.1', 'after': 'IP-CIDR,192.0.2.1/32,no-resolve'},
    ]
    assert not result['errors']
    assert result['warnings'][0]['line'] == 3
    assert result['content'] == 'DOMAIN-SUFFIX,tmdb.org\nIP-CIDR,192.0.2.1/32,no-resolve\n*.example.com'


def test_save_accepts_bare_classical_entries_without_mutation():
    rule = {'source_type': 'content', 'behavior': 'classical', 'content': '+.tmdb.org'}
    before = deepcopy(rule)
    validate_domain_content(rule)
    assert rule == before


def test_classical_validation_only_checks_edited_handwritten_content():
    existing = {'source_type': 'content', 'behavior': 'classical', 'content': '+.tmdb.org', 'enabled': True}
    validate_domain_content({**existing, 'enabled': False}, previous=existing)
    validate_domain_content({**existing, 'name': 'Renamed'}, previous=existing)
    validate_domain_content({**existing, 'source_type': 'url'})
    validate_domain_content({**existing, 'behavior': 'ipcidr'})
    validate_domain_content({**existing, 'content': '*.example.com\nfull:example.com'})
    with pytest.raises(ProfileValidationError):
        validate_domain_content({**existing, 'content': 'payload: [true]'})
    validate_domain_content({**existing, 'content': '+.changed.test'}, previous=existing)


def _classical_matches(query, line):
    """Interpret the generated type/value fields independently of parse_rule_line."""
    parts = line.split(',')
    kind, value = parts[:2]
    if kind == 'DOMAIN':
        return query.lower() == value.lower()
    if kind == 'DOMAIN-SUFFIX':
        return query.lower() == value.lower() or query.lower().endswith('.' + value.lower())
    assert kind in ('IP-CIDR', 'IP-CIDR6') and parts[2:] == ['no-resolve']
    try:
        return ipaddress.ip_address(query) in ipaddress.ip_network(value, strict=False)
    except ValueError:
        return False


@pytest.mark.parametrize('line', [
    '+.tmdb.org', 'api.tmdb.org', '192.0.2.1', '192.0.2.9/24',
    '2001:db8::1', '2001:db8::9/32',
])
def test_normalized_classical_semantics_agree_with_rule_matcher(line):
    normalized, warnings = normalize_classical_lines(line)
    assert not warnings
    for query in ('tmdb.org', 'api.tmdb.org', 'sub.api.tmdb.org', 'eviltmdb.org', 'other.test',
                  '192.0.2.1', '192.0.2.200', '198.51.100.1', '2001:db8::1', '2001:db8:1::2', '2001:db9::1'):
        assert match_query(query, *parse_rule_line(line)) == _classical_matches(query, normalized), (line, query)


@pytest.mark.parametrize('key', ['payload', '"payload"', "'payload'"])
@pytest.mark.parametrize('prefix', ['', '\ufeff'])
def test_quoted_yaml_payload_keys_use_the_same_lossless_rewrite(key, prefix):
    source = prefix + f"# keep\n{key}:\n  - '+.tmdb.org' # suffix\n"
    expected = prefix + f'# keep\n{key}:\n  - "DOMAIN-SUFFIX,tmdb.org" # suffix\n'
    assert normalize_classical_lines(source) == (expected, [])
    assert normalize_classical_lines(expected) == (expected, [])
    lines, payload = read_rule_payload(source)
    assert lines == ['+.tmdb.org'] and payload is not None


@pytest.mark.parametrize(('behavior', 'value'), [
    ('domain', 'DOMAIN-SUFFIX,tmdb.org'), ('classical', '+.tmdb.org'),
])
@pytest.mark.parametrize('properties', ['!!str &host', '&host !!str'])
def test_tagged_payload_anchor_never_leaves_a_dangling_alias(behavior, value, properties):
    source = f'payload:\n  - {properties} {value}\nother: *host\n'
    assert yaml.safe_load(source)['other'] == value
    checked = inspect_domain_content(source, behavior)
    assert checked['content'] == source
    assert checked['errors'] and not checked['changes']
    if behavior == 'classical':
        normalized, warnings = normalize_classical_lines(source)
        assert normalized == source and warnings
        assert yaml.safe_load(normalized)['other'] == value


def test_anchors_outside_payload_and_literal_ampersands_are_untouched():
    source = 'note: &note "a&b"\ncopy: *note\npayload: ["+.tmdb.org", "DOMAIN-REGEX,a&b"]\n'
    normalized, warnings = normalize_classical_lines(source)
    assert not warnings
    assert normalized == source.replace('+.tmdb.org', 'DOMAIN-SUFFIX,tmdb.org')
    assert yaml.safe_load(normalized)['copy'] == 'a&b'
