"""Native entry-flow editing never regenerates a user's hand-written topology."""
import base64
import json
from copy import deepcopy

import pytest
import yaml

from backend.common.oxidns_common import apply_common_settings, read_common_settings
from backend.common.oxidns_native import load_native_yaml
from backend.common.oxidns_settings import OxidnsSettingsError
from backend.test_oxidns_integration import oxidns_app


def native():
    return {'log': {'level': 'warn', 'future': {'keep': False}}, 'plugins': [
        {'tag': 'local_forward', 'type': 'forward', 'args': {'upstreams': [{'addr': 'udp://192.0.2.53'}], 'concurrent': 1}},
        {'tag': 'shared', 'type': 'sequence', 'notes': {'zero': 0},
         'args': [{'exec': '$local_forward'}, {'exec': 'accept'}]},
        {'tag': 'entry_alpha', 'type': 'sequence', 'args': [
            {'matches': 'qname full:alpha.test', 'exec': 'goto shared', 'future': [False, None, 0]},
            {'exec': 'goto shared'}]},
        {'tag': 'entry_beta', 'type': 'sequence', 'args': [{'exec': '$shared'}, {'exec': 'return'}]},
        {'tag': 'sequence_main', 'type': 'sequence', 'args': [{'exec': 'accept'}]},
        {'tag': 'future_plugin', 'type': 'future_executor', 'custom': {'nil': None}, 'args': ['opaque', False, 0]},
        {'tag': 'udp_alpha', 'type': 'udp_server', 'args': {'listen': '127.0.0.1:15353', 'entry': 'entry_alpha'}},
        {'tag': 'tcp_alpha', 'type': 'tcp_server', 'args': {'listen': '127.0.0.1:15353', 'entry': 'entry_alpha'}},
        {'tag': 'quic_beta', 'type': 'quic_server', 'args': {
            'listen': '127.0.0.1:15354', 'entry': 'entry_beta', 'cert': 'server.crt', 'key': 'server.key'}},
        {'tag': 'http_dns', 'type': 'http_server', 'args': {'listen': '127.0.0.1:15355', 'entries': [
            {'path': '/dns-query', 'exec': 'entry_alpha'}, {'path': '/forward', 'exec': 'local_forward'}]}},
    ], 'runtime': {'worker_threads': 2}, 'future_global': {'off': False, 'zero': 0, 'nil': None}}


def settings(doc=None, files=None):
    return {'config_mode': 'native', 'native_config': yaml.safe_dump(doc or native(), allow_unicode=True, sort_keys=False),
            'additional_files': deepcopy(files or [])}


def projection(value):
    return read_common_settings(value, {})['entry_flow']


def node(value, tag):
    return next(item for item in projection(value)['plugins'] if item['tag'] == tag)


def patch(value, target, replacement):
    return apply_common_settings(value, {'entry_flow': {
        'source': target['source'], 'index': target['index'], 'before': target['value'], 'value': replacement}}, {})


def test_projection_lists_all_native_entries_shared_flows_parameters_and_edges_without_mutation():
    value = settings()
    before = deepcopy(value)
    flow = projection(value)
    nodes = {item['tag']: item for item in flow['plugins']}
    assert len(flow['plugins']) == len(native()['plugins'])
    assert len(flow['entries']) == 5
    assert len({item['id'] for item in flow['entries']}) == 5
    assert {item['listener_type'] for item in flow['entries']} == {'udp_server', 'tcp_server', 'quic_server', 'http_server'}
    assert sum(item['entry'] == 'entry_alpha' for item in flow['entries']) == 3
    direct = next(item for item in flow['entries'] if item['entry'] == 'local_forward')
    assert direct['reference_path'] == ['args', 'entries', 1, 'exec']
    assert direct['path'] == '/forward'
    assert all('path' not in item for item in flow['entries'] if item['listener_type'] != 'http_server')
    assert nodes['local_forward']['value']['type'] == 'forward'
    assert nodes['future_plugin']['kind'] == 'unknown'
    assert nodes['future_plugin']['editable']
    assert nodes['future_plugin']['value']['args'] == ['opaque', False, 0]
    assert {'from': nodes['entry_alpha']['id'], 'to': nodes['shared']['id'], 'target': 'shared',
            'path': ['args', 0, 'exec']} in flow['edges']
    assert flow['globals']['value'] == {key: item for key, item in native().items() if key != 'plugins'}
    assert value == before


