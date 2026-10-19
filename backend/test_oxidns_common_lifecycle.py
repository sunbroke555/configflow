"""Common setting lifecycle regressions: disabled features stay inactive."""
from copy import deepcopy

import pytest
import yaml

from backend.common.oxidns_common import apply_common_settings, read_common_settings
from backend.common.oxidns_editor import prepare_editor
from backend.common.oxidns_settings import OxidnsSettingsError
from backend.common.oxidns_validation import validate_settings
from backend.converters.oxidns import generate_oxidns_config


def prepared(**values):
    return prepare_editor(values, {})


def document(settings):
    return yaml.safe_load(settings['native_config'])


def plugin(settings, tag):
    return next(item for item in document(settings)['plugins'] if item['tag'] == tag)


def edited(settings, edit):
    result = deepcopy(settings)
    value = document(result)
    edit(value)
    result['native_config'] = yaml.safe_dump(value, allow_unicode=True, sort_keys=False)
    return result


def apply(settings, changes):
    original = deepcopy(settings)
    result = apply_common_settings(settings, changes, {})
    assert settings == original
    return result['settings']


def assert_valid(settings):
    result = validate_settings(settings)
    assert result['valid'], [issue for issue in result['issues'] if issue['severity'] == 'error']


@pytest.mark.parametrize('empty', ['', None])
def test_webui_clear_removes_inactive_object_and_restores_metadata(empty):
    settings = prepared(api={'enabled': True, 'webui_root': './web'})
    settings = edited(settings, lambda value: value['api']['http']['webui'].update(
        index='custom.html', future={'keep': [False, 0, None]}))
    original = deepcopy(document(settings)['api']['http']['webui'])
    disabled = apply(settings, {'api.webui_root': empty})
    assert document(disabled)['api']['http'].get('webui') is None
    assert document(disabled)['api']['http']['listen'] == '127.0.0.1:9199'
    assert_valid(disabled)
    restored = apply(disabled, {'api.webui_root': './next-web'})
    assert document(restored)['api']['http']['webui'] == {**original, 'root': './next-web'}
    assert_valid(restored)


@pytest.mark.parametrize('fields', [('api.username', 'api.password'), ('api.password', 'api.username')])
def test_clear_basic_auth_preserves_extension_metadata_for_reenable(fields):
    settings = prepared(api={'enabled': True, 'username': 'example', 'password': 'example-password'})
    settings = edited(settings, lambda value: value['api']['http']['auth'].update(future={'keep': False}))
    original = deepcopy(document(settings)['api']['http']['auth'])
    disabled = apply(settings, {field: '' for field in fields})
    assert document(disabled)['api']['http'].get('auth') is None
    assert_valid(disabled)
    restored = apply(disabled, {'api.username': 'next-user', 'api.password': 'next-password'})
    assert document(restored)['api']['http']['auth'] == {
        **original, 'username': 'next-user', 'password': 'next-password'}
    assert_valid(restored)


def test_api_disable_with_parameter_edits_stays_off_and_restores_latest_values():
    settings = prepared(api={'enabled': True, 'listen': '127.0.0.1:9199'})
    settings = edited(settings, lambda value: value['api']['http'].update(future={'keep': [False, 0]}))
    disabled = apply(settings, {'api.enabled': False, 'api.listen': '127.0.0.1:9299',
                                'api.username': 'example', 'api.password': 'example-password'})
    assert document(disabled).get('api', {}).get('http') is None
    assert_valid(disabled)
    restored = apply(disabled, {'api.enabled': True})
    http = document(restored)['api']['http']
    assert http['listen'] == '127.0.0.1:9299'
    assert http['auth']['username'] == 'example'
    assert http['future'] == {'keep': [False, 0]}
    assert_valid(restored)


def test_api_explicit_null_is_disabled_and_can_be_enabled():
    settings = edited(prepared(), lambda value: value.update(api={'http': None}))
    assert_valid(settings)
    state = read_common_settings(settings, {})
    assert state['values']['api']['enabled'] is False
    assert state['capabilities']['api.enabled']['editable'] is True
    enabled = apply(settings, {'api.enabled': True})
    assert document(enabled)['api']['http']['listen']
    assert_valid(enabled)


@pytest.mark.parametrize('empty', ['', None])
def test_clearing_tls_pair_allows_http_and_preserves_extension_fields(empty):
    settings = prepared(api={'enabled': True, 'ssl': {'cert': 'cert.pem', 'key': 'key.pem'}})
    settings = edited(settings, lambda value: value['api']['http']['ssl'].update(future={'keep': 0}))
    changed = apply(settings, {'api.ssl.cert': empty, 'api.ssl.key': empty})
    tls = document(changed)['api']['http']['ssl']
    assert tls.get('cert') is None and tls.get('key') is None
    assert tls['future'] == {'keep': 0}
    assert_valid(changed)
    restored = apply(changed, {'api.ssl.cert': 'next-cert.pem', 'api.ssl.key': 'next-key.pem'})
    assert document(restored)['api']['http']['ssl'] == {
        'cert': 'next-cert.pem', 'key': 'next-key.pem', 'future': {'keep': 0}}
    assert_valid(restored)


