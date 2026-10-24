"""Count upstream calls without contacting an airport or a running Sub-Store."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest
import requests
from flask import Flask

from backend.routes import subscriptions
from backend.utils import sub_store_client as client


URL = 'https://airport.test/sub?token=private'
YAML = 'proxies: [{name: A, type: ss, server: example.test, port: 443, cipher: aes-128-gcm, password: p}]'
FLOW = 'upload=1; download=2; total=100'


def response(text=YAML, headers=None):
    return SimpleNamespace(text=text, headers=headers or {}, status_code=200,
                           raise_for_status=lambda: None)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(client, '_fetch_results', {})
    monkeypatch.setattr(client, '_fetch_failures', {})
    monkeypatch.setattr(client, '_get_base_url', lambda: 'http://sub-store.test')
    monkeypatch.setattr(client, '_delete_subscription', lambda *args: None)


def test_yaml_and_traffic_use_one_get_and_no_sub_store(monkeypatch):
    seen = []
    def get(url, **kwargs):
        seen.append(url)
        return response(headers={'subscription-userinfo': FLOW})
    monkeypatch.setattr(client.requests, 'get', get)
    monkeypatch.setattr(client.requests, 'post', lambda *a, **k: pytest.fail('unnecessary conversion'))
    result = client.get_subscription_proxies_yaml('s', URL)
    assert tuple(result) == (YAML, 'direct_yaml')
    assert result.traffic == {'upload': 1, 'download': 2, 'total': 100}
    assert seen == [URL]


def test_raw_body_converted_locally_without_second_airport_get(monkeypatch):
    seen, payloads = [], []
    def get(url, **kwargs):
        seen.append(url)
        if url == URL:
            return response('ss://encoded-node', {'subscription-userinfo': FLOW})
        assert kwargs['params'] == {'noFlow': 'true'}
        return response()
    def post(url, json, **kwargs):
        payloads.append(json)
        return response()
    monkeypatch.setattr(client.requests, 'get', get)
    monkeypatch.setattr(client.requests, 'post', post)
    result = client.get_subscription_proxies_yaml('s', URL)
    assert result[1] == 'sub_store'
    assert result.traffic['total'] == 100
    assert seen.count(URL) == 1
    assert payloads[0]['source'] == 'local'
    assert payloads[0]['content'] == 'ss://encoded-node'
    assert payloads[0]['noFlow'] is True


def test_rendered_yaml_keeps_source_and_no_second_request(monkeypatch):
    seen = []
    def get(url, **kwargs):
        seen.append(url)
        return response(headers={'x-powered-by': 'Sub-Store'})
    monkeypatch.setattr(client.requests, 'get', get)
    assert client.get_subscription_proxies_yaml('s', URL)[1] == 'rendered_yaml'
    assert seen == [URL]


def test_direct_transport_failure_can_still_use_sub_store(monkeypatch):
    payloads = []
    def get(url, **kwargs):
        if url == URL:
            raise requests.Timeout('unavailable directly')
        return response()
    def post(url, json, **kwargs):
        payloads.append(json)
        return response()
    monkeypatch.setattr(client.requests, 'get', get)
    monkeypatch.setattr(client.requests, 'post', post)
    assert client.get_subscription_proxies_yaml('s', URL)[1] == 'sub_store'
    assert 'content' not in payloads[0]


def test_fragment_options_go_to_sub_store_without_probe(monkeypatch):
    seen, payloads = [], []
    url = URL + '#{"insecure":true,"userAgent":"custom"}'
    def get(dest, **kwargs):
        seen.append(dest)
        assert dest.startswith('http://sub-store.test/download/')
        assert kwargs['params']['noFlow'] == 'true'
        return response()
    def post(dest, json, **kwargs):
        payloads.append(json)
        return response()
    monkeypatch.setattr(client.requests, 'get', get)
    monkeypatch.setattr(client.requests, 'post', post)
    assert client.get_subscription_proxies_yaml('s', url)[1] == 'sub_store'
    assert len(seen) == 1
    assert payloads[0]['url'] == url
    assert 'content' not in payloads[0]


def test_concurrent_and_recent_refreshes_across_ids_share_one_get(monkeypatch):
    entered, release = Event(), Event()
    seen = []
    def get(url, **kwargs):
        seen.append(url)
        entered.set()
        assert release.wait(5)
        return response()
    monkeypatch.setattr(client.requests, 'get', get)
    with ThreadPoolExecutor(max_workers=5) as pool:
        first = pool.submit(client.get_subscription_proxies_yaml, 'manual', URL)
        assert entered.wait(5)
        others = [pool.submit(client.get_subscription_proxies_yaml, f'agg-{i}', URL, refresh=True) for i in range(4)]
        release.set()
        results = [first.result()] + [f.result() for f in others]
    assert all(r == results[0] for r in results)
    assert client.get_subscription_proxies_yaml('another-profile', URL) == results[0]
    assert seen == [URL]


def test_reuse_expires_and_changed_url_fetches_again(monkeypatch):
    clock, seen = [0], []
    monkeypatch.setattr(client, 'monotonic', lambda: clock[0])
    def get(url, **kwargs):
        seen.append(url)
        return response()
    monkeypatch.setattr(client.requests, 'get', get)
    client.get_subscription_proxies_yaml('s', URL)
    clock[0] = 299
    client.get_subscription_proxies_yaml('s', URL)
    client.get_subscription_proxies_yaml('s', URL + '&changed=1')
    clock[0] = 300
    client.get_subscription_proxies_yaml('s', URL)
    assert seen == [URL, URL + '&changed=1', URL]


def test_manual_refresh_debounces_then_renews_shared_automatic_cache(monkeypatch):
    clock, seen = [0], []
    monkeypatch.setattr(client, 'monotonic', lambda: clock[0])
    def get(url, **kwargs):
        seen.append(url)
        return response()
    monkeypatch.setattr(client.requests, 'get', get)
    client.get_subscription_proxies_yaml('auto', URL)
    clock[0] = 29
    client.get_subscription_proxies_yaml('manual', URL, refresh=True)
    assert seen == [URL]
    clock[0] = 30
    client.get_subscription_proxies_yaml('manual', URL, refresh=True)
    assert seen == [URL, URL]
    clock[0] = 329
    client.get_subscription_proxies_yaml('auto', URL)
    assert seen == [URL, URL]
    clock[0] = 330
    client.get_subscription_proxies_yaml('auto', URL)
    assert seen == [URL, URL, URL]


def test_conversion_failure_does_not_redownload_body(monkeypatch):
    seen = []
    def get(url, **kwargs):
        seen.append(url)
        return response('ss://node' if url == URL else '<html>failure</html>')
    monkeypatch.setattr(client.requests, 'get', get)
    monkeypatch.setattr(client.requests, 'post', lambda *a, **k: response())
    with pytest.raises(client.SubscriptionFetchError):
        client.get_subscription_proxies_yaml('s', URL)
    assert seen.count(URL) == 1


def test_completed_cache_entries_are_bounded(monkeypatch):
    monkeypatch.setattr(client, 'FETCH_CACHE_LIMIT', 2)
    monkeypatch.setattr(client.requests, 'get', lambda *a, **k: response())
    for i in range(10):
        client.get_subscription_proxies_yaml('s', URL + f'&i={i}')
    assert len(client._fetch_results) == 2


def test_background_reuses_recent_body_but_expires_at_fifteen_minutes(monkeypatch):
    clock, seen = [0], []
    monkeypatch.setattr(client, 'monotonic', lambda: clock[0])
    def get(url, **kwargs):
        seen.append(url)
        return response()
    monkeypatch.setattr(client.requests, 'get', get)
    client.get_subscription_proxies_yaml('auto', URL)
    for moment in (300, 899):
        clock[0] = moment
        client.get_subscription_proxies_yaml('background', URL, prefer_cached=True)
    assert seen == [URL]
    clock[0] = 900
    client.get_subscription_proxies_yaml('background', URL, prefer_cached=True)
    assert seen == [URL, URL]


def test_failure_backoff_caps_and_success_resets_it(monkeypatch):
    clock, seen, failing = [0], [], [True]
    monkeypatch.setattr(client, 'monotonic', lambda: clock[0])
    def fetch(*args):
        seen.append(clock[0])
        if failing[0]:
            raise client.SubscriptionFetchError(None, requests.Timeout())
        return client.SubscriptionResult(YAML, 'direct_yaml')
    monkeypatch.setattr(client, '_get_subscription_proxies_yaml', fetch)
    for cooldown in (3, 6, 12, 24, 48, 96, 192, 300, 300):
        with pytest.raises(client.SubscriptionFetchError):
            client.get_subscription_proxies_yaml('s', URL)
        count = len(seen)
        clock[0] += cooldown - 0.1
        with pytest.raises(client.SubscriptionFetchError):
            client.get_subscription_proxies_yaml('s', URL, refresh=True)
        assert len(seen) == count
        clock[0] += 0.1
    failing[0] = False
    client.get_subscription_proxies_yaml('s', URL)
    assert not client._fetch_failures
    failing[0] = True
    clock[0] += 300
    with pytest.raises(client.SubscriptionFetchError):
        client.get_subscription_proxies_yaml('s', URL)
    assert next(iter(client._fetch_results.values()))[1] == clock[0] + 3


def test_failed_bursts_share_error_but_retry_after_three_seconds(monkeypatch):
    clock, seen = [0], []
    monkeypatch.setattr(client, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(client.requests, 'post', lambda *a, **k: response())
    def get(url, **kwargs):
        seen.append(url)
        raise requests.Timeout('unavailable')
    monkeypatch.setattr(client.requests, 'get', get)
    for _ in range(5):
        with pytest.raises(client.SubscriptionFetchError):
            client.get_subscription_proxies_yaml('s', URL)
    assert len(seen) == 2  # direct GET + Sub-Store attempt, not five repetitions
    clock[0] = 4
    with pytest.raises(client.SubscriptionFetchError):
        client.get_subscription_proxies_yaml('s', URL)
    assert len(seen) == 4


@pytest.mark.parametrize('status', [403, 429])
def test_blocked_upstream_is_not_retried_via_sub_store(monkeypatch, status):
    clock, seen = [0], []
    monkeypatch.setattr(client, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(client.requests, 'post', lambda *a, **k: pytest.fail('retry while blocked'))
    def get(url, **kwargs):
        seen.append(url)
        resp = requests.Response()
        resp.status_code = status
        resp.raise_for_status()
    monkeypatch.setattr(client.requests, 'get', get)
    for moment in (0, 5, 30, 59):
        clock[0] = moment
        with pytest.raises(client.SubscriptionFetchError):
            client.get_subscription_proxies_yaml('s', URL, refresh=True)
    assert seen == [URL]
    clock[0] = 61
    with pytest.raises(client.SubscriptionFetchError):
        client.get_subscription_proxies_yaml('s', URL)
    assert seen == [URL, URL]


def test_manual_route_reuses_traffic_without_userinfo_get(monkeypatch):
    result = client.SubscriptionResult(YAML, 'direct_yaml', {'total': 100})
    recorded = []
    monkeypatch.setattr(subscriptions, 'get_shared_config', lambda: {
        'subscriptions': [{'id': 's', 'name': 'S', 'url': URL}], 'nodes': []})
    def fetch(*args, **kwargs):
        assert kwargs == {'refresh': True}
        return result
    # The manual route shares the scheduled task's download (系统设置 → 定时任务).
    monkeypatch.setattr(client, 'get_subscription_proxies_yaml', fetch)
    monkeypatch.setattr('backend.utils.subscription_cache.save_subscription_nodes', lambda *a: {'count': 1})
    monkeypatch.setattr(subscriptions.subscription_health, 'record_fetch', lambda *a, **k: recorded.append(k))
    monkeypatch.setattr(subscriptions.subscription_health, 'fetch_userinfo', lambda *a: pytest.fail('extra airport GET'))
    app = Flask(__name__)
    with app.test_request_context('/s/fetch', method='POST', json={'preview': True}):
        body = subscriptions.fetch_subscription.__wrapped__('s').get_json()
    assert body['success']
    assert recorded[0]['traffic'] == {'total': 100}
