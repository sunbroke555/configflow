from urllib.parse import parse_qs, quote, urlsplit

import pytest
import yaml

from backend.common import profile_context
from backend.converters.mihomo import generate_mihomo_config, get_mihomo_provider_downloads
from backend.converters.mosdns import get_mosdns_ruleset_downloads
from backend.converters.surge import convert_proxy_group_to_surge, generate_surge_config
from backend.utils.url_utils import safe_url_for_log
from backend.agents.go_install_script import generate_go_agent_install_script


SPECIAL_TOKEN = "token /&?"


@pytest.mark.parametrize(
    ("url", "expected_parts"),
    [
        (
            "https://alice:hunter2@example.test/path?config_token=raw-secret&api-key=key-secret&ok=visible",
            ["https://example.test/path", "config_token=%5BREDACTED%5D", "api-key=%5BREDACTED%5D", "ok=visible"],
        ),
        (
            "https%253A%252F%252Fbob%253Apass%2540example.test%252Fp%253F%252574oken%253Ddeep-secret",
            ["https://example.test/p", "token=%5BREDACTED%5D"],
        ),
    ],
)
def test_safe_url_for_log_removes_userinfo_and_redacts_sensitive_query_variants(url, expected_parts):
    redacted = safe_url_for_log(url)

    for expected in expected_parts:
        assert expected in redacted
    for secret in ["alice", "hunter2", "bob", "pass", "raw-secret", "key-secret", "deep-secret"]:
        assert secret not in redacted


def test_safe_url_for_log_fails_closed_for_invalid_url():
    invalid = "https://user:password@[broken?token=raw-secret"

    redacted = safe_url_for_log(invalid)

    assert redacted == "[INVALID URL REDACTED]"
    assert "password" not in redacted
    assert "raw-secret" not in redacted


@pytest.mark.parametrize("url", [
    "https://example.test/p?next=https%253A%252F%252Fnested.test%252Fp%253Ftoken%253Dnested-url-secret",
    "https://example.test/p?next=token%253Dnested-expression-secret",
    "https://example.test/p?next=api%252Bkey%253Dplus-layer-secret",
])
def test_safe_url_for_log_redacts_actual_review_nested_credentials(url):
    redacted = safe_url_for_log(url)
    assert "next=%5BREDACTED%5D" in redacted
    for secret in ("nested-url-secret", "nested-expression-secret", "plus-layer-secret"):
        assert secret not in redacted


@pytest.mark.parametrize("encoded_control", ["%250d%250a", "%2500", "%251f", "%257f"])
def test_safe_url_for_log_fails_closed_when_decoding_reveals_c0(encoded_control):
    assert safe_url_for_log(f"https://example.test/p?next=ok{encoded_control}token=secret") == "[INVALID URL REDACTED]"


@pytest.mark.parametrize("url", [
    "https://example.test/path#token=plain-fragment-secret",
    "https://example.test/path#token=encoded%2Dfragment%2Dsecret",
    "https://example.test/path#outer=https%253A%252F%252Fexample.test%252F%253Ftoken%253Dnested-secret",
    "https://example.test/path#safe=ok&api_key=mixed-fragment-secret",
])
def test_safe_url_for_log_never_preserves_fragment_credentials(url):
    redacted = safe_url_for_log(url)
    assert "#" not in redacted
    for secret in ("plain-fragment-secret", "fragment-secret", "nested-secret"):
        assert secret not in redacted


def test_generated_go_installer_does_not_print_binary_download_url():
    secret_url = "https://download.test/bin?token=installer-secret#key=fragment-secret"
    script = generate_go_agent_install_script(
        server_url="https://server.test/?token=server-secret",
        agent_name="probe", service_type="mihomo", binary_download_url=secret_url,
    )
    assert "URL: $BINARY_URL" not in script
    assert 'wget -O /tmp/configflow-agent "$BINARY_URL"' in script
    assert 'curl -L -o /tmp/configflow-agent "$BINARY_URL"' in script


