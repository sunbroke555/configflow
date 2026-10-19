"""The delivered DNS watchdog is local without rewriting native profile drafts."""
from copy import deepcopy
import hashlib
import io
import tarfile

import pytest
import yaml

from backend.agents.deployment_bundle import build_deployment_bundle, deployment_content_fingerprint
from backend.common.dns_watchdog import WATCHDOG_NAME, WATCHDOG_RULE, ensure_local_dns_watchdog
from backend.converters.mosdns import generate_mosdns_config
from backend.converters.oxidns import generate_oxidns_config, get_oxidns_custom_files


def unpack(bundle):
    with tarfile.open(fileobj=io.BytesIO(bundle.archive), mode='r:gz') as archive:
        content = archive.extractfile('files/' + bundle.manifest['config_path']).read()
    entry = next(item for item in bundle.manifest['files'] if item['role'] == 'config')
    assert entry['size'] == len(content)
    assert entry['sha256'] == hashlib.sha256(content).hexdigest()
    assert bundle.config_version == entry['sha256'][:8]
    return yaml.safe_load(content)


def bundle_for(service, content, config=None, **kwargs):
    return build_deployment_bundle({'id': 'watchdog', 'profile_id': 'default', 'service_type': service},
                                   config or {}, content, deployment_id='watchdog-fixture', **kwargs)


@pytest.mark.parametrize('service,generate', [('oxidns', generate_oxidns_config), ('mosdns', generate_mosdns_config)])
def test_default_publication_guards_actual_listener_before_recording_or_forwarding(service, generate):
    config = {service: {}}
    before = deepcopy(config)
    source = generate(config)
    bundle = bundle_for(service, source, config)
    document = unpack(bundle)
    tagged = {p['tag']: p for p in document['plugins'] if p.get('tag')}
    for server in document['plugins']:
        if server['type'] not in ('udp_server', 'tcp_server'):
            continue
        entry = server['args']['entry']
        assert tagged[entry]['args'][0] == WATCHDOG_RULE
        original = next(p for p in yaml.safe_load(source)['plugins'] if p.get('tag') == entry)
        assert tagged[entry]['args'][1:] == original['args']
    assert config == before
    assert generate(config) == source
    assert WATCHDOG_NAME not in source  # User-authored/generated draft remains intact.
    again = bundle_for(service, yaml.safe_dump(document, sort_keys=False), config)
    assert unpack(again) == document
    assert deployment_content_fingerprint(bundle.manifest) == deployment_content_fingerprint(again.manifest)


def test_existing_native_includes_custom_entries_and_attachments_are_preserved():
    included = {'plugins': [
        {'tag': 'upstream', 'type': 'forward', 'args': {'upstreams': [{'addr': 'udp://127.0.0.1:55399'}]}},
        {'tag': 'custom_entry', 'type': 'sequence', 'args': [{'exec': '$upstream'}]},
    ]}
    native = {'runtime': {'worker_threads': 3}, 'include': ['parts/entry.yaml'], 'plugins': [
        {'tag': 'dns_v4', 'type': 'udp_server', 'args': {'listen': '127.0.0.1:15053', 'entry': 'custom_entry'}},
        {'tag': 'dns_v6', 'type': 'udp_server', 'args': {'listen': '[::1]:15054', 'entry': 'upstream'}},
        {'tag': 'dns_tcp', 'type': 'tcp_server', 'args': {'listen': '127.0.0.1:15053', 'entry': 'custom_entry'}},
    ]}
    config = {'oxidns': {'config_mode': 'native', 'native_config': yaml.safe_dump(native),
                        'additional_files': [{'path': 'parts/entry.yaml', 'content': yaml.safe_dump(included)}]}}
    before = deepcopy(config)
    content = generate_oxidns_config(config)
    bundle = bundle_for('oxidns', content, config, custom_files=get_oxidns_custom_files(config))
    document = unpack(bundle)
    plugins = {p['tag']: p for p in document['plugins']}
    assert plugins['custom_entry']['args'] == [WATCHDOG_RULE, {'exec': '$upstream'}]
    wrapper = plugins[plugins['dns_v6']['args']['entry']]
    assert wrapper['type'] == 'sequence'
    assert wrapper['args'] == [WATCHDOG_RULE, {'exec': '$upstream'}]
    assert plugins['upstream'] == included['plugins'][0]
    assert plugins['dns_v4'] == native['plugins'][0]
    assert plugins['dns_tcp'] == native['plugins'][2]
    assert document['runtime'] == native['runtime']
    assert config == before
    assert generate_oxidns_config(config) == content
    assert ensure_local_dns_watchdog(document) == document
    from backend.agents.oxidns_validation_receipt import validate_bundle
    assert validate_bundle('watchdog', bundle)['sha256'] == bundle.sha256


