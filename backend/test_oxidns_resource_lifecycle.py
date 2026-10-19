"""Resource removal must leave a runnable native draft and preserve user state."""
from copy import deepcopy
import json

import pytest
import yaml

from backend.common.oxidns_common import apply_common_settings, read_common_settings
from backend.common.oxidns_editor import prepare_editor
from backend.common.oxidns_native import native_document, validate_resource_files
from backend.common.oxidns_settings import normalize_oxidns_settings
from backend.converters.oxidns import get_oxidns_custom_files, get_oxidns_ruleset_downloads


URL = 'https://example.test/filter.txt'
CONFIG = {
    'system_config': {'server_domain': 'http://config.test', 'rule_proxy_token': 'test-only'},
    'rule_configs': [
        {'id': 'subscription', 'itemType': 'ruleset', 'behavior': 'domain', 'url': URL},
        {'id': 'inline_ip', 'itemType': 'rule', 'rule_type': 'IP-CIDR', 'value': '192.0.2.0/24'},
    ],
}


def _plugin(document, tag):
    return next(item for item in document['plugins'] if item['tag'] == tag)


def _edit(settings, callback):
    settings = deepcopy(settings)
    document = yaml.safe_load(settings['native_config'])
    callback(document)
    settings['native_config'] = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    return settings


def _prepared(source):
    fields = {'adguard': {'enabled': True, 'urls': [URL]}} if source == 'adguard' else {'direct_rulesets': ['subscription']}
    settings = prepare_editor({**fields, 'rule_update': {'enabled': True}}, CONFIG, 'http://config.test')
    settings = _edit(settings, lambda document: (
        _plugin(document, 'rules_download')['args'].update(timeout='17s', insecure_skip_verify=True),
        _plugin(document, 'rules_cron')['args'].update(timezone='UTC'),
        _plugin(document, 'rules_cron')['args']['jobs'][0].update(schedule='5 2 * * *'),
    ))
    native_document(settings)
    return settings


def _patch(settings, changes):
    before = deepcopy(settings)
    result = apply_common_settings(settings, changes, CONFIG, 'http://config.test')['settings']
    assert settings == before
    return result


def _resources(source, present):
    return {'adguard.urls': [URL] if present else []} if source == 'adguard' else {'direct_rulesets': ['subscription'] if present else []}


def _reopen(settings):
    # The persisted settings contain JSON metadata as well as the native YAML.
    saved = json.loads(json.dumps(settings))
    return prepare_editor(normalize_oxidns_settings(saved, validate_native_arguments=False), CONFIG, 'http://config.test')


@pytest.mark.parametrize('source', ['adguard', 'routing'])
def test_subscriptions_create_and_restore_manual_updates_without_scheduling(source):
    settings = prepare_editor({}, CONFIG, 'http://config.test')
    changes = _resources(source, True)
    if source == 'adguard':
        changes['adguard.enabled'] = True
    active = _patch(settings, changes)
    for _ in range(2):
        document = native_document(active)
        assert _plugin(document, 'rules_download')['args']['downloads']
        assert _plugin(document, 'rules_reload')['args']
        assert _plugin(document, 'rules_refresh')['args'] == [{'exec': '$rules_download'}, {'exec': '$rules_reload'}]
        assert all(item['tag'] != 'rules_cron' for item in document['plugins'])
        assert read_common_settings(active, CONFIG)['values']['rule_update']['enabled'] is False
        inactive = _patch(active, _resources(source, False))
        assert not any(item['tag'] in ('rules_download', 'rules_reload', 'rules_refresh', 'rules_cron')
                       for item in native_document(inactive)['plugins'])
        active = _patch(_reopen(inactive), changes)


@pytest.mark.parametrize('source', ['adguard', 'routing'])
@pytest.mark.parametrize('disable_together', [False, True])
def test_last_subscription_removal_and_reenable_produce_valid_native_configs(source, disable_together):
    original = _prepared(source)
    changes = _resources(source, False)
    if disable_together:
        changes['rule_update.enabled'] = False
    disabled = _patch(original, changes)
    native_document(disabled)
    assert read_common_settings(disabled, CONFIG)['values']['rule_update']['enabled'] is False
    reopened = _reopen(disabled)
    assert reopened['native_config'] == disabled['native_config']
    restored = _patch(reopened, {**_resources(source, True), 'rule_update.enabled': True})
    document = native_document(restored)
    assert read_common_settings(restored, CONFIG)['values']['rule_update']['enabled'] is True
    downloader = _plugin(document, 'rules_download')['args']
    assert len(downloader['downloads']) == 1
    assert downloader['timeout'] == '17s'
    assert downloader['insecure_skip_verify'] is True
    cron = _plugin(document, 'rules_cron')['args']
    assert cron['timezone'] == 'UTC'
    assert [job['schedule'] for job in cron['jobs'] if job.get('name') == 'refresh_rules'] == ['5 2 * * *']


