"""Complete-form edits share common controls' resource/executor lifecycle."""
from copy import deepcopy

import pytest
import yaml

from backend.common.oxidns_common import read_common_settings
from backend.common.oxidns_editor import prepare_editor
from backend.common.oxidns_native_apply import apply_native_settings
from backend.common.oxidns_references import OxidnsReferenceError
from backend.common.oxidns_validation import validate_settings
from backend.converters.oxidns import get_oxidns_ruleset_downloads, get_oxidns_custom_files
from backend.test_oxidns_common import prepared, apply, doc, plugin, edited
from backend.test_oxidns_integration import oxidns_app


def change(settings, callback):
    candidate = doc(settings)
    callback(candidate)
    before = deepcopy(settings)
    result = apply_native_settings(settings, yaml.safe_dump(candidate, sort_keys=False), {})
    assert settings == before
    return result


def delete(settings, tag):
    return change(settings, lambda d: d.update(plugins=[p for p in d['plugins'] if p['tag'] != tag]))


def all_features():
    return prepared(
        query_recorder={'enabled': True}, ip_selector={'enabled': True},
        dynamic_domain_set={'enabled': True, 'learn': True},
        ros_address_list={'enabled': True, 'args': {'address': '192.0.2.1:8728', 'username': 'test', 'password': 'test'}},
        ros_route={'enabled': True, 'args': {'address': '192.0.2.1:8728', 'username': 'test', 'password': 'test', 'routing_table': 'main', 'gateway4': '192.0.2.254'}},
        custom_hosts='full:one.test 192.0.2.2\n',
        adguard={'enabled': True, 'urls': ['https://example.test/filters']},
        rule_update={'enabled': True}, fallback_dns='udp://192.0.2.53')


@pytest.mark.parametrize('tag', [
    'lazy_cache', 'local_probe_cache', 'query_recorder', 'ip_selector', 'ros_address_list', 'ros_route',
    'dynamic_domains', 'learn_classified_domain', 'custom_hosts',
    'adguard_rules', 'adguard_block', 'adguard_reject',
    'rules_download', 'rules_reload', 'rules_refresh', 'rules_cron',
    'forward_fallback', 'fallback_dns', 'fallback',
])
def test_deleting_managed_plugin_reconciles_owned_edges_and_preserves_other_options(tag):
    settings = all_features()
    original_remote = deepcopy(plugin(doc(settings), 'forward_remote'))
    result = delete(settings, tag)
    assert validate_settings(result['settings'])['valid']
    assert all(item['tag'] != tag for item in doc(result['settings'])['plugins'])
    assert plugin(doc(result['settings']), 'forward_remote') == original_remote
    assert result['notices']


@pytest.mark.parametrize('empty', [[], [{}], [{'addr': ''}], [{'addr': '  '}]])
def test_clearing_last_backup_address_disables_chain(empty):
    result = change(prepared(fallback_dns='udp://192.0.2.53'), lambda d: plugin(d, 'forward_fallback')['args'].update(upstreams=empty))
    assert result['values']['fallback_dns'] == ''
    assert plugin(doc(result['settings']), 'proxy_dns_seq')['args'] == [{'exec': 'goto remote_dns'}]
    assert validate_settings(result['settings'])['valid']


@pytest.mark.parametrize('placeholder', [[], [{}], [{'addr': ''}]])
def test_backup_placeholder_is_editable_and_connects_only_after_address_is_filled(placeholder):
    settings = prepared()
    draft = change(settings, lambda d: d['plugins'].append({'tag': 'forward_fallback', 'type': 'forward', 'args': {'upstreams': placeholder, 'concurrent': 3}}))
    assert plugin(doc(draft['settings']), 'forward_fallback')['args']['upstreams'] == placeholder
    assert plugin(doc(draft['settings']), 'proxy_dns_seq')['args'] == [{'exec': 'goto remote_dns'}]
    result = change(draft['settings'], lambda d: plugin(d, 'forward_fallback')['args'].update(upstreams=[{'addr': 'tcp://192.0.2.53'}]))
    assert plugin(doc(result['settings']), 'proxy_dns_seq')['args'] == [{'exec': '$fallback'}, {'exec': 'accept'}]
    assert plugin(doc(result['settings']), 'forward_fallback')['args']['concurrent'] == 3
    assert validate_settings(result['settings'])['valid']


