"""Common controls edit one native draft without rewriting unrelated state."""
from copy import deepcopy

import pytest
import yaml

from backend.common.oxidns_common import read_common_settings, apply_common_settings
from backend.common.oxidns_editor import prepare_editor
from backend.common.oxidns_settings import BLOCK_QTYPES_EXEC, OxidnsSettingsError
from backend.common.oxidns_validation import validate_native_config
from backend.test_oxidns_integration import oxidns_app


def prepared(**values):
    return prepare_editor(values, {})


def doc(settings):
    return yaml.safe_load(settings['native_config'])


def plugin(document, tag):
    return next(item for item in document['plugins'] if item['tag'] == tag)


def edited(settings, callback):
    settings = deepcopy(settings)
    document = doc(settings)
    callback(document)
    settings['native_config'] = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    return settings


def apply(settings, changes, config=None):
    return apply_common_settings(settings, changes, config or {})


def test_reads_current_native_values_instead_of_old_template_inputs():
    settings = prepared(worker_threads=2)
    settings = edited(settings, lambda value: (
        value['runtime'].update(worker_threads=9),
        plugin(value, 'forward_local')['args'].update(concurrent=4, upstreams=[{'addr': 'udp://192.0.2.53', 'future': [False, 0, None]}]),
        plugin(value, 'lazy_cache')['args'].update(size=700),
    ))
    settings['template_settings'].update(worker_threads=99, local_concurrent=99, cache_size=99)
    read = read_common_settings(settings, {})
    assert read['values']['worker_threads'] == 9
    assert read['values']['local_concurrent'] == 4
    assert read['values']['cache_size'] == 700
    assert yaml.safe_load(read['values']['local_dns'])[0]['future'] == [False, 0, None]


def test_batch_updates_only_requested_fields_and_preserves_unknown_values_files_backups():
    settings = prepared(api={'enabled': True, 'listen': '127.0.0.1:9199'})
    settings = edited(settings, lambda value: (
        value.update(future={'zero': 0, 'false': False, 'null': None}),
        plugin(value, 'lazy_cache')['args'].update(extension={'keep': [0, False, None]}),
        value['plugins'].append({'tag': 'manual', 'type': 'future', 'args': {'value': None}}),
    ))
    settings['legacy_native_config'] = 'backup as typed'
    settings['additional_files'] = [{'path': 'private.txt', 'content': 'file content', 'encoding': 'text'}]
    original = deepcopy(settings)
    result = apply(settings, {'worker_threads': 5, 'cache_size': 300, 'api.listen': '0.0.0.0:9199'})
    expected = doc(settings)
    expected['runtime']['worker_threads'] = 5
    expected['api']['http']['listen'] = '0.0.0.0:9199'
    plugin(expected, 'lazy_cache')['args']['size'] = 300
    assert doc(result['settings']) == expected
    assert settings == original
    for key in ('legacy_native_config', 'additional_files', 'template_settings', 'template_base_config'):
        assert result['settings'][key] == settings[key]


def test_missing_native_fields_are_unset_not_template_defaults():
    settings = {'config_mode': 'native', 'native_config': 'plugins: []\n', 'worker_threads': 91}
    result = read_common_settings(settings, {})
    assert result['values']['worker_threads'] is None
    assert result['capabilities']['worker_threads']['absent'] is True
    assert result['values']['cache_size'] is None
    assert result['values']['query_recorder']['enabled'] is False
    assert result['values']['query_recorder']['args']['queue_size'] is None
    assert not result['capabilities']['query_recorder.enabled']['editable']
    changed = apply(settings, {'worker_threads': 3})
    assert doc(changed['settings']) == {'runtime': {'worker_threads': 3}, 'plugins': []}


def test_unrelated_scalar_remains_editable_when_routing_is_custom():
    settings = edited(prepared(), lambda value: plugin(value, 'sequence_main')['args'].append({'exec': '$unusual', 'future': True}))
    result = apply(settings, {'worker_threads': 8})
    assert plugin(doc(result['settings']), 'sequence_main') == plugin(doc(settings), 'sequence_main')


