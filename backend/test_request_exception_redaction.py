import logging

import pytest
import requests
from flask import Flask

from backend.routes import aggregations, mosdns, rules, subscriptions
from backend.utils import sub_store_client
from backend.utils.url_utils import safe_exception_details


SECRET_URL = "https://upstream.test/sub?token=dynamic-request-secret#secret-fragment"


class _Response:
    status_code = 200
    headers = {"content-type": "text/yaml"}
    text = "proxies:\n  - {name: safe-name, type: vless, server: safe.test, port: 443}\n"

    def raise_for_status(self):
        return None


def test_safe_request_error_details_include_only_type_and_status():
    response = requests.Response()
    response.status_code = 502
    error = requests.HTTPError(f"failed for {SECRET_URL}", response=response)

    assert safe_exception_details(error) == "exception_type=HTTPError status=502"


def test_direct_subscription_fetch_log_does_not_include_subscription_url(monkeypatch, caplog):
    monkeypatch.setattr(sub_store_client.requests, "get", lambda *args, **kwargs: _Response())

    with caplog.at_level(logging.INFO):
        result = sub_store_client._fetch_direct_subscription_yaml(SECRET_URL)

    assert "proxies:" in result
    for forbidden in ("upstream.test", "dynamic-request-secret", "secret-fragment"):
        assert forbidden not in caplog.text
    assert "Direct subscription fetch started" in caplog.text


def test_convert_proxy_string_logs_only_event_type_and_length(monkeypatch, caplog):
    proxy_uri = "vless://user-secret@example.test:443?token=converter-secret#fragment-secret"
    monkeypatch.setattr(sub_store_client, "_get_base_url", lambda: "http://sub-store.test")
    monkeypatch.setattr(sub_store_client, "_create_subscription", lambda *args: True)
    monkeypatch.setattr(sub_store_client, "_delete_subscription", lambda *args: None)
    monkeypatch.setattr(sub_store_client.requests, "get", lambda *args, **kwargs: _Response())

    with caplog.at_level(logging.INFO):
        result = sub_store_client.convert_proxy_string(proxy_uri)

    assert result["type"] == "vless"
    assert "vless://" not in caplog.text
    for forbidden in ("user-secret", "example.test", "converter-secret", "fragment-secret"):
        assert forbidden not in caplog.text
    assert "proxy_type=vless" in caplog.text
    assert f"input_length={len(proxy_uri)}" in caplog.text


def test_sub_store_combined_requests_errors_keep_causes_without_leaking(monkeypatch, caplog):
    first = requests.ConnectionError(f"sub-store request failed for {SECRET_URL}")
    second = requests.Timeout(f"direct request failed for {SECRET_URL}")
    monkeypatch.setattr(sub_store_client, "_looks_like_sub_store_rendered_yaml_response", lambda url: (False, None))
    monkeypatch.setattr(sub_store_client, "_create_subscription", lambda *args: True)
    monkeypatch.setattr(sub_store_client, "_delete_subscription", lambda *args: None)

    errors = iter((first, second))
    def fail_get(*args, **kwargs):
        raise next(errors)
    monkeypatch.setattr(sub_store_client.requests, "get", fail_get)

    with caplog.at_level(logging.WARNING), pytest.raises(sub_store_client.SubscriptionFetchError) as caught:
        sub_store_client.get_subscription_proxies_yaml("sub-1", SECRET_URL)

    error = caught.value
    assert error.sub_store_error is first
    assert error.direct_error is second
    assert error.__cause__ is second
    assert "dynamic-request-secret" not in str(error)
    assert "dynamic-request-secret" not in caplog.text
    assert "upstream.test" not in caplog.text
    assert "ConnectionError" in caplog.text
    assert "Timeout" in caplog.text


def test_rules_request_exception_log_does_not_leak_url(monkeypatch, caplog):
    exc = requests.ConnectionError(f"GET failed for {SECRET_URL}")
    monkeypatch.setattr(rules.requests, "get", lambda *args, **kwargs: (_ for _ in ()).throw(exc))

    with caplog.at_level(logging.WARNING):
        result = rules.get_ruleset_content({"name": "probe", "url": SECRET_URL})

    assert result == ""
    assert "dynamic-request-secret" not in caplog.text
    assert "ConnectionError" in caplog.text