def test_target_choices_follow_the_selected_node_instead_of_sequence_main():
    value = settings()
    alpha = {item['tag']: item for item in node(value, 'entry_alpha')['targets']}
    shared = {item['tag']: item for item in node(value, 'shared')['targets']}
    assert 'entry_alpha' not in alpha
    assert alpha['shared']['exec'] == 'goto shared'
    assert alpha['local_forward']['exec'] == '$local_forward'
    assert 'sequence_main' in alpha
    assert not {'shared', 'entry_alpha', 'entry_beta'} & shared.keys()
    assert 'local_forward' in shared
    assert not {'future_plugin', 'udp_alpha', 'tcp_alpha'} & alpha.keys()
    assert node(value, 'local_forward')['targets'] == []
    assert node(value, 'udp_alpha')['targets'] == []
    assert node(value, 'future_plugin')['targets'] == []


def test_reorder_and_edit_raw_sequence_preserve_every_other_value_and_key_position():
    value = settings()
    target = node(value, 'entry_alpha')
    replacement = deepcopy(target['value'])
    replacement['args'][0]['matches'] = ['qname full:changed.test', 'qtype A']
    replacement['args'][0]['exec'] = '$local_forward'
    replacement['args'].reverse()
    replacement['new_extension'] = {'zero': 0, 'off': False}
    result = patch(value, target, replacement)
    expected = native()
    expected['plugins'][target['index']] = replacement
    assert load_native_yaml(result['settings']['native_config']) == expected
    assert node(result['settings'], 'entry_alpha')['value'] == replacement
    assert list(load_native_yaml(result['settings']['native_config'])) == list(native())
    assert value == settings()


def test_nonsequence_and_unknown_plugin_parameters_are_edited_without_type_conversion():
    value = settings()
    forward = node(value, 'local_forward')
    replacement = deepcopy(forward['value'])
    replacement['args'].update(concurrent=3, future={'array': [False, None, 0]})
    value = patch(value, forward, replacement)['settings']
    assert node(value, 'local_forward')['value'] == replacement
    future = node(value, 'future_plugin')
    replacement = {**future['value'], 'args': {'new_shape': None}, 'custom': {'edited': False}}
    changed = patch(value, future, replacement)['settings']
    assert node(changed, 'future_plugin')['value'] == replacement
    assert node(changed, 'local_forward')['value']['type'] == 'forward'


@pytest.mark.parametrize('encoding', ['text', 'base64'])
def test_nested_include_edit_changes_only_the_declaring_attachment(encoding):
    root = native()
    shared = root['plugins'].pop(1)
    root['include'] = ['dns/outer.yaml']
    included = {'future_global': {'keep': False}, 'plugins': [shared]}
    text = yaml.safe_dump(included, sort_keys=False)
    files = [{'path': 'dns/outer.yaml', 'content': '# keep this file byte exact\ninclude: [inner.yaml]\nplugins: []\n'},
             {'path': 'dns/inner.yaml', 'encoding': encoding,
              'content': base64.b64encode(text.encode()).decode() if encoding == 'base64' else text,
              'future_metadata': {'keep': True}},
             {'path': 'unused.txt', 'content': 'untouched\n'}]
    value = settings(root, files)
    before = deepcopy(value)
    target = node(value, 'shared')
    assert target['id'] == 'dns/inner.yaml#0'
    assert target['source'] == 'dns/inner.yaml'
    replacement = deepcopy(target['value'])
    replacement['args'].insert(0, {'matches': ['qtype AAAA'], 'exec': 'reject 0', 'future': {'nil': None}})
    changed = patch(value, target, replacement)['settings']
    assert changed['native_config'] == before['native_config']
    assert changed['additional_files'][0] == before['additional_files'][0]
    assert changed['additional_files'][2] == before['additional_files'][2]
    saved = changed['additional_files'][1]
    assert saved['encoding'] == encoding
    assert saved['future_metadata'] == {'keep': True}
    decoded = base64.b64decode(saved['content']).decode() if encoding == 'base64' else saved['content']
    assert load_native_yaml(decoded) == {**included, 'plugins': [replacement]}
    assert node(changed, 'shared')['id'] == target['id']
    assert value == before


