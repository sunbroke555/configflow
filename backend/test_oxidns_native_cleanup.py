"""Native field cleanup shares common persistence and Hosts lifecycle."""
from copy import deepcopy

import pytest
import yaml

from backend.common.oxidns_native_apply import apply_native_settings
from backend.common.oxidns_references import OxidnsReferenceError
from backend.common.oxidns_validation import validate_settings
from backend.converters.oxidns import generate_oxidns_config, get_oxidns_custom_files
from backend.test_oxidns_common import apply, doc, edited, plugin, prepared
from backend.test_oxidns_native_apply import change, delete


@pytest.mark.parametrize('empty', ['', '  ', '\t', None, 'omitted'])
def test_native_dump_path_clear_removes_interval_and_common_enable_recovers_both(empty):
    settings = prepared(cache_dump_enabled=True, cache_dump_file='./state/original.dump', cache_dump_interval=183)
    expected_chain = deepcopy(plugin(doc(settings), 'china_dns'))
    def clear(document):
        args = plugin(document, 'lazy_cache')['args']
        if empty == 'omitted': args.pop('dump_file')
        else: args['dump_file'] = empty
    stopped = change(settings, clear)
    args = plugin(doc(stopped['settings']), 'lazy_cache')['args']
    assert 'dump_file' not in args and 'dump_interval' not in args
    assert plugin(doc(stopped['settings']), 'china_dns') == expected_chain
    assert stopped['values']['cache_enabled'] is True
    assert stopped['values']['cache_dump_enabled'] is False
    assert validate_settings(stopped['settings'])['valid']
    resumed = apply(stopped['settings'], {'cache_dump_enabled': True})
    args = plugin(doc(resumed['settings']), 'lazy_cache')['args']
    assert args['dump_file'] == './state/original.dump'
    assert args['dump_interval'] == 183


def test_native_removing_both_dump_options_preserves_before_interval_for_restore():
    settings = prepared(cache_dump_enabled=True, cache_dump_interval=231)
    stopped = change(settings, lambda value: (plugin(value, 'lazy_cache')['args'].pop('dump_file'),
                                              plugin(value, 'lazy_cache')['args'].pop('dump_interval')))
    resumed = apply(stopped['settings'], {'cache_dump_enabled': True})
    assert plugin(doc(resumed['settings']), 'lazy_cache')['args']['dump_interval'] == 231


@pytest.mark.parametrize('interval', [None, 257, 'unfinished'])
def test_native_dump_clear_parks_explicit_interval_without_validating_inactive_options(interval):
    settings = prepared(cache_dump_enabled=True, cache_dump_interval=183)
    stopped = change(settings, lambda value: plugin(value, 'lazy_cache')['args'].update(dump_file='', dump_interval=interval))
    args = plugin(doc(stopped['settings']), 'lazy_cache')['args']
    assert 'dump_file' not in args and 'dump_interval' not in args
    assert validate_settings(stopped['settings'])['valid']
    assert stopped['settings']['_common_disabled']['cache_dump_interval'] == interval
    resumed = apply(stopped['settings'], {'cache_dump_enabled': True})
    args = plugin(doc(resumed['settings']), 'lazy_cache')['args']
    if interval is None: assert 'dump_interval' not in args
    else: assert args['dump_interval'] == interval


def test_native_interval_omission_and_native_path_addition_keep_core_default_semantics():
    settings = prepared(cache_dump_enabled=True, cache_dump_interval=183)
    no_interval = change(settings, lambda value: plugin(value, 'lazy_cache')['args'].pop('dump_interval'))
    assert plugin(doc(no_interval['settings']), 'lazy_cache')['args']['dump_file']
    assert 'dump_interval' not in plugin(doc(no_interval['settings']), 'lazy_cache')['args']
    stopped = change(settings, lambda value: plugin(value, 'lazy_cache')['args'].pop('dump_file'))
    readded = change(stopped['settings'], lambda value: plugin(value, 'lazy_cache')['args'].update(dump_file='./state/new.dump'))
    assert plugin(doc(readded['settings']), 'lazy_cache')['args']['dump_file'] == './state/new.dump'
    assert 'dump_interval' not in plugin(doc(readded['settings']), 'lazy_cache')['args']


def test_unrelated_native_edit_does_not_normalize_existing_disabled_dump_interval():
    settings = edited(prepared(cache_dump_enabled=False), lambda value: plugin(value, 'lazy_cache')['args'].update(dump_interval=183))
    result = change(settings, lambda value: value['runtime'].update(worker_threads=7))
    assert plugin(doc(result['settings']), 'lazy_cache')['args']['dump_interval'] == 183


@pytest.mark.parametrize('args', [
    {'entries': ['full:inline.test 192.0.2.9']},
    {'files': ['./rules/private-hosts.txt'], 'entries': ['full:inline.test 192.0.2.9']},
])
def test_native_hosts_plugin_deletion_cleans_owned_pair_and_standard_restore_keeps_sources(args):
    settings = prepared()
    settings['additional_files'] = [{'path': 'rules/private-hosts.txt', 'content': 'full:file.test 192.0.2.10\n', 'encoding': 'text'}]
    definition = {'tag': 'custom_hosts', 'type': 'hosts', 'args': deepcopy(args)}
    settings = change(settings, lambda value: value['plugins'].append(deepcopy(definition)))['settings']
    original_main = deepcopy(plugin(doc(settings), 'sequence_main'))
    removed = delete(settings, 'custom_hosts')
    main = plugin(doc(removed['settings']), 'sequence_main')['args']
    assert {'exec': '$custom_hosts'} not in main
    assert all(item['tag'] != 'custom_hosts' for item in doc(removed['settings'])['plugins'])
    assert validate_settings(removed['settings'])['valid']
    candidate = doc(removed['settings'])
    candidate['plugins'].append({'tag': 'custom_hosts', 'type': 'hosts', 'args': {}})
    restored = apply_native_settings(removed['settings'], yaml.safe_dump(candidate), {}, standard_add=['custom_hosts'])
    assert plugin(doc(restored['settings']), 'custom_hosts') == definition
    assert plugin(doc(restored['settings']), 'sequence_main') == original_main
    assert validate_settings(restored['settings'])['valid']


