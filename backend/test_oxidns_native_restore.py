"""Native additions restore runnable owned edges without reviving withdrawn inputs."""
from copy import deepcopy

import pytest
import yaml

from backend.common.oxidns_native_apply import apply_native_settings
from backend.common.oxidns_settings import OxidnsSettingsError
from backend.common.oxidns_validation import validate_settings
from backend.test_oxidns_common import apply, doc, edited, plugin, prepared
from backend.test_oxidns_native_apply import all_features


OWNED_CHAINS = {
    'lazy_cache': ('china_dns',),
    'local_probe_cache': ('local_probe',),
    'query_recorder': ('sequence_main',),
    'ip_selector': ('china_dns', 'direct_ip', 'default_direct_ip'),
    'ros_address_list': ('china_dns', 'direct_ip', 'default_direct_ip'),
    'ros_route': ('china_dns', 'direct_ip', 'default_direct_ip'),
    'dynamic_domains': ('sequence_main', 'direct_ip'),
    'learn_classified_domain': ('direct_ip',),
    'custom_hosts': ('sequence_main',),
    'adguard_rules': ('sequence_main', 'adguard_reject'),
    'adguard_block': ('sequence_main', 'adguard_reject'),
    'adguard_reject': ('sequence_main',),
    'rules_download': ('rules_refresh', 'rules_cron'),
    'rules_reload': ('rules_refresh', 'rules_cron'),
    'rules_refresh': ('rules_cron',),
    'rules_cron': ('rules_refresh',),
    'forward_fallback': ('proxy_dns_seq', 'fallback_dns'),
}
STANDARD = ('lazy_cache', 'local_probe_cache', 'query_recorder', 'ip_selector',
            'ros_address_list', 'ros_route', 'dynamic_domains', 'learn_classified_domain',
            'custom_hosts', 'adguard_rules', 'rules_download')


def reconcile(settings, candidate, **intent):
    original_settings, original_candidate = deepcopy(settings), deepcopy(candidate)
    try:
        return apply_native_settings(settings, yaml.safe_dump(candidate, sort_keys=False), {}, **intent)
    finally:
        assert settings == original_settings
        assert candidate == original_candidate


def remove(settings, tag):
    candidate = doc(settings)
    candidate['plugins'] = [item for item in candidate['plugins'] if item['tag'] != tag]
    return reconcile(settings, candidate)['settings']


def append(settings, definition, **intent):
    candidate = doc(settings)
    candidate['plugins'].append(deepcopy(definition))
    return reconcile(settings, candidate, **intent)


def graph(settings):
    return {item['tag']: item for item in doc(settings)['plugins']}


@pytest.mark.parametrize('tag', tuple(OWNED_CHAINS))
def test_raw_definition_roundtrip_restores_owned_edges_order_and_parameters(tag):
    original = edited(all_features(), lambda value: plugin(value, tag).update(
        future_options={'preserved': [0, False, None]}))
    before = graph(original)
    disabled = remove(original, tag)
    restored = append(disabled, before[tag])
    after = graph(restored['settings'])
    assert after[tag] == before[tag]
    for chain in OWNED_CHAINS[tag]:
        assert after[chain]['args'] == before[chain]['args'], (tag, chain)
    # Neither the primary resolver nor domestic upstreams are dependencies to recreate.
    assert after['forward_local'] == before['forward_local']
    assert after['forward_remote'] == before['forward_remote']
    assert validate_settings(restored['settings'])['valid']
    assert restored['notices']
    repeated = reconcile(restored['settings'], doc(restored['settings']))
    assert doc(repeated['settings']) == doc(restored['settings'])


@pytest.mark.parametrize('tag', STANDARD)
def test_standard_empty_placeholder_restores_parked_definition_and_owned_edges(tag):
    original = edited(all_features(), lambda value: plugin(value, tag).update(
        future_options={'preserved': [0, False, None]}))
    before = graph(original)
    disabled = remove(original, tag)
    restored = append(disabled, {'tag': tag, 'type': before[tag]['type'], 'args': {}},
                      standard_add=[tag])
    after = graph(restored['settings'])
    assert after[tag] == before[tag]
    for chain in OWNED_CHAINS[tag]:
        assert after[chain]['args'] == before[chain]['args'], (tag, chain)
    assert validate_settings(restored['settings'])['valid']