@pytest.mark.parametrize('reference', [
    {'tag': 'shared', 'type': 'fallback', 'args': {'primary': 'lazy_cache', 'secondary': 'forward_remote'}},
    {'tag': 'shared', 'type': 'sequence', 'args': [{'exec': '${EXECUTOR}'}]},
    {'tag': 'shared', 'type': 'sequence', 'args': [{'exec': '${EXECUTOR:-$lazy_cache}'}]},
])
def test_shared_or_unresolved_references_block_deletion_with_candidate_locations(reference):
    settings = edited(prepared(), lambda d: d['plugins'].append(reference))
    candidate = doc(settings)
    candidate['plugins'] = [item for item in candidate['plugins'] if item['tag'] != 'lazy_cache']
    index = next(i for i, item in enumerate(candidate['plugins']) if item['tag'] == 'shared')
    with pytest.raises(OxidnsReferenceError) as error:
        apply_native_settings(settings, yaml.safe_dump(candidate), {})
    assert any(item['path'][:3] == ['plugins', index, 'args'] for item in error.value.references)


def test_form_can_repair_custom_calls_after_a_rejected_deletion():
    settings = edited(prepared(), lambda d: plugin(d, 'china_dns')['args'].insert(0, {'matches': ['qtype A'], 'exec': '$lazy_cache'}))
    with pytest.raises(OxidnsReferenceError):
        delete(settings, 'lazy_cache')
    result = change(settings, lambda d: (
        d.update(plugins=[item for item in d['plugins'] if item['tag'] != 'lazy_cache']),
        plugin(d, 'china_dns').update(args=[item for item in plugin(d, 'china_dns')['args'] if item.get('exec') != '$lazy_cache']),
    ))
    assert validate_settings(result['settings'])['valid']


def test_tag_only_rename_updates_explicit_executor_bare_and_provider_references():
    settings = all_features()
    result = change(settings, lambda d: plugin(d, 'remote_dns').update(tag='my_remote'))
    assert plugin(doc(result['settings']), 'fallback')['args']['primary'] == 'my_remote'
    assert validate_settings(result['settings'])['valid']
    result = change(result['settings'], lambda d: plugin(d, 'dynamic_domains').update(tag='my_domains'))
    assert plugin(doc(result['settings']), 'learn_classified_domain')['args']['provider'] == 'my_domains'
    assert any('qname $my_domains' in item.get('matches', []) for item in plugin(doc(result['settings']), 'sequence_main')['args'])
    assert validate_settings(result['settings'])['valid']


def test_explicit_rename_intent_supports_same_batch_argument_edits():
    settings = prepared()
    candidate = doc(settings)
    plugin(candidate, 'lazy_cache').update(tag='my_cache')
    plugin(candidate, 'my_cache')['args']['size'] = 987
    result = apply_native_settings(settings, yaml.safe_dump(candidate), {}, renames={'lazy_cache': 'my_cache'})
    assert {'exec': '$my_cache'} in plugin(doc(result['settings']), 'china_dns')['args']
    assert plugin(doc(result['settings']), 'my_cache')['args']['size'] == 987
    assert validate_settings(result['settings'])['valid']
    assert result['values']['cache_enabled'] is True
    for field in ('cache_enabled', 'cache_size', 'cache_dump_enabled'):
        assert result['capabilities'][field]['editable'] is False
        assert result['capabilities'][field]['target'] == 'my_cache'
    with pytest.raises(ValueError, match='重命名'):
        apply(result['settings'], {'cache_enabled': True})
    restored = change(result['settings'], lambda d: plugin(d, 'my_cache').update(tag='lazy_cache'))
    assert restored['capabilities']['cache_enabled']['editable'] is True
    assert restored['values']['cache_enabled'] is True


