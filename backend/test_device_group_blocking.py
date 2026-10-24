"""Shared blocking projection preserves order, reachability and layer semantics."""
from copy import deepcopy

import pytest

from backend.common.device_group_blocking import compile_blocking_rules


def rule(identity, kind='DOMAIN', policy='DIRECT', **values):
    return {'id': identity, 'itemType': 'rule', 'rule_type': kind,
            'value': identity + '.example.test', 'policy': policy, **values}


def test_layers_use_the_same_reachable_prefix_but_independent_blocking_actions():
    rules = [rule('allow'), rule('dns'), rule('reject', policy='REJECT'),
             rule('drop', 'IP-CIDR', 'REJECT-DROP', value='192.0.2.0/24', no_resolve=True),
             rule('off', policy='REJECT', enabled=False), rule('off-match', 'MATCH', enabled=False),
             rule('blackhole'), rule('end', 'MATCH', 'REJECT'),
             rule('after', policy='REJECT'), rule('after-dns'), rule('later-match', 'MATCH', 'REJECT')]
    config = {'rule_configs': rules, 'oxidns': {
        'rule_actions': {'dns': {'action': 'refused'}, 'drop': {'action': 'nxdomain'},
                         'off': {'action': 'nodata'}, 'blackhole': {'action': 'blackhole'},
                         'end': {'action': 'blackhole'}, 'after-dns': {'action': 'nodata'}},
        'rule_dns_parked': {'allow': {'action': 'blackhole'}},
    }}
    before = deepcopy(config)
    result = compile_blocking_rules(config)
    assert [(r['item']['id'], r['action']) for r in result['oxidns']] == [
        ('dns', 'refused'), ('blackhole', 'blackhole')]
    assert [r['id'] for r in result['mihomo']] == ['reject', 'drop']
    assert result['mihomo'][1]['no_resolve'] is True
    assert result['cutoff'] == 'end'
    assert result['excluded'] == ['after', 'after-dns']
    assert config == before


@pytest.mark.parametrize('behavior,domain', [('domain', True), ('classical', True), ('ipcidr', False)])
def test_library_behavior_and_enabled_state_are_resolved_before_dns_classification(behavior, domain):
    config = {'profile_id': 'office', 'rule_configs': [
        {'id': 'ref', 'itemType': 'ruleset', 'library_rule_id': 'lib',
         'behavior': 'ipcidr', 'policy': 'REJECT'},
    ], 'rule_library': [{'id': 'lib', 'name': '规则库来源', 'behavior': behavior,
                         'source_type': 'content', 'content': 'DOMAIN,a.test\n'}],
        'oxidns': {'rule_actions': {'ref': {'action': 'blackhole'}}}}
    result = compile_blocking_rules(config)
    assert bool(result['oxidns']) is domain
    assert result['mihomo'][0]['name'] == '规则库来源'
    assert result['mihomo'][0]['url'] == '/api/profiles/office/rule-library/content/lib'
    config['rule_library'][0]['enabled'] = False
    assert compile_blocking_rules(config)['mihomo'] == []


def test_unrelated_incomplete_native_draft_and_parked_or_invalid_actions_do_not_lock_mihomo():
    config = {'rule_configs': [rule('reject', policy='REJECT'), rule('parked'), rule('bad')],
              'oxidns': {'config_mode': 'native', 'native_config': 'plugins: [unfinished',
                         'rule_actions': {'bad': {'action': 'unknown'}},
                         'rule_dns_parked': {'parked': {'action': 'refused'}}}}
    result = compile_blocking_rules(config)
    assert [r['id'] for r in result['mihomo']] == ['reject']
    assert result['oxidns'] == []


def test_legacy_rule_arrays_remain_supported_without_inventing_a_cutoff():
    config = {'rules': [{'id': 'legacy', 'rule_type': 'DOMAIN', 'value': 'legacy.test', 'policy': 'REJECT'}]}
    result = compile_blocking_rules(config)
    assert [r['id'] for r in result['mihomo']] == ['legacy']
    assert result['cutoff'] is None


def test_raw_rule_configs_match_without_item_type_still_stops_dns_copying():
    config = {'rule_configs': [{'id': 'end', 'rule_type': 'MATCH', 'policy': 'REJECT'},
                              rule('after', policy='REJECT')],
              'oxidns': {'rule_actions': {'after': {'action': 'blackhole'}}}}
    assert compile_blocking_rules(config) == {
        'oxidns': [], 'mihomo': [], 'cutoff': 'end', 'excluded': ['after']}