@pytest.mark.parametrize('changes', [
    {'cache_dump_enabled': False, 'cache_dump_file': './state/next.dump', 'cache_dump_interval': 900},
    {'cache_dump_file': './state/next.dump', 'cache_dump_interval': 900, 'cache_dump_enabled': False},
])
def test_disable_cache_dump_wins_over_simultaneous_parameter_edits(changes):
    settings = prepared(cache_dump_enabled=True)
    disabled = apply(settings, changes)
    assert plugin(disabled, 'lazy_cache')['args'].get('dump_file') is None
    assert 'dump_interval' not in plugin(disabled, 'lazy_cache')['args']
    assert read_common_settings(disabled, {})['values']['cache_dump_enabled'] is False
    assert read_common_settings(disabled, {})['values']['cache_dump_interval'] == 900
    assert_valid(disabled)
    restored = apply(disabled, {'cache_dump_enabled': True})
    args = plugin(restored, 'lazy_cache')['args']
    assert args['dump_file'] == './state/next.dump'
    assert args['dump_interval'] == 900
    assert_valid(restored)


def test_cache_dump_file_edit_while_disabled_does_not_enable_writes():
    settings = apply(prepared(cache_dump_enabled=True), {'cache_dump_enabled': False})
    changed = apply(settings, {'cache_dump_file': './state/next.dump', 'cache_dump_interval': 600})
    assert plugin(changed, 'lazy_cache')['args'].get('dump_file') is None
    assert 'dump_interval' not in plugin(changed, 'lazy_cache')['args']
    assert_valid(changed)
    restored = apply(changed, {'cache_dump_enabled': True})
    args = plugin(restored, 'lazy_cache')['args']
    assert args['dump_file'] == './state/next.dump'
    assert args['dump_interval'] == 600
    assert_valid(restored)


@pytest.mark.parametrize('empty', ['', None, '  '])
def test_clearing_cache_dump_file_does_not_leave_an_empty_runtime_path(empty):
    settings = apply(prepared(cache_dump_enabled=True), {'cache_dump_file': empty})
    assert plugin(settings, 'lazy_cache')['args'].get('dump_file') is None
    assert 'dump_interval' not in plugin(settings, 'lazy_cache')['args']
    assert read_common_settings(settings, {})['values']['cache_dump_enabled'] is False
    assert_valid(settings)


@pytest.mark.parametrize('interval', [913, None])
def test_cache_dump_disable_restore_preserves_custom_or_omitted_interval(interval):
    settings = prepared(cache_dump_enabled=True, cache_dump_file='./state/custom.dump')
    settings = apply(settings, {'cache_dump_interval': interval})
    before = plugin(settings, 'lazy_cache')
    disabled = apply(settings, {'cache_dump_enabled': False})
    disabled = apply(disabled, {'cache_dump_enabled': False})
    assert set(plugin(disabled, 'lazy_cache')['args']).isdisjoint({'dump_file', 'dump_interval'})
    projection = read_common_settings(disabled, {})['values']
    assert projection['cache_dump_file'] == './state/custom.dump'
    assert projection['cache_dump_interval'] == interval
    rendered = yaml.safe_load(generate_oxidns_config({'oxidns': disabled}))
    runtime_cache = next(item for item in rendered['plugins'] if item['tag'] == 'lazy_cache')
    assert set(runtime_cache['args']).isdisjoint({'dump_file', 'dump_interval'})
    assert '_common_disabled' not in rendered
    assert_valid(disabled)
    restored = apply(disabled, {'cache_dump_enabled': True})
    assert plugin(restored, 'lazy_cache') == before
    assert_valid(restored)


def test_cache_dump_edit_while_main_cache_is_disabled_does_not_restore_plugin():
    settings = prepared(cache_dump_enabled=True)
    disabled = apply(settings, {'cache_enabled': False, 'cache_dump_enabled': False})
    edited_off = apply(disabled, {'cache_dump_interval': 711, 'cache_dump_file': './state/edited.dump'})
    assert not any(item['tag'] == 'lazy_cache' for item in document(edited_off)['plugins'])
    assert_valid(edited_off)
    cache_on = apply(edited_off, {'cache_enabled': True})
    assert set(plugin(cache_on, 'lazy_cache')['args']).isdisjoint({'dump_file', 'dump_interval'})
    restored = apply(cache_on, {'cache_dump_enabled': True})
    assert plugin(restored, 'lazy_cache')['args']['dump_interval'] == 711
    assert plugin(restored, 'lazy_cache')['args']['dump_file'] == './state/edited.dump'
    assert_valid(restored)


def test_reset_interval_while_dump_disabled_uses_core_default_after_reenable():
    settings = apply(prepared(cache_dump_enabled=True), {'cache_dump_enabled': False})
    settings = apply(settings, {'cache_dump_interval': None})
    restored = apply(settings, {'cache_dump_enabled': True})
    assert plugin(restored, 'lazy_cache')['args']['dump_file']
    assert 'dump_interval' not in plugin(restored, 'lazy_cache')['args']
    assert_valid(restored)


