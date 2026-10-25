"""Custom match actions resolve native plugin names without rewriting user execs."""
from copy import deepcopy

import pytest
import yaml

from backend.common.oxidns_common import apply_common_settings, read_common_settings
from backend.common.oxidns_editor import prepare_editor
from backend.common.oxidns_native_apply import apply_native_settings
from backend.common.oxidns_settings import OxidnsSettingsError
from backend.test_oxidns_common_routing import fixture_config
from backend.test_oxidns_integration import oxidns_app


def document(settings):
    return yaml.safe_load(settings['native_config'])


def plugin(doc, tag):
    return next(item for item in doc['plugins'] if item['tag'] == tag)


def edited(settings, callback):
    result = deepcopy(settings)
    doc = document(result)
    callback(doc)
    result['native_config'] = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)
    return result


def prepared(config=None, **values):
    return prepare_editor(values, config or {})


def renamed(settings, mapping, config=None):
    doc = document(settings)
    for old, new in mapping.items():
        plugin(doc, old)['tag'] = new
    return apply_native_settings(settings, yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
                                 config or {}, renames=mapping)['settings']


def targets(state):
    return {item['tag']: item for item in state['custom_match_targets']}


def test_targets_use_each_configured_sequence_and_executor_without_guessing_dns_roles():
    settings = edited(prepared(), lambda doc: doc['plugins'].extend([
        {'tag': 'local_dns', 'type': 'sequence', 'args': [{'exec': '$china_forward'}]},
        {'tag': 'china_forward', 'type': 'forward', 'args': {'upstreams': [{'addr': 'udp://192.0.2.20'}]}},
        {'tag': 'alternate_dns', 'type': 'sequence', 'args': [{'exec': 'accept'}]},
        {'tag': 'other_forward', 'type': 'forward', 'args': {'upstreams': [{'addr': 'udp://192.0.2.21'}]}},
        {'tag': 'domain_source', 'type': 'domain_set', 'args': {'exps': ['full:example.test']}},
        {'tag': 'match_query', 'type': 'qtype', 'args': 'A'},
    ]))
    before = deepcopy(settings)
    state = read_common_settings(settings, {})
    available = targets(state)
    assert available['local_dns'] == {'tag': 'local_dns', 'type': 'sequence',
                                      'exec': 'goto local_dns', 'source': 'config.yaml'}
    assert available['alternate_dns']['exec'] == 'goto alternate_dns'
    assert available['china_forward']['exec'] == '$china_forward'
    assert available['other_forward']['exec'] == '$other_forward'
    assert not {'sequence_main', 'server_udp', 'server_tcp', 'domain_source', 'match_query'} & available.keys()
    assert {'domain_source', 'match_query', 'sequence_main'} <= set(state['custom_match_target_tags'])
    assert settings == before
    changed = apply_common_settings(settings, {'custom_matches': [
        {'matches': ['qname full:first.test'], 'exec': 'goto alternate_dns'},
        {'matches': ['qname full:second.test'], 'exec': '$china_forward'},
    ]}, {})
    assert [entry['exec'] for entry in changed['values']['custom_matches']] == ['goto alternate_dns', '$china_forward']
    assert changed['custom_match_targets'] == state['custom_match_targets']


def test_targets_expand_nested_includes_and_report_the_declaring_source():
    settings = prepared()
    settings['additional_files'] = [
        {'path': 'dns/main.yaml', 'content': yaml.safe_dump({'include': ['executors.yaml'], 'plugins': [
            {'tag': 'included_dns', 'type': 'sequence', 'args': [{'exec': '$included_forward'}]}]})},
        {'path': 'dns/executors.yaml', 'content': yaml.safe_dump({'plugins': [
            {'tag': 'included_forward', 'type': 'forward', 'args': {'upstreams': [{'addr': 'udp://192.0.2.22'}]}}]})},
    ]
    settings = edited(settings, lambda doc: doc.update(include=['dns/main.yaml']))
    state = read_common_settings(settings, {})
    assert targets(state)['included_dns']['source'] == 'dns/main.yaml'
    assert targets(state)['included_forward']['source'] == 'dns/executors.yaml'
    changed = apply_common_settings(settings, {'custom_matches': [
        {'matches': ['qname full:include.test'], 'exec': 'goto included_dns'}]}, {})
    assert changed['values']['custom_matches'][0]['exec'] == 'goto included_dns'
    assert changed['settings']['additional_files'] == settings['additional_files']