def test_standard_restore_uses_current_fast_edits_over_parked_values_on_retry():
    original = edited(all_features(), lambda value: plugin(value, 'ros_route')['args'].update(
        timeout='17s', tls={'server_name': 'router.test', 'insecure': False}))
    disabled = remove(original, 'ros_route')
    candidate = doc(disabled)
    candidate['plugins'].append({'tag': 'ros_route', 'type': 'ros_route', 'args': {
        'timeout': '23s', 'tls': None, 'future': [False, 0, None]}})
    first = reconcile(disabled, candidate, standard_add=['ros_route'])
    retry = reconcile(disabled, candidate, standard_add=['ros_route'])
    args = graph(first['settings'])['ros_route']['args']
    assert args['timeout'] == '23s' and args['tls'] is None
    assert args['future'] == [False, 0, None]
    assert args['address'] == graph(original)['ros_route']['args']['address']
    assert args['password'] == graph(original)['ros_route']['args']['password']
    assert first['settings'] == retry['settings']
    for chain in ('china_dns', 'direct_ip', 'default_direct_ip'):
        assert graph(first['settings'])[chain]['args'].count({'exec': '$ros_route'}) == 1


def test_standard_new_feature_has_defaults_and_its_actual_main_entry():
    settings = prepared(query_recorder={'enabled': False})
    result = append(settings, {'tag': 'query_recorder', 'type': 'query_recorder', 'args': {}},
                    standard_add=['query_recorder'])
    after = graph(result['settings'])
    assert after['query_recorder']['args']
    assert after['sequence_main']['args'].count({'exec': '$query_recorder'}) == 1
    assert validate_settings(result['settings'])['valid']


def test_multiple_standard_additions_restore_options_even_with_empty_backup_placeholder():
    original = edited(all_features(), lambda value: (
        plugin(value, 'lazy_cache')['args'].update(size=321),
        plugin(value, 'query_recorder')['args'].update(queue_size=1234)))
    disabled = apply(original, {'cache_enabled': False, 'query_recorder.enabled': False,
                                'fallback_dns': ''})['settings']
    candidate = doc(disabled)
    candidate['plugins'].extend([
        {'tag': 'lazy_cache', 'type': 'cache', 'args': {}},
        {'tag': 'query_recorder', 'type': 'query_recorder', 'args': {}},
        {'tag': 'forward_fallback', 'type': 'forward', 'args': {}},
    ])
    result = reconcile(disabled, candidate,
                       standard_add=['lazy_cache', 'query_recorder', 'forward_fallback'])
    after = graph(result['settings'])
    assert after['lazy_cache']['args']['size'] == 321
    assert after['query_recorder']['args']['queue_size'] == 1234
    assert after['proxy_dns_seq']['args'] == [{'exec': 'goto remote_dns'}]
    assert after['china_dns']['args'].count({'exec': '$lazy_cache'}) == 1
    assert after['sequence_main']['args'].count({'exec': '$query_recorder'}) == 1


def test_provider_and_updater_standard_restore_can_be_combined_in_one_candidate():
    original = all_features()
    disabled = remove(original, 'adguard_rules')
    candidate = doc(disabled)
    candidate['plugins'].extend([
        {'tag': 'adguard_rules', 'type': 'adguard_rule', 'args': {}},
        {'tag': 'rules_download', 'type': 'download', 'args': {}},
    ])
    result = reconcile(disabled, candidate, standard_add=['adguard_rules', 'rules_download'])
    after, before = graph(result['settings']), graph(original)
    assert after['adguard_rules'] == before['adguard_rules']
    assert after['rules_download'] == before['rules_download']
    assert after['rules_refresh'] == before['rules_refresh']
    assert after['rules_cron'] == before['rules_cron']
    assert after['sequence_main']['args'] == before['sequence_main']['args']
    assert validate_settings(result['settings'])['valid']