@pytest.mark.parametrize('feature,chains', [
    ('query_recorder', ['sequence_main']),
    ('ip_selector', ['china_dns', 'direct_ip', 'default_direct_ip']),
    ('ros_address_list', ['china_dns', 'direct_ip', 'default_direct_ip']),
    ('ros_route', ['china_dns', 'direct_ip', 'default_direct_ip']),
])
def test_feature_enable_connects_expected_chains_disable_preserves_arguments(feature, chains):
    settings = prepared()
    result = apply(settings, {f'{feature}.enabled': True})
    document = doc(result['settings'])
    for chain in chains:
        assert {'exec': f'${feature}'} in plugin(document, chain)['args']
    existing = deepcopy(plugin(document, feature))
    result = apply(result['settings'], {f'{feature}.enabled': False})
    document = doc(result['settings'])
    assert not any(item['tag'] == feature for item in document['plugins'])
    assert result['settings']['_common_disabled'][feature] == existing
    assert not [issue for issue in validate_native_config(document) if issue['severity'] == 'error']
    assert result['values'][feature]['enabled'] is False
    for chain in chains:
        assert {'exec': f'${feature}'} not in plugin(document, chain)['args']
    restored = apply(result['settings'], {f'{feature}.enabled': True})
    assert plugin(doc(restored['settings']), feature) == existing


def test_custom_feature_caller_is_preserved_and_toggle_is_explained_and_blocked():
    settings = apply(prepared(), {'ip_selector.enabled': True})['settings']
    settings = edited(settings, lambda value: value['plugins'].append({'tag': 'manual_cron', 'type': 'cron', 'args': {'jobs': [{'executors': ['$ip_selector']}]}}))
    read = read_common_settings(settings, {})
    assert read['values']['ip_selector']['enabled'] is True
    assert not read['capabilities']['ip_selector.enabled']['editable']
    assert read['capabilities']['ip_selector.enabled']['reason']
    with pytest.raises(OxidnsSettingsError, match='自定义'):
        apply(settings, {'ip_selector.enabled': False})
    result = apply(settings, {'ip_selector.args.top_n': 7})
    assert plugin(doc(result['settings']), 'manual_cron') == plugin(doc(settings), 'manual_cron')
    assert plugin(doc(result['settings']), 'ip_selector')['args']['top_n'] == 7


def test_batch_enable_feature_and_edit_parameters_uses_new_native_instance():
    result = apply(prepared(), {'query_recorder.enabled': True, 'query_recorder.args.path': './state/custom.db', 'query_recorder.args.memory_tail': 0})
    args = plugin(doc(result['settings']), 'query_recorder')['args']
    assert args['path'] == './state/custom.db'
    assert args['memory_tail'] == 0
    assert result['values']['query_recorder']['args']['path'] == './state/custom.db'


def test_optional_null_deletes_only_requested_field_and_preserves_siblings():
    settings = apply(prepared(), {'ros_address_list.enabled': True, 'ros_address_list.args.tls': {'ca': 'a.pem', 'insecure': False, 'future': 0}})['settings']
    result = apply(settings, {'ros_address_list.args.tls.ca': None})
    args = plugin(doc(result['settings']), 'ros_address_list')['args']
    assert args['tls'] == {'insecure': False, 'future': 0}
    result = apply(result['settings'], {'ros_address_list.args.tls': None})
    assert 'tls' not in plugin(doc(result['settings']), 'ros_address_list')['args']
    assert result['values']['ros_address_list']['args']['tls'] is None


def test_api_off_and_on_preserves_native_parameters_but_not_active_listener():
    settings = prepared(api={'enabled': True, 'listen': '127.0.0.1:9199', 'username': 'local', 'password': 'secret'})
    settings = edited(settings, lambda value: value['api']['http'].update(future={'retain': False}))
    original = doc(settings)['api']['http']
    off = apply(settings, {'api.enabled': False})
    assert 'api' not in doc(off['settings'])
    assert off['values']['api']['enabled'] is False
    assert off['values']['api']['listen'] == ''
    assert not off['capabilities']['api.listen']['editable']
    on = apply(off['settings'], {'api.enabled': True, 'api.listen': '0.0.0.0:9393'})
    assert doc(on['settings'])['api']['http'] == {**original, 'listen': '0.0.0.0:9393'}


