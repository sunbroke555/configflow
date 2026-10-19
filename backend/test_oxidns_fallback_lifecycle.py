"""Empty backup addresses disable only the recognizable proxy fallback graph."""
from copy import deepcopy

import pytest
import yaml

from backend.common.oxidns_common import apply_common_settings, read_common_settings
from backend.common.oxidns_editor import prepare_editor
from backend.common.oxidns_settings import normalize_oxidns_settings, OxidnsSettingsError
from backend.common.oxidns_validation import validate_native_config
from backend.converters.oxidns import generate_oxidns_config
from backend.test_oxidns_integration import oxidns_app


BACKUP = '- addr: tcp://192.0.2.3:1053\n  idle_timeout: 60\n'
TAGS = {'forward_fallback', 'fallback_dns', 'fallback'}


def prepared():
    return prepare_editor({'fallback_dns': BACKUP, 'fallback_threshold': 800,
                           'fallback_concurrent': 1, 'always_standby': False}, {})


def document(settings):
    return yaml.safe_load(settings['native_config'])


def plugins(settings):
    return {item['tag']: item for item in document(settings)['plugins']}


def edit(settings, change):
    result = deepcopy(settings)
    doc = document(result)
    change(doc, {item['tag']: item for item in doc['plugins']})
    result['native_config'] = yaml.safe_dump(doc, sort_keys=False)
    return result


def apply(settings, changes):
    original = deepcopy(settings)
    result = apply_common_settings(settings, changes, {})
    assert settings == original
    return result


def assert_valid(settings):
    assert not [item for item in validate_native_config(document(settings)) if item['severity'] == 'error']


@pytest.mark.parametrize('empty', ['', ' \n ', '[]', 'null', '~'])
def test_template_empty_backup_has_no_implicit_domestic_fallback(empty):
    settings = normalize_oxidns_settings({'local_dns': 'udp://192.0.2.10', 'fallback_dns': empty})
    assert normalize_oxidns_settings(settings) == settings
    doc = yaml.safe_load(generate_oxidns_config({'oxidns': settings}))
    items = {item['tag']: item for item in doc['plugins']}
    assert not TAGS & items.keys()
    assert items['proxy_dns_seq']['args'] == [{'exec': 'goto remote_dns'}]
    assert items['forward_local']['args']['upstreams'] == [{'addr': 'udp://192.0.2.10'}]
    assert not [item for item in validate_native_config(doc) if item['severity'] == 'error']


@pytest.mark.parametrize('empty', ['', None, '[]', 'null', '~'])
def test_clear_parks_backup_parameters_and_never_restores_deleted_addresses(empty):
    original = prepared()
    result = apply(original, {'fallback_dns': empty})
    current = result['settings']
    items = plugins(current)
    assert not TAGS & items.keys()
    assert items['proxy_dns_seq']['args'] == [{'exec': 'goto remote_dns'}]
    assert result['values']['fallback_dns'] == ''
    assert 'fallback_enabled' not in result['values']
    assert current['_common_disabled']['forward_fallback']['args']['upstreams'] == []
    assert current['_common_disabled']['fallback']['args']['threshold'] == 800
    assert items['remote_dns'] == plugins(original)['remote_dns']
    assert_valid(current)
    saved = normalize_oxidns_settings(current)
    assert yaml.safe_load(generate_oxidns_config({'oxidns': saved})) == document(current)
    assert read_common_settings(saved, {})['values']['fallback_dns'] == ''
    changed = apply(current, {'fallback_threshold': 950, 'fallback_concurrent': 2})
    assert not TAGS & plugins(changed['settings']).keys()
    assert changed['values']['fallback_dns'] == ''
    assert changed['values']['fallback_threshold'] == 950
    assert_valid(changed['settings'])