def test_native_hosts_deletion_still_rejects_shared_executor_references():
    settings = prepared(custom_hosts='full:one.test 192.0.2.9\n')
    settings = edited(settings, lambda value: value['plugins'].append({
        'tag': 'shared_hosts', 'type': 'fallback', 'args': {'primary': 'custom_hosts', 'secondary': 'forward_remote'}}))
    original = deepcopy(settings)
    with pytest.raises(OxidnsReferenceError): delete(settings, 'custom_hosts')
    assert settings == original


@pytest.mark.parametrize('field', ['files', 'entries'])
@pytest.mark.parametrize('empty', [[], None, 'omitted'])
def test_native_last_hosts_source_removal_cleans_render_and_does_not_restore_withdrawn_source(field, empty):
    settings = prepared(custom_hosts='full:old.test 192.0.2.9\n')
    if field == 'entries':
        settings = edited(settings, lambda value: plugin(value, 'custom_hosts').update(args={
            'entries': ['full:inline.test 192.0.2.10'], 'short_circuit': False}))
    else:
        settings = edited(settings, lambda value: plugin(value, 'custom_hosts')['args'].update(short_circuit=False))
    def clear(value):
        args = plugin(value, 'custom_hosts')['args']
        if empty == 'omitted': args.pop(field)
        else: args[field] = deepcopy(empty)
    stopped = change(settings, clear)
    runtime = yaml.safe_load(generate_oxidns_config({'oxidns': stopped['settings']}))
    assert all(item['tag'] != 'custom_hosts' for item in runtime['plugins'])
    assert {'exec': '$custom_hosts'} not in plugin(runtime, 'sequence_main')['args']
    assert stopped['settings']['custom_hosts'] == ''
    assert get_oxidns_custom_files({'oxidns': stopped['settings']}) == []
    parked = stopped['settings']['_common_disabled']['custom_hosts']['args']
    assert not parked.get('files') and not parked.get('entries')
    assert parked['short_circuit'] is False
    candidate = doc(stopped['settings'])
    candidate['plugins'].append({'tag': 'custom_hosts', 'type': 'hosts', 'args': {}})
    restored = apply_native_settings(stopped['settings'], yaml.safe_dump(candidate), {}, standard_add=['custom_hosts'])
    args = plugin(doc(restored['settings']), 'custom_hosts')['args']
    assert not args.get('files') and not args.get('entries')
    assert get_oxidns_custom_files({'oxidns': restored['settings']}) == []


@pytest.mark.parametrize('remaining', ['entries', 'files'])
def test_native_hosts_source_removal_keeps_remaining_sources_and_call(remaining):
    settings = prepared(custom_hosts='full:old.test 192.0.2.9\n')
    settings = edited(settings, lambda value: plugin(value, 'custom_hosts')['args'].update(entries=['full:inline.test 192.0.2.10']))
    removed = 'entries' if remaining == 'files' else 'files'
    result = change(settings, lambda value: plugin(value, 'custom_hosts')['args'].pop(removed))
    assert plugin(doc(result['settings']), 'custom_hosts')['args'][remaining]
    assert {'exec': '$custom_hosts'} in plugin(doc(result['settings']), 'sequence_main')['args']


def test_native_empty_hosts_unrelated_edit_is_not_implicitly_cleaned_up():
    settings = change(prepared(), lambda value: value['plugins'].append({'tag': 'custom_hosts', 'type': 'hosts', 'args': {}}))['settings']
    result = change(settings, lambda value: value['runtime'].update(worker_threads=7))
    assert plugin(doc(result['settings']), 'custom_hosts')
    assert {'exec': '$custom_hosts'} in plugin(doc(result['settings']), 'sequence_main')['args']


@pytest.mark.parametrize('reference', ['shared', 'include', 'env'])
def test_native_last_hosts_source_removal_rejects_external_or_shared_calls(reference):
    settings = prepared(custom_hosts='full:old.test 192.0.2.9\n')
    if reference == 'shared':
        settings = edited(settings, lambda value: value['plugins'].append({'tag': 'shared', 'type': 'fallback', 'args': {
            'primary': 'custom_hosts', 'secondary': 'forward_remote'}}))
    elif reference == 'env':
        settings = edited(settings, lambda value: value['plugins'].append({'tag': 'shared', 'type': 'sequence', 'args': [
            {'exec': '${HOSTS_EXECUTOR}'}]}))
    else:
        settings = edited(settings, lambda value: value.update(include=['other.yaml']))
        settings['additional_files'] = [{'path': 'other.yaml', 'content': 'plugins: []\n', 'encoding': 'text'}]
    original = deepcopy(settings)
    with pytest.raises(OxidnsReferenceError) as error:
        change(settings, lambda value: plugin(value, 'custom_hosts')['args'].update(files=[]))
    assert error.value.references
    assert settings == original