@pytest.mark.parametrize('http', ['127.0.0.1:9199', {'listen': '127.0.0.1:9199', 'auth': {'type': 'jwt', 'token': 'keep'}}])
def test_api_native_variants_never_project_through_basic_auth(http):
    settings = edited(prepared(), lambda value: value.update(api={'http': http}))
    read = read_common_settings(settings, {})
    assert not read['capabilities']['api.username']['editable']
    with pytest.raises(OxidnsSettingsError):
        apply(settings, {'api.username': 'wrong'})
    result = apply(settings, {'worker_threads': 4})
    assert doc(result['settings'])['api']['http'] == http


def test_tcp_off_on_keeps_unknown_listener_fields_and_listener_state():
    settings = edited(prepared(), lambda value: (plugin(value, 'server_tcp')['args'].update(idle_timeout='2m', future=0), plugin(value, 'server_tcp').update(extension=[False, 0])))
    original = plugin(doc(settings), 'server_tcp')
    off = apply(settings, {'tcp_enabled': False})
    assert not any(item['tag'] == 'server_tcp' for item in doc(off['settings'])['plugins'])
    on = apply(off['settings'], {'tcp_enabled': True})
    assert plugin(doc(on['settings']), 'server_tcp') == original


def test_metrics_ipv4_and_qtypes_follow_actual_execution_chain():
    settings = prepared()
    off = apply(settings, {'metrics_enabled': False, 'prefer_ipv4': False, 'block_qtypes': []})
    main = plugin(doc(off['settings']), 'sequence_main')['args']
    assert not any(item.get('exec') in ('metrics_collector metrics', 'prefer_ipv4', BLOCK_QTYPES_EXEC) for item in main)
    restored = apply(off['settings'], {'metrics_enabled': True, 'prefer_ipv4': True, 'block_qtypes': [28, 65]})
    main = plugin(doc(restored['settings']), 'sequence_main')['args']
    assert main[:3] == [{'exec': 'metrics_collector metrics'}, {'exec': 'prefer_ipv4'},
                        {'matches': ['qtype 28 65'], 'exec': BLOCK_QTYPES_EXEC}]
    custom = edited(restored['settings'], lambda value: plugin(value, 'sequence_main')['args'][1].update(matches=['qname full:example.test']))
    assert not read_common_settings(custom, {})['capabilities']['prefer_ipv4']['editable']


def test_blocked_qtypes_written_by_earlier_versions_stay_editable():
    def legacy(value):
        rule = next(item for item in plugin(value, 'sequence_main')['args'] if item.get('exec') == BLOCK_QTYPES_EXEC)
        rule['exec'] = 'reject 0'
    settings = edited(prepared(), legacy)
    common = read_common_settings(settings, {})
    assert common['values']['block_qtypes'] == [65, 255]
    assert common['capabilities']['block_qtypes']['editable']
    unrelated = apply(settings, {'metrics_enabled': False})
    assert {'matches': ['qtype 65 255'], 'exec': 'reject 0'} in plugin(doc(unrelated['settings']), 'sequence_main')['args']
    saved = apply(settings, {'block_qtypes': [65]})
    main = plugin(doc(saved['settings']), 'sequence_main')['args']
    assert {'matches': ['qtype 65'], 'exec': BLOCK_QTYPES_EXEC} in main
    assert not any(item.get('exec') == 'reject 0' for item in main)


def test_dynamic_learning_tracks_route_and_preserves_existing_args():
    on = apply(prepared(), {'dynamic_domain_set.enabled': True, 'dynamic_domain_set.learn': True, 'dynamic_domain_set.args.path': './state/learning.txt'})
    document = doc(on['settings'])
    assert {'exec': '$learn_classified_domain'} in plugin(document, 'direct_ip')['args']
    moved = apply(on['settings'], {'dynamic_domain_set.route': 'proxy'})
    document = doc(moved['settings'])
    assert {'exec': '$learn_classified_domain'} not in plugin(document, 'direct_ip')['args']
    assert {'exec': '$learn_classified_domain'} in plugin(document, 'ip_requery_proxy')['args']
    off = apply(moved['settings'], {'dynamic_domain_set.enabled': False})
    document = doc(off['settings'])
    assert not any(item['tag'] in ('dynamic_domains', 'learn_classified_domain') for item in document['plugins'])
    assert off['settings']['_common_disabled']['dynamic_domains']['args']['path'] == './state/learning.txt'
    assert off['values']['dynamic_domain_set']['enabled'] is False
    restored = apply(off['settings'], {'dynamic_domain_set.enabled': True})
    assert restored['values']['dynamic_domain_set']['route'] == 'proxy'
    assert restored['values']['dynamic_domain_set']['learn'] is True
    assert plugin(doc(restored['settings']), 'dynamic_domains')['args']['path'] == './state/learning.txt'


