"""Sub-Store 临时订阅必须跳过流量信息拉取。

Sub-Store 下载订阅时默认会额外 HEAD/GET 订阅 URL 获取 subscription-userinfo，
单次超时 8s；configflow 用不到流量信息，这一步会把聚合 provider 拖到 20s+，
导致 Mihomo 拉取 provider 报 context deadline exceeded。
"""
from types import SimpleNamespace

import pytest

from backend.utils import sub_store_client


YAML_TEXT = "proxies:\n  - {name: a, type: ss, server: 1.1.1.1, port: 1, cipher: aes-128-gcm, password: p}\n"


@pytest.fixture
def created_urls(monkeypatch):
    urls = []

    def fake_post(url, json=None, timeout=None):
        urls.append(json['url'])
        return SimpleNamespace(status_code=201)

    def fake_get(url, *args, **kwargs):
        return SimpleNamespace(text=YAML_TEXT, raise_for_status=lambda: None, headers={})

    monkeypatch.setattr(sub_store_client, '_get_base_url', lambda: 'http://sub-store.test')
    monkeypatch.setattr(sub_store_client, '_looks_like_sub_store_rendered_yaml_response', lambda url: (False, None))
    monkeypatch.setattr(sub_store_client, '_delete_subscription', lambda base, name: None)
    monkeypatch.setattr(sub_store_client.requests, 'post', fake_post)
    monkeypatch.setattr(sub_store_client.requests, 'get', fake_get)
    return urls


def test_subscription_temp_sub_disables_flow_info(created_urls):
    sub_store_client.get_subscription_proxies_yaml('sub_1', 'http://airport.test/c/?token=abc')

    assert created_urls == ['http://airport.test/c/?token=abc#noFlow']


def test_node_convert_temp_sub_disables_flow_info(created_urls):
    sub_store_client.convert_proxy_string('ss://YWVzLTEyOC1nY206cA@1.1.1.1:1#a')

    assert len(created_urls) == 1
    assert created_urls[0].endswith('#noFlow')


def test_url_with_existing_fragment_is_left_unchanged(created_urls):
    sub_store_client.get_subscription_proxies_yaml('sub_1', 'http://airport.test/sub#{"insecure":true}')

    assert created_urls == ['http://airport.test/sub#{"insecure":true}']