def test_refill_connects_fallback_and_restores_saved_options_and_unknown_fields():
    original = edit(prepared(), lambda doc, p: (
        p['forward_fallback'].update(extension={'keep': [False, 0, None]}),
        p['fallback']['args'].update(future={'keep': 0}),
    ))
    cleared = apply(original, {'fallback_dns': ''})['settings']
    next_upstreams = '- addr: tls://dns.example.test\n  dial_addr: 192.0.2.4\n'
    restored = apply(cleared, {'fallback_dns': next_upstreams})
    items = plugins(restored['settings'])
    assert TAGS <= items.keys()
    assert items['proxy_dns_seq']['args'] == [{'exec': '$fallback'}, {'exec': 'accept'}]
    assert items['forward_fallback']['args'] == {
        'concurrent': 1, 'upstreams': [{'addr': 'tls://dns.example.test', 'dial_addr': '192.0.2.4'}]}
    assert items['forward_fallback']['extension'] == {'keep': [False, 0, None]}
    assert items['fallback']['args']['threshold'] == 800
    assert items['fallback']['args']['future'] == {'keep': 0}
    assert_valid(restored['settings'])


def test_new_template_can_enable_backup_by_address_and_update_options_atomically():
    settings = prepare_editor({}, {})
    assert not TAGS & plugins(settings).keys()
    result = apply(settings, {'fallback_dns': BACKUP, 'fallback_threshold': 700,
                             'fallback_concurrent': 1, 'always_standby': True})
    assert plugins(result['settings'])['fallback']['args'] == {
        'primary': 'remote_dns', 'secondary': 'fallback_dns', 'threshold': 700, 'always_standby': True}
    assert_valid(result['settings'])


def test_native_read_and_unrelated_edit_never_reinterpret_stale_template_fields():
    settings = prepared()
    settings['fallback_dns'] = ''
    before = document(settings)
    read = read_common_settings(settings, {})
    assert yaml.safe_load(read['values']['fallback_dns'])[0]['addr'] == 'tcp://192.0.2.3:1053'
    assert document(settings) == before
    changed = apply(settings, {'worker_threads': 5})['settings']
    for tag in (*TAGS, 'proxy_dns_seq'):
        assert plugins(changed)[tag] == plugins(settings)[tag]


@pytest.mark.parametrize('calls', [
    [{'exec': 'goto remote_dns'}], [{'exec': '$remote_dns'}, {'exec': 'accept'}],
])
def test_canonical_and_older_remote_only_chains_can_be_refilled(calls):
    settings = edit(prepared(), lambda doc, p: p['proxy_dns_seq'].update(args=calls))
    assert read_common_settings(settings, {})['values']['fallback_dns'] == ''
    result = apply(settings, {'fallback_dns': BACKUP})
    assert plugins(result['settings'])['proxy_dns_seq']['args'] == [{'exec': '$fallback'}, {'exec': 'accept'}]


@pytest.mark.parametrize('customize', [
    lambda doc, p: p['proxy_dns_seq']['args'].append({'exec': 'ttl 10'}),
    lambda doc, p: p['proxy_dns_seq']['args'][0].update(matches=['qtype 1']),
    lambda doc, p: p['fallback']['args'].update(secondary='remote_dns'),
    lambda doc, p: p['fallback_dns']['args'][1].update(exec='reject 2'),
    lambda doc, p: doc['plugins'].append({'tag': 'other', 'type': 'fallback',
                                         'args': {'primary': 'remote_dns', 'secondary': 'fallback_dns'}}),
    lambda doc, p: doc['plugins'].append({'tag': 'other', 'type': 'udp_server',
                                         'args': {'entry': 'fallback_dns', 'listen': '127.0.0.1:5353'}}),
    lambda doc, p: doc['plugins'].append({'tag': 'other', 'type': 'sequence', 'args': [{'exec': '$fallback'}]}),
    lambda doc, p: doc.update(future={'executor': '$forward_fallback'}),
])
def test_custom_graph_and_extra_references_are_preserved_and_cannot_be_cleared(customize):
    settings = edit(prepared(), customize)
    before = deepcopy(settings)
    state = read_common_settings(settings, {})
    assert state['capabilities']['fallback_dns']['editable'] is False
    assert state['capabilities']['fallback_dns']['target'] == 'proxy_dns_seq'
    with pytest.raises(OxidnsSettingsError, match='自定义'):
        apply(settings, {'fallback_dns': ''})
    assert settings == before


def test_include_is_not_rewritten_or_parked_by_backup_controls():
    settings = edit(prepared(), lambda doc, p: doc.update(include=['extra.yaml']))
    settings['additional_files'] = [{'path': 'extra.yaml', 'content': 'plugins: []\n'}]
    assert read_common_settings(settings, {})['capabilities']['fallback_dns']['editable'] is False
    with pytest.raises(OxidnsSettingsError, match='include'):
        apply(settings, {'fallback_dns': ''})