def test_resource_content_reflects_actual_provider_references():
    settings = prepared(custom_hosts='full:one.test 192.0.2.1\n', adguard={'enabled': True, 'content': '||ads.test^', 'urls': ['https://example.test/filter.txt']})
    read = read_common_settings(settings, {})
    assert read['values']['custom_hosts'] == 'full:one.test 192.0.2.1\n'
    assert read['values']['adguard']['urls'] == ['https://example.test/filter.txt']
    settings = edited(settings, lambda value: plugin(value, 'adguard_rules')['args'].update(files=['./rules/custom-source.txt']))
    read = read_common_settings(settings, {})
    assert read['values']['adguard']['urls'] == []
    assert not read['capabilities']['adguard.urls']['editable']
    assert read['values']['adguard']['content'] == ''


def test_clearing_hosts_removes_managed_references_and_retains_other_source_consumers():
    settings = prepared(custom_hosts='full:one.test 192.0.2.1\n')
    result = apply(settings, {'custom_hosts': ''})
    document = doc(result['settings'])
    assert not any(item['tag'] == 'custom_hosts' for item in document['plugins'])
    assert {'exec': '$custom_hosts'} not in plugin(document, 'sequence_main')['args']
    assert result['settings']['custom_hosts'] == ''
    reenabled = apply(result['settings'], {'custom_hosts': 'full:two.test 192.0.2.2\n'})
    assert reenabled['values']['custom_hosts'] == 'full:two.test 192.0.2.2\n'
    assert plugin(doc(reenabled['settings']), 'custom_hosts')['args']['files'] == ['./rules/custom_hosts.txt']
    settings = edited(settings, lambda value: value['plugins'].append({'tag': 'manual_hosts', 'type': 'hosts', 'args': {'files': ['./rules/custom_hosts.txt']}}))
    result = apply(settings, {'custom_hosts': ''})
    assert result['values']['custom_hosts'] == ''
    assert result['settings']['custom_hosts'] == settings['custom_hosts']
    assert plugin(doc(result['settings']), 'manual_hosts') == plugin(doc(settings), 'manual_hosts')


def test_filter_toggle_never_rewrites_unknown_files():
    settings = prepared(adguard={'enabled': True, 'content': '||ads.test^'})
    settings = edited(settings, lambda value: plugin(value, 'adguard_rules')['args'].update(files=['rules/external.txt'], extension={'preserve': 0}))
    read = read_common_settings(settings, {})
    assert not read['capabilities']['adguard.urls']['editable']
    assert read['capabilities']['adguard.enabled']['editable']
    off = apply(settings, {'adguard.enabled': False})
    assert not any(item['tag'] == 'adguard_rules' for item in doc(off['settings'])['plugins'])
    assert off['settings']['_common_disabled']['adguard_rules'] == plugin(doc(settings), 'adguard_rules')
    on = apply(off['settings'], {'adguard.enabled': True})
    assert plugin(doc(on['settings']), 'adguard_rules') == plugin(doc(settings), 'adguard_rules')


@pytest.mark.parametrize('field,feature,call', [
    ('adguard.enabled', 'adguard', {'matches': ['question $adguard_rules'], 'exec': 'goto adguard_reject'}),
    ('dynamic_domain_set.enabled', 'dynamic_domain_set', {'matches': ['qname $dynamic_domains'], 'exec': 'goto china_dns'}),
])
def test_custom_filter_or_dynamic_callers_disable_unsafe_common_switch(field, feature, call):
    settings = prepared()
    settings = edited(settings, lambda value: value['plugins'].append({'tag': 'manual', 'type': 'sequence', 'args': [call]}))
    read = read_common_settings(settings, {})
    assert read['values'][feature]['enabled'] is True
    assert not read['capabilities'][field]['editable']
    with pytest.raises(OxidnsSettingsError):
        apply(settings, {field: False})


