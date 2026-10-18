"""Provider failures retain exact source context without changing delivery rules."""
from unittest.mock import Mock

import pytest
import yaml

from backend.utils.dialer_references import DialerReferenceError
from backend.utils.strategy_references import StrategyReferenceError
from backend.utils.provider_delivery import (
    DeliverySnapshot, parse_provider_proxies, prepare_subscription_provider,
    prepare_provider_bundle, validate_rendered_bundle,
)


@pytest.fixture(autouse=True)
def isolated_source_cache(tmp_path, monkeypatch):
    from backend.common import config
    from backend.common.config_repository import ProfileRepository
    monkeypatch.setattr(config, '_repository', ProfileRepository(tmp_path))
    config.reset_config_context()
    yield
    config.reset_config_context()


def snapshot(main=None):
    return DeliverySnapshot.capture('default', main or {'proxies': [], 'proxy-groups': []})


def test_invalid_yaml_keeps_source_position_and_original_problem():
    text = 'proxies:\n  - name: Broken\n    type: [http\n'
    with pytest.raises(DialerReferenceError) as caught:
        parse_provider_proxies(text, name='机场 A', path='./providers/a.yaml')
    detail = str(caught.value)
    assert '订阅「机场 A」' in detail
    assert '文件：./providers/a.yaml' in detail
    assert '内容来源：本次订阅返回的内容' in detail
    assert '第 4 行，第 1 列' in detail
    assert '出错内容：[文件末尾]' in detail
    assert "expected ',' or ']'" in detail
    assert isinstance(caught.value, StrategyReferenceError)


@pytest.mark.parametrize('text,field,line,content', [
    ('proxies: wrong\n', 'proxies', 1, 'proxies: wrong'),
    ('proxies:\n  - null\n', 'proxies[0]', 2, '- null'),
    ('proxies:\n  - name: []\n', 'proxies[0].name', 2, 'name: []'),
    ('proxies:\n  - name: Missing type\n    type: []\n', 'proxies[0].type', 3, 'type: []'),
    ('proxies:\n  - name: Empty name\n    proxies: null\n', 'proxies[0].proxies', 3, 'proxies: null'),
    ('proxies:\n  - name: Same\n    type: http\n  - name: Same\n    type: []\n', 'proxies[1].type', 5, 'type: []'),
    ('proxies: []\nproxies:\n  - name: Duplicate key\n    type: []\n', 'proxies[0].type', 4, 'type: []'),
    ('base: &base\n  type: []\nproxies:\n  - <<: *base\n    name: Merged\n', 'proxies[0].type', 2, 'type: []'),
])
def test_shapes_report_exact_field_and_source_line(text, field, line, content):
    with pytest.raises(DialerReferenceError) as caught:
        parse_provider_proxies(text, name='Feed')
    detail = str(caught.value)
    assert f'字段：{field}' in detail
    assert f'第 {line} 行' in detail
    assert content in detail


def test_unknown_document_shape_does_not_invent_a_line_number():
    with pytest.raises(DialerReferenceError) as caught:
        parse_provider_proxies('- not-a-provider\n', name='Feed')
    assert '订阅「Feed」' in str(caught.value)
    assert '字段：文档根节点' in str(caught.value)
    assert '位置：' not in str(caught.value)


def test_source_excerpt_and_url_never_expose_node_credentials():
    text = 'proxies: [{name: Broken, type: [], password: secret-password, uuid: secret-uuid}]'
    with pytest.raises(DialerReferenceError) as caught:
        parse_provider_proxies(text, name='Feed', path='https://user:secret-login@example.test/feed?token=secret-token')
    detail = str(caught.value)
    assert 'proxies[0].type' in detail
    assert 'type: []' in detail
    for secret in ('secret-password', 'secret-uuid', 'secret-login', 'secret-token'):
        assert secret not in detail
    assert '[REDACTED]' in detail