def test_globals_edit_preserves_plugins_order_unknown_fields_and_attachments():
    value = settings(files=[{'path': 'notes.txt', 'content': 'unchanged'}])
    flow = projection(value)
    replacement = deepcopy(flow['globals']['value'])
    replacement['log']['level'] = 'debug'
    replacement['future_global']['zero'] = 7
    replacement['new_global'] = [False, None, 0]
    result = apply_common_settings(value, {'entry_flow': {'kind': 'globals', 'before': flow['globals']['value'],
                                                         'value': replacement}}, {})
    actual = load_native_yaml(result['settings']['native_config'])
    assert actual['plugins'] == native()['plugins']
    assert {key: item for key, item in actual.items() if key != 'plugins'} == replacement
    assert list(actual) == [*native(), 'new_global']
    assert result['settings']['additional_files'] == value['additional_files']


def test_globals_include_changes_refresh_nodes_without_rewriting_plugins_or_files():
    attachment = {'path': 'extra.yaml', 'content': yaml.safe_dump({'plugins': [
        {'tag': 'included_dns', 'type': 'sequence', 'args': [{'exec': 'accept'}]}]})}
    value = settings(files=[attachment])
    original_plugins = deepcopy(native()['plugins'])
    original_globals = projection(value)['globals']['value']
    added = apply_common_settings(value, {'entry_flow': {'kind': 'globals', 'before': original_globals,
                                                        'value': {**original_globals, 'include': ['extra.yaml']}}}, {})
    assert node(added['settings'], 'included_dns')['source'] == 'extra.yaml'
    target = node(added['settings'], 'entry_alpha')
    replacement = deepcopy(target['value'])
    replacement['args'].insert(0, {'exec': 'goto included_dns'})
    referenced = patch(added['settings'], target, replacement)
    globals = referenced['entry_flow']['globals']['value']
    removed = apply_common_settings(referenced['settings'], {'entry_flow': {
        'kind': 'globals', 'before': globals, 'value': {key: item for key, item in globals.items() if key != 'include'}}}, {})
    assert all(item['tag'] != 'included_dns' for item in removed['entry_flow']['plugins'])
    assert next(edge for edge in removed['entry_flow']['edges'] if edge['target'] == 'included_dns')['to'] is None
    expected = deepcopy(original_plugins)
    expected[target['index']] = replacement
    assert load_native_yaml(removed['settings']['native_config'])['plugins'] == expected
    assert removed['settings']['additional_files'] == [attachment]
    assert value == settings(files=[attachment])


@pytest.mark.parametrize('change', ['content', 'index', 'bool_type', 'tag', 'type'])
def test_stale_or_identity_changing_plugin_edits_fail_without_mutation(change):
    value = settings()
    target = node(value, 'shared')
    replacement = deepcopy(target['value'])
    if change == 'content':
        target['value']['args'].append({'exec': 'reject'})
    elif change == 'index':
        target['index'] += 1
    elif change == 'bool_type':
        target['value']['notes']['zero'] = False
    else:
        replacement[change] = 'changed'
    before = deepcopy(value)
    with pytest.raises(OxidnsSettingsError):
        patch(value, target, replacement)
    assert value == before


def test_globals_stale_snapshot_plugins_injection_and_bad_include_are_rejected_atomically():
    value = settings()
    before = projection(value)['globals']['value']
    stale = deepcopy(before)
    stale['runtime']['worker_threads'] = 99
    for old, new in [(stale, before), (before, {**before, 'plugins': []}),
                     (before, {**before, 'include': ['missing.yaml']})]:
        with pytest.raises(OxidnsSettingsError):
            apply_common_settings(value, {'entry_flow': {'kind': 'globals', 'before': old, 'value': new}}, {})
    assert value == settings()


def test_unreferenced_attachment_and_mixed_common_changes_cannot_bypass_identity_checks():
    value = settings(files=[{'path': 'unused.yaml', 'content': yaml.safe_dump({'plugins': [native()['plugins'][0]]})}])
    target = node(value, 'local_forward')
    target.update(source='unused.yaml', index=0)
    with pytest.raises(OxidnsSettingsError, match='不在当前配置'):
        patch(value, target, target['value'])
    target = node(value, 'local_forward')
    with pytest.raises(OxidnsSettingsError, match='单独保存'):
        apply_common_settings(value, {'worker_threads': 4, 'entry_flow': {
            'source': target['source'], 'index': target['index'], 'before': target['value'], 'value': target['value']}}, {})