def test_replacing_last_subscription_with_inline_rule_does_not_leave_empty_download():
    result = _patch(_prepared('routing'), {'direct_rulesets': [], 'direct_rules': ['inline_ip']})
    document = native_document(result)
    assert read_common_settings(result, CONFIG)['values']['direct_rules'] == ['inline_ip']
    assert any(item['type'] == 'ip_set' for item in document['plugins'])
    assert read_common_settings(result, CONFIG)['values']['rule_update']['enabled'] is False


@pytest.mark.parametrize('source', ['adguard', 'routing'])
def test_last_subscription_removal_preserves_unrelated_cron_job(source):
    custom_job = {'name': 'manual', 'schedule': '0 * * * *', 'executors': ['$manual_executor']}
    settings = _edit(_prepared(source), lambda document: (
        document['plugins'].append({'tag': 'manual_executor', 'type': 'sequence', 'args': [{'exec': 'accept'}]}),
        _plugin(document, 'rules_cron')['args']['jobs'].append(deepcopy(custom_job)),
    ))
    result = _patch(settings, _resources(source, False))
    document = native_document(result)
    assert _plugin(document, 'rules_cron')['args']['jobs'] == [custom_job]
    assert read_common_settings(result, CONFIG)['values']['rule_update']['enabled'] is False
    restored = _patch(_reopen(result), {**_resources(source, True), 'rule_update.enabled': True})
    jobs = _plugin(native_document(restored), 'rules_cron')['args']['jobs']
    assert jobs.count(custom_job) == 1
    assert sum(job.get('name') == 'refresh_rules' for job in jobs) == 1


@pytest.mark.parametrize('source', ['adguard', 'routing'])
def test_removing_owned_subscription_keeps_custom_download_reload_and_job(source):
    custom_download = {'url': 'https://manual.test/list.txt', 'dir': 'manual', 'filename': 'list.txt', 'extension': False}
    custom_job = {'name': 'manual', 'schedule': '0 * * * *', 'executors': ['$manual_executor']}
    settings = _edit(_prepared(source), lambda document: (
        document['plugins'].extend([
            {'tag': 'manual_provider', 'type': 'domain_set', 'args': {'exps': ['full:manual.test']}},
            {'tag': 'manual_executor', 'type': 'sequence', 'args': [{'exec': 'accept'}]},
        ]),
        _plugin(document, 'rules_download')['args']['downloads'].append(deepcopy(custom_download)),
        _plugin(document, 'rules_reload')['args'].append('$manual_provider'),
        _plugin(document, 'rules_cron')['args']['jobs'].append(deepcopy(custom_job)),
    ))
    result = _patch(settings, _resources(source, False))
    document = native_document(result)
    assert _plugin(document, 'rules_download')['args']['downloads'] == [custom_download]
    assert '$manual_provider' in _plugin(document, 'rules_reload')['args']
    assert custom_job in _plugin(document, 'rules_cron')['args']['jobs']


@pytest.mark.parametrize('resource', ['url', 'content'])
def test_clearing_adguard_resource_preserves_source_consumed_by_include(resource):
    fields = {'urls': [URL]} if resource == 'url' else {'content': '||ads.test^'}
    settings = prepare_editor({'adguard': {'enabled': True, **fields}}, CONFIG, 'http://config.test')
    document = yaml.safe_load(settings['native_config'])
    path = _plugin(document, 'adguard_rules')['args']['files'][0]
    document['include'] = ['manual.yaml']
    settings['native_config'] = yaml.safe_dump(document, sort_keys=False)
    settings['additional_files'] = [{
        'path': 'manual.yaml', 'encoding': 'text',
        'content': yaml.safe_dump({'plugins': [
            {'tag': 'manual_filter', 'type': 'adguard_rule', 'args': {'files': [path]}},
        ]}),
    }]
    native_document(settings)
    changes = {'adguard.urls': []} if resource == 'url' else {'adguard.content': ''}
    result = _patch(settings, changes)
    projection = read_common_settings(result, CONFIG)['values']['adguard']
    assert projection['urls' if resource == 'url' else 'content'] == ([] if resource == 'url' else '')
    reopened = _reopen(result)
    downloads = get_oxidns_ruleset_downloads({**CONFIG, 'oxidns': reopened}, 'http://config.test')
    files = get_oxidns_custom_files({**CONFIG, 'oxidns': reopened})
    validate_resource_files(native_document(reopened), [item['local_path'] for item in downloads] + [item['path'] for item in files])
    assert result['adguard']['urls' if resource == 'url' else 'content'] == ([URL] if resource == 'url' else '||ads.test^')