@pytest.mark.parametrize('text', [
    'proxies:\n  - name: Broken\n    password: "hidden-first\n      hidden-second" broken\n',
    'proxies:\n  - name: Broken\n    uuid: "hidden-first\n      hidden-second" broken\n',
    'proxies: [{name: Broken, password: "hidden-first\n    hidden-second" broken}]\n',
    'proxies: [{name: Broken, password: "hidden-first\n    hidden-second\\q',
])
def test_yaml_error_on_secret_continuation_reports_location_without_value(text):
    with pytest.raises(DialerReferenceError) as caught:
        parse_provider_proxies(text, name='Sensitive')
    detail = str(caught.value)
    assert '位置：第 ' in detail
    assert '原始诊断：' in detail
    assert 'hidden-first' not in detail and 'hidden-second' not in detail
    assert '敏感内容已隐藏' in detail


@pytest.mark.parametrize('quote', ['"', "'"])
@pytest.mark.parametrize('flow', [True, False])
def test_closed_quoted_secret_does_not_hide_following_nonsecret_field(quote, flow):
    if flow:
        text = f'proxies: [{{name: Broken, password: {quote}hidden-first\n    hidden-second{quote},\n    type: []}}]\n'
        line = 3
    else:
        text = f'proxies:\n  - name: Broken\n    password: {quote}hidden-first\n      hidden-second{quote}\n    type: []\n'
        line = 5
    with pytest.raises(DialerReferenceError) as caught:
        parse_provider_proxies(text, name='Feed')
    detail = str(caught.value)
    assert f'第 {line} 行' in detail
    assert '出错内容：    type: []' in detail
    assert 'hidden-first' not in detail and 'hidden-second' not in detail


@pytest.mark.parametrize('text', ['{}', 'proxies: []', 'proxies: [{name: Good, type: http, server: example.test, port: 80}]'])
def test_valid_provider_output_and_success_path_parsing_unchanged(monkeypatch, text):
    from backend.utils.sub_store_client import parse_proxies_from_yaml
    expected = parse_proxies_from_yaml(text)
    compose = Mock(side_effect=AssertionError('No source-location parsing on success'))
    monkeypatch.setattr(yaml, 'compose', compose)
    assert parse_provider_proxies(text, name='Feed') == expected
    compose.assert_not_called()


def test_invalid_download_does_not_fall_back_to_cache(monkeypatch):
    cache = Mock(side_effect=[None, AssertionError('Content errors must not use cached nodes')])
    monkeypatch.setattr('backend.utils.subscription_cache.load_subscription_cache', cache)
    sub = {'id': 's', 'name': 'Feed', 'url': 'https://example.test/sub'}
    with pytest.raises(DialerReferenceError) as caught:
        prepare_subscription_provider(sub, {}, snapshot(),
            fetch=lambda *_: ('proxies: [{name: Bad, type: []}]', 'rendered_yaml'),
            allow_transport_fallback=True, source_path='./providers/s.yaml')
    detail = str(caught.value)
    assert detail.count('订阅「Feed」') == 1
    assert detail.count('文件：./providers/s.yaml') == 1
    assert 'proxies[0].type' in detail
    cache.assert_called_once_with('s')


def test_missing_cache_retains_transport_diagnostic_and_source(monkeypatch):
    monkeypatch.setattr('backend.utils.subscription_cache.load_subscription_cache', lambda _: None)
    sub = {'id': 's', 'name': 'Feed', 'url': 'https://example.test/sub'}
    fetch = Mock(side_effect=ConnectionError('Connection refused https://user:secret-login@example.test/sub?token=secret-token'))
    with pytest.raises(DialerReferenceError) as caught:
        prepare_subscription_provider(sub, {}, snapshot(), fetch=fetch)
    detail = str(caught.value)
    assert '订阅「Feed」' in detail
    assert '订阅没有可用缓存或远程代理' in detail
    assert 'Connection refused' in detail
    assert 'secret-login' not in detail and 'secret-token' not in detail
    assert '位置：' not in detail


