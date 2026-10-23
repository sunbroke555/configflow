"""Native configuration preserves core semantics through settings and packaging."""
import base64
import copy
import io
import tarfile
import zipfile

import pytest
import yaml

from backend.agents.deployment_bundle import build_deployment_bundle, DeploymentPreparationError
from backend.common.config_export import redact_oxidns_settings
from backend.common.oxidns_native import load_native_yaml, managed_files, native_document
from backend.common.oxidns_settings import normalize_oxidns_settings, OxidnsSettingsError
from backend.common.oxidns_validation import format_path
from backend.converters.oxidns import generate_oxidns_config, get_oxidns_custom_files, get_oxidns_ruleset_downloads
from backend.utils.oxidns_archive import build_oxidns_zip, OxidnsArchiveError

AGENT = {'id': 'native-test', 'service_type': 'oxidns', 'profile_id': 'default'}


def config(document, files=(), **settings):
    return {'oxidns': {'config_mode': 'native', 'native_config': yaml.safe_dump(document),
                       'additional_files': list(files), **settings}}


def test_native_keeps_all_global_plugin_and_unknown_values_ignoring_template_draft():
    document = {
        'runtime': {'worker_threads': 9},
        'api': {'http': {'listen': '127.0.0.1:29999', 'cors': {'allowed_origins': ['https://ui.test']},
                         'auth': {'type': 'basic', 'username': 'native', 'password': 'native-secret'}}},
        'log': {'level': 'warn', 'file': 'logs/native.log', 'rotation': {'type': 'weekly', 'max_files': 3}},
        'network': {'outbound': {'default': 'direct', 'profiles': {'direct': {'resolver': {
            'nameservers': [{'addr': '1.1.1.1'}], 'timeout': '3s'}}}}},
        'future_option': {'keep': [False, None, 1.25]},
        'plugins': [{'tag': 'cache', 'type': 'cache', 'args': {'size': 77, 'shards': 4, 'future': True}},
                    {'tag': 'main', 'type': 'sequence', 'args': [{'exec': '$cache'}]}],
    }
    value = config(document, custom_matches=[{'enabled': True}], listen='', cache_size=-1,
                   custom_config='not: [valid', adguard=None, worker_threads=0)
    before = copy.deepcopy(value)
    assert yaml.safe_load(generate_oxidns_config(value)) == document
    assert get_oxidns_custom_files(value) == []
    assert get_oxidns_ruleset_downloads(value) == []
    assert value == before


def test_core_denied_unknown_field_is_preserved_in_draft_but_blocks_generation():
    document = {'network': {'outbound': {'profiles': {'direct': {
        'resolver': 'system', 'future': {'empty': {}, 'values': [False, None, 0]},
    }}}}, 'plugins': []}
    value = config(document)
    before = copy.deepcopy(value)
    draft = normalize_oxidns_settings(value['oxidns'], validate_native_arguments=False)
    assert draft['native_config'] == value['oxidns']['native_config']
    assert load_native_yaml(draft['native_config']) == document
    with pytest.raises(OxidnsSettingsError) as raised:
        generate_oxidns_config(value)
    assert any(issue['path'] == ['network', 'outbound', 'profiles', 'direct', 'future']
               and issue['code'] == 'unknown-field' and issue['severity'] == 'error'
               for issue in raised.value.issues)
    assert value == before


def test_native_yaml_12_scalars_survive_without_yaml_11_coercion():
    parsed = load_native_yaml('on: off\nyes: no\ndate: 2026-10-16\ndecimal: 0123\noctal: 0o17\nclock: 1:20\nexponent: 1e3\nflag: false\nplugins: []')
    assert parsed == {'on': 'off', 'yes': 'no', 'date': '2026-10-16', 'decimal': 123,
                      'octal': 15, 'clock': '1:20', 'exponent': 1000., 'flag': False, 'plugins': []}