def test_provider_fast_clear_before_combined_restore_cannot_download_withdrawn_source():
    disabled = remove(all_features(), 'adguard_rules')
    candidate = doc(disabled)
    candidate['plugins'].extend([
        {'tag': 'adguard_rules', 'type': 'adguard_rule', 'args': {'files': []}},
        {'tag': 'rules_download', 'type': 'download', 'args': {}},
    ])
    try:
        result = reconcile(disabled, candidate, standard_add=['adguard_rules', 'rules_download'])
    except OxidnsSettingsError:
        return
    after = graph(result['settings'])
    assert after['adguard_rules']['args']['files'] == []
    assert not after.get('rules_download', {}).get('args', {}).get('downloads')
    assert not any(job.get('executors') == ['$rules_refresh']
                   for job in after.get('rules_cron', {}).get('args', {}).get('jobs', []))


@pytest.mark.parametrize('helper', ['fallback', 'fallback_dns'])
def test_fallback_helper_without_live_upstreams_cannot_revive_cleared_dns(helper):
    original = all_features()
    before = graph(original)
    disabled = remove(original, helper)
    try:
        result = append(disabled, before[helper])
    except OxidnsSettingsError:
        return  # Refusing incomplete helper restoration is also safe.
    after = graph(result['settings'])
    assert after['proxy_dns_seq']['args'] == [{'exec': 'goto remote_dns'}]
    assert not after.get('forward_fallback', {}).get('args', {}).get('upstreams')
    assert after['forward_local'] == before['forward_local']
    assert after['forward_remote'] == before['forward_remote']
    assert result['notices']


@pytest.mark.parametrize('explicit', [{}, {'upstreams': []}, {'upstreams': [{'addr': '  '}]}])
def test_standard_backup_restore_keeps_explicitly_cleared_addresses_empty(explicit):
    original = all_features()
    disabled = apply(original, {'fallback_dns': ''})['settings']
    result = append(disabled, {'tag': 'forward_fallback', 'type': 'forward', 'args': explicit},
                    standard_add=['forward_fallback'])
    after = graph(result['settings'])
    assert after['proxy_dns_seq']['args'] == [{'exec': 'goto remote_dns'}]
    assert not any(isinstance(item, dict) and str(item.get('addr', '')).strip()
                   for item in after.get('forward_fallback', {}).get('args', {}).get('upstreams', []))
    assert after['forward_local'] == graph(original)['forward_local']


def test_standard_backup_fast_address_edit_connects_once_without_restoring_old_dns():
    original = all_features()
    disabled = apply(original, {'fallback_dns': ''})['settings']
    definition = {'tag': 'forward_fallback', 'type': 'forward', 'args': {
        'upstreams': [{'addr': 'tls://new-dns.example.test', 'future': False}], 'concurrent': 1}}
    result = append(disabled, definition, standard_add=['forward_fallback'])
    after = graph(result['settings'])
    assert after['forward_fallback']['args']['upstreams'] == definition['args']['upstreams']
    assert after['forward_fallback']['args']['concurrent'] == 1
    assert after['proxy_dns_seq']['args'] == [{'exec': '$fallback'}, {'exec': 'accept'}]
    assert after['forward_local'] == graph(original)['forward_local']


def test_independent_delete_and_standard_add_both_apply_in_one_draft():
    settings = apply(all_features(), {'query_recorder.enabled': False})['settings']
    candidate = doc(settings)
    candidate['plugins'] = [item for item in candidate['plugins'] if item['tag'] != 'lazy_cache']
    candidate['plugins'].append({'tag': 'query_recorder', 'type': 'query_recorder', 'args': {}})
    result = reconcile(settings, candidate, standard_add=['query_recorder'])
    after = graph(result['settings'])
    assert 'lazy_cache' not in after
    assert {'exec': '$lazy_cache'} not in after['china_dns']['args']
    assert after['sequence_main']['args'].count({'exec': '$query_recorder'}) == 1


