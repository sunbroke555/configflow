"""Transactional deployment preparation, persistent ownership and result handling."""
import hashlib
import io
import json
import tarfile
from copy import deepcopy

import pytest
import requests
import yaml

from backend.agents.deployment_bundle import (
    DeploymentPreparationError, build_deployment_bundle, convert_mosdns_rules,
)
from backend.agents.manager import AgentManager
from backend.common import config as config_module
from backend.common.config_repository import ProfileRepository


@pytest.fixture
def env(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    config_module.set_repository(repository)
    manager = AgentManager(repository)
    registration = manager.register_agent({'name': 'test', 'host': 'agent.test', 'port': 8080,
                                           'service_type': 'mihomo'})
    agent = manager.get_agent_by_id(registration['id'])
    config = {'profile_id': 'default', 'rule_library': [], 'system_config': {'server_domain': 'https://config.test'}}
    return repository, manager, agent, config


def response(payload, status=200):
    value = requests.Response()
    value.status_code = status
    value._content = json.dumps(payload).encode()
    value._content_consumed = True
    return value


def contents(bundle):
    with tarfile.open(fileobj=io.BytesIO(bundle.archive), mode='r:gz') as archive:
        return {member.name: archive.extractfile(member).read() for member in archive.getmembers()}


def minimal_bundle(agent, config, deployment_id='test-release'):
    return build_deployment_bundle(agent, config, 'mode: rule\nrules: [MATCH,DIRECT]', deployment_id=deployment_id)


def test_bundle_preserves_binary_mrs_and_all_hashes(env, monkeypatch):
    _, _, agent, config = env
    original = b'\x00MRS\xff\xfe\x80\r\n'
    seen = []
    def fetch(url, **kwargs):
        seen.append((url, kwargs))
        result = response(None)
        result._content = original
        return result
    monkeypatch.setattr('backend.agents.deployment_bundle.request_rule', fetch)
    main = {'rule-providers': {'binary': {'type': 'http', 'format': 'mrs', 'behavior': 'domain',
                                         'url': 'https://rules.test/raw.mrs', 'path': './ruleset/binary.mrs'}}}
    bundle = build_deployment_bundle(agent, config, yaml.safe_dump(main))
    members = contents(bundle)
    assert members['files/ruleset/binary.mrs'] == original
    assert hashlib.sha256(bundle.archive).hexdigest() == bundle.sha256
    for member in bundle.manifest['files']:
        content = members['files/' + member['path']]
        assert member['size'] == len(content)
        assert member['sha256'] == hashlib.sha256(content).hexdigest()
    assert seen[0][1]['config_data'] == config
    assert seen[0][1]['stream'] is True


def test_inline_rule_snapshot_and_empty_files_do_not_use_self_http(env, monkeypatch):
    _, _, agent, config = env
    config['rule_library'] = [{'id': 'r', 'name': 'Empty', 'source_type': 'content', 'content': ''}]
    monkeypatch.setattr('backend.agents.deployment_bundle.request_rule', lambda *_a, **_k: pytest.fail('self HTTP'))
    main = {'rule-providers': {'empty': {'type': 'http', 'path': './ruleset/empty.list',
                                       'url': 'https://config.test/api/profiles/default/rules/local/Empty'}}}
    bundle = build_deployment_bundle(agent, config, yaml.safe_dump(main))
    assert contents(bundle)['files/ruleset/empty.list'] == b''


def test_failed_required_download_aborts_without_leaking_credentials(env, monkeypatch):
    _, _, agent, config = env
    def fail(*_args, **_kwargs):
        raise requests.exceptions.ProxyError('http://secret:password@proxy.test')
    monkeypatch.setattr('backend.agents.deployment_bundle.request_rule', fail)
    with pytest.raises(DeploymentPreparationError) as exc:
        build_deployment_bundle(agent, config, 'mode: rule', ruleset_downloads=[{
            'name': 'required', 'url': 'https://secret.test', 'local_path': './rules/list'}])
    assert 'password' not in str(exc.value)
    assert 'required' in str(exc.value)


@pytest.mark.parametrize('path', ['/etc/passwd', '../config.yaml', 'rules/../../config.yaml', '.configflow-deployments/x'])
def test_bundle_rejects_unsafe_paths(env, path):
    _, _, agent, config = env
    with pytest.raises(DeploymentPreparationError):
        build_deployment_bundle(agent, config, 'mode: rule', custom_files=[{'path': path, 'content': ''}])


def test_bundle_rejects_unbundled_file_and_duplicate_paths(env):
    _, _, agent, config = env
    with pytest.raises(DeploymentPreparationError, match='bundled'):
        build_deployment_bundle(agent, config, 'proxy-providers: {local: {type: file, path: ./missing.yaml}}')
    with pytest.raises(DeploymentPreparationError, match='Duplicate'):
        build_deployment_bundle(agent, config, 'mode: rule', custom_files=[{'path': 'config.yaml', 'content': 'bad'}])


def test_mosdns_conversion_and_snapshot_callback(env, monkeypatch):
    _, _, agent, config = env
    agent['service_type'] = 'mosdns'
    config['rule_library'] = [{'id': 'r', 'source_type': 'content', 'content': 'DOMAIN-SUFFIX,example.test\nIP-CIDR,10.0.0.0/8,no-resolve'}]
    config['rule_configs'] = [{'library_rule_id': 'r', 'enabled': True}]
    url = 'https://config.test/api/profiles/default/mosdns/rule-proxy?url=https%3A%2F%2Fconfig.test%2Fapi%2Fprofiles%2Fdefault%2Frule-library%2Fcontent%2Fr'
    monkeypatch.setattr('backend.agents.deployment_bundle.request_rule', lambda *_a, **_k: pytest.fail('self HTTP'))
    bundle = build_deployment_bundle(agent, config, 'plugins: [{type: domain_set, args: {files: [./rules/r.txt]}}]',
                                     ruleset_downloads=[{'name': 'r', 'url': url, 'local_path': './rules/r.txt'}])
    assert contents(bundle)['files/rules/r.txt'] == b'domain:example.test'
    assert convert_mosdns_rules(b'') == b''
    with pytest.raises(DeploymentPreparationError):
        convert_mosdns_rules(b'\xffMRS')


@pytest.mark.parametrize('yaml_payload', [False, True])
def test_mosdns_apple_clash_rules_omit_ip_and_process_entries(yaml_payload):
    # Real Apple.list entries: the IPv6 network made the production MosDNS
    # domain_set fail with "unsupported match type [2403]" after replacement.
    lines = [
        'DOMAIN,apple-events.akamaized.net',
        'DOMAIN-SUFFIX,apple.com',
        'DOMAIN-KEYWORD,apple',
        'IP-CIDR,139.178.128.0/18,no-resolve',
        'IP-CIDR6,2403:300::/32,no-resolve',
        'PROCESS-NAME,CoreLocationAgent',
    ]
    source = yaml.safe_dump({'payload': lines}) if yaml_payload else '\n'.join(lines)
    assert convert_mosdns_rules(source.encode()) == (
        b'full:apple-events.akamaized.net\ndomain:apple.com\nkeyword:apple')


def test_mosdns_domain_matching_semantics_agree_with_rule_proxy(monkeypatch):
    from flask import Flask
    from backend.routes import mosdns

    source = '\n'.join([
        'DOMAIN,exact.example.test',
        'DOMAIN-SUFFIX,suffix.example.test',
        'DOMAIN-KEYWORD,keyword',
        r'DOMAIN-REGEX,^node[0-9]{1,3}\.example\.test$',
        '+.all.example.test',
        '.children.example.test',
        '*.direct-child.example.test',
        'plain.example.test',
        'IP-CIDR,192.0.2.0/24,no-resolve',
        'IP-CIDR6,2001:db8::/32,no-resolve',
        'GEOIP,CN',
        'PROCESS-NAME,example',
    ])
    monkeypatch.setattr(mosdns, 'get_config', lambda: {})
    monkeypatch.setattr(mosdns, '_require_rule_proxy_auth', lambda: True)
    monkeypatch.setattr(mosdns, '_local_content_rule', lambda *_: None)
    monkeypatch.setattr(mosdns, '_fetch_remote_content', lambda _: source)
    app = Flask(__name__)
    with app.test_request_context('/api/mosdns/rule-proxy?url=https://rules.test/mixed.list'):
        legacy, status, _headers = mosdns.mosdns_rule_proxy()
    assert status == 200
    assert convert_mosdns_rules(source.encode()) == legacy.encode()
    assert legacy.splitlines() == [
        'full:exact.example.test',
        'domain:suffix.example.test',
        'keyword:keyword',
        r'regexp:^node[0-9]{1,3}\.example\.test$',
        'domain:all.example.test',
        r'regexp:.+\.children\.example\.test$',
        r'regexp:^[^.]+\.direct\-child\.example\.test$',
        'full:plain.example.test',
    ]



@pytest.mark.parametrize('plugin_type,expected', [
    ('domain_set', b'full:exact.example.test\ndomain:suffix.example.test\nkeyword:keyword\nregexp:^node[0-9]{1,3}\\.example\\.test$'),
    ('ip_set', b'192.0.2.0/24\n2001:db8::/32\n198.51.100.7\n2001:db8:1::1\n203.0.113.0/24'),
])
@pytest.mark.parametrize('yaml_payload', [False, True])
@pytest.mark.parametrize('callback', [False, True])
def test_mosdns_bundle_converts_for_final_plugin_type(env, monkeypatch, plugin_type, expected, yaml_payload, callback):
    _, _, agent, config = env
    agent['service_type'] = 'mosdns'
    lines = [
        'DOMAIN,exact.example.test', 'DOMAIN-SUFFIX,suffix.example.test',
        'DOMAIN-KEYWORD,keyword', r'DOMAIN-REGEX,^node[0-9]{1,3}\.example\.test$',
        'IP-CIDR,192.0.2.0/24,no-resolve', 'IP-CIDR6,2001:db8::/32,no-resolve',
        '198.51.100.7', '2001:db8:1::1', 'ip:203.0.113.0/24',
        'PROCESS-NAME,example',
    ]
    source = yaml.safe_dump({'payload': lines}) if yaml_payload else '\n'.join(lines)
    config['rule_library'] = [{'id': 'mixed', 'name': 'mixed', 'source_type': 'content', 'content': source}]
    url = 'https://config.test/api/profiles/default/rule-library/content/mixed'
    if callback:
        from urllib.parse import urlencode
        url = 'https://config.test/api/profiles/default/mosdns/rule-proxy?' + urlencode({'url': url})
    monkeypatch.setattr('backend.agents.deployment_bundle.request_rule', lambda *_a, **_k: pytest.fail('self HTTP'))
    main = {'plugins': [{'type': plugin_type, 'args': {'files': ['./rules/mixed.txt']}}]}
    archive = build_deployment_bundle(agent, config, yaml.safe_dump(main),
        ruleset_downloads=[{'name': 'mixed', 'url': url, 'local_path': 'rules/mixed.txt'}])
    assert contents(archive)['files/rules/mixed.txt'] == expected
    rule = next(item for item in archive.manifest['files'] if item['path'] == 'rules/mixed.txt')
    assert rule['sha256'] == hashlib.sha256(expected).hexdigest()


def test_mosdns_bundle_rejects_shared_domain_and_ip_file_before_download(env, monkeypatch):
    _, _, agent, config = env
    agent['service_type'] = 'mosdns'
    main = {'plugins': [{'type': kind, 'args': {'files': [path]}} for kind, path in
                        [('domain_set', './rules/mixed.txt'), ('ip_set', 'rules/mixed.txt')]]}
    monkeypatch.setattr('backend.agents.deployment_bundle.request_rule', lambda *_a, **_k: pytest.fail('download before conflict check'))
    with pytest.raises(DeploymentPreparationError, match='both domain_set and ip_set'):
        build_deployment_bundle(agent, config, yaml.safe_dump(main), ruleset_downloads=[
            {'name': 'mixed', 'url': 'https://rules.test/mixed', 'local_path': './rules/mixed.txt'}])


@pytest.mark.parametrize('network', ['not-an-address', '2001:db8::/129', '192.0.2.0/99'])
def test_mosdns_ip_set_rejects_invalid_network_instead_of_empty_file(env, network):
    _, _, agent, config = env
    agent['service_type'] = 'mosdns'
    with pytest.raises(DeploymentPreparationError, match='Invalid IP network'):
        build_deployment_bundle(agent, config, 'plugins: [{type: ip_set, args: {files: [./rules/ip.txt]}}]',
            custom_files=[{'path': 'rules/ip.txt', 'content': 'IP-CIDR,' + network + ',no-resolve'}])

def test_upload_ready_does_not_mark_version_and_status_survives_restart(env, monkeypatch):
    repository, manager, agent, config = env
    bundle = minimal_bundle(agent, config)
    seen = []
    def get(url, **_kwargs):
        if url.endswith('/capabilities'):
            return response({'success': True, 'deployment_protocols': [1], 'service_type': 'mihomo'})
        return response({'success': True, 'deployment_id': bundle.manifest['deployment_id'],
                         'status': 'succeeded', 'config_version': 'agent-final-hash'})
    def post(url, **kwargs):
        seen.append((url, kwargs))
        return response({'success': True, 'deployment_id': bundle.manifest['deployment_id'], 'status': 'ready'}, 202)
    monkeypatch.setattr('backend.agents.manager.requests.get', get)
    monkeypatch.setattr('backend.agents.manager.requests.post', post)
    result = manager.publish_deployment(agent['id'], bundle, activate=False)
    assert result['status'] == 'ready'
    assert manager.get_agent_by_id(agent['id'])['config_version'] == '0'
    assert seen[0][1]['data'] == bundle.archive
    assert seen[0][1]['headers']['X-Activate'] == 'false'
    assert seen[0][1]['headers']['X-Content-SHA256'] == bundle.sha256
    restarted = AgentManager(repository)
    result = restarted.get_deployment(agent['id'], result['deployment_id'])
    assert result['status'] == 'succeeded'
    assert restarted.get_agent_by_id(agent['id'])['config_version'] == 'agent-final-hash'
    assert restarted.get_agent_by_id(agent['id'])['latest_deployment']['status'] == 'succeeded'


def test_old_agents_fail_closed_without_legacy_upload(env, monkeypatch):
    _, manager, agent, config = env
    monkeypatch.setattr('backend.agents.manager.requests.get', lambda *_a, **_k: response({}, 404))
    monkeypatch.setattr('backend.agents.manager.requests.post', lambda *_a, **_k: pytest.fail('legacy fallback'))
    result = manager.publish_deployment(agent['id'], minimal_bundle(agent, config))
    assert result['success'] is False
    assert result['http_status'] == 409
    assert result['status'] == 'failed'
    assert 'upgrade' in result['message']
    assert manager.get_agent_by_id(agent['id'])['config_version'] == '0'


def test_unknown_upload_keeps_queryable_identity(env, monkeypatch):
    repository, manager, agent, config = env
    monkeypatch.setattr('backend.agents.manager.requests.get', lambda *_a, **_k: response({
        'success': True, 'deployment_protocols': [1], 'service_type': 'mihomo'}))
    def timeout(*_args, **_kwargs):
        raise requests.Timeout('http://token-secret@agent')
    monkeypatch.setattr('backend.agents.manager.requests.post', timeout)
    result = manager.publish_deployment(agent['id'], minimal_bundle(agent, config))
    assert result['success'] is True
    assert result['status'] == 'unknown'
    assert 'token-secret' not in str(result)
    assert AgentManager(repository).get_agent_by_id(agent['id'])['latest_deployment']['deployment_id'] == 'test-release'
    assert manager.begin_deployment(agent['id'], 'test-release')['existing']
    assert manager.begin_deployment(agent['id'], 'another')['http_status'] == 409


def test_preparation_interruption_and_profile_binding(env):
    repository, manager, agent, _ = env
    manager.begin_deployment(agent['id'], 'interrupted')
    assert manager.get_deployment(agent['id'], 'interrupted')['status'] == 'preparing'
    restarted = AgentManager(repository)
    assert restarted.get_deployment(agent['id'], 'interrupted')['status'] == 'preparing'
    manager.finish_deployment_preparation(agent['id'], 'interrupted')
    assert restarted.get_deployment(agent['id'], 'interrupted')['status'] == 'failed'
    repository.create_profile({'id': 'other'})
    restarted.begin_deployment(agent['id'], 'bound')
    # Simulate an out-of-band edit to verify status/activation ownership too.
    repository.update_system_transaction(lambda system: system['agents'][0].update(profile_id='other'))
    assert restarted.get_deployment(agent['id'], 'bound')['http_status'] == 409
    assert restarted.activate_deployment(agent['id'], 'bound')['http_status'] == 409


def test_route_client_id_persisted_and_staging_repeated_request_is_idempotent(env, monkeypatch):
    from flask import Flask
    from backend.routes import register_blueprints
    repository, manager, agent, _ = env
    monkeypatch.setattr('backend.routes.agents.get_agent_manager', lambda: manager)
    monkeypatch.setattr('backend.routes.agents.generate_mihomo_config', lambda *_a, **_k: 'mode: rule')
    monkeypatch.setattr('backend.routes.agents.get_mihomo_provider_downloads', lambda *_a, **_k: [])
    monkeypatch.setattr('backend.routes.agents.get_mihomo_ruleset_downloads', lambda *_a, **_k: [])
    posts = []
    def post(url, **kwargs):
        posts.append(url)
        return response({'success': True, 'deployment_id': 'client-id', 'status': 'ready'}, 202)
    def get(url, **kwargs):
        if url.endswith('/capabilities'):
            return response({'success': True, 'deployment_protocols': [1], 'service_type': 'mihomo'})
        return response({'success': True, 'deployment_id': 'client-id', 'status': 'ready'})
    monkeypatch.setattr('backend.agents.manager.requests.post', post)
    monkeypatch.setattr('backend.agents.manager.requests.get', get)
    app = Flask(__name__)
    register_blueprints(app)
    client = app.test_client()
    path = f'/api/agents/{agent["id"]}/push-config'
    first = client.post(path, json={'deployment_id': 'client-id', 'restart': False})
    assert first.status_code == 202
    assert first.json['status'] == 'ready'
    assert client.post(path, json={'deployment_id': 'client-id'}).status_code == 202
    assert len(posts) == 1
    assert client.get(f'/api/agents/{agent["id"]}/deployments/client-id').json['status'] == 'ready'


@pytest.mark.parametrize('rule_name', ['Claude', 'Google'])
def test_rule_download_failure_is_reported_without_pushing_to_agent(env, monkeypatch, rule_name):
    from flask import Flask
    from backend.routes import agents as routes, register_blueprints

    _, manager, agent, _ = env
    main = {'rule-providers': {rule_name: {'type': 'http', 'format': 'mrs',
                                         'url': 'https://rules.test/list', 'path': './rules/test.mrs'}}}
    monkeypatch.setattr(routes, 'get_agent_manager', lambda: manager)
    monkeypatch.setattr(routes, 'generate_mihomo_config', lambda *_a, **_k: yaml.safe_dump(main))
    monkeypatch.setattr(routes, 'get_mihomo_provider_downloads', lambda *_a, **_k: [])
    monkeypatch.setattr(routes, 'get_mihomo_ruleset_downloads', lambda *_a, **_k: [])
    monkeypatch.setattr(manager, 'push_config_to_agent', lambda *_a, **_k: pytest.fail('pushed incomplete bundle'))

    def fetch(*_args, **_kwargs):
        raise requests.exceptions.ConnectionError('https://rules.test/list?token=secret')

    monkeypatch.setattr('backend.agents.deployment_bundle.request_rule', fetch)
    app = Flask(__name__)
    register_blueprints(app)
    client = app.test_client()
    result = client.post(f'/api/agents/{agent["id"]}/push-config', json={'deployment_id': 'download-failure'})
    assert result.status_code == 400
    assert result.json['status'] == 'failed'
    assert result.json['message'] == (
        f'无法下载「{rule_name}」规则集：连接失败，请检查规则下载地址及网络连接。'
        '本次发布已停止，设备仍使用原配置。')
    recorded = client.get(f'/api/agents/{agent["id"]}/deployments/download-failure').json
    assert recorded['status'] == 'failed'
    assert recorded['message'] == result.json['message']
    assert recorded['error_details'] == result.json['error_details']
    assert 'ConnectionError:' in result.json['error_details']
    assert 'rules.test/list' in result.json['error_details']
    assert 'token=secret' not in result.json['error_details']
    assert not manager._preparation_running(agent['id'], 'download-failure')


def test_custom_installed_filename_negotiated_without_changing_bytes(env, monkeypatch):
    _, manager, agent, config = env
    bundle = minimal_bundle(agent, config)
    original_config = contents(bundle)['files/config.yaml']
    monkeypatch.setattr('backend.agents.manager.requests.get', lambda *_a, **_k: response({
        'success': True, 'deployment_protocols': [1], 'service_type': 'mihomo', 'config_path': 'custom.conf'}))
    def receive(url, **kwargs):
        with tarfile.open(fileobj=io.BytesIO(kwargs['data']), mode='r:gz') as archive:
            manifest = json.load(archive.extractfile('manifest.json'))
            assert manifest['config_path'] == 'custom.conf'
            assert archive.extractfile('files/custom.conf').read() == original_config
            assert 'files/config.yaml' not in archive.getnames()
            assert kwargs['headers']['X-Content-SHA256'] == hashlib.sha256(kwargs['data']).hexdigest()
        return response({'success': True, 'status': 'verifying'})
    monkeypatch.setattr('backend.agents.manager.requests.post', receive)
    assert manager.publish_deployment(agent['id'], bundle)['status'] == 'verifying'


def test_inline_rule_provider_and_local_file_failure(env):
    _, _, agent, config = env
    bundle = build_deployment_bundle(agent, config,
        'rule-providers: {inline: {type: inline, behavior: domain, payload: [example.test]}}')
    assert len(bundle.manifest['files']) == 1
    with pytest.raises(DeploymentPreparationError, match='managed rule source'):
        build_deployment_bundle(agent, config,
            'rule-providers: {local: {type: file, behavior: domain, path: ./rules/local.list}}')


def test_failed_rollback_is_blocking_and_can_reconcile_after_agent_recovery(env, monkeypatch):
    _, manager, agent, _ = env
    manager.begin_deployment(agent['id'], 'broken')
    manager.record_deployment(agent['id'], 'broken', {'success': False, 'status': 'rollback_failed', 'submitted': True})
    assert manager.begin_deployment(agent['id'], 'next')['http_status'] == 409
    monkeypatch.setattr('backend.agents.manager.requests.get', lambda *_a, **_k: response({
        'success': False, 'status': 'rolled_back'}))
    assert manager.get_deployment(agent['id'], 'broken')['status'] == 'rolled_back'
    assert manager.begin_deployment(agent['id'], 'next')['success']


@pytest.mark.parametrize('listen,expected', [(None, ':53'), ('127.0.0.1:23053', '127.0.0.1:23053')])
def test_mosdns_optional_listener_supports_isolated_deployment_without_changing_default(listen, expected):
    from backend.converters.mosdns import generate_mosdns_config
    config = {'rule_configs': [], 'mosdns': {'cache_enabled': False}}
    if listen is not None:
        config['mosdns']['listen_address'] = listen
    rendered = yaml.safe_load(generate_mosdns_config(config))
    listeners = [plugin['args']['listen'] for plugin in rendered['plugins']
                 if plugin['type'] in ('udp_server', 'tcp_server')]
    assert listeners == [expected, expected]


def test_late_pending_response_never_regresses_terminal_result(env):
    _, manager, agent, _ = env
    manager.begin_deployment(agent['id'], 'late-response')
    manager.record_deployment(agent['id'], 'late-response', {'status': 'unknown', 'message': 'Awaiting upload'})
    result = manager.record_deployment(agent['id'], 'late-response', {
        'success': True, 'status': 'succeeded', 'config_version': 'confirmed'})
    assert 'message' not in result
    late = manager.record_deployment(agent['id'], 'late-response', {'success': True, 'status': 'verifying'})
    assert late['status'] == 'succeeded'
    assert manager.get_agent_by_id(agent['id'])['config_version'] == 'confirmed'
    manager.finish_deployment_preparation(agent['id'], 'late-response')


@pytest.mark.parametrize('finish_during_get', [False, True])
def test_poll_before_upload_registration_does_not_fail_publication(env, monkeypatch, finish_during_get):
    _, manager, agent, _ = env
    deployment_id = 'upload-registering'
    manager.begin_deployment(agent['id'], deployment_id)
    manager.record_deployment(agent['id'], deployment_id, {
        'status': 'uploading', 'submitted': True, 'success': True})

    def not_registered(*_args, **_kwargs):
        if finish_during_get:
            manager.record_deployment(agent['id'], deployment_id, {'status': 'checking'})
            manager.finish_deployment_preparation(agent['id'], deployment_id)
        return {'success': False, 'http_status': 404}

    monkeypatch.setattr(manager, '_deployment_request', not_registered)
    pending = manager.get_deployment(agent['id'], deployment_id)
    assert pending['pending'] is True
    assert pending['status'] == 'uploading'
    assert manager.get_agent_by_id(agent['id'])['deployments'][deployment_id]['status'] != 'failed'
    manager.finish_deployment_preparation(agent['id'], deployment_id)
    monkeypatch.setattr(manager, '_deployment_request', lambda *_a, **_k: {
        'success': True, 'status': 'succeeded', 'config_version': 'confirmed'})
    assert manager.get_deployment(agent['id'], deployment_id)['status'] == 'succeeded'


def test_missing_agent_journal_after_upload_still_fails(env, monkeypatch):
    _, manager, agent, _ = env
    manager.begin_deployment(agent['id'], 'missing-upload')
    manager.record_deployment(agent['id'], 'missing-upload', {'status': 'uploading', 'submitted': True})
    manager.finish_deployment_preparation(agent['id'], 'missing-upload')
    monkeypatch.setattr(manager, '_deployment_request', lambda *_a, **_k: {
        'success': False, 'http_status': 404})
    assert manager.get_deployment(agent['id'], 'missing-upload')['status'] == 'failed'


@pytest.mark.parametrize('fail', [False, True])
def test_route_preparation_progress_is_visible_throttled_and_terminal_safe(env, monkeypatch, fail):
    from flask import Flask
    from backend.routes import agents as routes, register_blueprints
    _, manager, agent, config = env
    monkeypatch.setattr(routes, 'get_agent_manager', lambda: manager)
    now = [10.0]
    monkeypatch.setattr(routes.time, 'monotonic', lambda: now[0])
    messages = []
    original_record = manager.record_deployment

    def record(agent_id, deployment_id, updates):
        if updates.get('status') == 'preparing':
            messages.append(updates['message'])
        return original_record(agent_id, deployment_id, updates)

    monkeypatch.setattr(manager, 'record_deployment', record)
    app = Flask(__name__)
    register_blueprints(app)
    client = app.test_client()

    def prepare(*_args, progress, **_kwargs):
        for done in range(6):
            progress(done, 5)
            if done == 2:
                now[0] += 1.1
        state = client.get(f'/api/agents/{agent["id"]}/deployments/progress').json
        assert state['status'] == 'preparing'
        assert state['message'] == '规则文件准备完成，正在打包'
        if fail:
            raise DeploymentPreparationError('Invalid candidate')
        return {'bundle': minimal_bundle(agent, config, 'progress'), 'config_content': 'mode: rule',
                'prepared_providers': [], 'provider_downloads': [], 'ruleset_downloads': [], 'custom_files': []}

    monkeypatch.setattr(routes, '_prepare_transactional_content', prepare)
    monkeypatch.setattr(manager, 'push_config_to_agent', lambda *_a, **_k:
                        manager.record_deployment(agent['id'], 'progress', {'success': True, 'status': 'succeeded'}))
    result = client.post(f'/api/agents/{agent["id"]}/push-config', json={'deployment_id': 'progress'})
    assert result.status_code == (400 if fail else 200)
    assert result.json['status'] == ('failed' if fail else 'succeeded')
    assert messages == ['正在生成并检查配置', '正在准备规则文件 0/5', '正在准备规则文件 3/5',
                        '规则文件准备完成，正在打包'] + ([] if fail else ['文件已准备，正在校验并连接设备'])
    final = client.get(f'/api/agents/{agent["id"]}/deployments/progress').json
    assert final['status'] == result.json['status']
    assert final.get('message') == ('Invalid candidate' if fail else None)
    assert not manager._preparation_running(agent['id'], 'progress')


@pytest.mark.parametrize('status', ['preparing', 'uploading', 'checking', 'ready', 'rollback_failed'])
def test_profile_rebinding_rejected_for_every_unfinished_deployment(env, status):
    from backend.agents.manager import AgentDeploymentConflict
    repository, manager, agent, _ = env
    repository.create_profile({'id': 'other'})
    manager.begin_deployment(agent['id'], 'bound-publish')
    manager.record_deployment(agent['id'], 'bound-publish', {'status': status})
    with pytest.raises(AgentDeploymentConflict):
        manager.update_agent(agent['id'], {'profile_id': 'other'})
    assert manager.get_agent_by_id(agent['id'])['profile_id'] == 'default'
    assert manager.update_agent(agent['id'], {'profile_id': 'default', 'name': 'renamed'})
    manager.finish_deployment_preparation(agent['id'], 'bound-publish')


def test_profile_guard_observes_publication_started_before_transaction(env, monkeypatch):
    from backend.agents.manager import AgentDeploymentConflict
    repository, manager, agent, _ = env
    repository.create_profile({'id': 'other'})
    original = repository.update_system_transaction
    def race(updater):
        monkeypatch.setattr(repository, 'update_system_transaction', original)
        manager.begin_deployment(agent['id'], 'raced-publish')
        return original(updater)
    monkeypatch.setattr(repository, 'update_system_transaction', race)
    with pytest.raises(AgentDeploymentConflict):
        manager.update_agent(agent['id'], {'profile_id': 'other'})
    assert manager.get_agent_by_id(agent['id'])['profile_id'] == 'default'
    manager.record_deployment(agent['id'], 'raced-publish', {'status': 'succeeded'})
    assert manager.update_agent(agent['id'], {'profile_id': 'other'})
    manager.finish_deployment_preparation(agent['id'], 'raced-publish')


@pytest.mark.parametrize('suffix', ['deployments', 'deployments/test/activate', 'deployments/test/rollback'])
def test_post_retries_only_exact_busy_with_identical_archive_and_identity(env, monkeypatch, suffix):
    _, manager, agent, _ = env
    calls = []
    sleeps = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        if len(calls) == 1:
            return response({'success': False, 'message': 'another service operation is in progress'}, 409)
        return response({'success': True, 'status': 'verifying'}, 202)
    monkeypatch.setattr('backend.agents.manager.requests.post', post)
    monkeypatch.setattr('backend.agents.manager.time.sleep', sleeps.append)
    body = b'archive-original-bytes'
    result = manager._deployment_request(agent, 'post', suffix, data=body,
                                        headers={'X-Deployment-ID': 'same-id', 'X-Content-SHA256': 'same-sha'})
    assert result['status'] == 'verifying'
    assert len(calls) == 2 and sleeps == [.1]
    assert calls[0] == calls[1]
    assert calls[1][1]['data'] is body


def test_busy_retries_bounded_other_conflicts_and_network_errors_never_retried(env, monkeypatch):
    _, manager, agent, _ = env
    calls = []
    def busy(url, **kwargs):
        calls.append(url)
        return response({'message': 'another service operation is in progress'}, 409)
    monkeypatch.setattr('backend.agents.manager.requests.post', busy)
    monkeypatch.setattr('backend.agents.manager.time.sleep', lambda _: None)
    monkeypatch.setattr('backend.agents.manager.time.monotonic', lambda: 0)
    assert manager._deployment_request(agent, 'post', 'deployments')['http_status'] == 409
    assert len(calls) == 21
    calls.clear()
    assert manager._deployment_request(agent, 'post', 'restart')['http_status'] == 409
    assert len(calls) == 1
    def conflict(url, **kwargs):
        calls.append(url)
        return response({'message': 'deployment ID already belongs to different content'}, 409)
    monkeypatch.setattr('backend.agents.manager.requests.post', conflict)
    calls.clear()
    assert manager._deployment_request(agent, 'post', 'deployments')['http_status'] == 409
    assert len(calls) == 1
    def timeout(url, **kwargs):
        calls.append(url)
        raise requests.Timeout('ambiguous outcome')
    monkeypatch.setattr('backend.agents.manager.requests.post', timeout)
    calls.clear()
    with pytest.raises(requests.Timeout):
        manager._deployment_request(agent, 'post', 'deployments')
    assert len(calls) == 1


def test_profile_binding_route_reports_deployment_conflict_as_409(env, monkeypatch):
    from flask import Flask
    from backend.routes import register_blueprints
    repository, manager, agent, _ = env
    repository.create_profile({'id': 'other'})
    manager.begin_deployment(agent['id'], 'binding-request')
    monkeypatch.setattr('backend.routes.agents.get_agent_manager', lambda: manager)
    app = Flask(__name__)
    register_blueprints(app)
    result = app.test_client().put(f'/api/agents/{agent["id"]}', json={'profile_id': 'other'})
    assert result.status_code == 409
    assert 'unfinished deployment' in result.json['message']
    assert manager.get_agent_by_id(agent['id'])['profile_id'] == 'default'
    manager.finish_deployment_preparation(agent['id'], 'binding-request')


def successful_deployment(manager, agent, deployment_id='manual-test'):
    manager.begin_deployment(agent['id'], deployment_id)
    manager.record_deployment(agent['id'], deployment_id, {
        'status': 'succeeded', 'success': True, 'submitted': True,
        'config_version': 'current', 'config_revision': 9})
    manager.finish_deployment_preparation(agent['id'], deployment_id)
    return deployment_id


def test_manual_rollback_reserves_before_post_and_invalidates_applied_baseline(env, monkeypatch):
    repository, manager, agent, _ = env
    deployment_id = successful_deployment(manager, agent)
    repository.update_system_transaction(lambda system: system['agents'][0].update(applied_content={'fingerprint': 'current'}))
    def post(url, **kwargs):
        assert url.endswith(f'/deployments/{deployment_id}/rollback')
        reserved = manager.get_agent_by_id(agent['id'])['latest_deployment']
        assert reserved['manual_rollback'] and reserved['status'] == 'rolling_back'
        assert manager.begin_deployment(agent['id'], 'raced-publish')['http_status'] == 409
        # An old publication poll must not mark the rollback complete.
        assert manager.record_deployment(agent['id'], deployment_id, {'status': 'succeeded'})['status'] == 'rolling_back'
        return response({'success': True, 'status': 'rolling_back', 'manual_rollback': True}, 202)
    monkeypatch.setattr('backend.agents.manager.requests.post', post)
    assert manager.rollback_deployment(agent['id'], deployment_id)['status'] == 'rolling_back'
    monkeypatch.setattr('backend.agents.manager.requests.get', lambda *_a, **_k: response({
        'success': True, 'status': 'rolled_back', 'manual_rollback': True, 'config_version': 'previous'}))
    restarted = AgentManager(repository)
    result = restarted.get_deployment(agent['id'], deployment_id)
    assert result['success'] and result['manual_rollback'] and result['status'] == 'rolled_back'
    current = restarted.get_agent_by_id(agent['id'])
    assert current['config_version'] == 'previous'
    assert not {'applied_content', 'pushed_at', 'config_revision'} & current.keys()
    assert current['latest_deployment']['manual_rollback']
    # Repeated rollback requests preserve the same completed result.
    assert restarted.rollback_deployment(agent['id'], deployment_id) == result


@pytest.mark.parametrize('rejection', [400, 404, 405, 409])
def test_manual_rollback_definite_rejection_preserves_successful_publication(env, monkeypatch, rejection):
    _, manager, agent, _ = env
    deployment_id = successful_deployment(manager, agent)
    monkeypatch.setattr('backend.agents.manager.requests.post', lambda *_a, **_k: response({
        'message': 'active managed files changed since deployment; refusing rollback'}, rejection))
    result = manager.rollback_deployment(agent['id'], deployment_id)
    assert result['http_status'] == rejection
    current = manager.get_agent_by_id(agent['id'])
    assert current['config_version'] == 'current'
    assert current['latest_deployment']['status'] == 'succeeded'
    assert current['latest_deployment']['rollback_rejected']
    assert not current['latest_deployment'].get('manual_rollback')
    assert manager.begin_deployment(agent['id'], 'after-rejection')['success']
    manager.finish_deployment_preparation(agent['id'], 'after-rejection')


@pytest.mark.parametrize('retry_rejected', [False, True])
def test_manual_rollback_transport_timeout_reconciles_same_identity(env, monkeypatch, retry_rejected):
    _, manager, agent, _ = env
    deployment_id = successful_deployment(manager, agent)
    def timeout(*_a, **_k):
        raise requests.Timeout('secret URL must not leak')
    monkeypatch.setattr('backend.agents.manager.requests.post', timeout)
    result = manager.rollback_deployment(agent['id'], deployment_id)
    assert result['status'] == 'unknown' and result['manual_rollback']
    assert 'secret' not in str(result)
    monkeypatch.setattr('backend.agents.manager.requests.get', lambda *_a, **_k: response({
        'success': True, 'status': 'succeeded'}))
    calls = []
    def retry(url, **kwargs):
        calls.append(url)
        if retry_rejected:
            return response({'message': 'rollback backup checksum mismatch'}, 409)
        return response({'success': True, 'status': 'rolled_back', 'manual_rollback': True, 'config_version': 'old'})
    monkeypatch.setattr('backend.agents.manager.requests.post', retry)
    result = manager.get_deployment(agent['id'], deployment_id)
    assert calls == [f'http://agent.test:8080/api/deployments/{deployment_id}/rollback']
    if retry_rejected:
        assert result['status'] == 'succeeded' and result['rollback_rejected']
        assert result['rollback_message'] == 'rollback backup checksum mismatch'
        assert not result.get('manual_rollback')
    else:
        assert result['status'] == 'rolled_back' and result['manual_rollback']


@pytest.mark.parametrize('change', ['profile', 'latest', 'upgrade', 'status'])
def test_manual_rollback_ownership_and_operation_guards(env, monkeypatch, change):
    repository, manager, agent, _ = env
    deployment_id = successful_deployment(manager, agent)
    repository.create_profile({'id': 'another-profile'})
    def edit(system):
        saved = system['agents'][0]
        if change == 'profile':
            saved['profile_id'] = 'another-profile'
        elif change == 'latest':
            saved['latest_deployment'] = {'deployment_id': 'another-deployment'}
        elif change == 'upgrade':
            saved['latest_upgrade'] = {'status': 'installing'}
        else:
            saved['deployments'][deployment_id]['status'] = 'failed'
    repository.update_system_transaction(edit)
    monkeypatch.setattr('backend.agents.manager.requests.post', lambda *_a, **_k: pytest.fail('guard must precede network'))
    assert manager.rollback_deployment(agent['id'], deployment_id)['http_status'] == 409


def test_manual_rollback_route_retains_flag_and_reports_completed_rollback_as_success(env, monkeypatch):
    from flask import Flask
    from backend.routes import register_blueprints
    _, manager, agent, _ = env
    deployment_id = successful_deployment(manager, agent)
    monkeypatch.setattr('backend.routes.agents.get_agent_manager', lambda: manager)
    monkeypatch.setattr('backend.agents.manager.requests.post', lambda *_a, **_k: response({
        'success': True, 'status': 'rolled_back', 'manual_rollback': True, 'config_version': 'before'}))
    app = Flask(__name__)
    register_blueprints(app)
    client = app.test_client()
    path = f'/api/agents/{agent["id"]}/deployments/{deployment_id}'
    result = client.post(path + '/rollback', json={})
    assert result.status_code == 200 and result.json['success']
    assert result.json['manual_rollback'] and result.json['status'] == 'rolled_back'
    polled = client.get(path)
    assert polled.status_code == 200 and polled.json['manual_rollback']


def test_manual_rollback_busy_after_ambiguous_request_remains_pending(env, monkeypatch):
    _, manager, agent, _ = env
    deployment_id = successful_deployment(manager, agent)
    def timeout(*_a, **_k):
        raise requests.Timeout('ambiguous')
    monkeypatch.setattr('backend.agents.manager.requests.post', timeout)
    assert manager.rollback_deployment(agent['id'], deployment_id)['status'] == 'unknown'
    monkeypatch.setattr('backend.agents.manager.requests.get', lambda *_a, **_k: response({'status': 'succeeded'}))
    monkeypatch.setattr('backend.agents.manager.requests.post', lambda *_a, **_k: response({
        'message': 'another service operation is in progress'}, 409))
    monkeypatch.setattr('backend.agents.manager.time.sleep', lambda _: None)
    monkeypatch.setattr('backend.agents.manager.time.monotonic', lambda: 0)
    result = manager.get_deployment(agent['id'], deployment_id)
    assert result['status'] == 'unknown' and result['manual_rollback'] and result['pending']
    assert manager.begin_deployment(agent['id'], 'unsafe-next')['http_status'] == 409
