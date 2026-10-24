"""Rule DNS actions through the real generator and native applier.

The generated scenarios always pass the ConfigFlow validator. The real core run
is optional: OXIDNS_TEST_BINARY=/path/to/oxidns pytest backend/test_oxidns_rule_actions_core.py
"""
import os

import pytest
import yaml

from backend.common.rule_dns import REAL_WRAPPERS
from scripts.verify_oxidns_native import RULE_DNS_SCENARIOS, RULE_DNS_STUBS, prepare_rule_dns, run_rule_dns

BINARY = os.environ.get('OXIDNS_TEST_BINARY')


def generated(root, scenario):
    prepare_rule_dns(root, scenario)
    return yaml.safe_load((root / 'config.yaml').read_text())


def plugins(document):
    return {plugin['tag']: plugin for plugin in document['plugins']}


def main_execs(document):
    return [entry['exec'] for entry in plugins(document)['sequence_main']['args']]


def rule_execs(document):
    """Execs of the rule group entries only, without block_qtypes and defaults."""
    return [entry['exec'] for entry in plugins(document)['sequence_main']['args']
            if entry.get('matches', [''])[0].startswith(('qname $rules_', 'resp_ip $rules_'))]


def chains(document):
    """Hash key and step execs of each pinned chain, by the source of its upstream."""
    found = plugins(document)
    sources = {RULE_DNS_STUBS['pin_' + source][0]: source for source in ('local', 'remote', 'fallback')}
    return {sources[plugin['args']['upstreams'][0]['addr']]:
            (tag[11:], [step['exec'] for step in found['cf_dns_up_' + tag[11:]]['args']])
            for tag, plugin in found.items() if tag.startswith('cf_dns_fwd_')}


@pytest.mark.parametrize('scenario', [name for name in RULE_DNS_SCENARIOS if name != 'classical'])
def test_every_action_and_pinned_chain_is_generated(tmp_path, scenario):
    _, _, features = RULE_DNS_SCENARIOS[scenario]
    document = generated(tmp_path, scenario)
    execs = rule_execs(document)
    for route in ('reject', 'reject 3', 'reject 0', '$cf_dns_blackhole'):
        # Once among the domain groups and once among the IP groups.
        assert execs.count(route) == 2, route
    assert plugins(document)['cf_dns_blackhole']['args'] == {'mode': 'null', 'short_circuit': True}
    cache = features['cache_enabled']
    wrappers = [f'${name}' for name in REAL_WRAPPERS] if 'ip_selector' in features else []
    found = chains(document)
    assert found.keys() == {'local', 'remote', 'fallback'}
    for source, (key, steps) in found.items():
        expected = ['drop_resp', *(wrappers if source == 'local' else []),
                    *([f'$cf_dns_cache_{key}'] if cache else []), f'$cf_dns_fwd_{key}', 'reject 3',
                    *([f'ttl {features["remote_ttl"]}'] if source != 'local' and features['remote_ttl'] else []),
                    'accept']
        assert steps == expected, source
        # Domain and IP rules share one chain per pinned upstream.
        assert execs.count(f'goto cf_dns_up_{key}') == (1 if source == 'fallback' else 2)
        assert (f'cf_dns_cache_{key}' in plugins(document)) is cache


def test_native_applier_writes_the_template_helpers(tmp_path):
    native = generated(tmp_path / 'native', 'native')
    template = generated(tmp_path / 'template', 'cached-features')
    helpers = [plugin for plugin in template['plugins'] if plugin['tag'].startswith('cf_dns_')]
    assert [plugin for plugin in native['plugins'] if plugin['tag'].startswith('cf_dns_')] == helpers
    assert main_execs(native) == main_execs(template)


def test_routed_classical_ruleset_turns_on_local_probe(tmp_path):
    document = generated(tmp_path, 'classical')
    assert main_execs(document)[-4:] == ['jump local_probe', 'reject 3', 'drop_resp', 'goto proxy_dns_seq']
    assert len(list((tmp_path / 'rules').iterdir())) == 2


@pytest.mark.skipif(not BINARY, reason='Set OXIDNS_TEST_BINARY for real-core acceptance')
@pytest.mark.parametrize('scenario', list(RULE_DNS_SCENARIOS))
def test_real_core_answers_every_action(tmp_path, scenario):
    prepare_rule_dns(tmp_path, scenario)
    results = run_rule_dns(tmp_path, BINARY)
    assert results['check'].startswith('Configuration is valid')
    if scenario == 'classical':
        assert results['classical_probe'] is True
    else:
        assert results['actions'] is results['pinned_down_servfail'] is True
        assert results['pinned_cache'] == ('hit' if RULE_DNS_SCENARIOS[scenario][2]['cache_enabled'] else 'disabled')