def test_mixed_delete_does_not_swallow_an_unsafe_standard_add_error():
    settings = apply(all_features(), {'query_recorder.enabled': False})['settings']
    settings = edited(settings, lambda value: (
        value['plugins'].append({'tag': 'unused_custom', 'type': 'sequence', 'args': []}),
        plugin(value, 'sequence_main').update(type='future_sequence')))
    candidate = doc(settings)
    candidate['plugins'] = [item for item in candidate['plugins'] if item['tag'] != 'unused_custom']
    candidate['plugins'].append({'tag': 'query_recorder', 'type': 'query_recorder', 'args': {}})
    with pytest.raises(OxidnsSettingsError):
        reconcile(settings, candidate, standard_add=['query_recorder'])


def test_deleting_dependency_and_adding_its_consumer_does_not_undo_the_deletion():
    settings = apply(all_features(), {'dynamic_domain_set.learn': False})['settings']
    candidate = doc(settings)
    candidate['plugins'] = [item for item in candidate['plugins'] if item['tag'] != 'dynamic_domains']
    candidate['plugins'].append({'tag': 'learn_classified_domain', 'type': 'learn_domain', 'args': {}})
    with pytest.raises(OxidnsSettingsError):
        reconcile(settings, candidate, standard_add=['learn_classified_domain'])


@pytest.mark.parametrize('hazard', ['include', 'env', 'custom-call', 'duplicate-call'])
def test_standard_restore_does_not_rewire_external_or_custom_graphs(hazard):
    settings = apply(all_features(), {'cache_enabled': False})['settings']
    candidate = doc(settings)
    candidate['plugins'].append({'tag': 'lazy_cache', 'type': 'cache', 'args': {}})
    if hazard == 'include':
        candidate['include'] = ['extra.yaml']
        settings['additional_files'] = [{'path': 'extra.yaml', 'content': 'plugins: []\n', 'encoding': 'text'}]
    elif hazard == 'env':
        candidate['plugins'].append({'tag': 'custom', 'type': 'sequence', 'args': [{'exec': '${EXECUTOR}'}]})
    elif hazard == 'custom-call':
        candidate['plugins'].append({'tag': 'custom', 'type': 'fallback', 'args': {
            'primary': 'lazy_cache', 'secondary': 'forward_remote'}})
    else:
        plugin(candidate, 'china_dns')['args'][:0] = [{'exec': '$lazy_cache'}, {'exec': '$lazy_cache'}]
    with pytest.raises(OxidnsSettingsError):
        reconcile(settings, candidate, standard_add=['lazy_cache'])


def test_empty_include_and_non_reference_env_values_do_not_block_restore():
    settings = apply(all_features(), {'cache_enabled': False})['settings']
    candidate = doc(settings)
    candidate['include'] = []
    plugin(candidate, 'forward_local')['args']['upstreams'][0]['addr'] = '${LOCAL_DNS}'
    plugin(candidate, 'ros_route')['args']['password'] = '${ROS_PASSWORD}'
    candidate['plugins'].append({'tag': 'lazy_cache', 'type': 'cache', 'args': {}})
    result = reconcile(settings, candidate, standard_add=['lazy_cache'])
    after = graph(result['settings'])
    assert after['china_dns']['args'].count({'exec': '$lazy_cache'}) == 1
    assert after['forward_local']['args']['upstreams'][0]['addr'] == '${LOCAL_DNS}'
    assert after['ros_route']['args']['password'] == '${ROS_PASSWORD}'