def test_existing_missing_and_unknown_references_remain_visible_and_do_not_block_draft_edits():
    doc = native()
    doc['plugins'][2]['args'].extend([{'exec': 'goto missing'}, {'exec': '$future_plugin'}])
    value = settings(doc)
    flow = projection(value)
    missing = next(edge for edge in flow['edges'] if edge['target'] == 'missing')
    assert missing['to'] is None
    assert next(edge for edge in flow['edges'] if edge['target'] == 'future_plugin')['to'] == node(value, 'future_plugin')['id']
    target = node(value, 'entry_alpha')
    replacement = deepcopy(target['value'])
    replacement['args'][0]['matches'] = 'qname full:edited.test'
    changed = patch(value, target, replacement)['settings']
    assert node(changed, 'entry_alpha')['value']['args'][-2:] == [{'exec': 'goto missing'}, {'exec': '$future_plugin'}]


def test_repeated_include_has_one_source_node_and_ambiguous_reference_stays_unresolved():
    root = native()
    shared = root['plugins'].pop(1)
    root['include'] = ['shared.yaml', 'shared.yaml']
    value = settings(root, [{'path': 'shared.yaml', 'content': yaml.safe_dump({'plugins': [shared]})}])
    flow = projection(value)
    assert sum(item['tag'] == 'shared' for item in flow['plugins']) == 1
    assert all(edge['to'] is None for edge in flow['edges'] if edge['target'] == 'shared')
    assert 'shared' not in {item['tag'] for item in node(value, 'entry_alpha')['targets']}


def test_noop_edits_preserve_original_yaml_text_and_return_fresh_metadata():
    value = settings()
    value['native_config'] = '# retain all formatting when unchanged\n' + value['native_config']
    target = node(value, 'shared')
    changed = patch(value, target, target['value'])
    assert changed['settings']['native_config'] == value['native_config']
    globals = projection(value)['globals']['value']
    changed = apply_common_settings(value, {'entry_flow': {'kind': 'globals', 'before': globals, 'value': globals}}, {})
    assert changed['settings']['native_config'] == value['native_config']


@pytest.mark.parametrize('prefix', ['/api/oxidns', '/api/profiles/office/oxidns'])
def test_entry_flow_api_uses_existing_profile_context_without_persisting(oxidns_app, prefix):
    app, repository = oxidns_app
    client = app.test_client()
    before = deepcopy(repository.get_profile('office'))
    value = settings()
    read = client.post(prefix + '/common-settings?profile=office', json={'settings': value})
    assert read.status_code == 200, read.get_json()
    target = next(item for item in read.get_json()['entry_flow']['plugins'] if item['tag'] == 'entry_beta')
    replacement = deepcopy(target['value'])
    replacement['args'] = [{'matches': ['qtype AAAA'], 'exec': 'reject 0'}, *replacement['args']]
    updated = client.post(prefix + '/common-settings/apply?profile=office', json={'settings': value, 'changes': {
        'entry_flow': {'source': target['source'], 'index': target['index'], 'before': target['value'], 'value': replacement}}})
    assert updated.status_code == 200, updated.get_json()
    assert next(item for item in updated.get_json()['entry_flow']['plugins'] if item['tag'] == 'entry_beta')['value'] == replacement
    assert updated.headers['Cache-Control'] == 'no-store'
    assert repository.get_profile('office') == before


@pytest.mark.parametrize('included', [False, True])
def test_plugin_edit_detaches_top_level_yaml_aliases(included):
    original = native()['plugins']
    declaring = {'saved_plugins': original, 'plugins': original, 'future': {'keep': False}}
    text = yaml.safe_dump(declaring, sort_keys=False)
    loaded = load_native_yaml(text)
    assert loaded['saved_plugins'] is loaded['plugins']
    value = settings({'include': ['aliased.yaml'], 'plugins': []}, [{'path': 'aliased.yaml', 'content': text}]) \
        if included else settings(declaring)
    before = deepcopy(value)
    target = node(value, 'entry_alpha')
    replacement = deepcopy(target['value'])
    replacement['args'][0]['exec'] = 'reject 0'
    changed = patch(value, target, replacement)['settings']
    saved = load_native_yaml(changed['additional_files'][0]['content'] if included else changed['native_config'])
    expected = deepcopy(original)
    expected[target['index']] = replacement
    assert saved['plugins'] == expected
    assert saved['saved_plugins'] == original
    assert saved['saved_plugins'] is not saved['plugins']
    assert saved['future'] == {'keep': False}
    if included:
        assert changed['native_config'] == before['native_config']
    assert value == before


