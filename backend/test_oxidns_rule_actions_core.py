"""Rule DNS actions through the real generator and native applier.

The generated scenarios always pass the ConfigFlow validator. The real core run
is optional: OXIDNS_TEST_BINARY=/path/to/oxidns pytest backend/test_oxidns_rule_actions_core.py
"""
import os
import subprocess
import time

import pytest
import yaml

from backend.common.rule_dns import REAL_WRAPPERS
from backend.common.oxidns_common import apply_common_settings, read_common_settings
from backend.common.oxidns_editor import prepare_editor
from backend.converters.oxidns import generate_oxidns_config
from scripts.verify_oxidns_native import (
    RULE_DNS_SCENARIOS, RULE_DNS_STUBS, MockDNS, free_port, prepare_rule_dns, query, run_rule_dns,
)

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
        # Only domain rules affect DNS answers.
        assert execs.count(route) == 1, route
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
        assert execs.count(f'goto cf_dns_up_{key}') == 1
        assert (f'cf_dns_cache_{key}' in plugins(document)) is cache


def test_native_applier_writes_the_template_helpers(tmp_path):
    native = generated(tmp_path / 'native', 'native')
    template = generated(tmp_path / 'template', 'cached-features')
    helpers = [plugin for plugin in template['plugins'] if plugin['tag'].startswith('cf_dns_')]
    assert [plugin for plugin in native['plugins'] if plugin['tag'].startswith('cf_dns_')] == helpers
    assert main_execs(native) == main_execs(template)


def test_routed_classical_ruleset_uses_only_domain_part(tmp_path):
    document = generated(tmp_path, 'classical')
    assert main_execs(document)[-2:] == ['reject 3', 'goto proxy_dns_seq']
    assert 'jump local_probe' not in main_execs(document)
    assert len(list((tmp_path / 'rules').iterdir())) == 1


@pytest.mark.skipif(not BINARY, reason='Set OXIDNS_TEST_BINARY for real-core acceptance')
@pytest.mark.parametrize('scenario', list(RULE_DNS_SCENARIOS))
def test_real_core_answers_every_action(tmp_path, scenario):
    prepare_rule_dns(tmp_path, scenario)
    results = run_rule_dns(tmp_path, BINARY)
    assert results['check'].startswith('Configuration is valid')
    if scenario == 'classical':
        assert results['classical_domain_only'] is True
    else:
        assert results['actions'] is results['pinned_down_servfail'] is True
        assert results['pinned_cache'] == ('hit' if RULE_DNS_SCENARIOS[scenario][2]['cache_enabled'] else 'disabled')


@pytest.mark.skipif(not BINARY, reason='Set OXIDNS_TEST_BINARY for real-core acceptance')
def test_custom_remote_match_order_against_overlapping_direct_rule(tmp_path):
    """Changing priority, not the goto target, decides which upstream answers."""
    config = {'rule_configs': [
        {'id': 'china', 'itemType': 'rule', 'rule_type': 'DOMAIN-SUFFIX', 'value': 'hhanclub.net'},
    ]}
    name = 'tracker.hhanclub.net'
    custom = {'matches': ['qname full:' + name], 'exec': 'goto remote_dns'}
    local, remote = MockDNS('192.0.2.10'), MockDNS('198.51.100.20')
    process = None
    try:
        settings = prepare_editor({
            'direct_rules': ['china'], 'prefer_ipv4': False, 'metrics_enabled': False,
            'cache_enabled': False, 'probe_cache_enabled': False, 'api': {'enabled': False},
            'local_dns': f'udp://127.0.0.1:{local.server_address[1]}',
            'remote_dns': f'udp://127.0.0.1:{remote.server_address[1]}',
        }, config)
        settings = apply_common_settings(settings, {'custom_matches': [custom]}, config)['settings']
        # New entries retain the existing tail default. Moving to head and back
        # must preserve the original condition and action through serialization.
        for index, (position, expected_ip, expected_hits) in enumerate([
            ('tail', '192.0.2.10', (2, 0)),
            ('head', '198.51.100.20', (0, 2)),
            ('tail', '192.0.2.10', (2, 0)),
        ]):
            if index:
                settings = apply_common_settings(settings, {'custom_match_position': position}, config)['settings']
            settings = yaml.safe_load(yaml.safe_dump(settings))
            values = read_common_settings(settings, config)['values']
            assert values['custom_matches'] == [custom]
            assert values['custom_match_position'] == position
            generated = yaml.safe_load(generate_oxidns_config({**config, 'oxidns': settings}))
            main = plugins(generated)['sequence_main']['args']
            assert custom in main
            direct_index = next(i for i, step in enumerate(main)
                                if step.get('exec') == 'goto china_dns' and step.get('matches'))
            assert (main.index(custom) < direct_index) is (position == 'head')
            dns_port = free_port()
            for plugin in generated['plugins']:
                if plugin['type'] in ('udp_server', 'tcp_server'):
                    plugin['args']['listen'] = f'127.0.0.1:{dns_port}'
            runtime = tmp_path / f'{index}-{position}.yaml'
            runtime.write_text(yaml.safe_dump(generated, sort_keys=False))
            checked = subprocess.run([BINARY, 'check', '-c', str(runtime), '-d', str(tmp_path)],
                                     capture_output=True, text=True, timeout=30)
            assert checked.returncode == 0, checked.stdout + checked.stderr
            log_path = tmp_path / f'{index}-{position}.log'
            with log_path.open('w') as log:
                process = subprocess.Popen([BINARY, 'start', '-c', str(runtime), '-d', str(tmp_path)],
                                           stdout=log, stderr=subprocess.STDOUT)
                for _ in range(100):
                    assert process.poll() is None, log_path.read_text()[-3000:]
                    try:
                        query(dns_port, 'ready.test', timeout=.3)
                        break
                    except OSError:
                        time.sleep(.05)
                else:
                    raise AssertionError('temporary OxiDNS did not answer')
                before = (local.count(name), remote.count(name))
                for tcp in (False, True):
                    reply = query(dns_port, name, tcp=tcp)
                    assert reply['rcode'] == 0 and reply['ips'] == [expected_ip], (position, tcp, reply)
                assert (local.count(name) - before[0], remote.count(name) - before[1]) == expected_hits
                process.terminate()
                process.wait(timeout=5)
                process = None
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        local.close()
        remote.close()
