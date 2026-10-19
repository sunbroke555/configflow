"""Clearing managed Hosts removes only withdrawn rules and unused runtime state."""
from copy import deepcopy
import json

import pytest
import yaml

from backend.common.oxidns_common import read_common_settings
from backend.common.oxidns_native_apply import apply_native_settings
from backend.common.oxidns_references import reference_locations
from backend.common.oxidns_settings import OxidnsSettingsError
from backend.common.oxidns_validation import validate_settings
from backend.converters.oxidns import generate_oxidns_config, get_oxidns_custom_files
from backend.test_oxidns_common import apply, doc, edited, plugin, prepared


OLD = 'full:withdrawn.test 192.0.2.10\n'
NEW = 'full:replacement.test 192.0.2.11\n'
INLINE = ['full:inline.test 192.0.2.12']
HOSTS_PATH = 'rules/custom_hosts.txt'


def render(settings):
    config = {'oxidns': settings}
    assert validate_settings(settings)['valid']
    return yaml.safe_load(generate_oxidns_config(config)), get_oxidns_custom_files(config)


def clear(settings, value=''):
    before = deepcopy(settings)
    result = apply(settings, {'custom_hosts': value})
    assert settings == before
    return result['settings']


def assert_absent(document, files):
    assert not any(item['tag'] == 'custom_hosts' for item in document['plugins'])
    assert reference_locations(document, ['custom_hosts']) == []
    assert not any(item['path'].removeprefix('./') == HOSTS_PATH for item in files)


@pytest.mark.parametrize('empty', ['', ' \n\t', None])
def test_clear_removes_runtime_hosts_and_restores_options_only_with_new_rules(empty):
    settings = edited(prepared(custom_hosts=OLD), lambda value: plugin(value, 'custom_hosts')['args'].update(
        entries=[], short_circuit=False, future={'preserve': [0, False, None]}))
    main = deepcopy(plugin(doc(settings), 'sequence_main')['args'])
    index = main.index({'exec': '$custom_hosts'})
    del main[index:index + 2]

    disabled = clear(settings, empty)
    document, files = render(disabled)
    assert_absent(document, files)
    assert plugin(document, 'sequence_main')['args'] == main
    assert read_common_settings(disabled, {})['values']['custom_hosts'] == ''

    reopened = json.loads(json.dumps(disabled))
    restored = apply(reopened, {'custom_hosts': NEW})['settings']
    document, files = render(restored)
    args = plugin(document, 'custom_hosts')['args']
    assert args['short_circuit'] is False
    assert args['future'] == {'preserve': [0, False, None]}
    assert [item['content'] for item in files if item['path'].removeprefix('./') == HOSTS_PATH] == [NEW]
    assert 'withdrawn.test' not in str(files)
    assert plugin(document, 'sequence_main')['args'].count({'exec': '$custom_hosts'}) == 1


def test_clear_is_idempotent_and_standard_restore_cannot_revive_withdrawn_source():
    disabled = clear(prepared(custom_hosts=OLD))
    disabled = clear(json.loads(json.dumps(disabled)))
    assert_absent(*render(disabled))
    candidate = doc(disabled)
    candidate['plugins'].append({'tag': 'custom_hosts', 'type': 'hosts', 'args': {}})
    restored = apply_native_settings(disabled, yaml.safe_dump(candidate), {}, standard_add=['custom_hosts'])['settings']
    document, files = render(restored)
    assert plugin(document, 'custom_hosts')['args'].get('files', []) == []
    assert not any(item['path'].removeprefix('./') == HOSTS_PATH for item in files)
    assert 'withdrawn.test' not in str(files)


def test_clear_without_a_plugin_does_not_create_one_or_keep_stale_native_source():
    settings = prepared()
    settings['_common_disabled'] = {'native_hosts_source': OLD}
    result = clear(settings)
    assert_absent(*render(result))
    assert 'native_hosts_source' not in result['_common_disabled']


def test_clear_does_not_park_an_invalid_null_entries_value_for_restore():
    settings = edited(prepared(custom_hosts=OLD), lambda value: plugin(value, 'custom_hosts')['args'].update(entries=None))
    disabled = clear(settings)
    assert_absent(*render(disabled))
    restored = apply(disabled, {'custom_hosts': NEW})['settings']
    document, _ = render(restored)
    assert 'entries' not in plugin(document, 'custom_hosts')['args']