@pytest.mark.parametrize('text', [
    'plugins: []\nplugins: []', 'plugins: !!python/object:dict {}', 'plugins: &cycle [*cycle]',
    'plugins: []\nconstructor: bad', 'plugins: []\nnumber: .inf', '[]',
    'plugins: [{tag: x, type: cache}, {tag: x, type: cache}]',
    'plugins: [{tag: ../bad, type: cache}]', 'plugins: [{tag: ok}]',
])
def test_native_rejects_ambiguous_unsafe_or_invalid_document_structure(text):
    with pytest.raises(OxidnsSettingsError):
        normalize_oxidns_settings({'config_mode': 'native', 'native_config': text})


@pytest.mark.parametrize('kind,args,path', [
    ('http_server', {}, 'args.listen'),
    ('quic_server', {}, 'args.listen'),
    ('udp_server', {}, 'args.listen'),
    ('tcp_server', {}, 'args.listen'),
    ('http_server', None, 'args'),
    ('quic_server', [], 'args'),
    ('udp_server', {'listen': ':53'}, 'args.entry'),
    ('tcp_server', {'listen': ':53', 'entry': '  '}, 'args.entry'),
    ('http_server', {'listen': ':8053'}, 'args.entries'),
    ('http_server', {'listen': ':8053', 'entries': None}, 'args.entries'),
    ('http_server', {'listen': ':8053', 'entries': [{}]}, 'args.entries[0].path'),
    ('http_server', {'listen': ':8053', 'entries': [{'path': '/dns-query'}]}, 'args.entries[0].exec'),
    ('http_server', {'listen': ':8053', 'entries': ['main']}, 'args.entries[0]'),
    ('quic_server', {'listen': ':853', 'entry': 'main'}, 'args.cert'),
    ('quic_server', {'listen': ':853', 'entry': 'main', 'cert': '/tls/cert.pem'}, 'args.key'),
    ('tcp_server', {'listen': ':853', 'entry': 'main', 'key': '/tls/key.pem'}, 'args.cert'),
    ('http_server', {'listen': ':8053', 'entries': [], 'enable_http3': True}, 'args.cert'),
    ('forward', {}, 'args.upstreams'),
    ('forward', {'upstreams': []}, 'args.upstreams'),
    ('forward', {'upstreams': None}, 'args.upstreams'),
    ('forward', {'upstreams': [{}]}, 'args.upstreams[0].addr'),
    ('forward', {'upstreams': [{'addr': ''}]}, 'args.upstreams[0].addr'),
    ('forward', {'upstreams': ['1.1.1.1']}, 'args.upstreams[0]'),
])
def test_incomplete_known_plugin_arguments_are_rejected_with_location(kind, args, path):
    value = config({'plugins': [{'tag': 'needs_configuration', 'type': kind, 'args': args}]})
    with pytest.raises(OxidnsSettingsError) as raised:
        generate_oxidns_config(value)
    assert any(format_path(issue['path']) == f'plugins[0].{path}' and issue['severity'] == 'error'
               for issue in raised.value.issues)
    assert 'needs_configuration' not in str(raised.value)
    assert 'needs_configuration' not in repr(raised.value.issues)


def test_known_plugin_validation_keeps_defaults_unknown_values_and_quick_setup():
    document = {'plugins': [
        {'tag': 'reverse', 'type': 'reverse_lookup', 'args': {}},
        {'tag': 'reverse_default', 'type': 'reverse_lookup'},
        {'tag': 'reverse_options', 'type': 'reverse_lookup', 'args': {'ttl': None, 'handle_ptr': False}},
        {'tag': 'future', 'type': 'future_plugin', 'args': {'empty': {}, 'values': [False, None, 0]}},
        {'tag': 'main', 'type': 'sequence', 'args': [{'exec': 'forward 1.1.1.1'}]},
        {'tag': 'dns', 'type': 'udp_server', 'args': {'listen': '${LISTEN}', 'entry': '${ENTRY}'}},
        {'tag': 'tcp', 'type': 'tcp_server', 'args': {'listen': ':53', 'entry': 'main', 'cert': None, 'key': None}},
        {'tag': 'http', 'type': 'http_server', 'args': {'listen': ':8053', 'entries': [], 'enable_http3': False}},
        {'tag': 'doh', 'type': 'http_server', 'args': {'listen': ':443', 'entries': [{'path': '/dns-query', 'exec': 'main'}],
                                                  'enable_http3': True, 'cert': '/tls/cert', 'key': '/tls/key'}},
        {'tag': 'quic', 'type': 'quic_server', 'args': {'listen': ':853', 'entry': 'main', 'cert': '/tls/cert', 'key': '/tls/key'}},
        {'tag': 'upstream', 'type': 'forward', 'args': {'upstreams': [{'addr': '1.1.1.1', 'timeout': None}], 'concurrent': None}},
    ]}
    value = config(document)
    before = copy.deepcopy(value)
    assert yaml.safe_load(generate_oxidns_config(value)) == document
    assert value == before


