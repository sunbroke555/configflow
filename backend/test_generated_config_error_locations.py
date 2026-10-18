"""Generated YAML failures keep their file location before bundle preparation."""
import pytest
import yaml
from flask import Flask

from backend.agents.deployment_locations import DeploymentPreparationError
from backend.common.oxidns_validation import OxidnsValidationError
from backend.routes import agents, register_blueprints
from backend.test_agent_deployments import env


def generated(monkeypatch, service, text):
    target = ('backend.routes.agents.generate_mihomo_config' if service == 'mihomo'
              else 'backend.converters.oxidns.generate_oxidns_config')
    monkeypatch.setattr(target, lambda *_args, **_kwargs: text)


@pytest.mark.parametrize('service', ['mihomo', 'oxidns'])
def test_generated_yaml_parse_error_has_actual_mark_and_stops_preparation(monkeypatch, service):
    text = 'mode: rule\nrules:\n  - [bad}\n'
    generated(monkeypatch, service, text)
    monkeypatch.setattr('backend.agents.deployment_bundle.build_deployment_bundle',
                        lambda *_a, **_k: pytest.fail('invalid YAML reached bundle preparation'))
    with pytest.raises(yaml.YAMLError) as original:
        yaml.safe_load(text)
    with pytest.raises(DeploymentPreparationError) as failure:
        agents._prepare_transactional_content({'service_type': service}, {})
    detail = str(failure.value)
    mark = original.value.problem_mark
    assert '文件：config.yaml（生成配置）' in detail
    assert f'位置：第 {mark.line + 1} 行，第 {mark.column + 1} 列' in detail
    assert '[bad}' in detail
    assert original.value.problem in detail
    assert isinstance(failure.value.__cause__, yaml.YAMLError)


def test_oxidns_schema_error_keeps_issues_and_exact_generated_field(monkeypatch):
    text = ('plugins:\n  - tag: main\n    type: udp_server\n    args:\n'
            '      listen: invalid-address\n      entry: main\n')
    generated(monkeypatch, 'oxidns', text)
    monkeypatch.setattr('backend.agents.deployment_bundle.build_deployment_bundle',
                        lambda *_a, **_k: pytest.fail('invalid schema reached bundle preparation'))
    with pytest.raises(DeploymentPreparationError) as failure:
        agents._prepare_transactional_content({'service_type': 'oxidns'}, {})
    exc = failure.value
    assert isinstance(exc.__cause__, OxidnsValidationError)
    assert exc.issues is exc.__cause__.issues
    assert len(exc.issues) == 3
    assert str(exc.__cause__) in str(exc)
    assert '文件：config.yaml（生成配置）' in str(exc)
    assert '位置：第 5 行，第 15 列' in str(exc)
    assert 'listen: invalid-address' in str(exc)


def test_missing_generated_field_keeps_path_without_inventing_line(monkeypatch):
    text = 'plugins:\n  - tag: main\n    type: udp_server\n    args:\n      listen: 127.0.0.1:53\n'
    generated(monkeypatch, 'oxidns', text)
    with pytest.raises(DeploymentPreparationError) as failure:
        agents._prepare_transactional_content({'service_type': 'oxidns'}, {})
    detail = str(failure.value)
    assert 'plugins[0].args.entry' in detail
    assert '文件：config.yaml（生成配置）' in detail
    assert '位置：第' not in detail
    assert '出错内容：' not in detail


def test_document_error_without_field_location_does_not_invent_one(monkeypatch):
    generated(monkeypatch, 'oxidns', '[]\n')
    with pytest.raises(DeploymentPreparationError) as failure:
        agents._prepare_transactional_content({'service_type': 'oxidns'}, {})
    assert failure.value.issues[0]['path'] == []
    assert '文件：config.yaml（生成配置）' in str(failure.value)
    assert '位置：第' not in str(failure.value)


def test_schema_mark_matches_last_duplicate_yaml_field(monkeypatch):
    text = ('plugins:\n  - tag: main\n    type: udp_server\n    args:\n'
            '      listen: 127.0.0.1:53\n      listen: invalid-address\n      entry: main\n')
    generated(monkeypatch, 'oxidns', text)
    with pytest.raises(DeploymentPreparationError) as failure:
        agents._prepare_transactional_content({'service_type': 'oxidns'}, {})
    assert '位置：第 6 行，第 15 列' in str(failure.value)
    assert 'listen: invalid-address' in str(failure.value)