@pytest.mark.parametrize('renames', [{'lazy_cache': 'forward_remote'}, {'absent': 'new'}, {'a': 'x', 'b': 'x'}, ['bad']])
def test_invalid_rename_intent_does_not_guess_or_overwrite_plugins(renames):
    settings = prepared()
    with pytest.raises(ValueError, match='重命名|renames'):
        apply_native_settings(settings, settings['native_config'], {}, renames=renames)


@pytest.mark.parametrize('new', ['', ' ', 'not a tag'])
@pytest.mark.parametrize('explicit', [False, True])
def test_incomplete_rename_is_rejected_without_mutating_either_input(new, explicit):
    settings = prepared()
    original = deepcopy(settings)
    candidate = doc(settings)
    plugin(candidate, 'lazy_cache')['tag'] = new
    text = yaml.safe_dump(candidate)
    with pytest.raises(ValueError, match='tag'):
        apply_native_settings(settings, text, {}, renames={'lazy_cache': new} if explicit else None)
    assert settings == original
    assert yaml.safe_load(text) == candidate


def test_renaming_cron_job_preserves_explicit_native_execution():
    settings = prepared(adguard={'enabled': True, 'urls': ['https://example.test/filters']}, rule_update={'enabled': True})
    result = change(settings, lambda d: plugin(d, 'rules_cron')['args']['jobs'][0].update(name='my_job'))
    assert plugin(doc(result['settings']), 'rules_cron')['args']['jobs'][0]['name'] == 'my_job'
    assert plugin(doc(result['settings']), 'rules_download')
    assert validate_settings(result['settings'])['valid']


def test_adguard_switch_and_complete_delete_both_suspend_downloads_and_restore_sources():
    settings = prepared(adguard={'enabled': True, 'urls': ['https://example.test/filters']}, rule_update={'enabled': True})
    for disabled in (apply(settings, {'adguard.enabled': False}), delete(settings, 'adguard_rules')):
        assert disabled['values']['adguard']['enabled'] is False
        assert get_oxidns_ruleset_downloads({'oxidns': disabled['settings']}) == []
        assert disabled['values']['rule_update']['enabled'] is False
        assert validate_settings(disabled['settings'])['valid']
        restored = apply(disabled['settings'], {'adguard.enabled': True, 'rule_update.enabled': True})
        assert len(get_oxidns_ruleset_downloads({'oxidns': restored['settings']})) == 1
        assert validate_settings(restored['settings'])['valid']


def test_unrelated_full_form_change_does_not_repair_or_rewrite_native_graph():
    settings = edited(prepared(fallback_dns='udp://192.0.2.53'), lambda d: plugin(d, 'fallback_dns')['args'].append({'exec': 'debug_print'}))
    expected = doc(settings)
    expected['runtime']['worker_threads'] = 7
    result = apply_native_settings(settings, yaml.safe_dump(expected, sort_keys=False), {})
    assert doc(result['settings']) == expected
    assert result['notices'] == []


@pytest.mark.parametrize('prefix', ['/api/oxidns', '/api/profiles/office/oxidns'])
def test_native_apply_api_is_profile_scoped_no_store_and_does_not_persist(oxidns_app, prefix):
    app, repository = oxidns_app
    settings = prepared()
    candidate = doc(settings)
    candidate['plugins'] = [item for item in candidate['plugins'] if item['tag'] != 'lazy_cache']
    before = deepcopy(repository.get_profile('office'))
    response = app.test_client().post(prefix + '/native-settings/apply?profile=office', json={'settings': settings, 'native_config': yaml.safe_dump(candidate)})
    assert response.status_code == 200, response.get_json()
    assert response.headers['Cache-Control'] == 'no-store'
    assert response.get_json()['values']['cache_enabled'] is False
    assert repository.get_profile('office') == before