def test_included_incomplete_listener_is_rejected_after_expansion():
    value = config({'include': ['servers.yaml'], 'plugins': []}, [
        {'path': 'servers.yaml', 'encoding': 'text',
         'content': 'plugins: [{tag: included_http, type: http_server, args: {}}]'},
    ])
    with pytest.raises(OxidnsSettingsError) as raised:
        generate_oxidns_config(value)
    assert any(issue['source'] == 'servers.yaml' and issue['path'] == ['plugins', 0, 'args', 'listen']
               and issue['severity'] == 'error' for issue in raised.value.issues)
    assert 'included_http' not in str(raised.value)


def test_include_matches_official_nested_order_and_only_imports_plugins():
    value = config({'runtime': {'worker_threads': 3}, 'include': ['./plugins/first.yaml'],
                    'plugins': [{'tag': 'last', 'type': 'debug_print'}]}, [
        {'path': 'plugins/first.yaml', 'content': 'include: [nested/second.yaml]\nruntime: {worker_threads: 99}\nplugins: [{tag: middle, type: debug_print}]', 'encoding': 'text'},
        {'path': 'plugins/nested/second.yaml', 'content': 'plugins: [{tag: first, type: debug_print}]', 'encoding': 'text'},
    ])
    generated = yaml.safe_load(generate_oxidns_config(value))
    assert [plugin['tag'] for plugin in generated['plugins']] == ['first', 'middle', 'last']
    assert generated['runtime'] == {'worker_threads': 3}
    assert 'include' not in generated
    assert yaml.safe_load(value['oxidns']['native_config'])['include'] == ['./plugins/first.yaml']


def test_include_parent_path_stays_within_managed_root():
    value = config({'include': ['plugins/nested.yaml']}, [
        {'path': 'plugins/nested.yaml', 'encoding': 'text', 'content': 'include: [../common.yaml]'},
        {'path': 'common.yaml', 'encoding': 'text', 'content': 'plugins: [{tag: shared, type: debug_print}]'},
    ])
    assert yaml.safe_load(generate_oxidns_config(value))['plugins'][0]['tag'] == 'shared'
    value['oxidns']['additional_files'][0]['content'] = 'include: [../../outside.yaml]'
    with pytest.raises(OxidnsSettingsError, match='相对路径'):
        generate_oxidns_config(value)


@pytest.mark.parametrize('include,files', [
    ('missing.yaml', []), ('../outside.yaml', []), ('${INCLUDE}', []),
    ('a.yaml', [{'path': 'a.yaml', 'content': 'include: [a.yaml]', 'encoding': 'text'}]),
    ('a.yaml', [{'path': 'a.yaml', 'content': 'plugins: [{tag: main, type: cache}]', 'encoding': 'text'}]),
])
def test_include_missing_traversal_cycle_and_duplicate_tags_rejected(include, files):
    with pytest.raises(OxidnsSettingsError):
        generate_oxidns_config(config({'include': [include], 'plugins': [{'tag': 'main', 'type': 'cache'}]}, files))