def test_transport_fallback_still_returns_empty_without_false_content_error(monkeypatch):
    monkeypatch.setattr('backend.utils.subscription_cache.load_subscription_cache', lambda _: None)
    sub = {'id': 's', 'name': 'Feed', 'url': 'https://example.test/sub'}
    result = prepare_subscription_provider(sub, {}, snapshot(),
        fetch=Mock(side_effect=ConnectionError('unavailable')), allow_transport_fallback=True)
    assert result == {'content': '', 'cache_updates': []}


def test_final_bundle_error_identifies_actual_provider_file():
    providers = [
        {'name': 'First', 'local_path': './providers/first.yaml', 'content': 'proxies: []'},
        {'name': 'Second', 'local_path': './providers/second.yaml', 'content': 'proxies:\n - name: Bad\n   type: 11'},
    ]
    with pytest.raises(DialerReferenceError) as caught:
        validate_rendered_bundle(snapshot(), providers)
    detail = str(caught.value)
    assert '节点来源「Second」' in detail
    assert './providers/second.yaml' in detail
    assert '内容来源：本次准备的文件内容' in detail
    assert '第 3 行' in detail
    assert 'First' not in detail


def test_prepared_provider_failure_keeps_alias_and_real_output_path():
    main = {'proxy-providers': {
        'Provider alias': {'url': '/subscriptions/s/proxies', 'path': './providers/actual.yaml'},
        'Other alias': {'url': '/subscriptions/s/proxies', 'path': './providers/other.yaml'},
    }}
    config = {'profile_id': 'default', 'subscriptions': [{'id': 's', 'name': 'Source name'}]}
    with pytest.raises(DialerReferenceError) as caught:
        prepare_provider_bundle(config, main, render_all=True,
            prepared={'Provider alias': {'content': 'proxies:\n - name: Invalid\n   type: []', 'cache_updates': []}})
    detail = str(caught.value)
    assert 'Provider alias' in detail
    assert './providers/actual.yaml' in detail
    assert '第 3 行' in detail
    assert 'Other alias' not in detail


def test_topology_error_has_source_but_no_guessed_line():
    main = {'proxies': [{'name': 'Entry', 'type': 'http', 'dialer-proxy': 'Upstream'},
                       {'name': 'Upstream', 'type': 'http'}], 'proxy-groups': []}
    provider = {'name': 'Feed', 'local_path': './providers/feed.yaml',
                'content': 'proxies: [{name: Bad, type: http, dialer-proxy: Missing}]'}
    with pytest.raises(DialerReferenceError) as caught:
        validate_rendered_bundle(snapshot(main), [provider])
    assert '节点来源「Feed」' in str(caught.value)
    assert './providers/feed.yaml' in str(caught.value)
    assert '位置：' not in str(caught.value)


@pytest.mark.parametrize('direct', [True, False])
@pytest.mark.parametrize('consumer', ['push', 'provider'])
def test_publication_entrypoints_preserve_subscription_and_aggregation_hierarchy(tmp_path, monkeypatch, direct, consumer):
    from backend.test_qa_integrity import make_app
    from backend.test_dialer_delivery_snapshot import seed_profile, agent_manager
    from backend.utils.subscription_cache import load_subscription_cache
    app, repo = make_app(tmp_path)
    seed_profile(repo, direct=direct, chain='none')
    before = load_subscription_cache('s')
    manager = agent_manager(monkeypatch)
    text = 'proxies:\n  - name: Broken\n    type: []\n'
    target = 'subscriptions' if direct else 'aggregations'
    monkeypatch.setattr(f'backend.routes.{target}.get_subscription_proxies_yaml',
                        Mock(return_value=(text, 'rendered_yaml')))
    client = app.test_client()
    response = (client.post('/api/agents/a/push-config', json={'restart': False}) if consumer == 'push'
                else client.get('/api/subscriptions/s/proxies' if direct else '/api/aggregations/agg/provider'))
    assert response.status_code == 400
    payload = response.get_json()
    detail = str(payload)
    assert 'Feed' in detail
    assert 'proxies[0].type' in detail
    assert '第 3 行' in detail
    assert 'type: []' in detail
    if not direct:
        assert 'Agg' in detail
    manager.push_config_to_agent.assert_not_called()
    assert load_subscription_cache('s') == before