@pytest.mark.parametrize('field,tag', [('cache_enabled', 'lazy_cache'), ('probe_cache_enabled', 'local_probe_cache'), ('query_recorder.enabled', 'query_recorder'), ('dynamic_domain_set.learn', 'learn_classified_domain')])
def test_common_switch_rejects_bare_executor_reference(field, tag):
    settings = edited(all_features(), lambda d: d['plugins'].append({'tag': 'shared', 'type': 'fallback', 'args': {'primary': tag, 'secondary': 'forward_remote'}}))
    assert validate_settings(settings)['valid']
    assert not read_common_settings(settings, {})['capabilities'][field]['editable']
    with pytest.raises(ValueError, match='引用'):
        apply(settings, {field: False})


def test_disabling_scheduler_still_validates_live_manual_download_options():
    settings = prepared(adguard={'enabled': True, 'urls': ['https://example.test/filters']}, rule_update={'enabled': True})
    disabled = apply(settings, {'rule_update.enabled': False, 'rule_update.args.timeout': 'unfinished'})
    assert not validate_settings(disabled['settings'])['valid']
    assert disabled['values']['rule_update']['args']['timeout'] == 'unfinished'
    changed = apply(disabled['settings'], {'rule_update.args.timeout': '17s'})
    assert validate_settings(changed['settings'])['valid']
    assert plugin(doc(changed['settings']), 'rules_download')['args']['timeout'] == '17s'
    assert not any(item['tag'] == 'rules_cron' for item in doc(changed['settings'])['plugins'])
    restored = apply(changed['settings'], {'rule_update.enabled': True})
    assert plugin(doc(restored['settings']), 'rules_download')['args']['timeout'] == '17s'
    assert validate_settings(restored['settings'])['valid']


def test_disabling_scheduler_keeps_executors_used_by_an_independent_caller():
    settings = prepared(adguard={'enabled': True, 'urls': ['https://example.test/filters']}, rule_update={'enabled': True})
    settings = edited(settings, lambda d: d['plugins'].append({'tag': 'manual', 'type': 'fallback', 'args': {'primary': 'rules_download', 'secondary': 'forward_remote'}}))
    result = apply(settings, {'rule_update.enabled': False})
    assert plugin(doc(result['settings']), 'rules_download')
    assert validate_settings(result['settings'])['valid']


def test_deleting_cron_preserves_manual_pipeline_and_reports_incomplete_arguments():
    settings = prepared(adguard={'enabled': True, 'urls': ['https://example.test/filters']}, rule_update={'enabled': True})
    settings = edited(settings, lambda d: plugin(d, 'rules_download')['args'].update(timeout=''))
    assert not validate_settings(settings)['valid']
    result = delete(settings, 'rules_cron')
    assert not validate_settings(result['settings'])['valid']
    assert result['values']['rule_update']['enabled'] is False
    assert plugin(doc(result['settings']), 'rules_download')['args']['timeout'] == ''


@pytest.mark.parametrize('remove_plugin', [True, False])
def test_full_form_removes_only_schedule_and_keeps_manual_updates(remove_plugin):
    settings = prepared(adguard={'enabled': True, 'urls': ['https://example.test/filters']}, rule_update={'enabled': True})
    original = doc(settings)
    result = delete(settings, 'rules_cron') if remove_plugin else change(
        settings, lambda d: plugin(d, 'rules_cron')['args'].update(jobs=[]))
    document = doc(result['settings'])
    assert validate_settings(result['settings'])['valid']
    assert not result['values']['rule_update']['enabled']
    assert not any(item['tag'] == 'rules_cron' for item in document['plugins'])
    for tag in ('rules_download', 'rules_reload', 'rules_refresh'):
        assert plugin(document, tag) == plugin(original, tag)