def test_include_depth_limit_is_eight():
    files = [{'path': f'{index}.yaml', 'encoding': 'text',
              'content': f'include: [{index + 1}.yaml]\nplugins: []'} for index in range(9)]
    files[-1]['content'] = 'plugins: []'
    with pytest.raises(OxidnsSettingsError, match='8 层'):
        generate_oxidns_config(config({'include': ['0.yaml'], 'plugins': []}, files))
    files[7]['content'] = 'plugins: []'
    assert yaml.safe_load(generate_oxidns_config(config({'include': ['0.yaml'], 'plugins': []}, files))) == {'plugins': []}


@pytest.mark.parametrize('path', ['../bad', '/etc/key', 'a/../key', 'a//key', 'state/cache', 'state',
                                  'config.yaml', './config.yaml', '.configflow-active', 'manifest.json'])
def test_managed_file_path_cannot_escape_or_overwrite_runtime_metadata(path):
    with pytest.raises(OxidnsSettingsError):
        managed_files([{'path': path, 'content': 'secret', 'encoding': 'text'}])


@pytest.mark.parametrize('files', [
    [{'path': 'a', 'content': '', 'encoding': 'text'}, {'path': './a', 'content': '', 'encoding': 'text'}],
    [{'path': 'a', 'content': '', 'encoding': 'text'}, {'path': 'a/b', 'content': '', 'encoding': 'text'}],
    [{'path': 'a', 'content': '**invalid**', 'encoding': 'base64'}],
    [{'path': 'a', 'content': '', 'encoding': 'zip'}],
])
def test_managed_file_conflicts_and_invalid_encoding_rejected(files):
    with pytest.raises(OxidnsSettingsError):
        managed_files(files)


def test_complete_native_zip_and_agent_pack_binary_credentials_and_webui_exactly():
    binary = b'\x00\xff\x01\xc0geo'
    document = {
        'api': {'http': {'listen': '127.0.0.1:9199', 'ssl': {'cert': './tls/server.crt', 'key': './tls/server.key', 'client_ca': './tls/ca.pem'},
                         'webui': {'root': './webui', 'index': 'index.html'}}},
        'plugins': [{'tag': 'geo', 'type': 'geosite', 'args': {'file': 'data/geo.dat', 'selectors': ['cn']}},
                    {'tag': 'dot', 'type': 'tcp_server', 'args': {'listen': ':853', 'entry': 'main', 'cert': './tls/server.crt', 'key': './tls/server.key'}},
                    {'tag': 'ros', 'type': 'ros_address_list', 'args': {
                        'address': '192.0.2.1:8729', 'username': 'test-user', 'password': 'test-password', 'address_list4': 'dns4',
                        'tls': {'ca': './tls/ca.pem'}, 'persistent': {'files': ['./rules/keep.txt']}}},
                    {'tag': 'main', 'type': 'sequence', 'args': [{'exec': 'accept'}]}],
    }
    contents = {'tls/server.crt': b'CERT', 'tls/server.key': b'PRIVATE KEY', 'tls/ca.pem': b'CA',
                'webui/index.html': '<html>中文</html>'.encode(), 'rules/keep.txt': b'192.0.2.0/24', 'data/geo.dat': binary}
    value = config(document, [{'path': path, 'encoding': 'base64', 'content': base64.b64encode(data).decode()}
                              for path, data in contents.items()])
    generated, custom = generate_oxidns_config(value), get_oxidns_custom_files(value)
    with zipfile.ZipFile(build_oxidns_zip(value, generated, [], custom)) as archive:
        assert {path: archive.read(path) for path in contents} == contents
    bundle = build_deployment_bundle(AGENT, value, generated, custom_files=custom)
    with tarfile.open(fileobj=io.BytesIO(bundle.archive)) as archive:
        assert {path: archive.extractfile('files/' + path).read() for path in contents} == contents
        assert all(archive.getmember('files/' + path).mode == 0o600 for path in contents)


