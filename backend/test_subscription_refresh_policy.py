"""Provider polls must respect upstream refresh policy, including after restart."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from backend.common import config as config_module
from backend.common.config_repository import ProfileRepository
from backend.routes import aggregations, subscriptions
from backend.test_qa_integrity import make_app
from backend.utils import scheduled_refresh, sub_store_client, task_status
from backend.utils.provider_delivery import prepare_subscription_provider
from backend.utils.subscription_cache import load_subscription_cache, save_subscription_nodes
from backend.utils.subscription_refresh import fetch_subscription_source

URL = 'https://airport.test/sub?token=private'
PROXIES = [{'name': 'Remote', 'type': 'ss', 'server': 'example.test', 'port': 443,
            'cipher': 'aes-128-gcm', 'password': 'p'}]
CONTENT = yaml.safe_dump({'proxies': PROXIES})


@pytest.fixture
def setup(tmp_path, monkeypatch):
    app, repo = make_app(tmp_path)
    sub = {'id': 's', 'name': 'Feed', 'url': URL, 'enabled': True, 'interval': 0}
    repo.save_shared({'subscriptions': [sub], 'nodes': [],
                     'subscription_aggregations': [{'id': 'a', 'name': 'All', 'subscriptions': ['s'], 'nodes': []}],
                     'rule_library': []})
    repo.save_profile('default', {'proxy_groups': [{'id': 'g', 'name': 'Proxy', 'type': 'select',
        'manual_nodes': ['DIRECT'], 'subscriptions': ['s'], 'aggregations': ['a']}], 'rule_configs': []})
    monkeypatch.setattr(sub_store_client, '_fetch_results', {})
    monkeypatch.setattr(sub_store_client, '_fetch_failures', {})
    monkeypatch.setattr(sub_store_client.requests, 'get', lambda *a, **k: pytest.fail('unexpected upstream request'))
    return app, repo, sub


def cache_source(repo, *, age=0, url=URL):
    value = save_subscription_nodes('s', sub_store_client.proxies_to_nodes(PROXIES), {'url': url})
    value['updated_at'] = (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()
    repo.write_shared_json('subscribes/s.json', value)
    return value


@pytest.mark.parametrize('path', ['/api/subscriptions/s/proxies', '/api/subscriptions/proxies',
                                  '/api/aggregations/a/provider'])
@pytest.mark.parametrize('interval', [0, 259200])
def test_auto_endpoints_read_cache_without_upstream_or_extending_age(setup, path, interval):
    app, repo, sub = setup
    repo.update_shared_transaction(lambda shared: shared['subscriptions'][0].update(interval=interval))
    before = cache_source(repo, age=120)
    # Loading a new repository proves this is durable cache, not the 5 minute
    # transport debounce (which is empty in this test).
    config_module.set_repository(ProfileRepository(repo.data_dir))
    for _ in range(2):
        response = app.test_client().get(path)
        assert response.status_code == 200, response.get_data(as_text=True)
        assert [p['name'] for p in yaml.safe_load(response.text)['proxies']] == ['Remote'] * (1 if path != '/unused' else 0)
    assert load_subscription_cache('s') == before


@pytest.mark.parametrize('path', ['/api/subscriptions/s/proxies', '/api/subscriptions/proxies',
                                  '/api/aggregations/a/provider'])
def test_manual_only_without_cache_explains_action_without_fetching(setup, path):
    app, _, _ = setup
    response = app.test_client().get(path)
    assert response.status_code == 400
    assert '手动更新' in response.json['message']


def test_manual_refresh_is_allowed_and_next_poll_uses_new_cache(setup, monkeypatch):
    app, _, _ = setup
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(text=CONTENT, headers={}, raise_for_status=lambda: None)
    monkeypatch.setattr(sub_store_client.requests, 'get', get)
    response = app.test_client().post('/api/subscriptions/s/fetch', json={'preview': True})
    assert response.status_code == 200, response.text
    assert response.json['count'] == 1
    assert app.test_client().get('/api/subscriptions/s/proxies').status_code == 200
    assert app.test_client().get('/api/aggregations/a/provider').status_code == 200
    assert calls == [URL]


def test_expired_cache_is_served_and_the_scheduled_task_refreshes_it(setup, monkeypatch):
    app, repo, _ = setup
    repo.update_shared_transaction(lambda shared: shared['subscriptions'][0].update(interval=259200))
    before = cache_source(repo, age=259201)
    fetch = Mock(side_effect=AssertionError('devices read the stored nodes'))
    monkeypatch.setattr(subscriptions, 'get_subscription_proxies_yaml', fetch)
    monkeypatch.setattr(aggregations, 'get_subscription_proxies_yaml', fetch)
    for path in ['/api/subscriptions/proxies', '/api/subscriptions/s/proxies', '/api/aggregations/a/provider']:
        response = app.test_client().get(path)
        assert response.status_code == 200, response.text
    fetch.assert_not_called()
    assert load_subscription_cache('s') == before

    download = Mock(return_value=(CONTENT, 'direct_yaml'))
    monkeypatch.setattr(sub_store_client, 'get_subscription_proxies_yaml', download)
    assert scheduled_refresh.run_round() == 1
    download.assert_called_once_with('s', URL, refresh=True)
    assert scheduled_refresh.run_round() == 0  # due again only after the interval
    entry = task_status.get('subscription:s')
    assert (entry['last_status'], entry['run_count'], entry['last_trigger']) == ('success', 1, 'schedule')
    assert load_subscription_cache('s')['updated_at'] > before['updated_at']


def test_failed_scheduled_refresh_keeps_nodes_and_retries_sooner(setup, monkeypatch):
    _, repo, _ = setup
    repo.update_shared_transaction(lambda shared: shared['subscriptions'][0].update(interval=86400))
    before = cache_source(repo, age=86401)
    monkeypatch.setattr(sub_store_client, 'get_subscription_proxies_yaml',
                        Mock(return_value=('proxies: []', 'direct_yaml')))
    assert scheduled_refresh.run_round() == 1
    assert load_subscription_cache('s') == before
    entry = task_status.get('subscription:s')
    assert entry['last_status'] == 'failed' and entry['cache_retained'] is True
    assert entry['last_error'] == '订阅没有返回任何节点，已保留原有缓存'
    due = scheduled_refresh.next_run_at(entry, 86400)
    assert due - task_status.parse_time(entry['last_run_at']) == timedelta(seconds=scheduled_refresh.RETRY_SECONDS)


def test_publish_preparation_uses_cache_and_stages_no_fake_refresh(setup):
    _, repo, sub = setup
    before = cache_source(repo, age=999999)
    fetch = Mock(side_effect=AssertionError('publish must not bypass manual mode'))
    snapshot = SimpleNamespace(validate=lambda proxies, provider_name=None: None, has_chains=False)
    result = prepare_subscription_provider(sub, {}, snapshot, fetch=fetch)
    assert yaml.safe_load(result['content'])['proxies'] == PROXIES
    assert result['cache_updates'] == []
    fetch.assert_not_called()
    assert load_subscription_cache('s') == before


def test_dry_previews_share_download_across_restart_without_changing_validated_nodes(setup):
    _, repo, sub = setup
    sub['interval'] = 259200
    before = None  # stored nodes would be served as they are
    fetch = Mock(return_value=(CONTENT, 'direct_yaml'))
    snapshot = SimpleNamespace(validate=lambda proxies, provider_name=None: None, has_chains=False)
    first = prepare_subscription_provider(sub, {}, snapshot, fetch=fetch)
    config_module.set_repository(ProfileRepository(repo.data_dir))
    second = prepare_subscription_provider(sub, {}, snapshot, fetch=fetch)
    assert first['content'] == second['content']
    assert first['cache_updates'][0][2]['fetched_at'] == second['cache_updates'][0][2]['fetched_at']
    fetch.assert_called_once()
    assert load_subscription_cache('s') == before


def test_expired_download_is_refetched_and_manual_mode_never_uses_it_without_nodes(setup):
    _, repo, sub = setup
    from backend.utils.subscription_refresh import _download_cache_path
    sub['interval'] = 60
    fetch = Mock(return_value=(CONTENT, 'direct_yaml'))
    fetch_subscription_source(sub, fetch=fetch)
    entry = repo.read_shared_json(_download_cache_path(URL))
    entry['fetched_at'] = (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat()
    repo.write_shared_json(_download_cache_path(URL), entry)
    fetch_subscription_source(sub, fetch=fetch)
    assert fetch.call_count == 2
    sub['interval'] = 0
    with pytest.raises(ValueError, match='手动更新'):
        fetch_subscription_source(sub, fetch=fetch)
    assert fetch.call_count == 2


def test_changed_url_in_manual_mode_needs_manual_refresh(setup):
    _, repo, sub = setup
    cache_source(repo)
    sub['url'] += '&new=1'
    with pytest.raises(ValueError, match='当前订阅地址'):
        fetch_subscription_source(sub, fetch=Mock(side_effect=AssertionError('no request')))


@pytest.mark.parametrize('interval', [None, '', '0', True, -1, 1, 59, 60.5, 604801])
def test_invalid_source_intervals_are_rejected_without_saving(setup, interval):
    app, repo, sub = setup
    before = repo.get_shared()
    response = app.test_client().put('/api/subscriptions/s', json={**sub, 'interval': interval})
    assert response.status_code == 400
    assert repo.get_shared() == before


@pytest.mark.parametrize('interval', [0, 60, 3600, 604800])
def test_mihomo_sync_setting_is_independent_and_applies_to_aggregations(setup, interval):
    app, repo, sub = setup
    response = app.test_client().post('/api/custom-config/mihomo', json={
        'config': 'mixed-port: 7890\n', 'provider_update_interval': interval})
    assert response.status_code == 200, response.text
    assert app.test_client().get('/api/custom-config/mihomo').json['provider_update_interval'] == interval
    from backend.converters.mihomo import generate_mihomo_config
    content = yaml.safe_load(generate_mihomo_config(repo.get_compat_config('default'), preflight_providers=False))
    assert {p['interval'] for p in content['proxy-providers'].values()} == {interval}
    assert repo.get_shared()['subscriptions'][0]['interval'] == 0


@pytest.mark.parametrize('interval', ['', None, -1, 1, 0.5, True])
def test_invalid_mihomo_sync_setting_does_not_save_yaml(setup, interval):
    app, repo, _ = setup
    before = repo.get_profile('default')
    response = app.test_client().post('/api/custom-config/mihomo', json={
        'config': 'mixed-port: 9999', 'provider_update_interval': interval})
    assert response.status_code == 400
    assert repo.get_profile('default') == before
