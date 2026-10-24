"""Delivery layout compatibility does not change source rule semantics."""
import json

import pytest
import yaml

from backend.utils.ruleset_content import normalize_mihomo_ruleset, normalize_mihomo_yaml


@pytest.mark.parametrize('source', [
    '# header\npayload: ["+.tmdb.org", "192.0.2.0/24"] # tail\nother: keep\n',
    '"payload": [\n  "+.tmdb.org", # suffix\n  "192.0.2.0/24",\n]\nother: keep\n',
    '{"other":"keep","payload":["+.tmdb.org","192.0.2.0/24"]}',
    '\ufeff{"other":"keep","payload":["+.tmdb.org","192.0.2.0/24"]}\n',
    '# header\n{other: keep, # metadata\npayload: ["+.tmdb.org", "192.0.2.0/24"]} # tail\n',
    '--- {other: keep, payload: ["+.tmdb.org", "192.0.2.0/24"]}\n',
])
def test_flow_layout_becomes_block_without_changing_values(source):
    result, warnings = normalize_mihomo_yaml(source)
    assert warnings == []
    assert yaml.safe_load(result) == yaml.safe_load(source)
    root = yaml.compose(result)
    payload = next(value for key, value in root.value if key.value == 'payload')
    assert not root.flow_style and not payload.flow_style
    for comment in ('# header', '# tail', '# suffix', '# metadata'):
        if comment in source:
            assert comment in result
    assert result.startswith('\ufeff') == source.startswith('\ufeff')
    assert normalize_mihomo_yaml(result) == (result, [])
    assert normalize_mihomo_yaml(source) == (result, [])


@pytest.mark.parametrize('source', [
    'payload:\n  - "DOMAIN,tmdb.org" # preserve\nother: keep\n',
    '\ufeff"payload":\r\n  - "+.tmdb.org"\r\n',
    'payload: []\n',
])
def test_existing_block_and_empty_payload_are_byte_preserved(source):
    assert normalize_mihomo_yaml(source) == (source, [])


@pytest.mark.parametrize('source', [
    'payload: ["+.tmdb.org"', 'payload: true', 'payload: [true]',
    'payload: ["tmdb.org"]\npayload: ["other.org"]',
    'base: &list ["tmdb.org"]\npayload: *list',
    'payload: [&host "tmdb.org", *host]',
    'payload:\n  - |\n    tmdb.org\n',
    'payload: ["tmdb.\norg"]',
    '{payload: ["tmdb.org"], other: 1, other: 2}',
    '{payload: ["tmdb.org"], metadata: {key: 1, key: 2}}',
])
def test_ambiguous_yaml_is_unchanged_with_warning(source):
    result, warnings = normalize_mihomo_yaml(source)
    assert result == source
    assert warnings and warnings[0]['message']


def test_hash_in_rule_value_is_not_moved_into_a_comment():
    source = 'payload: ["DOMAIN-REGEX,(?x)^tmdb # (", "DOMAIN,literal#value"] # note\n'
    result, warnings = normalize_mihomo_yaml(source)
    assert not warnings
    assert yaml.safe_load(result) == yaml.safe_load(source)
    assert result.count('# note') == 1


def test_long_rules_are_not_wrapped_by_flow_mapping_serialization():
    value = 'DOMAIN-REGEX,' + 'abc ' * 100
    result, warnings = normalize_mihomo_yaml(json.dumps({'payload': [value]}))
    assert not warnings
    payload = yaml.compose(result).value[0][1]
    assert payload.value[0].start_mark.line == payload.value[0].end_mark.line
    assert yaml.safe_load(result)['payload'] == [value]


@pytest.mark.parametrize('behavior,value', [
    ('domain', '+.tmdb.org'), ('ipcidr', '192.0.2.0/24'),
    ('classical', 'DOMAIN-SUFFIX,tmdb.org'),
])
def test_delivery_wrapper_preserves_each_behavior(behavior, value):
    source = json.dumps({'payload': [value]})
    result, warnings = normalize_mihomo_ruleset(source, behavior, 'yaml')
    assert not warnings
    assert yaml.safe_load(result) == {'payload': [value]}
    assert normalize_mihomo_ruleset(result, behavior, 'yaml') == (result, [])


def test_delivery_wrapper_combines_classical_repairs_and_layout_only_when_selected():
    source = 'payload: ["+.tmdb.org", "*.other.org"]\n'
    result, warnings = normalize_mihomo_ruleset(source, 'classical', 'yaml')
    assert yaml.safe_load(result)['payload'] == ['DOMAIN-SUFFIX,tmdb.org', '*.other.org']
    assert len(warnings) == 1 and warnings[0]['before'] == '*.other.org'
    assert normalize_mihomo_ruleset(source, 'domain', 'text') == (source, [])
    assert normalize_mihomo_ruleset(source, 'classical', 'mrs') == (source, [])
