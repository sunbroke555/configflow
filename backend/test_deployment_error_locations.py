"""Publication failures identify the source without changing delivered bytes."""
from copy import deepcopy

import pytest
import requests
import yaml

from backend.agents.deployment_bundle import (
    DeploymentPreparationError, build_deployment_bundle, convert_mosdns_rules,
    deployment_content_fingerprint, retarget_config_path,
)
from backend.agents.deployment_locations import source_error, source_excerpt
from backend.test_agent_deployments import contents, env


def rule_source(config, content):
    config['rule_library'] = [{'id': 'grok-id', 'name': 'grok', 'source_type': 'content', 'content': content}]
    return {'name': 'grok', 'url': 'https://config.test/api/profiles/default/rule-library/content/grok-id',
            'local_path': './rules/grok.txt'}


@pytest.mark.parametrize('service', ['mosdns', 'oxidns'])
@pytest.mark.parametrize(('content', 'line', 'excerpt'), [
    ('# comment\r\n\r\n11\r\n', 3, '11'),
    ('\ufeff# comment\n\n11', 3, '11'),
    ('# comment\npayload:\n  - DOMAIN,ok.test\n  - "11"\n', 4, '  - "11"'),
    ('# comment\npayload:\n  - DOMAIN,ok.test\n  - 11\n', 4, '  - 11'),
    ('# comment\npayload: 11\n', 2, 'payload: 11'),
    ('payload: [DOMAIN,ok.test]\npayload:\n  - "11"\n', 3, '  - "11"'),
])
def test_rule_conversion_error_retains_name_actual_line_and_source(env, monkeypatch, service, content, line, excerpt):
    _, _, agent, config = env
    agent['service_type'] = service
    item = rule_source(config, content)
    monkeypatch.setattr('backend.agents.deployment_bundle.request_rule', lambda *_a, **_k: pytest.fail('self HTTP'))
    main = 'plugins: [{tag: domain_grok, type: domain_set, args: {files: [./rules/grok.txt]}}]'
    with pytest.raises(DeploymentPreparationError) as caught:
        build_deployment_bundle(agent, config, main, ruleset_downloads=[item])
    detail = str(caught.value)
    assert '规则集「grok」' in detail
    assert '文件：./rules/grok.txt' in detail
    assert f'位置：第 {line} 行' in detail
    assert '内容来源：转换前的规则内容' in detail
    assert f'出错内容：{excerpt}' in detail
    assert '原始错误：' in detail


def test_invalid_ip_rule_points_to_original_payload_line():
    text = '# source\npayload:\n  - IP-CIDR,192.168.0.0/24\n  - IP-CIDR,999.1.2.3/24\n'
    with pytest.raises(DeploymentPreparationError) as caught:
        convert_mosdns_rules(text.encode(), 'ip_set')
    assert '第 4 行' in str(caught.value)
    assert '出错内容：  - IP-CIDR,999.1.2.3/24' in str(caught.value)


def test_yaml_parser_location_uses_its_actual_mark_and_keeps_reason():
    text = '# source\npayload:\n  - DOMAIN,ok.test\n  - [\n'
    try:
        yaml.safe_load(text)
    except yaml.YAMLError as expected:
        line, problem = expected.problem_mark.line + 1, expected.problem
    with pytest.raises(DeploymentPreparationError) as caught:
        convert_mosdns_rules(text.encode())
    assert f'第 {line} 行' in str(caught.value)
    assert problem in str(caught.value)
    assert '出错内容：[文件末尾]' in str(caught.value)


@pytest.mark.parametrize('service', ['mihomo', 'mosdns', 'oxidns'])
def test_generated_yaml_has_file_and_true_line(env, service):
    _, _, agent, config = env
    agent['service_type'] = service
    with pytest.raises(DeploymentPreparationError) as caught:
        build_deployment_bundle(agent, config, 'key: [\n')
    assert service in str(caught.value)
    assert 'config.yaml' in str(caught.value)
    assert '第 2 行' in str(caught.value)
    assert 'expected' in str(caught.value)


def test_custom_missing_content_has_file_name_without_invented_line(env):
    _, _, agent, config = env
    with pytest.raises(DeploymentPreparationError) as caught:
        build_deployment_bundle(agent, config, 'mode: rule', custom_files=[{
            'path': './rules/local.list', 'name': '自定义规则'}])
    detail = str(caught.value)
    assert '附加文件「自定义规则」' in detail
    assert '文件：./rules/local.list' in detail
    assert 'Missing deployment file content' in detail
    assert '位置：' not in detail