def _provider_config(profile_id=None):
    config = {
        "system_config": {
            "server_domain": "https://config.test",
            "config_token": SPECIAL_TOKEN,
            "rule_proxy_token": "internal /&?",
        },
        "subscriptions": [{"id": "sub-1", "name": "Primary", "enabled": True}],
        "subscription_aggregations": [{
            "id": "agg-1",
            "name": "Combined",
            "enabled": True,
            "subscriptions": [],
        }],
        "proxy_groups": [{
            "id": "group-1",
            "name": "Proxy",
            "type": "select",
            "enabled": True,
            "subscriptions": ["sub-1"],
            "aggregations": ["agg-1"],
        }],
        "nodes": [],
        "rule_configs": [],
    }
    if profile_id:
        config["profile_id"] = profile_id
    return config


def _assert_query(generated_url, **expected):
    assert parse_qs(urlsplit(generated_url).query, keep_blank_values=True) == {
        key: [value] for key, value in expected.items()
    }


def test_append_url_query_encodes_values_and_preserves_existing_query():
    url = profile_context.append_url_query(
        "https://config.test/path?existing=first%20value",
        {"token": SPECIAL_TOKEN, "format": "surge"},
    )

    _assert_query(url, existing="first value", token=SPECIAL_TOKEN, format="surge")


@pytest.mark.parametrize("profile_id", [None, "alpha"], ids=["legacy", "profile"])
def test_mihomo_provider_and_aggregation_urls_round_trip_special_token(profile_id):
    config = _provider_config(profile_id)

    generated = yaml.safe_load(generate_mihomo_config(config))
    generated_urls = [provider["url"] for provider in generated["proxy-providers"].values()]
    download_urls = [item["url"] for item in get_mihomo_provider_downloads(config)]

    assert len(generated_urls) == len(download_urls) == 2
    for url in generated_urls + download_urls:
        _assert_query(url, token=SPECIAL_TOKEN)


@pytest.mark.parametrize(
    "token",
    ["raw-secret-token", "encoded secret/token"],
    ids=["raw", "encoded"],
)
def test_mihomo_provider_log_redacts_config_token(caplog, token):
    config = _provider_config()
    config["system_config"]["config_token"] = token

    generate_mihomo_config(config)

    logs = caplog.text
    assert token not in logs
    assert quote(token, safe="") not in logs
    assert "token=%5BREDACTED%5D" in logs or "token=[REDACTED]" in logs


@pytest.mark.parametrize("profile_id", [None, "alpha"], ids=["legacy", "profile"])
def test_surge_managed_subscription_and_aggregation_urls_round_trip_special_token(profile_id):
    config = _provider_config(profile_id)
    group = config["proxy_groups"][0]

    managed_url = generate_surge_config(config).splitlines()[0].split()[1]
    group_line = convert_proxy_group_to_surge(group, config)
    policy_urls = [
        part.split(",", 1)[0]
        for part in group_line.split("policy-path = ")[1:]
    ]

    _assert_query(managed_url, token=SPECIAL_TOKEN)
    assert len(policy_urls) == 2
    for url in policy_urls:
        _assert_query(url, token=SPECIAL_TOKEN, format="surge")


@pytest.mark.parametrize("profile_id", [None, "alpha"], ids=["legacy", "profile"])
def test_mosdns_rule_proxy_url_round_trips_remote_url_and_special_token(profile_id):
    config = _provider_config(profile_id)
    remote_url = "https://rules.test/list?a=1&b=2"
    config["mosdns"] = {"direct_rulesets": ["rules-1"], "proxy_rulesets": []}
    config["rule_configs"] = [{
        "id": "rules-1",
        "name": "Rules",
        "itemType": "ruleset",
        "url": remote_url,
    }]

    generated_url = get_mosdns_ruleset_downloads(config)[0]["url"]

    _assert_query(generated_url, url=remote_url, token="internal /&?")