@pytest.mark.parametrize('mode', ['template', 'native'])
@pytest.mark.parametrize('kind,content', [
    ('domain_set', b'# Native bare domains match suffixes\r\nexample.com\r\nlocalhost\r\n  domain:keep.test  \r\n'),
    ('ip_set', b'# Keep native formatting\n192.0.2.1\n  2001:db8::/32  \n'),
])
def test_native_provider_attachments_keep_semantics_and_bytes_in_zip_and_agent(mode, kind, content):
    document = {'plugins': [{'tag': 'native_asset', 'type': kind, 'args': {'files': ['rules/native.txt']}}]}
    value = config(document, [{'path': 'rules/native.txt', 'encoding': 'base64',
                               'content': base64.b64encode(content).decode()}])
    if mode == 'template':
        value['oxidns'].update(config_mode='template', custom_config=yaml.safe_dump(document))
    generated, custom = generate_oxidns_config(value), get_oxidns_custom_files(value)
    with zipfile.ZipFile(build_oxidns_zip(value, generated, [], custom)) as archive:
        assert archive.read('rules/native.txt') == content
    bundle = build_deployment_bundle(AGENT, value, generated, custom_files=custom)
    with tarfile.open(fileobj=io.BytesIO(bundle.archive)) as archive:
        assert archive.extractfile('files/rules/native.txt').read() == content


@pytest.mark.parametrize('document', [
    {'api': {'http': {'listen': '127.0.0.1:9199', 'ssl': {'cert': 'missing.crt', 'key': 'missing.key'}}}, 'plugins': []},
    {'api': {'http': {'listen': '127.0.0.1:9199', 'webui': {'root': 'webui'}}}, 'plugins': []},
    {'plugins': [{'tag': 'dot', 'type': 'tcp_server', 'args': {'listen': ':853', 'entry': 'main', 'cert': './missing.crt', 'key': './missing.key'}},
                 {'tag': 'main', 'type': 'sequence', 'args': [{'exec': 'accept'}]}]},
    {'plugins': [{'tag': 'ros', 'type': 'ros_route', 'args': {
        'address': '192.0.2.1:8729', 'username': 'test-user', 'password': 'test-password',
        'routing_table': 'main', 'gateway4': '192.0.2.254', 'tls': {'ca': 'missing.pem'}}}]},
])
def test_zip_and_agent_both_refuse_missing_relative_native_resources(document):
    value = config(document)
    generated = generate_oxidns_config(value)
    with pytest.raises(OxidnsArchiveError, match='未打包'):
        build_oxidns_zip(value, generated, [], [])
    with pytest.raises(DeploymentPreparationError, match='附加文件里没有它'):
        build_deployment_bundle(AGENT, value, generated)


def test_target_owned_resources_and_runtime_paths_are_not_packaged():
    value = config({'plugins': [
        {'tag': 'geo', 'type': 'geoip', 'args': {'file': '/etc/shared/geoip.dat'}},
        {'tag': 'domains', 'type': 'domain_set', 'args': {'files': ['${RULES_FILE}']}},
        {'tag': 'recorder', 'type': 'query_recorder', 'args': {'path': '/var/lib/oxidns/queries.db'}},
    ]})
    generated = generate_oxidns_config(value)
    with zipfile.ZipFile(build_oxidns_zip(value, generated, [], [])) as archive:
        assert archive.namelist() == ['config.yaml']
    assert len(build_deployment_bundle(AGENT, value, generated).manifest['files']) == 1


def test_managed_runtime_collision_is_rejected_and_shareable_backup_omits_secrets():
    value = config({'plugins': [{'tag': 'recorder', 'type': 'query_recorder', 'args': {'path': 'queries.db'}}]},
                   [{'path': 'queries.db', 'content': 'secret', 'encoding': 'text'}])
    generated, custom = generate_oxidns_config(value), get_oxidns_custom_files(value)
    with pytest.raises(OxidnsArchiveError, match='运行数据'):
        build_oxidns_zip(value, generated, [], custom)
    redacted = redact_oxidns_settings(value['oxidns'])
    assert redacted['native_config'] == '' and redacted['additional_files'] == []
    assert 'secret' not in repr(redacted)