def test_unrelated_edit_preserves_explicit_native_inactive_dump_interval():
    settings = edited(prepared(), lambda value: next(
        item for item in value['plugins'] if item['tag'] == 'lazy_cache')['args'].update(dump_interval=917))
    changed = apply(settings, {'cache_size': 2000})
    assert plugin(changed, 'lazy_cache')['args']['dump_interval'] == 917


@pytest.mark.parametrize('args', [123, 'unfinished', ['unfinished']])
def test_parked_dump_options_do_not_break_projection_of_unfinished_native_args(args):
    settings = apply(prepared(cache_dump_enabled=True), {'cache_dump_enabled': False})
    settings = edited(settings, lambda value: next(
        item for item in value['plugins'] if item['tag'] == 'lazy_cache').update(args=args))
    state = read_common_settings(settings, {})
    assert state['capabilities']['cache_dump_enabled']['editable'] is False
    assert state['values']['cache_dump_enabled'] is False


@pytest.mark.parametrize('empty', ['', None])
def test_clearing_log_file_removes_runtime_file_output(empty):
    settings = prepared(log={'file': './state/app.log', 'rotation': 'daily', 'max_files': 5})
    original_rotation = document(settings)['log']['rotation']
    changed = apply(settings, {'log.file': empty})
    assert document(changed)['log'].get('file') is None
    assert document(changed)['log']['rotation'] == original_rotation
    assert_valid(changed)
    restored = apply(changed, {'log.file': './state/next.log'})
    assert document(restored)['log']['file'] == './state/next.log'
    assert document(restored)['log']['rotation'] == original_rotation
    assert_valid(restored)


def test_log_retention_edit_without_rotation_preserves_core_never_default():
    changed = apply(prepared(), {'log.max_files': 7})
    assert document(changed)['log']['rotation'] == {'type': 'never', 'max_files': 7}
    assert_valid(changed)


def test_reset_log_rotation_keeps_retention_with_core_default():
    settings = prepared(log={'file': './state/app.log', 'rotation': 'daily', 'max_files': 5})
    changed = apply(settings, {'log.rotation': None})
    assert document(changed)['log']['rotation'] == {'type': 'never', 'max_files': 5}
    assert_valid(changed)


@pytest.mark.parametrize('field', ['log.rotation', 'log.max_files', 'api.webui_root', 'api.ssl.cert'])
def test_unsetting_missing_optional_field_does_not_create_an_empty_parent(field):
    settings = prepared(api={'enabled': True})
    original = document(settings)
    changed = apply(settings, {field: None})
    assert document(changed) == original
    assert_valid(changed)


@pytest.mark.parametrize('field', ['outbound', 'socks5'])
def test_clearing_optional_rule_download_transport_omits_the_key(field):
    settings = apply(prepared(adguard={'enabled': True, 'urls': ['https://example.test/filter.txt']}),
                     {'rule_update.enabled': True})
    changed = apply(settings, {f'rule_update.args.{field}': ''})
    assert field not in plugin(changed, 'rules_download')['args']
    assert_valid(changed)


def test_tcp_creation_requires_a_recognized_udp_listener():
    def rename_listener(value):
        value['plugins'] = [item for item in value['plugins'] if item['tag'] != 'server_tcp']
        next(item for item in value['plugins'] if item['tag'] == 'server_udp')['tag'] = 'my_udp'
    settings = edited(prepared(), rename_listener)
    assert_valid(settings)
    assert read_common_settings(settings, {})['capabilities']['tcp_enabled']['editable'] is False
    with pytest.raises(OxidnsSettingsError):
        apply(settings, {'tcp_enabled': True})


def test_existing_tcp_can_be_disabled_without_a_udp_listener():
    settings = edited(prepared(), lambda value: value.update(
        plugins=[item for item in value['plugins'] if item['tag'] != 'server_udp']))
    assert_valid(settings)
    assert read_common_settings(settings, {})['capabilities']['tcp_enabled']['editable'] is True
    disabled = apply(settings, {'tcp_enabled': False})
    assert not any(item['tag'] == 'server_tcp' for item in document(disabled)['plugins'])


@pytest.mark.parametrize('changes', [
    {'tcp_enabled': False, 'listen': '127.0.0.1:5353'},
    {'listen': '127.0.0.1:5353', 'tcp_enabled': False},
])
def test_tcp_disable_listen_edit_and_reenable_preserves_metadata(changes):
    settings = edited(prepared(), lambda value: next(
        item for item in value['plugins'] if item['tag'] == 'server_tcp')['args'].update(
            idle_timeout='2m', future={'keep': False}))
    disabled = apply(settings, changes)
    assert not any(item['tag'] == 'server_tcp' for item in document(disabled)['plugins'])
    restored = apply(disabled, {'tcp_enabled': True})
    args = plugin(restored, 'server_tcp')['args']
    assert args['listen'] == '127.0.0.1:5353'
    assert args['idle_timeout'] == '2m'
    assert args['future'] == {'keep': False}
    assert_valid(restored)
