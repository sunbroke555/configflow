"""Agent logs hide credentials without blanking the whole log."""
from urllib.parse import quote

from flask import Flask

from backend.common.config_export import redact_secrets_in_text
from backend.common.profile_context import install_profile_context
from backend.routes import register_blueprints
from backend.test_agent_deployments import env


def _set_rule_token(repository, secret):
    system = repository.get_system()
    system.setdefault('system_config', {})['rule_proxy_token'] = secret
    repository.save_system(system)


def test_agent_logs_keep_lines_and_hide_rule_proxy_token(env, monkeypatch):
    repository, manager, agent, _ = env
    secret = 'rule/proxy+secret=0123456789'
    _set_rule_token(repository, secret)
    logs = (
        'INFO download batch finished successes=1\n'
        f'INFO fetch https://cf.example/api/profiles/default/mosdns/rule-proxy?token={quote(secret, safe="")}&url=x\n'
        f'WARN retry https://cf.example/rule-proxy?token={quote(quote(secret, safe=""), safe="")}\n'
        'INFO ip_set snapshot built\n'
    )
    monkeypatch.setattr(manager, 'get_agent_logs', lambda *_args, **_kwargs: {'success': True, 'logs': logs})
    monkeypatch.setattr('backend.routes.agents.get_agent_manager', lambda: manager)
    app = Flask(__name__)
    register_blueprints(app)
    install_profile_context(app)

    response = app.test_client().get(f'/api/agents/{agent["id"]}/logs', query_string={'source': 'core'})

    assert response.status_code == 200
    assert response.json['logs'].splitlines() == [
        'INFO download batch finished successes=1',
        'INFO fetch https://cf.example/api/profiles/default/mosdns/rule-proxy?token=[REDACTED]&url=x',
        '[REDACTED]',
        'INFO ip_set snapshot built',
    ]
    assert secret not in response.get_data(as_text=True)


def test_redact_secrets_in_text_leaves_clean_text_untouched(env):
    repository, _, agent, _ = env
    _set_rule_token(repository, 'another-rule-secret-abcdef')
    assert redact_secrets_in_text('plain\nlog\n') == 'plain\nlog\n'
    assert redact_secrets_in_text(f'Bearer {agent["token"]}\nok') == 'Bearer [REDACTED]\nok'
    assert redact_secrets_in_text('') == ''
    assert redact_secrets_in_text(None) is None