def test_targets_exclude_duplicate_invalid_unknown_and_recursive_targets():
    settings = edited(prepared(), lambda doc: doc['plugins'].extend([
        {'tag': 'duplicate', 'type': 'sequence', 'args': [{'exec': 'accept'}]},
        {'tag': 'duplicate', 'type': 'forward', 'args': {'upstreams': [{'addr': 'udp://192.0.2.23'}]}},
        {'tag': 'uses_duplicate', 'type': 'sequence', 'args': [{'exec': '$duplicate'}]},
        {'tag': 'bad tag', 'type': 'sequence', 'args': []},
        {'tag': 'qs.exec.reserved', 'type': 'sequence', 'args': []},
        {'tag': 'future_dns', 'type': 'future_executor', 'args': {'keep': True}},
        {'tag': 'back_to_main', 'type': 'sequence', 'args': [{'exec': 'goto sequence_main'}]},
        {'tag': 'nested_back', 'type': 'sequence', 'args': [{'exec': '$back_to_main'}]},
        {'tag': 'fallback_back', 'type': 'fallback', 'args': {'primary': 'sequence_main', 'secondary': 'china_dns'}},
        {'tag': 'probe_back', 'type': 'prefer_ipv4', 'args': {'probe_executor': 'nested_back'}},
        {'tag': 'cycle_a', 'type': 'sequence', 'args': [{'exec': '$cycle_b'}]},
        {'tag': 'cycle_b', 'type': 'sequence', 'args': [{'exec': 'jump cycle_a'}]},
        {'tag': 'uses_cycle', 'type': 'sequence', 'args': [{'exec': 'goto cycle_a'}]},
        {'tag': 'self_loop', 'type': 'sequence', 'args': [{'exec': '$self_loop'}]},
    ]))
    state = read_common_settings(settings, {})
    unavailable = {'duplicate', 'uses_duplicate', 'bad tag', 'qs.exec.reserved', 'future_dns',
                   'back_to_main', 'nested_back', 'fallback_back', 'probe_back',
                   'cycle_a', 'cycle_b', 'uses_cycle', 'self_loop'}
    assert not unavailable & targets(state).keys()
    assert unavailable <= set(state['custom_match_target_tags'])
    assert state['custom_match_target_tags'].count('duplicate') == 1
    assert 'china_dns' in targets(state)


def test_missing_and_unknown_targets_remain_verbatim_and_have_distinct_presence():
    matches = [{'matches': ['qname full:unknown.test'], 'exec': '$future_dns', 'future': {'keep': [False, 0]}},
               {'matches': ['qname full:deleted.test'], 'exec': 'goto deleted_dns'}]
    def edit(doc):
        doc['plugins'].append({'tag': 'future_dns', 'type': 'future_executor', 'args': {'opaque': None}})
        plugin(doc, 'sequence_main')['args'][-1:-1] = deepcopy(matches)
    settings = edited(prepared(), edit)
    state = read_common_settings(settings, {})
    assert state['values']['custom_matches'] == matches
    assert 'future_dns' in state['custom_match_target_tags']
    assert 'deleted_dns' not in state['custom_match_target_tags']
    assert 'future_dns' not in targets(state)
    changed = apply_common_settings(settings, {'custom_match_position': 'head'}, {})
    assert changed['values']['custom_matches'] == matches
    assert changed['custom_match_target_tags'] == state['custom_match_target_tags']


@pytest.mark.parametrize('default_forward', ['forward_local', 'forward_remote'])
@pytest.mark.parametrize('mapping', [
    {'china_dns': 'local_dns'},
    {'sequence_main': 'entry_dns', 'china_dns': 'local_dns', 'proxy_dns_seq': 'external_dns'},
])
def test_recorded_managed_renames_keep_matches_editable_and_other_entries_exact(mapping, default_forward):
    config = fixture_config()
    original = prepared(config, direct_rules=['a', 'ip'], proxy_rules=['c'], default_forward=default_forward,
                        custom_matches=[{'matches': ['qname full:custom.test'], 'exec': 'goto china_dns'}])
    original = edited(original, lambda doc: plugin(doc, 'sequence_main')['args'][-1].update(
        future={'off': False, 'nil': None, 'zero': 0}))
    settings = renamed(original, mapping, config)
    before = document(settings)
    main_tag = mapping.get('sequence_main', 'sequence_main')
    state = read_common_settings(settings, config)
    match = {'matches': ['qname full:custom.test'], 'exec': 'goto local_dns'}
    assert state['capabilities']['custom_matches']['editable']
    assert state['capabilities']['custom_match_position']['target'] == main_tag
    assert state['values']['custom_matches'] == [match]
    assert targets(state)['local_dns']['exec'] == 'goto local_dns'
    assert main_tag not in targets(state)
    replacement = {**match, 'exec': '$forward_remote', 'future': {'off': False}}
    changed = apply_common_settings(settings, {'custom_matches': [replacement], 'custom_match_position': 'head'}, config)
    after = document(changed['settings'])
    old_main, new_main = plugin(before, main_tag)['args'], plugin(after, main_tag)['args']
    assert [entry for entry in new_main if entry != replacement] == [entry for entry in old_main if entry != match]
    assert new_main.index(replacement) < next(index for index, entry in enumerate(new_main)
                                              if entry.get('matches', [''])[0].startswith('qname $rules_domain'))
    plugin(after, main_tag)['args'] = old_main
    assert after == before
    assert changed['settings']['_common_disabled'] == settings['_common_disabled']
    assert changed['values']['custom_matches'] == [replacement]
    assert changed['values']['custom_match_position'] == 'head'
    assert document(settings) == before