def test_withdrawn_source_is_not_restored_after_editing_a_disabled_pipeline():
    settings = prepared(adguard={'enabled': True, 'urls': ['https://example.test/old']}, rule_update={'enabled': True})
    disabled = apply(settings, {'rule_update.enabled': False})['settings']
    removed = apply(disabled, {'adguard.urls': []})['settings']
    changed = apply(removed, {'rule_update.args.timeout': '17s'})['settings']
    result = apply(changed, {'adguard.urls': ['https://example.test/new'], 'rule_update.enabled': True})
    entries = plugin(doc(result['settings']), 'rules_download')['args']['downloads']
    assert [item['url'] for item in entries] == ['https://example.test/new']


@pytest.mark.parametrize('expression', ['${EXECUTOR}', '${EXECUTOR:-$lazy_cache}'])
def test_common_cache_switch_is_conservative_about_environment_references(expression):
    settings = edited(prepared(), lambda d: d['plugins'].append({'tag': 'manual', 'type': 'sequence', 'args': [{'exec': expression}]}))
    with pytest.raises(ValueError, match='环境变量'):
        apply(settings, {'cache_enabled': False})


def test_non_reference_environment_values_and_empty_include_do_not_block_common_switch():
    settings = edited(prepared(), lambda d: (
        d.update(include=[]),
        plugin(d, 'forward_remote')['args'].update(upstreams=[{'addr': '${DNS_ADDR}', 'password': '${PASSWORD}'}]),
    ))
    result = apply(settings, {'cache_enabled': False})
    assert result['values']['cache_enabled'] is False


def test_native_apply_api_reference_paths_are_arrays_and_index_the_candidate(oxidns_app):
    app, _ = oxidns_app
    settings = edited(prepared(), lambda d: d['plugins'].append({'tag': 'shared', 'type': 'fallback', 'args': {'primary': 'lazy_cache', 'secondary': 'forward_remote'}}))
    candidate = doc(settings)
    candidate['plugins'] = [item for item in candidate['plugins'] if item['tag'] != 'lazy_cache']
    response = app.test_client().post('/api/oxidns/native-settings/apply?profile=office', json={'settings': settings, 'native_config': yaml.safe_dump(candidate)})
    assert response.status_code == 400
    assert response.headers['Cache-Control'] == 'no-store'
    refs = response.get_json()['references']
    assert refs[0]['path'] == ['plugins', len(candidate['plugins']) - 1, 'args', 'primary']


@pytest.mark.parametrize('prefix', ['/api/oxidns', '/api/profiles/office/oxidns'])
def test_native_apply_api_roundtrips_credentials_capability_urls_and_parked_metadata(oxidns_app, prefix):
    app, repository = oxidns_app
    config = repository.get_compat_config('office')
    token = config['system_config']['rule_proxy_token']
    settings = prepare_editor({**config['oxidns'], 'api': {'enabled': True, 'username': 'editor', 'password': token},
                               'rule_update': {'enabled': True}, 'ros_address_list': {'enabled': True, 'args': {
                                   'address': '192.0.2.1:8728', 'username': 'editor', 'password': token}}}, config, 'http://config.test')
    settings = apply(settings, {'ros_address_list.enabled': False}, config)['settings']
    candidate = doc(settings)
    candidate['plugins'] = [item for item in candidate['plugins'] if item['tag'] != 'lazy_cache']
    before = deepcopy(repository.get_profile('office'))
    response = app.test_client().post(prefix + '/native-settings/apply?profile=office', json={'settings': settings, 'native_config': yaml.safe_dump(candidate)})
    assert response.status_code == 200, response.get_json()
    result = response.get_json()
    assert result['values']['api']['password'] == token
    assert result['settings']['_common_disabled']['ros_address_list']['args']['password'] == token
    native = doc(result['settings'])
    assert native['api']['http']['auth']['password'] == token
    assert token in plugin(native, 'rules_download')['args']['downloads'][0]['url']
    assert '[REDACTED]' not in result['settings']['native_config']
    assert response.headers['Cache-Control'] == 'no-store'
    assert repository.get_profile('office') == before