@pytest.mark.parametrize('reference_state', ['new', 'existing'])
def test_rule_removal_keeps_provider_used_by_custom_match(reference_state):
    settings = prepare_editor({'direct_rulesets': ['subscription']}, CONFIG, 'http://config.test')
    original = native_document(settings)
    provider = next(item for item in original['plugins'] if item['type'] == 'domain_set')
    custom = {'matches': [f'qname ${provider["tag"]}', 'qtype 1'], 'exec': 'goto china_dns'}
    changes = {'direct_rulesets': []}
    if reference_state == 'existing':
        settings = _patch(settings, {'custom_matches': [custom]})
    else:
        changes['custom_matches'] = [custom]
    result = _patch(settings, changes)
    reopened = _reopen(result)
    document = native_document(reopened)
    assert _plugin(document, provider['tag']) == provider
    assert custom in _plugin(document, 'sequence_main')['args']
    downloads = get_oxidns_ruleset_downloads({**CONFIG, 'oxidns': reopened}, 'http://config.test')
    validate_resource_files(document, [item['local_path'] for item in downloads])


def test_removing_rule_and_its_last_custom_reference_removes_unused_provider():
    settings = prepare_editor({'direct_rulesets': ['subscription']}, CONFIG, 'http://config.test')
    provider = next(item for item in native_document(settings)['plugins'] if item['type'] == 'domain_set')
    custom = {'matches': [f'qname ${provider["tag"]}', 'qtype 1'], 'exec': 'goto china_dns'}
    settings = _patch(settings, {'custom_matches': [custom]})
    result = _patch(settings, {'direct_rulesets': [], 'custom_matches': []})
    reopened = _reopen(result)
    document = native_document(reopened)
    assert all(item['tag'] != provider['tag'] for item in document['plugins'])
    assert get_oxidns_ruleset_downloads({**CONFIG, 'oxidns': reopened}, 'http://config.test') == []


@pytest.mark.parametrize('field,value', [('timeout', '23s'), ('outbound', 'direct'), ('socks5', 'socks5://127.0.0.1:1080')])
def test_manual_download_options_are_live_without_enabling_schedule(field, value):
    settings = prepare_editor({'adguard': {'enabled': True, 'urls': [URL]}}, CONFIG, 'http://config.test')
    if field == 'outbound':
        settings = _edit(settings, lambda d: d.update(network={'outbound': {'profiles': {'direct': {'proxy': 'direct'}}}}))
    result = _patch(settings, {f'rule_update.args.{field}': value})
    document = native_document(result)
    assert _plugin(document, 'rules_download')['args'][field] == value
    assert all(item['tag'] != 'rules_cron' for item in document['plugins'])
    restored = _patch(_reopen(result), {'rule_update.enabled': True})
    document = native_document(restored)
    assert _plugin(document, 'rules_download')['args'][field] == value


def test_editing_parked_download_options_does_not_recreate_empty_plugin():
    disabled = _patch(_prepared('adguard'), {'adguard.urls': []})
    edited = _patch(_reopen(disabled), {'rule_update.args.timeout': '31s'})
    document = native_document(edited)
    assert all(item['tag'] != 'rules_download' for item in document['plugins'])
    restored = _patch(_reopen(edited), {'adguard.urls': [URL], 'rule_update.enabled': True})
    assert _plugin(native_document(restored), 'rules_download')['args']['timeout'] == '31s'


@pytest.mark.parametrize('reference', ['manual_sequence', 'manual_cron', 'custom_refresh'])
def test_removing_resources_with_custom_pipeline_callers_is_atomic(reference):
    settings = _prepared('adguard')
    if reference == 'manual_sequence':
        settings = _edit(settings, lambda d: d['plugins'].append(
            {'tag': 'manual', 'type': 'sequence', 'args': [{'exec': '$rules_download'}]}))
    elif reference == 'manual_cron':
        settings = _edit(settings, lambda d: _plugin(d, 'rules_cron')['args']['jobs'].append(
            {'name': 'custom', 'schedule': '0 * * * *', 'executors': ['$rules_download']}))
    else:
        settings = _edit(settings, lambda d: _plugin(d, 'rules_refresh')['args'].append({'exec': 'accept'}))
    original = deepcopy(settings)
    from backend.common.oxidns_settings import OxidnsSettingsError
    with pytest.raises(OxidnsSettingsError, match='自定义'):
        _patch(settings, {'adguard.urls': []})
    assert settings == original