def test_custom_cron_argument_shapes_do_not_block_unrelated_scalar_edits():
    settings = edited(prepared(), lambda value: value['plugins'].append({'tag': 'rules_cron', 'type': 'cron', 'args': {'jobs': [{'executors': None}]}}))
    assert apply(settings, {'worker_threads': 7})['values']['worker_threads'] == 7


def test_boolean_to_number_change_is_serialized_with_native_type():
    settings = edited(prepared(), lambda value: value['runtime'].update(worker_threads=True))
    result = apply(settings, {'worker_threads': 1})
    assert type(doc(result['settings'])['runtime']['worker_threads']) is int


def test_read_and_apply_endpoints_do_not_persist_and_return_no_store(oxidns_app):
    app, repository = oxidns_app
    client = app.test_client()
    before = repository.get_profile('office')
    settings = prepared(worker_threads=3)
    response = client.post('/api/oxidns/common-settings?profile=office', json={'settings': settings})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()['values']['worker_threads'] == 3
    assert response.headers['Cache-Control'] == 'no-store'
    response = client.post('/api/oxidns/common-settings/apply?profile=office', json={'settings': settings, 'changes': {'worker_threads': 6, 'cache_size': 700}})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()['values']['worker_threads'] == 6
    assert response.get_json()['values']['cache_size'] == 700
    assert repository.get_profile('office') == before


def test_invalid_batch_is_atomic_and_unknown_fields_are_rejected():
    settings = prepared()
    before = deepcopy(settings)
    for changes in ({'worker_threads': 9, 'unknown': True}, {'worker_threads': 9, 'cache_size': 'unfinished'}):
        with pytest.raises(OxidnsSettingsError):
            apply(settings, changes)
    assert settings == before


def test_semantic_invalid_common_value_is_retained_in_draft_and_reported_by_shared_rules():
    settings = prepared()
    before = deepcopy(settings)
    result = apply(settings, {'worker_threads': 9, 'cache_size': -1})
    document = doc(result['settings'])
    assert document['runtime']['worker_threads'] == 9
    assert plugin(document, 'lazy_cache')['args']['size'] == -1
    cache_index = document['plugins'].index(plugin(document, 'lazy_cache'))
    assert any(issue['path'] == ['plugins', cache_index, 'args', 'size'] and issue['severity'] == 'error'
               for issue in validate_native_config(document))
    assert settings == before


def test_unfinished_listener_does_not_block_common_draft_edits():
    settings = edited(prepared(), lambda value: value['plugins'].append({'tag': 'http_incomplete', 'type': 'http_server', 'args': {}}))
    result = apply(settings, {'worker_threads': 6})
    assert plugin(doc(result['settings']), 'http_incomplete')['args'] == {}


def test_disabling_last_rule_update_job_removes_cron_and_restores_its_settings():
    settings = prepared(adguard={'enabled': True, 'urls': ['https://example.test/filter.txt']})
    enabled = apply(settings, {'rule_update.enabled': True, 'rule_update.args.schedule': '5 2 * * *',
                               'rule_update.args.timezone': 'UTC'})['settings']
    enabled = edited(enabled, lambda value: plugin(value, 'rules_cron').update(description='keep metadata'))
    original = doc(enabled)
    for _ in range(2):
        before = deepcopy(enabled)
        disabled = apply(enabled, {'rule_update.enabled': False})
        assert enabled == before
        expected = deepcopy(original)
        expected['plugins'] = [item for item in expected['plugins'] if item['tag'] != 'rules_cron']
        assert doc(disabled['settings']) == expected
        assert disabled['values']['rule_update']['enabled'] is False
        assert not [issue for issue in validate_native_config(doc(disabled['settings'])) if issue['severity'] == 'error']
        enabled = apply(disabled['settings'], {'rule_update.enabled': True})['settings']
        assert doc(enabled) == original