@pytest.mark.parametrize('intent', ['lazy_cache', [1], ['lazy_cache', 'lazy_cache'], ['unknown'], ['forward_local'], ['query_recorder']])
def test_invalid_standard_add_intent_is_rejected_without_mutating_settings(intent):
    settings = prepared()
    candidate = doc(settings)
    candidate['plugins'].append({'tag': 'query_recorder', 'type': 'cache', 'args': {}})
    original = deepcopy(settings)
    with pytest.raises(ValueError, match='standard_add|标准功能'):
        apply_native_settings(settings, yaml.safe_dump(candidate), {}, standard_add=intent)
    assert settings == original


@pytest.mark.parametrize('args', [{}, None, {'size': None, 'future': [0, False, None]}])
def test_raw_canonical_addition_keeps_explicit_empty_and_null_arguments(args):
    settings = apply(prepared(), {'cache_enabled': False})['settings']
    result = change(settings, lambda d: d['plugins'].append({'tag': 'lazy_cache', 'type': 'cache', 'args': deepcopy(args)}))
    assert plugin(doc(result['settings']), 'lazy_cache')['args'] == args
    assert plugin(doc(result['settings']), 'china_dns')['args'].count({'exec': '$lazy_cache'}) == 1


@pytest.mark.parametrize('standard', [False, True])
def test_hosts_native_roundtrip_restores_resource_bytes_and_exact_main_pair(standard):
    settings = prepared(custom_hosts='full:roundtrip.test 192.0.2.19\n')
    source = get_oxidns_custom_files({'oxidns': settings})
    definition = deepcopy(plugin(doc(settings), 'custom_hosts'))
    disabled = delete(settings, 'custom_hosts')['settings']
    assert get_oxidns_custom_files({'oxidns': disabled}) == []
    candidate = doc(disabled)
    candidate['plugins'].append({**definition, 'args': {}} if standard else definition)
    result = apply_native_settings(disabled, yaml.safe_dump(candidate), {}, standard_add=['custom_hosts'] if standard else None)
    assert get_oxidns_custom_files({'oxidns': result['settings']}) == source
    assert plugin(doc(result['settings']), 'sequence_main') == plugin(doc(settings), 'sequence_main')
    assert 'native_hosts_source' not in result['settings']['_common_disabled']


def test_raw_hosts_addition_preserves_private_files_and_inline_entries():
    settings = prepared()
    settings['additional_files'] = [{'path': 'rules/private-hosts.txt', 'content': 'full:file.test 192.0.2.30\n', 'encoding': 'text'}]
    definition = {'tag': 'custom_hosts', 'type': 'hosts', 'args': {
        'files': ['./rules/private-hosts.txt'], 'entries': ['full:inline.test 192.0.2.31'], 'short_circuit': False}}
    result = change(settings, lambda d: d['plugins'].append(deepcopy(definition)))
    assert plugin(doc(result['settings']), 'custom_hosts') == definition
    main = plugin(doc(result['settings']), 'sequence_main')['args']
    index = main.index({'exec': '$custom_hosts'})
    assert main[index + 1] == {'matches': ['has_resp'], 'exec': 'accept'}
    assert result['settings']['custom_hosts'] == ''
    assert get_oxidns_custom_files({'oxidns': result['settings']}) == get_oxidns_custom_files({'oxidns': settings})
    assert validate_settings(result['settings'])['valid']