@pytest.mark.parametrize('managed_file', [False, True])
def test_clear_preserves_inline_hosts_rules_and_their_existing_call(managed_file):
    settings = edited(prepared(custom_hosts=OLD), lambda value: plugin(value, 'custom_hosts')['args'].update(
        entries=deepcopy(INLINE), short_circuit=False))
    if not managed_file:
        settings = edited(settings, lambda value: plugin(value, 'custom_hosts')['args'].update(files=[]))
    original_main = deepcopy(plugin(doc(settings), 'sequence_main'))

    result = clear(settings)
    document, files = render(result)
    args = plugin(document, 'custom_hosts')['args']
    assert args['entries'] == INLINE
    assert args['short_circuit'] is False
    assert args['files'] == []
    assert plugin(document, 'sequence_main') == original_main
    assert not any(item['path'].removeprefix('./') == HOSTS_PATH for item in files)
    assert read_common_settings(result, {})['values']['custom_hosts'] == ''


@pytest.mark.parametrize('paths', [
    ['./rules/private-hosts.txt'],
    ['./rules/custom_hosts.txt', './rules/private-hosts.txt'],
])
def test_custom_host_files_cannot_be_cleared_through_the_managed_field(paths):
    settings = edited(prepared(custom_hosts=OLD), lambda value: plugin(value, 'custom_hosts')['args'].update(files=paths))
    settings['additional_files'] = [{'path': 'rules/private-hosts.txt', 'encoding': 'text', 'content': NEW}]
    before = deepcopy(settings)
    assert read_common_settings(settings, {})['capabilities']['custom_hosts']['editable'] is False
    with pytest.raises(OxidnsSettingsError, match='Hosts|自定义'):
        apply(settings, {'custom_hosts': ''})
    assert settings == before
    assert render(settings) == render(before)


@pytest.mark.parametrize('consumer', [
    {'tag': 'manual', 'type': 'sequence', 'args': [{'exec': '$custom_hosts'}]},
    {'tag': 'manual', 'type': 'fallback', 'args': {'primary': 'custom_hosts', 'secondary': 'forward_remote'}},
    {'tag': 'manual', 'type': 'cron', 'args': {'jobs': [
        {'name': 'manual', 'schedule': '0 * * * *', 'executors': ['$custom_hosts']},
    ]}},
    {'tag': 'manual', 'type': 'sequence', 'args': [{'exec': '${HOSTS_EXECUTOR}'}]},
])
def test_shared_or_dynamic_executor_references_prevent_automatic_clear(consumer):
    settings = edited(prepared(custom_hosts=OLD), lambda value: value['plugins'].append(deepcopy(consumer)))
    before = deepcopy(settings)
    assert read_common_settings(settings, {})['capabilities']['custom_hosts']['editable'] is False
    with pytest.raises(OxidnsSettingsError, match='引用|执行链'):
        apply(settings, {'custom_hosts': ''})
    assert settings == before


def test_shared_file_consumer_keeps_source_bytes_after_standard_plugin_is_removed():
    consumer = {'tag': 'manual_hosts', 'type': 'hosts', 'args': {'files': ['./rules/custom_hosts.txt']}}
    settings = edited(prepared(custom_hosts=OLD), lambda value: value['plugins'].append(deepcopy(consumer)))
    result = clear(settings)
    document, files = render(result)
    assert not any(item['tag'] == 'custom_hosts' for item in document['plugins'])
    assert reference_locations(document, ['custom_hosts']) == []
    assert plugin(document, 'manual_hosts') == consumer
    assert result['custom_hosts'] == OLD
    assert read_common_settings(result, {})['values']['custom_hosts'] == ''
    assert [item['content'] for item in files if item['path'].removeprefix('./') == HOSTS_PATH] == [OLD]


@pytest.mark.parametrize('included_call', [False, True])
def test_include_keeps_automatic_host_cleanup_disabled(included_call):
    settings = edited(prepared(custom_hosts=OLD), lambda value: value.update(include=['manual.yaml']))
    included = {'plugins': [{'tag': 'manual', 'type': 'sequence', 'args': [
        {'exec': '$custom_hosts' if included_call else 'accept'},
    ]}]}
    settings['additional_files'] = [{'path': 'manual.yaml', 'encoding': 'text', 'content': yaml.safe_dump(included)}]
    before = deepcopy(settings)
    assert read_common_settings(settings, {})['capabilities']['custom_hosts']['editable'] is False
    with pytest.raises(OxidnsSettingsError, match='外部执行链'):
        apply(settings, {'custom_hosts': ''})
    assert settings == before
    assert render(settings) == render(before)