def test_direct_executor_wrapper_precedes_servers_and_avoids_user_tag_collisions():
    upstream = {'tag': 'remote', 'type': 'forward', 'args': {'upstreams': [{'addr': 'udp://127.0.0.1:55399'}]}}
    collision = 'cf_watchdog_' + hashlib.sha256(b'remote').hexdigest()[:16]
    source = {'plugins': [upstream,
        {'tag': collision, 'type': 'sequence', 'args': [{'exec': 'reject 5'}]},
        {'type': 'udp_server', 'args': {'listen': ':5300', 'entry': 'remote'}},
        {'type': 'tcp_server', 'args': {'listen': ':5300', 'entry': 'remote'}},
        {'tag': 'quic', 'type': 'quic_server', 'args': {'listen': ':853', 'entry': 'remote'}},
        {'tag': 'doh', 'type': 'http_server', 'args': {'listen': ':8053', 'entries': [
            {'path': '/dns-query', 'exec': 'remote'}, {'path': '/alternate', 'exec': 'remote'}]}},
    ]}
    document = ensure_local_dns_watchdog(source)
    wrapper = document['plugins'][2]
    assert wrapper['tag'] == collision + '_1'
    assert wrapper['args'] == [WATCHDOG_RULE, {'exec': '$remote'}]
    assert document['plugins'][:2] == source['plugins'][:2]
    assert all(p['args']['entry'] == wrapper['tag'] for p in document['plugins'][3:6])
    assert all(p['exec'] == wrapper['tag'] for p in document['plugins'][-1]['args']['entries'])
    assert ensure_local_dns_watchdog(document) == document
    assert source['plugins'][2]['args']['entry'] == 'remote'


def test_only_exact_probe_rule_moves_ahead_of_user_rules():
    broad_rule = {'matches': ['qname domain:invalid'], 'exec': 'reject 5'}
    source = {'plugins': [
        {'tag': 'entry', 'type': 'sequence', 'args': [
            {'exec': 'metrics_collector metrics'}, broad_rule, deepcopy(WATCHDOG_RULE),
            {'exec': 'reject 0'}, deepcopy(WATCHDOG_RULE)]},
        {'tag': 'dns', 'type': 'udp_server', 'args': {'listen': ':5300', 'entry': 'entry'}},
    ]}
    assert ensure_local_dns_watchdog(source)['plugins'][0]['args'] == [
        WATCHDOG_RULE, {'exec': 'metrics_collector metrics'}, broad_rule, {'exec': 'reject 0'}]


def test_imported_delivered_guard_is_not_a_custom_routing_rule():
    from backend.common.oxidns_common_routing import _prefix_end
    assert _prefix_end([WATCHDOG_RULE, {'exec': 'metrics_collector metrics'},
                        {'exec': '$query_recorder'}, {'exec': 'goto proxy_dns_seq'}]) == 3
    assert _prefix_end([{'matches': ['qname domain:invalid'], 'exec': 'reject 3'}]) == 0


def test_unresolved_external_include_is_left_to_core_and_watchdog_stays_disabled():
    document = {'include': ['external.yaml'], 'plugins': [
        {'type': 'udp_server', 'args': {'listen': ':5300', 'entry': 'external_entry'}}]}
    assert ensure_local_dns_watchdog(document) == document


def test_other_services_and_rule_only_legacy_bundles_are_unchanged():
    source = {'plugins': [{'tag': 'main', 'type': 'sequence', 'args': [{'exec': 'reject 0'}]},
                          {'type': 'udp_server', 'args': {'listen': ':5300', 'entry': 'main'}}]}
    assert unpack(bundle_for('mosdns', yaml.safe_dump(source), local_dns_watchdog=False)) == source
    mihomo = {'dns': {'enable': True, 'listen': '127.0.0.1:1053'}, 'rules': ['MATCH,DIRECT']}
    assert unpack(bundle_for('mihomo', yaml.safe_dump(mihomo))) == mihomo


@pytest.mark.parametrize('entry,listen', [('${ENTRY}', '127.0.0.1:5300'), ('main', '${LISTEN}')])
def test_native_runtime_environment_references_remain_publishable(entry, listen):
    source = {'plugins': [
        {'tag': 'main', 'type': 'sequence', 'args': [{'exec': 'reject 0'}]},
        {'tag': 'dns', 'type': 'udp_server', 'args': {'listen': listen, 'entry': entry}},
    ]}
    config = {'oxidns': {'config_mode': 'native', 'native_config': yaml.safe_dump(source)}}
    before = deepcopy(config)
    bundle = bundle_for('oxidns', generate_oxidns_config(config), config)
    document = unpack(bundle)
    assert document['plugins'][1] == source['plugins'][1]
    if entry.startswith('${'):
        assert document == source  # Do not guess which executor the core selects.
    else:
        assert document['plugins'][0]['args'] == [WATCHDOG_RULE, {'exec': 'reject 0'}]
    assert config == before
    from backend.agents.oxidns_validation_receipt import validate_bundle
    assert validate_bundle('watchdog', bundle)['sha256'] == bundle.sha256