@pytest.mark.parametrize('with_domain', [False, True])
@pytest.mark.parametrize('position', ['head', 'tail'])
def test_renamed_match_insertion_and_removal_preserve_empty_regions(with_domain, position):
    config = fixture_config()
    settings = renamed(prepared(config, direct_rules=['a'] if with_domain else [],
                                default_forward='forward_local'), {'china_dns': 'local_dns'}, config)
    before = document(settings)
    first = {'matches': ['qname full:custom.test'], 'exec': 'goto local_dns', 'future': {'nil': None, 'zero': 0}}
    added = apply_common_settings(settings, {'custom_matches': [first]}, config)
    moved = apply_common_settings(added['settings'], {'custom_match_position': position}, config)
    assert moved['values']['custom_matches'] == [first]
    assert moved['values']['custom_match_position'] == (position if with_domain else 'tail')
    removed = apply_common_settings(moved['settings'], {'custom_matches': []}, config)
    assert document(removed['settings']) == before
    assert removed['values']['custom_matches'] == []
    assert removed['values']['custom_match_position'] is None


def test_renamed_main_and_transitive_callers_are_not_offered():
    settings = renamed(prepared(), {'sequence_main': 'entry_dns'})
    settings = edited(settings, lambda doc: doc['plugins'].append(
        {'tag': 'back_to_entry', 'type': 'sequence', 'args': [{'exec': 'goto entry_dns'}]}))
    assert not {'entry_dns', 'back_to_entry'} & targets(read_common_settings(settings, {})).keys()


def test_renamed_routing_does_not_unlock_unrecorded_or_ambiguous_structures():
    original = prepared(default_forward='forward_local')
    settings = renamed(original, {'china_dns': 'local_dns'})
    unrecorded = deepcopy(settings)
    unrecorded.pop('_common_disabled', None)
    assert not read_common_settings(unrecorded, {})['capabilities']['custom_matches']['editable']
    malformed = edited(settings, lambda doc: plugin(doc, 'sequence_main')['args'].insert(-1, {'exec': 'debug_print'}))
    assert not read_common_settings(malformed, {})['capabilities']['custom_matches']['editable']
    assert not read_common_settings(settings, {})['capabilities']['default_forward']['editable']
    with pytest.raises(OxidnsSettingsError, match='完整参数'):
        apply_common_settings(malformed, {'custom_matches': [{'matches': ['qtype A'], 'exec': 'accept'}]}, {})
    with pytest.raises(OxidnsSettingsError, match='自定义匹配'):
        apply_common_settings(settings, {'custom_matches': [{'matches': ['qtype A'], 'exec': ''}]}, {})


@pytest.mark.parametrize('prefix', ['/api/oxidns', '/api/profiles/office/oxidns'])
def test_common_and_native_api_expose_current_target_catalog(oxidns_app, prefix):
    app, repository = oxidns_app
    client = app.test_client()
    config = repository.get_compat_config('office')
    settings = prepared(config)
    read = client.post(prefix + '/common-settings?profile=office', json={'settings': settings})
    assert read.status_code == 200, read.get_json()
    assert targets(read.get_json())['china_dns']['exec'] == 'goto china_dns'
    doc = document(settings)
    plugin(doc, 'china_dns')['tag'] = 'local_dns'
    changed = client.post(prefix + '/native-settings/apply?profile=office', json={
        'settings': settings, 'native_config': yaml.safe_dump(doc), 'renames': {'china_dns': 'local_dns'}})
    assert changed.status_code == 200, changed.get_json()
    assert targets(changed.get_json())['local_dns']['exec'] == 'goto local_dns'
    assert 'china_dns' not in changed.get_json()['custom_match_target_tags']