def test_subscription_request_exception_response_and_log_do_not_leak(monkeypatch, caplog):
    exc = requests.ConnectionError(f"GET failed for {SECRET_URL}")
    monkeypatch.setattr(subscriptions, "get_config", lambda: {
        "subscriptions": [{"id": "sub-1", "name": "Probe", "url": SECRET_URL}],
        "nodes": [],
    })
    monkeypatch.setattr(subscriptions, "get_subscription_proxies_yaml", lambda *args: (_ for _ in ()).throw(exc))
    monkeypatch.setattr(subscriptions, "load_subscription_cache", lambda *args, **kwargs: None)
    app = Flask(__name__)

    with app.test_request_context("/sub-1/fetch", method="POST", json={}), caplog.at_level(logging.WARNING):
        response, status = subscriptions.fetch_subscription.__wrapped__("sub-1")

    body = response.get_data(as_text=True)
    assert status == 500
    assert "dynamic-request-secret" not in body
    assert "dynamic-request-secret" not in caplog.text
    assert "ConnectionError" in caplog.text


def test_aggregation_request_exception_log_does_not_leak(monkeypatch, tmp_path, caplog):
    exc = requests.ConnectionError(f"GET failed for {SECRET_URL}")
    monkeypatch.setattr(aggregations, "get_config", lambda profile_id=None: {
        "subscriptions": [{"id": "sub-1", "name": "Probe", "url": SECRET_URL}],
        "nodes": [],
    })
    monkeypatch.setattr(aggregations, "get_subscription_proxies_yaml", lambda *args: (_ for _ in ()).throw(exc))
    monkeypatch.setattr(aggregations, "load_subscription_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(aggregations, "AGGREGATION_PROVIDERS_DIR", str(tmp_path))
    app = Flask(__name__)

    with app.test_request_context("/"), caplog.at_level(logging.WARNING):
        aggregations.generate_aggregation_provider({
            "id": "agg-1", "name": "Probe", "subscriptions": ["sub-1"], "nodes": []
        })

    assert "dynamic-request-secret" not in caplog.text
    assert "ConnectionError" in caplog.text


def test_mosdns_rule_proxy_request_exception_response_does_not_leak(monkeypatch, caplog):
    exc = requests.ConnectionError(f"GET failed for {SECRET_URL}")
    monkeypatch.setattr(mosdns, "_require_rule_proxy_auth", lambda: True)
    monkeypatch.setattr(mosdns, "apply_github_proxy_domain", lambda url, config: url)
    monkeypatch.setattr(mosdns, "_validate_remote_url", lambda url: None)
    monkeypatch.setattr(mosdns, "_fetch_remote_content", lambda url: (_ for _ in ()).throw(exc))
    monkeypatch.setattr(mosdns, "_load_cached_rule_content_for_url", lambda url: "")
    app = Flask(__name__)

    with app.test_request_context(
        "/rule-proxy", query_string={"url": SECRET_URL}
    ), caplog.at_level(logging.WARNING):
        response, status = mosdns.mosdns_rule_proxy()

    body = response.get_data(as_text=True)
    assert status == 500
    assert "Failed to fetch original URL" in body
    for forbidden in ("dynamic-request-secret", "secret-fragment", "upstream.test"):
        assert forbidden not in body
    for forbidden in ("dynamic-request-secret", "secret-fragment"):
        assert forbidden not in caplog.text


@pytest.mark.parametrize(
    ("exception", "expected_status", "expected_message"),
    [
        (ValueError(f"invalid URL {SECRET_URL}"), 400, "Invalid remote URL"),
        (RuntimeError(f"internal failure {SECRET_URL}"), 500, "Failed to process rule proxy request"),
    ],
)
def test_mosdns_rule_proxy_outer_exception_response_does_not_leak(
    monkeypatch, caplog, exception, expected_status, expected_message
):
    monkeypatch.setattr(mosdns, "_require_rule_proxy_auth", lambda: True)
    monkeypatch.setattr(mosdns, "apply_github_proxy_domain", lambda url, config: url)
    monkeypatch.setattr(mosdns, "_validate_remote_url", lambda url: (_ for _ in ()).throw(exception))
    app = Flask(__name__)

    with app.test_request_context(
        "/rule-proxy", query_string={"url": SECRET_URL}
    ), caplog.at_level(logging.WARNING):
        response, status = mosdns.mosdns_rule_proxy()

    body = response.get_data(as_text=True)
    assert status == expected_status
    assert expected_message in body
    for forbidden in ("dynamic-request-secret", "secret-fragment", "upstream.test", "internal failure"):
        assert forbidden not in body
        assert forbidden not in caplog.text