@pytest.mark.parametrize('service', ['mihomo', 'oxidns'])
@pytest.mark.parametrize('text', [
    'password: "private-first\n  private-second\n',
    'password: |\n  private-first\n  private-second\ninvalid: [\n',
])
def test_generated_yaml_failure_hides_multiline_credentials(monkeypatch, service, text):
    generated(monkeypatch, service, text)
    with pytest.raises(DeploymentPreparationError) as failure:
        agents._prepare_transactional_content({'service_type': service}, {})
    detail = str(failure.value)
    assert '文件：config.yaml（生成配置）' in detail
    assert '位置：第' in detail
    assert 'private-first' not in detail and 'private-second' not in detail


def test_schema_failure_excerpt_hides_password_field_and_keeps_issue():
    text = 'api:\n  password: |\n    private-first\n    private-second\n'
    original = OxidnsValidationError([{'path': ['api', 'password'], 'message': '参数无效', 'severity': 'error'}])
    failure = agents._generated_config_failure(original, text, 'OxiDNS')
    detail = str(failure)
    assert failure.issues is original.issues
    assert '位置：第 2 行' in detail
    assert '敏感内容已隐藏' in detail
    assert 'private-first' not in detail and 'private-second' not in detail


@pytest.mark.parametrize('service', ['mihomo', 'oxidns'])
def test_success_keeps_one_existing_parse_and_no_diagnostic_parse(monkeypatch, service):
    text = 'plugins: []\n' if service == 'oxidns' else 'mode: rule\n'
    generated(monkeypatch, service, text)
    for target in ['backend.routes.agents.get_mihomo_provider_downloads',
                   'backend.routes.agents.get_mihomo_ruleset_downloads',
                   'backend.converters.oxidns.get_oxidns_ruleset_downloads',
                   'backend.converters.oxidns.get_oxidns_custom_files']:
        monkeypatch.setattr(target, lambda *_a, **_k: [])
    sentinel = object()
    monkeypatch.setattr('backend.agents.deployment_bundle.build_deployment_bundle', lambda *_a, **_k: sentinel)
    parse = yaml.safe_load
    calls = []
    monkeypatch.setattr(yaml, 'safe_load', lambda content: (calls.append(content), parse(content))[1])
    monkeypatch.setattr(yaml, 'compose', lambda *_a, **_k: pytest.fail('successful preparation parsed diagnostic marks'))
    result = agents._prepare_transactional_content({'service_type': service}, {})
    assert result['bundle'] is sentinel
    assert calls == [text]


@pytest.mark.parametrize('service', ['mihomo', 'oxidns'])
def test_push_route_persists_generated_yaml_location(env, monkeypatch, service):
    repository, manager, agent, _ = env
    repository.update_system_transaction(lambda system: system['agents'][0].update(service_type=service))
    monkeypatch.setattr(agents, 'get_agent_manager', lambda: manager)
    generated(monkeypatch, service, 'rules:\n  - [bad}\n')
    monkeypatch.setattr(manager, 'push_config_to_agent', lambda *_a, **_k: pytest.fail('invalid config uploaded'))
    app = Flask(__name__)
    register_blueprints(app)
    client = app.test_client()
    result = client.post(f'/api/agents/{agent["id"]}/push-config', json={'deployment_id': 'generated-yaml-failure'})
    assert result.status_code == 400
    assert result.json['failed_stage'] == 'preparing'
    assert '文件：config.yaml（生成配置）' in result.json['error_details']
    assert '位置：第 2 行' in result.json['error_details']
    persisted = client.get(f'/api/agents/{agent["id"]}/deployments/generated-yaml-failure')
    assert persisted.json['error_details'] == result.json['error_details']


def test_push_route_keeps_schema_issues_with_source_excerpt(env, monkeypatch):
    repository, manager, agent, _ = env
    repository.update_system_transaction(lambda system: system['agents'][0].update(service_type='oxidns'))
    monkeypatch.setattr(agents, 'get_agent_manager', lambda: manager)
    generated(monkeypatch, 'oxidns', 'plugins:\n  - tag: main\n    type: udp_server\n    args:\n      listen: invalid-address\n')
    monkeypatch.setattr(manager, 'push_config_to_agent', lambda *_a, **_k: pytest.fail('invalid config uploaded'))
    app = Flask(__name__)
    register_blueprints(app)
    result = app.test_client().post(f'/api/agents/{agent["id"]}/push-config', json={'deployment_id': 'generated-schema-failure'})
    assert result.status_code == 400
    assert result.json['issues']
    # The first issue is a missing required entry: preserve that path without
    # manufacturing a line for the absent field or losing the other issues.
    assert result.json['issues'][0]['path'] == ['plugins', 0, 'args', 'entry']
    assert any(issue['path'] == ['plugins', 0, 'args', 'listen'] for issue in result.json['issues'])
    assert '文件：config.yaml（生成配置）' in result.json['error_details']