def test_dynamic_listener_entries_follow_validator_environment_expression_rules():
    doc = native()
    by_tag = {item['tag']: item for item in doc['plugins']}
    by_tag['udp_alpha']['args']['entry'] = '${DNS_ENTRY}'
    by_tag['tcp_alpha']['args']['entry'] = 'entry_${DNS_SUFFIX}'
    by_tag['quic_beta']['args']['entry'] = '$${ESCAPED_ENTRY}'
    by_tag['http_dns']['args']['entries'][0]['exec'] = '${HTTP_ENTRY}'
    flow = projection(settings(doc))
    entries = {item['listener_tag']: item for item in flow['entries'] if item['listener_type'] != 'http_server'}
    assert entries['udp_alpha']['dynamic'] is True
    assert entries['tcp_alpha']['dynamic'] is True
    assert not entries['quic_beta'].get('dynamic', False)
    http = next(item for item in flow['entries'] if item.get('path') == '/dns-query')
    assert http['dynamic'] is True
    assert http['entry'] == '${HTTP_ENTRY}'
    assert http['reference_path'] == ['args', 'entries', 0, 'exec']
    assert not next(item for item in flow['entries'] if item.get('path') == '/forward').get('dynamic', False)


def browser_json(value):
    # JSON.stringify emits integral browser Numbers as 0/1, not 0.0/1.0.
    return json.loads(json.dumps(value), parse_float=lambda raw: int(float(raw)) if float(raw).is_integer() else float(raw))


@pytest.mark.parametrize('scope', ['plugin', 'globals'])
def test_browser_number_snapshots_accept_integral_floats_without_rewriting_unchanged_values(scope):
    doc = native()
    numbers = {'zero': 0.0, 'one': 1.0, 'fraction': 0.25, 'flag': False, 'nested': [0.0, 1.0, False]}
    doc['plugins'][1]['notes'] = deepcopy(numbers)
    doc['plugins'][1]['args'][0]['future'] = deepcopy(numbers)
    doc['future_global'] = deepcopy(numbers)
    value = settings(doc)
    if scope == 'plugin':
        target = node(value, 'shared')
        target['value'] = browser_json(target['value'])
        assert type(target['value']['notes']['zero']) is int
        replacement = deepcopy(target['value'])
        replacement['args'][1]['exec'] = 'reject 0'
        changed = patch(value, target, replacement)['settings']
    else:
        before = browser_json(projection(value)['globals']['value'])
        assert type(before['future_global']['one']) is int
        replacement = deepcopy(before)
        replacement['runtime']['worker_threads'] = 3
        changed = apply_common_settings(value, {'entry_flow': {
            'kind': 'globals', 'before': before, 'value': replacement}}, {})['settings']
    saved = load_native_yaml(changed['native_config'])
    for original in (saved['plugins'][1]['notes'], saved['plugins'][1]['args'][0]['future'], saved['future_global']):
        assert type(original['zero']) is float
        assert type(original['one']) is float
        assert type(original['nested'][0]) is float
        assert type(original['nested'][1]) is float
        assert original['flag'] is False
        assert original['nested'][2] is False
        assert list(original) == list(numbers)
    assert saved['plugins'][0] == doc['plugins'][0]
    assert list(saved) == list(doc)


@pytest.mark.parametrize('scope', ['plugin', 'globals'])
def test_browser_number_snapshot_still_rejects_false_as_zero(scope):
    value = settings()
    if scope == 'plugin':
        target = node(value, 'shared')
        before = browser_json(target['value'])
        before['notes']['zero'] = False
        change = {'source': target['source'], 'index': target['index'], 'before': before, 'value': target['value']}
    else:
        before = browser_json(projection(value)['globals']['value'])
        replacement = deepcopy(before)
        before['future_global']['off'] = 0
        change = {'kind': 'globals', 'before': before, 'value': replacement}
    with pytest.raises(OxidnsSettingsError, match='已变化'):
        apply_common_settings(value, {'entry_flow': change}, {})


@pytest.mark.parametrize('scope', ['plugin', 'globals'])
def test_integral_float_browser_noop_retains_source_text(scope):
    doc = native()
    doc['plugins'][1]['notes']['zero'] = 0.0
    doc['future_global']['zero'] = 0.0
    value = settings(doc)
    value['native_config'] = '# preserve this source exactly\n' + value['native_config']
    if scope == 'plugin':
        target = node(value, 'shared')
        target['value'] = browser_json(target['value'])
        changed = patch(value, target, target['value'])['settings']
    else:
        before = browser_json(projection(value)['globals']['value'])
        changed = apply_common_settings(value, {'entry_flow': {
            'kind': 'globals', 'before': before, 'value': before}}, {})['settings']
    assert changed['native_config'] == value['native_config']