def test_rename_to_canonical_only_renames_existing_edges_and_does_not_add_owned_calls():
    settings = apply(all_features(), {'cache_enabled': False})['settings']
    settings = edited(settings, lambda value: value['plugins'].extend([
        {'tag': 'custom_cache', 'type': 'cache', 'args': {'size': 321}},
        {'tag': 'custom_sequence', 'type': 'sequence', 'args': [{'exec': '$custom_cache'}]},
    ]))
    before = graph(settings)
    candidate = doc(settings)
    plugin(candidate, 'custom_cache')['tag'] = 'lazy_cache'
    result = reconcile(settings, candidate, renames={'custom_cache': 'lazy_cache'})
    after = graph(result['settings'])
    assert after['custom_sequence']['args'] == [{'exec': '$lazy_cache'}]
    assert after['china_dns'] == before['china_dns']
    assert after['lazy_cache']['args'] == {'size': 321}


@pytest.mark.parametrize('has_custom_call', [False, True])
def test_rename_to_backup_canonical_does_not_activate_proxy_fallback(has_custom_call):
    settings = prepared()
    candidate = doc(settings)
    candidate['plugins'].append({'tag': 'custom_forward', 'type': 'forward',
                                 'args': {'upstreams': [{'addr': 'udp://192.0.2.99'}]}})
    if has_custom_call:
        candidate['plugins'].append({'tag': 'custom_sequence', 'type': 'sequence',
                                     'args': [{'exec': '$custom_forward'}]})
    settings = {**settings, 'native_config': yaml.safe_dump(candidate, sort_keys=False)}
    before = graph(settings)
    plugin(candidate, 'custom_forward')['tag'] = 'forward_fallback'
    result = reconcile(settings, candidate, renames={'custom_forward': 'forward_fallback'})
    after = graph(result['settings'])
    assert after['proxy_dns_seq'] == before['proxy_dns_seq']
    assert 'fallback' not in after and 'fallback_dns' not in after
    if has_custom_call:
        assert after['custom_sequence']['args'] == [{'exec': '$forward_fallback'}]


def test_filling_existing_backup_while_removing_its_helper_has_a_controlled_error():
    original = all_features()
    settings = apply(original, {'fallback_dns': ''})['settings']
    settings = edited(settings, lambda value: value['plugins'].extend([
        {'tag': 'forward_fallback', 'type': 'forward', 'args': {'upstreams': []}},
        deepcopy(graph(original)['fallback']),
    ]))
    candidate = doc(settings)
    candidate['plugins'] = [item for item in candidate['plugins'] if item['tag'] != 'fallback']
    plugin(candidate, 'forward_fallback')['args']['upstreams'] = [{'addr': 'udp://192.0.2.99'}]
    with pytest.raises(OxidnsSettingsError):
        reconcile(settings, candidate)


def test_custom_instance_addition_has_no_implicit_call_or_managed_restore():
    settings = apply(all_features(), {'cache_enabled': False})['settings']
    candidate = doc(settings)
    candidate['plugins'].append({'tag': 'lazy_cache_custom', 'type': 'cache', 'args': {'size': 321}})
    result = reconcile(settings, candidate)
    assert doc(result['settings']) == candidate
    assert 'lazy_cache' not in graph(result['settings'])


def test_updater_restore_without_current_resources_never_revives_withdrawn_downloads():
    settings = prepared(adguard={'enabled': True, 'urls': ['https://example.test/withdrawn']},
                        rule_update={'enabled': True})
    settings = apply(settings, {'rule_update.enabled': False})['settings']
    settings = apply(settings, {'adguard.urls': [], 'adguard.content': '', 'adguard.enabled': False})['settings']
    candidate = doc(settings)
    candidate['plugins'].append({'tag': 'rules_download', 'type': 'download', 'args': {}})
    try:
        result = reconcile(settings, candidate, standard_add=['rules_download'])
    except OxidnsSettingsError:
        return
    after = graph(result['settings'])
    assert not after.get('rules_download', {}).get('args', {}).get('downloads')
    assert not any(job.get('executors') == ['$rules_refresh']
                   for job in after.get('rules_cron', {}).get('args', {}).get('jobs', []))
    assert 'https://example.test/withdrawn' not in yaml.safe_dump(doc(result['settings']))