def test_custom_download_without_reload_targets_keeps_running_and_restores_new_providers():
    custom = {'url': 'https://manual.test/file', 'dir': 'manual', 'filename': 'resource.txt', 'extension': 0}
    settings = _edit(_prepared('routing'), lambda d: _plugin(d, 'rules_download')['args']['downloads'].append(deepcopy(custom)))
    result = _patch(settings, {'direct_rulesets': []})
    document = native_document(result)
    assert _plugin(document, 'rules_download')['args']['downloads'] == [custom]
    assert all(item['tag'] != 'rules_reload' for item in document['plugins'])
    assert _plugin(document, 'rules_refresh')['args'] == [{'exec': '$rules_download'}]
    assert read_common_settings(result, CONFIG)['values']['rule_update']['enabled'] is True
    # A repeated edit while there are still no providers must also stay valid.
    result = _patch(_reopen(result), {'direct_rulesets': []})
    native_document(result)
    restored = _patch(result, {'direct_rulesets': ['subscription']})
    document = native_document(restored)
    assert custom in _plugin(document, 'rules_download')['args']['downloads']
    assert len(_plugin(document, 'rules_download')['args']['downloads']) == 2
    assert _plugin(document, 'rules_refresh')['args'] == [{'exec': '$rules_download'}, {'exec': '$rules_reload'}]
    assert _plugin(document, 'rules_reload')['args']


def test_reenable_does_not_resurrect_removed_resources_or_provider_references():
    old = _prepared('routing')
    old_provider = next(item['tag'] for item in native_document(old)['plugins'] if item['type'] == 'domain_set')
    disabled = _patch(old, {'direct_rulesets': []})
    # The new updater uses a different resource family after a save/reload.
    restored = _patch(_reopen(disabled), {'adguard.enabled': True, 'adguard.urls': [URL], 'rule_update.enabled': True})
    document = native_document(restored)
    assert all(item['tag'] != old_provider for item in document['plugins'])
    assert _plugin(document, 'rules_reload')['args'] == ['$adguard_rules']
    downloads = _plugin(document, 'rules_download')['args']['downloads']
    assert len(downloads) == 1 and downloads[0]['filename'].startswith('adguard_')


def test_removed_download_options_are_restored_only_for_the_same_resource():
    settings = _edit(_prepared('adguard'), lambda d: _plugin(d, 'rules_download')['args']['downloads'][0].update(
        extension={'zero': 0, 'off': False}, url='https://override.test/custom-filter'))
    disabled = _patch(settings, {'adguard.urls': []})
    native_document(disabled)
    edited = _patch(_reopen(disabled), {'rule_update.args.timeout': '29s'})
    assert all(item['tag'] != 'rules_download' for item in native_document(edited)['plugins'])
    restored = _patch(edited, {'adguard.urls': [URL], 'rule_update.enabled': True})
    download = _plugin(native_document(restored), 'rules_download')['args']['downloads'][0]
    assert download['extension'] == {'zero': 0, 'off': False}
    assert download['url'] == 'https://override.test/custom-filter'


@pytest.mark.parametrize('invalid', [{'tag': 'other', 'type': 'reload_provider', 'args': []},
                                     {'tag': 'rules_reload', 'type': 'sequence', 'args': []}])
def test_invalid_reload_snapshot_is_not_restored_or_accepted_for_route_edits(invalid):
    custom = {'url': 'https://manual.test/file', 'dir': 'manual', 'filename': 'file'}
    settings = _edit(_prepared('routing'), lambda d: _plugin(d, 'rules_download')['args']['downloads'].append(custom))
    result = _patch(settings, {'direct_rulesets': []})
    result['_common_disabled']['rules_reload'] = invalid
    result = _patch(result, {'rule_update.args.timeout': '33s'})
    assert all(item['tag'] != 'rules_reload' for item in native_document(result)['plugins'])
    from backend.common.oxidns_settings import OxidnsSettingsError
    with pytest.raises(OxidnsSettingsError, match='自定义'):
        _patch(result, {'direct_rulesets': ['subscription']})


def test_empty_refresh_is_suspended_and_reenabled_without_losing_custom_downloads():
    custom = {'url': 'https://manual.test/file', 'dir': 'manual', 'filename': 'file', 'extension': False}
    settings = _edit(_prepared('adguard'), lambda d: (
        _plugin(d, 'rules_refresh').update(args=[]),
        _plugin(d, 'rules_download')['args']['downloads'].append(custom),
    ))
    result = _patch(settings, {'rule_update.enabled': False})
    assert all(item['tag'] != 'rules_refresh' for item in native_document(result)['plugins'])
    restored = _patch(_reopen(result), {'rule_update.enabled': True})
    document = native_document(restored)
    assert custom in _plugin(document, 'rules_download')['args']['downloads']
    assert _plugin(document, 'rules_refresh')['args'] == [{'exec': '$rules_download'}, {'exec': '$rules_reload'}]
    assert read_common_settings(restored, CONFIG)['values']['rule_update']['enabled'] is True