@pytest.mark.parametrize('changes', [
    {'rule_update.enabled': False, 'rule_update.args.timezone': 'UTC', 'rule_update.args.schedule': '5 2 * * *'},
    {'rule_update.args.schedule': '5 2 * * *', 'rule_update.args.timezone': 'UTC', 'rule_update.enabled': False},
])
def test_disabling_rule_update_with_argument_changes_does_not_recreate_empty_cron(changes):
    settings = prepared(adguard={'enabled': True, 'urls': ['https://example.test/filter.txt']})
    enabled = apply(settings, {'rule_update.enabled': True})['settings']
    disabled = apply(enabled, changes)['settings']
    assert not any(item['tag'] == 'rules_cron' for item in doc(disabled)['plugins'])
    restored = apply(disabled, {'rule_update.enabled': True})['settings']
    args = plugin(doc(restored), 'rules_cron')['args']
    assert args['timezone'] == 'UTC'
    assert args['jobs'] == [{'name': 'refresh_rules', 'schedule': '5 2 * * *', 'executors': ['$rules_refresh']}]


def test_rule_update_timezone_edits_while_disabled_remain_parked():
    settings = prepared(adguard={'enabled': True, 'urls': ['https://example.test/filter.txt']})
    enabled = apply(settings, {'rule_update.enabled': True})['settings']
    enabled = edited(enabled, lambda value: plugin(value, 'rules_cron').update(description='keep metadata'))
    disabled = apply(enabled, {'rule_update.enabled': False})['settings']
    changed = apply(disabled, {'rule_update.args.timezone': 'UTC'})['settings']
    assert not any(item['tag'] == 'rules_cron' for item in doc(changed)['plugins'])
    restored = apply(changed, {'rule_update.enabled': True})['settings']
    cron = plugin(doc(restored), 'rules_cron')
    assert cron['description'] == 'keep metadata'
    assert cron['args']['timezone'] == 'UTC'
    assert len(cron['args']['jobs']) == 1


def test_disabling_rule_update_repairs_legacy_empty_cron():
    settings = edited(prepared(), lambda value: value['plugins'].append(
        {'tag': 'rules_cron', 'type': 'cron', 'args': {'timezone': 'UTC', 'jobs': []}}))
    before = deepcopy(settings)
    result = apply(settings, {'rule_update.enabled': False})
    assert not any(item['tag'] == 'rules_cron' for item in doc(result['settings'])['plugins'])
    assert not [issue for issue in validate_native_config(doc(result['settings'])) if issue['severity'] == 'error']
    assert settings == before


@pytest.mark.parametrize('renamed', [
    ('rules_download', 'rules_reload', 'rules_refresh'),
    ('rules_download', 'rules_refresh'),
    ('rules_refresh',),
])
def test_imported_custom_updater_does_not_get_a_second_managed_chain(renamed):
    settings = prepared(adguard={'enabled': True, 'urls': ['https://example.test/filter.txt']})
    for tag in renamed:
        settings['native_config'] = settings['native_config'].replace(tag, tag.replace('rules_', 'custom_'))
    before = deepcopy(settings)
    changed = apply(settings, {'adguard.mode': 'refused'})['settings']
    assert not any(item['tag'] in (*renamed, 'rules_cron') for item in doc(changed)['plugins'])
    disabled = apply(settings, {'rule_update.enabled': False})['settings']
    assert doc(disabled) == doc(settings)
    with pytest.raises(OxidnsSettingsError, match='自定义规则更新'):
        apply(settings, {'rule_update.enabled': True})
    assert settings == before


def test_resource_edit_does_not_overwrite_an_unrelated_standard_tag_collision():
    settings = prepared(adguard={'enabled': True, 'urls': ['https://example.test/filter.txt']})
    settings = edited(settings, lambda d: d.update(plugins=[p for p in d['plugins']
        if p['tag'] not in ('rules_download', 'rules_reload', 'rules_refresh')]))
    settings = edited(settings, lambda d: d['plugins'].append(
        {'tag': 'rules_refresh', 'type': 'forward', 'args': {'upstreams': [{'addr': 'udp://192.0.2.53'}]}}))
    changed = apply(settings, {'adguard.mode': 'refused'})['settings']
    assert plugin(doc(changed), 'rules_refresh') == plugin(doc(settings), 'rules_refresh')
    assert not any(item['tag'] in ('rules_download', 'rules_reload', 'rules_cron') for item in doc(changed)['plugins'])


