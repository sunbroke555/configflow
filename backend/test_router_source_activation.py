"""Disabled shared sources never remain active in manual RouterOS previews."""
from copy import deepcopy

import pytest

from backend.routers.base import RouterError
from backend.routers.desired import compute_desired
from backend.routers.ros_policy import inspect_desired
from backend.routers.rule_sync_rules import selected_sources


def source_config():
    return {
        'mihomo': {'custom_config': 'dns: {fake-ip-range: 198.18.0.1/16}'},
        'rule_configs': [{'id': 'r', 'itemType': 'ruleset', 'name': 'Telegram', 'policy': 'PROXY',
                          'enabled': True, 'library_enabled': True, 'library_rule_id': 'lib'}],
        'rule_library': [{'id': 'lib', 'name': 'Telegram', 'enabled': True,
                          'content': '91.108.4.0/22', 'source_type': 'content', 'behavior': 'ipcidr'}],
    }


@pytest.mark.parametrize('disabled_field', ['enabled', 'library_enabled', 'source_enabled', 'missing_source'])
def test_manual_and_auto_share_source_activation_rules(disabled_field):
    config = source_config()
    if disabled_field == 'source_enabled':
        config['rule_library'][0]['enabled'] = False
    elif disabled_field == 'missing_source':
        config['rule_library'] = []
    else:
        config['rule_configs'][0][disabled_field] = False
    original = deepcopy(config)
    loader = lambda *_: pytest.fail('An inactive source must not read cache or fetch content')
    desired = compute_desired(config, '192.168.0.3', ['r'], content_loader=loader)
    assert desired['proxy_cidrs'] == [{'cidr': '198.18.0.0/16', 'sources': ['fake-ip']}]
    assert any('已停用' in message or '已不存在' in message for message in desired['warnings'])
    with pytest.raises(RouterError, match='已停用|已不存在'):
        selected_sources(config, {'type': 'routeros', 'ip_rule_sets': ['r']})
    assert config == original


def test_policy_bundle_keeps_blocker_until_selection_is_explicitly_cleared(monkeypatch):
    config = source_config()
    config['rule_library'][0]['enabled'] = False
    monkeypatch.setattr('backend.common.oxidns_device_policy.read_policies', lambda *_: {
        'editable': True, 'policies': [{'ip': '192.168.0.101', 'mode': 'smart'}]})
    previous = {'resources': [{'path': '/routing/table', 'id': '*existing'}]}
    router = {'id': 'router', 'type': 'routeros', 'ros_policy_rule_sets': ['r'], 'ros_policy_state': previous}
    loader = lambda *_: pytest.fail('Disabled policy sources must not read their content')
    desired = inspect_desired(config, router, {'proxy_gateway': '192.168.0.3'}, content_loader=loader)
    assert desired['cidrs'] == [] and '已停用' in desired['error']
    assert desired['previous'] == previous
    router['ros_policy_rule_sets'] = []
    withdrawn = inspect_desired(config, router, {'proxy_gateway': '192.168.0.3'}, content_loader=loader)
    assert withdrawn['error'] == '' and withdrawn['selected'] == []
    assert withdrawn['previous'] == previous  # existing guarded release still owns cleanup


def test_active_source_and_legacy_inline_selection_remain_supported():
    config = source_config()
    load = lambda item, source: source['content'] if source else '91.108.4.0/22'
    for legacy in (False, True):
        if legacy:
            config['rule_configs'][0].pop('library_rule_id')
            config['rule_library'] = []
        desired = compute_desired(config, '192.168.0.3', ['r'], content_loader=load)
        assert desired['warnings'] == []
        assert desired['proxy_cidrs'][0]['cidr'] == '91.108.4.0/22'