def test_network_error_has_name_path_and_reason_without_invented_line(env, monkeypatch):
    _, _, agent, config = env

    def fail(*_a, **_k):
        raise requests.ConnectionError('https://user:secret@rules.test/rules?token=hidden')

    monkeypatch.setattr('backend.agents.deployment_bundle.request_rule', fail)
    with pytest.raises(DeploymentPreparationError) as caught:
        build_deployment_bundle(agent, config, 'mode: rule', ruleset_downloads=[{
            'name': 'Remote', 'url': 'https://rules.test/rules', 'local_path': './rules/remote.txt'}])
    detail = str(caught.value)
    assert '「Remote」' in detail and '文件：./rules/remote.txt' in detail
    assert 'ConnectionError' in detail and 'rules.test/rules' in detail
    assert '位置：' not in detail
    assert 'secret' not in detail and 'hidden' not in detail


@pytest.mark.parametrize(('text', 'line', 'hidden'), [
    ('password: |\n  protected-value\nother: broken\n', 2, 'protected-value'),
    ('- password: |\n    protected-value\n  other: broken\n', 2, 'protected-value'),
    ('uuid: "first\n  protected-value"\n', 2, 'protected-value'),
    ('{password: "first\n  protected-value" broken}\n', 2, 'protected-value'),
    ('-----BEGIN PRIVATE KEY-----\nprotected-value\n-----END PRIVATE KEY-----\n', 2, 'protected-value'),
])
def test_source_excerpt_hides_sensitive_continuations(text, line, hidden):
    detail = str(source_error('invalid', text, line=line))
    assert hidden not in detail
    assert '已隐藏' in detail


def test_excerpt_preserves_indentation_and_does_not_hide_next_public_field():
    assert source_excerpt('password: |\n  sensitive\nname: wrong\n', 3) == '出错内容：name: wrong'
    assert source_excerpt('payload:\n  - 11\n', 2) == '出错内容：  - 11'


def test_manifest_names_survive_retarget_but_do_not_change_sync_fingerprint(env):
    _, _, agent, config = env
    agent['service_type'] = 'mosdns'
    item = rule_source(config, 'DOMAIN-SUFFIX,example.test')
    bundle = build_deployment_bundle(agent, config,
        'plugins: [{type: domain_set, args: {files: [./rules/grok.txt]}}]', ruleset_downloads=[item])
    rule = next(entry for entry in bundle.manifest['files'] if entry['role'] == 'rule')
    assert rule['name'] == 'grok'
    assert contents(bundle)['files/rules/grok.txt'] == b'domain:example.test'
    renamed = deepcopy(bundle.manifest)
    for entry in renamed['files']:
        entry['name'] = '新的显示名称'
    fingerprint = deployment_content_fingerprint(bundle.manifest)
    assert deployment_content_fingerprint(renamed) == fingerprint
    retargeted = retarget_config_path(bundle, 'actual.yaml')
    assert next(entry for entry in retargeted.manifest['files'] if entry['role'] == 'rule')['name'] == 'grok'
    assert contents(retargeted)['files/actual.yaml'] == contents(bundle)['files/config.yaml']
    assert deployment_content_fingerprint(retargeted.manifest) == fingerprint


@pytest.mark.parametrize('service', ['mosdns', 'oxidns'])
def test_failed_rule_details_reach_response_and_history_without_agent_upload(env, monkeypatch, service):
    from flask import Flask
    from backend.routes import agents as routes, register_blueprints
    from backend.converters import oxidns

    _, manager, agent, config = env
    manager.update_agent(agent['id'], {'service_type': service})
    item = rule_source(config, '# source\n11\n')
    main = 'plugins: [{tag: grok, type: domain_set, args: {files: [./rules/grok.txt]}}]'
    module = oxidns if service == 'oxidns' else routes
    monkeypatch.setattr(routes, 'get_agent_manager', lambda: manager)
    monkeypatch.setattr(routes, 'get_config', lambda *_a, **_k: deepcopy(config))
    monkeypatch.setattr('backend.agents.deployment_bundle.request_rule', lambda *_a, **_k: pytest.fail('self HTTP'))
    monkeypatch.setattr(module, f'generate_{service}_config', lambda *_a, **_k: main)
    monkeypatch.setattr(module, f'get_{service}_ruleset_downloads', lambda *_a, **_k: [item])
    monkeypatch.setattr(module, f'get_{service}_custom_files', lambda *_a, **_k: [])
    monkeypatch.setattr(manager, 'push_config_to_agent', lambda *_a, **_k: pytest.fail('upload before preparation completed'))
    app = Flask(__name__)
    register_blueprints(app)
    client = app.test_client()
    endpoint = f'/api/agents/{agent["id"]}'
    result = client.post(endpoint + '/push-config', json={'deployment_id': 'grok-location'})
    assert result.status_code == 400
    assert result.json['failed_stage'] == 'preparing'
    assert '规则集「grok」' in result.json['message']
    detail = result.json['error_details']
    assert '文件：./rules/grok.txt' in detail and '第 2 行' in detail and '出错内容：11' in detail
    stored = client.get(endpoint + '/deployments/grok-location').json
    assert stored['error_details'] == detail
    assert stored['status'] == 'failed'
    assert not manager._preparation_running(agent['id'], 'grok-location')