def test_standard_hosts_restore_recovers_inline_rules_without_a_common_source():
    definition = {'tag': 'custom_hosts', 'type': 'hosts', 'args': {'entries': ['full:inline.test 192.0.2.31'], 'short_circuit': False}}
    settings = change(prepared(), lambda d: d['plugins'].append(deepcopy(definition)))['settings']
    assert settings['custom_hosts'] == ''
    settings = delete(settings, 'custom_hosts')['settings']
    candidate = doc(settings)
    candidate['plugins'].append({'tag': 'custom_hosts', 'type': 'hosts', 'args': {}})
    result = apply_native_settings(settings, yaml.safe_dump(candidate), {}, standard_add=['custom_hosts'])
    assert plugin(doc(result['settings']), 'custom_hosts') == definition
    assert plugin(doc(result['settings']), 'sequence_main')['args'].count({'exec': '$custom_hosts'}) == 1
    assert validate_settings(result['settings'])['valid']


@pytest.mark.parametrize('replacement', ['', 'full:new.test 192.0.2.32\n'])
def test_common_hosts_edit_invalidates_native_parked_source_before_standard_restore(replacement):
    settings = delete(prepared(custom_hosts='full:old.test 192.0.2.31\n'), 'custom_hosts')['settings']
    settings = apply(settings, {'custom_hosts': replacement})['settings']
    if replacement:
        settings = delete(settings, 'custom_hosts')['settings']
    elif any(item['tag'] == 'custom_hosts' for item in doc(settings)['plugins']):
        settings = delete(settings, 'custom_hosts')['settings']
    candidate = doc(settings)
    candidate['plugins'].append({'tag': 'custom_hosts', 'type': 'hosts', 'args': {}})
    result = apply_native_settings(settings, yaml.safe_dump(candidate), {}, standard_add=['custom_hosts'])
    files = get_oxidns_custom_files({'oxidns': result['settings']})
    assert 'full:old.test' not in str(files)
    if replacement:
        assert any('full:new.test' in item['content'] for item in files)


@pytest.mark.parametrize('prefix', ['/api/oxidns', '/api/profiles/office/oxidns'])
def test_standard_add_api_restores_parked_values_without_persisting(oxidns_app, prefix):
    app, repository = oxidns_app
    settings = prepared(query_recorder={'enabled': True, 'args': {'path': './state/private-log.db', 'max_entries': 321}})
    settings = delete(settings, 'query_recorder')['settings']
    candidate = doc(settings)
    candidate['plugins'].append({'tag': 'query_recorder', 'type': 'query_recorder', 'args': {}})
    before = deepcopy(repository.get_profile('office'))
    response = app.test_client().post(prefix + '/native-settings/apply?profile=office', json={
        'settings': settings, 'native_config': yaml.safe_dump(candidate), 'standard_add': ['query_recorder']})
    assert response.status_code == 200, response.get_json()
    result = response.get_json()
    assert plugin(doc(result['settings']), 'query_recorder')['args']['path'] == './state/private-log.db'
    assert plugin(doc(result['settings']), 'sequence_main')['args'].count({'exec': '$query_recorder'}) == 1
    assert response.headers['Cache-Control'] == 'no-store'
    assert repository.get_profile('office') == before


def test_standard_batch_restores_adguard_resource_before_restarting_updater():
    settings = prepared(adguard={'enabled': True, 'urls': ['https://example.test/live']}, rule_update={'enabled': True})
    original = doc(settings)
    settings = delete(settings, 'adguard_rules')['settings']
    candidate = doc(settings)
    candidate['plugins'].extend([
        {'tag': 'adguard_rules', 'type': 'adguard_rule', 'args': {}},
        {'tag': 'rules_download', 'type': 'download', 'args': {}},
    ])
    result = apply_native_settings(settings, yaml.safe_dump(candidate), {}, standard_add=['adguard_rules', 'rules_download'])
    for tag in ('adguard_rules', 'adguard_reject', 'rules_download', 'rules_reload', 'rules_refresh', 'rules_cron'):
        assert plugin(doc(result['settings']), tag) == plugin(original, tag)
    assert result['values']['adguard']['enabled'] is True
    assert result['values']['rule_update']['enabled'] is True
    assert validate_settings(result['settings'])['valid']