def test_empty_include_list_does_not_block_backup_cleanup():
    settings = edit(prepared(), lambda doc, p: doc.update(include=[]))
    result = apply(settings, {'fallback_dns': ''})
    assert not TAGS & plugins(result['settings']).keys()
    assert document(result['settings'])['include'] == []
    assert_valid(result['settings'])


@pytest.mark.parametrize('fragment', [
    {'exec': '${BACKUP_EXEC}'},
    {'exec': '${BACKUP_EXEC:-$forward_fallback}'},
    {'matches': ['${BACKUP_MATCH}'], 'exec': 'accept'},
    {'entry': '${BACKUP_ENTRY}'},
    {'primary': '${BACKUP_PRIMARY}', 'secondary': 'remote_dns'},
    {'primary': 'remote_dns', 'secondary': '${BACKUP_SECONDARY}'},
    {'executors': ['${BACKUP_EXEC}']},
    {'provider': '${BACKUP_PROVIDER}'},
])
def test_unexpanded_reference_fields_block_cleanup_without_guessing_environment(fragment):
    settings = edit(prepared(), lambda doc, p: doc['plugins'].append({
        'tag': 'manual', 'type': 'future', 'args': fragment}))
    before = deepcopy(settings)
    cap = read_common_settings(settings, {})['capabilities']['fallback_dns']
    assert cap['editable'] is False
    assert '环境变量' in cap['reason']
    with pytest.raises(OxidnsSettingsError, match='环境变量'):
        apply(settings, {'fallback_dns': ''})
    assert settings == before


@pytest.mark.parametrize('plugin', [
    {'tag': '${BACKUP_TAG}', 'type': 'sequence', 'args': [{'exec': 'accept'}]},
    {'tag': 'manual_reload', 'type': 'reload_provider', 'args': ['${BACKUP_PROVIDER}']},
])
def test_unexpanded_tags_and_positional_reference_lists_block_cleanup(plugin):
    settings = edit(prepared(), lambda doc, p: doc['plugins'].append(plugin))
    with pytest.raises(OxidnsSettingsError, match='环境变量'):
        apply(settings, {'fallback_dns': ''})


def test_environment_values_in_addresses_and_credentials_do_not_block_cleanup():
    settings = edit(prepared(), lambda doc, p: (
        p['forward_local']['args'].update(upstreams=[{'addr': '${LOCAL_DNS}'}]),
        doc.update(api={'http': {'listen': '127.0.0.1:9199', 'auth': {
            'type': 'basic', 'username': '${API_USER}', 'password': '${API_PASSWORD}'}}}),
    ))
    result = apply(settings, {'fallback_dns': ''})
    assert not TAGS & plugins(result['settings']).keys()
    assert plugins(result['settings'])['forward_local']['args']['upstreams'][0]['addr'] == '${LOCAL_DNS}'
    assert document(result['settings'])['api'] == document(settings)['api']


@pytest.mark.parametrize('invalid', ['- wrong: 1', '- addr: ""', '[false]'])
def test_nonempty_invalid_backup_is_not_treated_as_disable(invalid):
    with pytest.raises(OxidnsSettingsError):
        apply(prepared(), {'fallback_dns': invalid})


def test_common_api_clear_save_render_roundtrip_is_valid_and_explicit(oxidns_app):
    app, repository = oxidns_app
    client = app.test_client()
    before = repository.get_profile('office')
    result = client.post('/api/profiles/office/oxidns/common-settings/apply', json={
        'settings': prepared(), 'changes': {'fallback_dns': ''}})
    assert result.status_code == 200, result.get_json()
    assert repository.get_profile('office') == before
    settings = result.get_json()['settings']
    saved = client.post('/api/profiles/office/oxidns/settings', json=settings)
    assert saved.status_code == 200, saved.get_json()
    rendered = client.post('/api/profiles/office/oxidns/render', json={'settings': saved.get_json()['settings']})
    assert rendered.status_code == 200, rendered.get_json()
    doc = yaml.safe_load(rendered.get_json()['config'])
    assert not TAGS & {item['tag'] for item in doc['plugins']}