def test_rule_update_reads_actual_job_and_preserves_other_jobs(oxidns_app):
    _, repository = oxidns_app
    config = repository.get_compat_config('office')
    settings = prepare_editor(config['oxidns'], config, 'http://config.test')
    result = apply_common_settings(settings, {'rule_update.enabled': True, 'rule_update.args.schedule': '5 2 * * *', 'rule_update.args.timeout': '9s'}, config, 'http://config.test')
    settings = edited(result['settings'], lambda value: plugin(value, 'rules_cron')['args']['jobs'].append({'name': 'custom', 'schedule': '0 * * * *', 'executors': ['$private']}))
    result = apply_common_settings(settings, {'rule_update.enabled': False}, config)
    cron = plugin(doc(result['settings']), 'rules_cron')
    assert cron['args']['jobs'] == [{'name': 'custom', 'schedule': '0 * * * *', 'executors': ['$private']}]
    assert result['values']['rule_update']['args']['schedule'] is None
    result = apply_common_settings(result['settings'], {'rule_update.enabled': True}, config)
    assert len(plugin(doc(result['settings']), 'rules_cron')['args']['jobs']) == 2
    assert plugin(doc(result['settings']), 'rules_download')['args']['timeout'] == '9s'


def test_batch_filter_subscription_and_updater_create_consistent_resource_pipeline():
    result = apply(prepared(), {'adguard.enabled': True, 'adguard.urls': ['https://one.test/filter.txt'], 'rule_update.enabled': True})
    document = doc(result['settings'])
    downloads = plugin(document, 'rules_download')['args']['downloads']
    assert downloads[0]['url'] == 'https://one.test/filter.txt'
    assert '$adguard_rules' in plugin(document, 'rules_reload')['args']
    result = apply(result['settings'], {'adguard.urls': ['https://two.test/filter.txt']})
    downloads = plugin(doc(result['settings']), 'rules_download')['args']['downloads']
    assert [item['url'] for item in downloads] == ['https://two.test/filter.txt']


def test_same_tag_wrong_plugin_type_disables_only_affected_controls():
    settings = edited(prepared(), lambda value: plugin(value, 'lazy_cache').update(type='future_cache'))
    read = read_common_settings(settings, {})
    assert not read['capabilities']['cache_enabled']['editable']
    assert not read['capabilities']['cache_size']['editable']
    result = apply(settings, {'worker_threads': 4})
    assert plugin(doc(result['settings']), 'lazy_cache') == plugin(doc(settings), 'lazy_cache')


def test_included_feature_cannot_be_overwritten_by_a_new_root_plugin():
    settings = {'config_mode': 'native', 'native_config': 'runtime: {worker_threads: 2}\ninclude: [plugins.yaml]\nplugins: []\n',
                'additional_files': [{'path': 'plugins.yaml', 'encoding': 'text', 'content': 'plugins: [{tag: query_recorder, type: query_recorder, args: {path: ./state/original.db}}]\n'}]}
    read = read_common_settings(settings, {})
    assert not read['capabilities']['query_recorder.args.path']['editable']
    result = apply(settings, {'worker_threads': 3})
    assert doc(result['settings'])['plugins'] == []
    assert result['settings']['additional_files'] == settings['additional_files']


@pytest.mark.parametrize('content', ['', 'full:current.test 192.0.2.20\n'])
def test_explicit_hosts_edit_invalidates_content_parked_by_native_deletion(content):
    settings = prepared()
    settings['_common_disabled'] = {'native_hosts_source': 'full:withdrawn.test 192.0.2.10\n'}
    before = deepcopy(settings)
    result = apply(settings, {'custom_hosts': content})
    assert 'native_hosts_source' not in result['settings']['_common_disabled']
    assert result['settings']['custom_hosts'] == content
    assert settings == before


def test_unrelated_common_edit_preserves_hosts_content_for_explicit_restoration():
    settings = prepared()
    settings['_common_disabled'] = {'native_hosts_source': 'full:restore.test 192.0.2.10\n'}
    result = apply(settings, {'worker_threads': 3})
    assert result['settings']['_common_disabled']['native_hosts_source'] == settings['_common_disabled']['native_hosts_source']
