"""Per-rule OxiDNS DNS handling: routes, helper chains, recognition and settings."""
from copy import deepcopy

import pytest
import yaml

from backend.common.config_repository import ProfileInUse
from backend.common.oxidns_settings import (
    get_default_oxidns_settings, normalize_oxidns_settings, OxidnsSettingsError,
)
from backend.common.rule_dns import (
    BLACKHOLE_PLUGIN, Invalid, OxidnsRoutingLocked, entry_key, follow_route, helper_plugins,
    hydrate_ruleset, parse_exec, pinned_action, route_exec, route_key, upstream_catalog,
)

LOCAL = {'action': 'upstream', 'source': 'local', 'upstream': {'addr': 'udp://10.0.0.53:53'}}
REMOTE = {'action': 'upstream', 'source': 'remote', 'upstream': {'addr': 'https://dns.test/dns-query', 'bootstrap': '1.1.1.1'}}
FALLBACK = {'action': 'upstream', 'source': 'fallback', 'upstream': {'addr': 'udp://9.9.9.9'}}
FEATURES = {'cache_enabled': True, 'cache_lazy_ttl': 300, 'remote_ttl': 60,
            'ip_selector': {'enabled': True}, 'ros_address_list': {'enabled': False}, 'ros_route': {'enabled': True}}


def tags(plugins):
    return {plugin['tag']: plugin for plugin in plugins}


def test_follow_route_maps_direct_group_reject_and_unconvertible():
    rule = {'itemType': 'rule', 'rule_type': 'DOMAIN-SUFFIX', 'value': 'a.test'}
    assert follow_route({**rule, 'policy': 'DIRECT'}) == 'direct'
    assert follow_route({**rule, 'policy': '节点选择'}) == 'proxy'
    for policy in ('REJECT', 'REJECT-DROP', 'PASS', '', None):
        assert follow_route({**rule, 'policy': policy}) is None
    assert follow_route({**rule, 'rule_type': 'GEOIP', 'policy': 'DIRECT'}) is None
    assert follow_route({**rule, 'rule_type': 'IP-CIDR6', 'policy': 'DIRECT'}) is None
    assert follow_route({'itemType': 'ruleset', 'behavior': 'classical', 'policy': 'Proxy'}) == 'proxy'


def test_entry_key_is_stable_and_order_independent():
    key = entry_key({'addr': 'udp://1.1.1.1', 'bootstrap': '8.8.8.8'}, 'remote')
    assert key == entry_key({'bootstrap': '8.8.8.8', 'addr': 'udp://1.1.1.1'}, 'remote')
    assert len(key) == 12 and int(key, 16) >= 0
    # The same address chosen from another list builds another chain.
    assert key != entry_key({'addr': 'udp://1.1.1.1', 'bootstrap': '8.8.8.8'}, 'fallback')
    assert route_key(LOCAL) == 'upstream:' + entry_key(LOCAL['upstream'], 'local')
    assert route_key({'action': 'nxdomain'}) == 'nxdomain'
    assert route_key({'action': 'direct'}) == 'direct'


def test_route_exec_for_every_route():
    key = entry_key(LOCAL['upstream'], 'local')
    expected = {'direct': ('goto china_dns', 'goto direct_ip'), 'proxy': ('goto proxy_dns_seq', 'goto ip_requery_proxy'),
                'refused': ('reject', 'reject'), 'nxdomain': ('reject 3', 'reject 3'), 'nodata': ('reject 0', 'reject 0'),
                'blackhole': ('$cf_dns_blackhole', '$cf_dns_blackhole'),
                f'upstream:{key}': (f'goto cf_dns_up_{key}', f'goto cf_dns_up_{key}')}
    for route, (domain, ip) in expected.items():
        assert (route_exec(route, 'domain'), route_exec(route, 'ip')) == (domain, ip)
    for route in ('drop', 'servfail', 'upstream:xyz', 'default'):
        with pytest.raises(OxidnsSettingsError, match='不支持的 DNS 处理'):
            route_exec(route, 'domain')


def test_upstream_chain_per_source_follows_china_dns_and_remote_features():
    local, remote, fallback = (route_key(action) for action in (LOCAL, REMOTE, FALLBACK))
    plugins = tags(helper_plugins([local, remote, fallback], {'a': LOCAL, 'b': REMOTE, 'c': FALLBACK}, FEATURES))
    h = local[9:]
    assert plugins[f'cf_dns_fwd_{h}'] == {'tag': f'cf_dns_fwd_{h}', 'type': 'forward',
                                         'args': {'upstreams': [{'addr': 'udp://10.0.0.53:53'}]}}
    assert plugins[f'cf_dns_cache_{h}']['args'] == {'size': 1024, 'lazy_cache_ttl': 300, 'short_circuit': True}
    assert plugins[f'cf_dns_up_{h}']['args'] == [
        {'exec': 'drop_resp'}, {'exec': '$ip_selector'}, {'exec': '$ros_route'}, {'exec': f'$cf_dns_cache_{h}'},
        {'exec': f'$cf_dns_fwd_{h}'}, {'matches': ['!has_resp'], 'exec': 'reject 3'}, {'exec': 'accept'}]
    for route in (remote, fallback):
        h = route[9:]
        assert plugins[f'cf_dns_up_{h}']['args'] == [
            {'exec': 'drop_resp'}, {'exec': f'$cf_dns_cache_{h}'}, {'exec': f'$cf_dns_fwd_{h}'},
            {'matches': ['!has_resp'], 'exec': 'reject 3'}, {'exec': 'ttl 60'}, {'exec': 'accept'}]
    assert plugins[f'cf_dns_fwd_{remote[9:]}']['args']['upstreams'] == [REMOTE['upstream']]


def test_upstream_chain_without_cache_lazy_ttl_or_remote_ttl():
    plain = {'cache_enabled': True, 'cache_lazy_ttl': 0, 'remote_ttl': 0}
    plugins = tags(helper_plugins([route_key(REMOTE)], {'b': REMOTE}, plain))
    h = route_key(REMOTE)[9:]
    assert plugins[f'cf_dns_cache_{h}']['args'] == {'size': 1024, 'short_circuit': True}
    assert {'exec': 'ttl 0'} not in plugins[f'cf_dns_up_{h}']['args']
    plugins = tags(helper_plugins([route_key(LOCAL)], {'a': LOCAL}, {'cache_enabled': False}))
    h = route_key(LOCAL)[9:]
    assert set(plugins) == {f'cf_dns_fwd_{h}', f'cf_dns_up_{h}'}
    assert plugins[f'cf_dns_up_{h}']['args'] == [
        {'exec': 'drop_resp'}, {'exec': f'$cf_dns_fwd_{h}'}, {'matches': ['!has_resp'], 'exec': 'reject 3'}, {'exec': 'accept'}]


def test_helper_plugins_only_for_used_routes_once_in_first_use_order():
    actions = {'a': LOCAL, 'b': LOCAL, 'c': REMOTE}
    assert helper_plugins(['direct', 'proxy', 'refused', 'nxdomain', 'nodata'], actions, FEATURES) == []
    result = helper_plugins([route_key(LOCAL), 'blackhole', route_key(LOCAL), 'blackhole'], actions, {})
    assert [plugin['tag'] for plugin in result] == [
        f'cf_dns_fwd_{route_key(LOCAL)[9:]}', f'cf_dns_up_{route_key(LOCAL)[9:]}', 'cf_dns_blackhole']
    assert result[-1] == BLACKHOLE_PLUGIN and result[-1] is not BLACKHOLE_PLUGIN
    with pytest.raises(OxidnsSettingsError):
        helper_plugins(['upstream:000000000000'], actions, {})


@pytest.mark.parametrize('action', [LOCAL, REMOTE, FALLBACK])
@pytest.mark.parametrize('features', [FEATURES, {'cache_enabled': False}, {'cache_enabled': True, 'cache_lazy_ttl': 0},
                                      {'remote_ttl': 0, 'cache_enabled': True, 'cache_lazy_ttl': 5}])
def test_parse_exec_recognises_generated_pinned_chains(action, features):
    route = route_key(action)
    plugins = tags(helper_plugins([route], {'x': action}, features))
    for part in ('domain', 'ip'):
        assert parse_exec(route_exec(route, part), part, plugins) == route
    assert pinned_action(route[9:], plugins) == action


def test_parse_exec_recognises_aliases_and_helpers():
    plugins = tags([deepcopy(BLACKHOLE_PLUGIN)])
    cases = {'goto china_dns': ('direct', None), 'goto proxy_dns_seq': ('proxy', None),
             'goto direct_ip': (None, 'direct'), 'goto ip_requery_proxy': (None, 'proxy'),
             'reject': ('refused', 'refused'), 'reject 5': ('refused', 'refused'), 'REJECT refused': ('refused', 'refused'),
             'reject 3': ('nxdomain', 'nxdomain'), 'reject NXDOMAIN': ('nxdomain', 'nxdomain'),
             'reject 0': ('nodata', 'nodata'), 'reject noerror': ('nodata', 'nodata'),
             '$cf_dns_blackhole': ('blackhole', 'blackhole'),
             'reject 2': (None, None), 'reject 3 extra': (None, None), 'accept': (None, None),
             '$cf_dns_up_0123456789ab': (None, None), 'goto cf_dns_up_short': (None, None)}
    for value, (domain, ip) in cases.items():
        assert (parse_exec(value, 'domain', plugins), parse_exec(value, 'ip', plugins)) == (domain, ip), value
    assert parse_exec(None, 'domain', plugins) is None


@pytest.mark.parametrize('args', [{'mode': None, 'short_circuit': True}, {'mode': 'nxdomain', 'short_circuit': True},
                                  {'mode': 'null'}, {'mode': 'null', 'short_circuit': 'true'}])
def test_modified_or_missing_blackhole_helper_is_invalid(args):
    edited = {'tag': 'cf_dns_blackhole', 'type': 'black_hole', 'args': args}
    for plugins in ({'cf_dns_blackhole': edited}, {}):
        result = parse_exec('$cf_dns_blackhole', 'domain', plugins)
        assert isinstance(result, Invalid) and 'cf_dns_blackhole' in result.reason and '"null"' in result.reason


def test_hand_edited_pinned_addr_is_recognised_under_content_key():
    route = route_key(REMOTE)
    plugins = tags(helper_plugins([route], {'x': REMOTE}, FEATURES))
    plugins[f'cf_dns_fwd_{route[9:]}']['args']['upstreams'][0]['addr'] = 'udp://192.0.2.1'
    edited = {**REMOTE, 'upstream': {**REMOTE['upstream'], 'addr': 'udp://192.0.2.1'}}
    # A remote chain without wrappers but with TTL is remote or fallback; remote is preferred.
    assert parse_exec(f'goto cf_dns_up_{route[9:]}', 'domain', plugins) == route_key(edited)
    assert pinned_action(route[9:], plugins) == edited


def test_pinned_source_prefers_tag_hash_when_shapes_match():
    plain = {'cache_enabled': True, 'cache_lazy_ttl': 300, 'remote_ttl': 0}
    for action in (LOCAL, REMOTE, FALLBACK):
        same = {**action, 'upstream': {'addr': 'udp://203.0.113.9'}}
        plugins = tags(helper_plugins([route_key(same)], {'x': same}, plain))
        assert parse_exec(route_exec(route_key(same), 'ip'), 'ip', plugins) == route_key(same)


@pytest.mark.parametrize('wrappers, ttl, expected', [
    ('ip_selector', None, 'remote'), (None, 'ttl 60', 'local'), (None, None, 'local'), ('ros_route', 'ttl 60', None)])
def test_hand_edited_bare_pinned_chain_source_matches_current_features(wrappers, ttl, expected):
    # Without hooks or TTL a chain could be any source; the hash no longer says
    # which, so the source the current features would build this shape for wins.
    action = {**REMOTE, 'upstream': {'addr': 'udp://203.0.113.9'}}
    key = route_key(action)[9:]
    plugins = tags(helper_plugins([route_key(action)], {'x': action}, {'remote_ttl': 0}))
    plugins[f'cf_dns_fwd_{key}']['args']['upstreams'][0]['addr'] = 'udp://203.0.113.10'
    plugins['china_dns'] = {'tag': 'china_dns', 'type': 'sequence',
                            'args': [{'exec': f'${wrappers}'}] if wrappers else []}
    plugins['remote_dns'] = {'tag': 'remote_dns', 'type': 'sequence', 'args': [{'exec': ttl}] if ttl else []}
    result = parse_exec(f'goto cf_dns_up_{key}', 'domain', plugins)
    if expected is None:
        assert isinstance(result, Invalid) and pinned_action(key, plugins) is None
        return
    edited = {'action': 'upstream', 'source': expected, 'upstream': {'addr': 'udp://203.0.113.10'}}
    assert result == route_key(edited) and pinned_action(key, plugins) == edited


def edit_chain(change):
    route = route_key(LOCAL)
    plugins = tags(helper_plugins([route], {'x': LOCAL}, FEATURES))
    change(plugins, route[9:])
    return parse_exec(f'goto cf_dns_up_{route[9:]}', 'domain', plugins)


@pytest.mark.parametrize('change', [
    lambda p, h: p[f'cf_dns_fwd_{h}']['args']['upstreams'].append({'addr': 'udp://10.0.0.54'}),
    lambda p, h: p[f'cf_dns_fwd_{h}']['args'].update(concurrent=2),
    lambda p, h: p[f'cf_dns_fwd_{h}']['args']['upstreams'][0].update(addr=''),
    lambda p, h: p.pop(f'cf_dns_fwd_{h}'),
    lambda p, h: p.pop(f'cf_dns_cache_{h}'),
    lambda p, h: p[f'cf_dns_cache_{h}']['args'].update(size=4096),
    lambda p, h: p[f'cf_dns_up_{h}']['args'].insert(1, {'exec': '$query_recorder'}),
    lambda p, h: p[f'cf_dns_up_{h}']['args'].pop(0),
    lambda p, h: p[f'cf_dns_up_{h}']['args'].insert(-1, {'exec': 'ttl 60'}),
    lambda p, h: p[f'cf_dns_up_{h}']['args'].reverse(),
    lambda p, h: p[f'cf_dns_up_{h}'].update(type='forward'),
])
def test_hand_modified_pinned_chain_is_invalid(change):
    result = edit_chain(change)
    assert isinstance(result, Invalid) and '指定上游处理链 cf_dns_up_' in result.reason


def test_upstream_catalog_template_lists_defaults_sources_and_links():
    settings = normalize_oxidns_settings({'local_dns': '', 'remote_dns': 'udp://127.0.0.1:1053\nudp://223.5.5.5',
                                          'fallback_dns': 'udp://223.5.5.5'})
    bindings = [{'upstream_tag': 'forward_remote', 'upstream_index': 0}]
    catalog = upstream_catalog(settings, bindings=bindings)
    assert [(item['source'], item['addr'], item['label'], item['linked']) for item in catalog] == [
        ('local', 'udp://223.5.5.5', '直连 · 223.5.5.5', False),
        ('local', 'udp://119.29.29.29', '直连 · 119.29.29.29', False),
        ('remote', 'udp://127.0.0.1:1053', '代理 · 127.0.0.1:1053', True),
        ('remote', 'udp://223.5.5.5', '代理 · 223.5.5.5', False),
        ('fallback', 'udp://223.5.5.5', '备用 · 223.5.5.5', False)]
    assert len({item['key'] for item in catalog}) == len(catalog)
    assert all(item['key'] == entry_key(item['entry'], item['source']) for item in catalog)
    broken = upstream_catalog({'remote_dns': '- addr: ['})
    assert {item['source'] for item in broken} == {'local'}


def test_upstream_catalog_native_reads_forwards_and_skips_parked_fallback():
    document = {'plugins': [
        {'tag': 'forward_local', 'type': 'forward', 'args': {'upstreams': [{'addr': 'udp://10.0.0.1'}, 'udp://bare', {'x': 1}]}},
        {'tag': 'forward_remote', 'type': 'forward', 'args': {'upstreams': [{'addr': 'tls://dns.test', 'socks5': 's:1'}]}},
        {'tag': 'cf_dns_fwd_0123456789ab', 'type': 'forward', 'args': {'upstreams': [{'addr': 'udp://10.9.9.9'}]}}]}
    settings = {'config_mode': 'native', '_common_disabled': {'forward_fallback': {
        'tag': 'forward_fallback', 'type': 'forward', 'args': {'upstreams': [{'addr': 'udp://9.9.9.9'}]}}}}
    catalog = upstream_catalog(settings, document, [{'upstream_tag': 'forward_local', 'upstream_index': 0}])
    assert [(item['source'], item['entry'], item['linked']) for item in catalog] == [
        ('local', {'addr': 'udp://10.0.0.1'}, True), ('remote', {'addr': 'tls://dns.test', 'socks5': 's:1'}, False)]
    assert catalog[1]['label'] == '代理 · tls://dns.test'
    native = {'config_mode': 'native', 'native_config': yaml.safe_dump(document)}
    assert upstream_catalog(native) == [dict(item, linked=False) for item in catalog]


def test_hydrate_ruleset_copies_library_source_in_place():
    item = {'id': 'r', 'itemType': 'ruleset', 'library_rule_id': 'lib', 'policy': 'DIRECT',
            'url': 'stale', 'format': 'yaml', 'library_enabled': True}
    entry = {'id': 'lib', 'name': 'Ads', 'source_type': 'content', 'content': 'DOMAIN,a.test', 'enabled': False}
    assert hydrate_ruleset(item, entry, 'home') is item
    assert item == {'id': 'r', 'itemType': 'ruleset', 'library_rule_id': 'lib', 'policy': 'DIRECT', 'name': 'Ads',
                    'source_type': 'content', 'content': 'DOMAIN,a.test', 'behavior': 'domain',
                    'library_enabled': False, 'url': '/api/profiles/home/rule-library/content/lib'}
    remote = hydrate_ruleset({'id': 'r'}, {'id': 'lib', 'url': 'https://rules.test/a', 'behavior': 'classical'}, None)
    assert remote == {'id': 'r', 'url': 'https://rules.test/a', 'behavior': 'classical', 'library_enabled': True}


def test_routing_locked_is_a_profile_conflict_with_reason():
    error = OxidnsRoutingLocked('当前分流执行链包含自定义结构')
    assert isinstance(error, ProfileInUse) and str(error) == '当前分流执行链包含自定义结构'
    assert error.usages == [{'kind': 'oxidns', 'code': 'oxidns_routing_custom', 'target': 'sequence_main',
                             'reason': '当前分流执行链包含自定义结构'}]


def test_defaults_include_rule_dns_fields():
    defaults = get_default_oxidns_settings()
    assert (defaults['rule_actions'], defaults['rule_dns_follow'], defaults['rule_dns_parked']) == ({}, [], {})
    assert normalize_oxidns_settings({})['rule_actions'] == {}


def test_null_action_value_is_deleted():
    settings = normalize_oxidns_settings({'rule_actions': {'a': {'action': 'blackhole'}, 'b': None},
                                          'rule_dns_parked': {'c': None, 'd': {'action': 'direct'}},
                                          'rule_dns_follow': ['b', 'c', 'b']})
    assert settings['rule_actions'] == {'a': {'action': 'blackhole'}}
    assert settings['rule_dns_parked'] == {'d': {'action': 'direct'}}
    assert settings['rule_dns_follow'] == ['b', 'c']


def test_upstream_action_is_a_copy_with_extra_keys_kept():
    upstream = {'addr': 'tls://dns.test', 'socks5': '127.0.0.1:1080', 'bootstrap': '1.1.1.1'}
    value = {'rule_actions': {'a': {'action': 'upstream', 'source': 'remote', 'upstream': upstream}}}
    settings = normalize_oxidns_settings(value)
    assert settings['rule_actions']['a']['upstream'] == upstream
    assert settings['rule_actions']['a']['upstream'] is not upstream


def test_follow_and_action_overlap_rejected():
    with pytest.raises(OxidnsSettingsError, match='规则 a 已设置 DNS 处理「黑洞」，不能同时跟随策略'):
        normalize_oxidns_settings({'rule_actions': {'a': {'action': 'blackhole'}}, 'rule_dns_follow': ['a']})


def test_follow_and_parked_overlap_rejected():
    with pytest.raises(OxidnsSettingsError, match='不能同时跟随策略'):
        normalize_oxidns_settings({'rule_dns_parked': {'a': {'action': 'proxy'}}, 'rule_dns_follow': ['a']})


def test_actions_may_overlap_selector_lists():
    settings = normalize_oxidns_settings({'direct_rules': ['a'], 'rule_actions': {'a': {'action': 'nodata'}}})
    assert settings['direct_rules'] == ['a'] and settings['rule_actions'] == {'a': {'action': 'nodata'}}


@pytest.mark.parametrize('value', [
    {'rule_actions': []}, {'rule_actions': {'a': 'reject'}}, {'rule_actions': {'a': {'action': 'drop'}}},
    {'rule_actions': {'a': {'action': 'servfail'}}}, {'rule_actions': {'a': {'action': 'direct'}}},
    {'rule_actions': {'a': {'action': 'refused', 'rcode': 5}}},
    {'rule_actions': {'a': {'action': 'upstream', 'upstream': {'addr': 'udp://1.1.1.1'}}}},
    {'rule_actions': {'a': {'action': 'upstream', 'source': 'other', 'upstream': {'addr': 'udp://1.1.1.1'}}}},
    {'rule_actions': {'a': {'action': 'upstream', 'source': 'local', 'upstream': {'addr': ' '}}}},
    {'rule_actions': {'a': {'action': 'upstream', 'source': 'local', 'upstream': 'udp://1.1.1.1'}}},
    {'rule_dns_parked': {'a': {'action': 'follow'}}}, {'rule_dns_parked': []},
    {'rule_dns_follow': 'a'}, {'rule_dns_follow': [1]},
])
def test_invalid_rule_dns_shapes_rejected(value):
    with pytest.raises(OxidnsSettingsError):
        normalize_oxidns_settings(value)


def test_rule_dns_fields_checked_before_native_early_return():
    native = {'config_mode': 'native', 'native_config': 'plugins: []\n'}
    with pytest.raises(OxidnsSettingsError, match='不能同时跟随策略'):
        normalize_oxidns_settings({**native, 'rule_actions': {'a': {'action': 'nodata'}}, 'rule_dns_follow': ['a']},
                                  validate_native_arguments=False)
    settings = normalize_oxidns_settings({**native, 'rule_actions': {'a': None}}, validate_native_arguments=False)
    assert settings['rule_actions'] == {}


# ---- Read model and writes -------------------------------------------------

def space(**oxidns):
    """A resolved template profile with a domain rule, an IP rule and a classical ruleset."""
    return {'profile_id': 'p', 'system_config': {'server_domain': 'http://config.test', 'rule_proxy_token': 't'},
            'agents': [], 'mosdns': {}, 'proxy_groups': [{'name': 'Proxy'}],
            'rule_configs': [
                {'id': 'a', 'itemType': 'rule', 'rule_type': 'DOMAIN', 'value': 'a.test', 'policy': 'DIRECT'},
                {'id': 'b', 'itemType': 'rule', 'rule_type': 'DOMAIN-SUFFIX', 'value': 'b.test', 'policy': 'Proxy'},
                {'id': 'c', 'itemType': 'rule', 'rule_type': 'DOMAIN', 'value': 'c.test', 'policy': 'Proxy'},
                {'id': 'ip', 'itemType': 'rule', 'rule_type': 'IP-CIDR', 'value': '192.0.2.0/24', 'policy': 'DIRECT'},
                {'id': 'mixed', 'itemType': 'ruleset', 'behavior': 'classical', 'policy': 'Proxy',
                 'url': 'https://example.test/mixed.txt'},
            ],
            'oxidns': {**get_default_oxidns_settings(), **oxidns}}


def test_legacy_unselected_shows_default_and_lists_show_explicit():
    from backend.common.rule_dns import rule_dns_state
    state = rule_dns_state(space(direct_rules=['a'], proxy_rules=['b']))
    assert state['rules'].pop('ip')['state'] == 'unsupported'
    assert state['rules'] == {'a': {'state': 'explicit', 'route': 'direct'},
                              'b': {'state': 'explicit', 'route': 'proxy'}}
    assert state['engine']['oxidns_in_use'] is True and state['engine']['default_forward'] == 'forward_remote'
    # Nothing in any list: every rule is the default upstream and OxiDNS is not in use.
    state = rule_dns_state(space())
    assert state['rules']['ip']['state'] == 'unsupported'
    assert state['engine']['oxidns_in_use'] is False


def test_follow_drift_state():
    from backend.common.rule_dns import rule_dns_state
    config = space(direct_rules=['a'], proxy_rules=['c'], rule_dns_follow=['a', 'b', 'c'],
                   rule_actions={'mixed': {'action': 'blackhole'}})
    config['rule_configs'][2]['policy'] = 'DIRECT'
    rules = rule_dns_state(config)['rules']
    assert rules['a'] == {'state': 'follow', 'route': 'direct', 'follow_route': 'direct'}
    assert rules['b'] == {'state': 'follow_drift', 'route': None, 'follow_route': 'proxy'}
    assert rules['c'] == {'state': 'follow_drift', 'route': 'proxy', 'follow_route': 'direct'}
    assert rules['mixed'] == {'state': 'explicit', 'route': 'blackhole'}


def test_classical_ip_metadata_never_enables_probe():
    from backend.common.rule_dns import has_ip_part, rule_dns_state
    # Source metadata still describes IP content for connection routing.
    assert has_ip_part({'itemType': 'ruleset', 'behavior': 'classical'})
    assert has_ip_part({'itemType': 'ruleset', 'behavior': 'ipcidr'})
    assert not has_ip_part({'itemType': 'ruleset', 'behavior': 'domain'})
    assert has_ip_part({'itemType': 'rule', 'rule_type': 'IP-CIDR6'})
    assert rule_dns_state(space(direct_rules=['a']))['engine']['local_probe_active'] is False
    # Unknown content: a classical set is still assumed to have IP lines.
    assert rule_dns_state(space(direct_rulesets=['mixed']))['engine']['local_probe_active'] is False
    # Proxy like the proxy fallback: the probe could not change any answer.
    assert rule_dns_state(space(proxy_rulesets=['mixed']))['engine']['local_probe_active'] is False


def test_old_ip_follow_mark_cannot_enable_dns_probe():
    from backend.common.rule_dns import apply_rule_dns
    before = space(direct_rules=['a'], rule_dns_follow=['ip'])
    before['rule_configs'][3]['policy'] = 'REJECT'
    after = deepcopy(before)
    after['rule_configs'][3]['policy'] = 'DIRECT'
    warnings = apply_rule_dns(before, after, {})
    assert warnings == []
    assert after['oxidns']['direct_rules'] == ['a']
    # Once a probe exists, later IP groups say nothing.
    again = deepcopy(after)
    again['rule_configs'][4]['policy'] = 'DIRECT'
    assert apply_rule_dns(after, again, {'mixed': 'proxy'}) == []


def test_apply_rule_dns_assignments_take_one_holder():
    from backend.common.config_repository import ProfileValidationError
    from backend.common.rule_dns import apply_rule_dns
    before = space(direct_rules=['a'], proxy_rules=['b'], rule_actions={'c': {'action': 'refused'}},
                   rule_dns_follow=['a'])
    after = deepcopy(before)
    assert apply_rule_dns(before, after, {'a': 'nodata', 'b': 'follow', 'c': 'default'}) == []
    assert after['oxidns']['rule_actions'] == {'a': {'action': 'nodata'}}
    assert after['oxidns']['direct_rules'] == [] and after['oxidns']['proxy_rules'] == ['b']
    assert after['oxidns']['rule_dns_follow'] == ['b']
    for assignments, message in (({'a': 'drop'}, '不支持的 DNS 处理：drop'),
                                 ({'gone': 'proxy'}, '规则不存在：gone'),
                                 ({'a': 'upstream:0123456789ab'}, '所选上游已不在')):
        with pytest.raises(ProfileValidationError, match=message):
            apply_rule_dns(before, deepcopy(before), assignments)
    # Edits that leave routing alone do not touch the settings at all.
    untouched = deepcopy(before)
    untouched['rule_configs'][0]['remark'] = 'note'
    assert apply_rule_dns(before, untouched, {}) == [] and untouched['oxidns'] == before['oxidns']


def test_malformed_stored_actions_read_as_default_and_are_dropped_on_write():
    from backend.common.rule_dns import apply_rule_dns, explicit_route, rule_dns_state
    broken = {'a': {'kind': 'blackhole'}, 'b': 'reject',
              'c': {'action': 'upstream', 'upstream': {'addr': '1.1.1.1'}}, 'mixed': {'action': 'nodata'}}
    before = space(rule_actions=broken, rule_dns_parked={'ip': {'action': 'drop'}})
    rules = rule_dns_state(before)['rules']
    assert rules.pop('ip')['state'] == 'unsupported'
    assert rules == {'mixed': {'state': 'explicit', 'route': 'nodata'}}
    assert explicit_route(before, 'a') is None
    # A write that leaves routing alone still removes what cannot be read.
    after = deepcopy(before)
    after['rule_configs'][0]['remark'] = 'note'
    assert apply_rule_dns(before, after, {}) == []
    assert after['oxidns']['rule_actions'] == {'mixed': {'action': 'nodata'}}
    assert after['oxidns']['rule_dns_parked'] == {}
    after = deepcopy(before)
    after['rule_configs'][0]['enabled'] = False
    apply_rule_dns(before, after, {'b': 'refused'})
    assert after['oxidns']['rule_actions'] == {'b': {'action': 'refused'}, 'mixed': {'action': 'nodata'}}
    assert after['oxidns']['rule_dns_parked'] == {}


def test_apply_rule_dns_native_locked_strict_and_lenient():
    from backend.common.oxidns_editor import prepare_editor
    from backend.common.rule_dns import apply_rule_dns
    config = space()
    config['oxidns'] = prepare_editor({'rule_actions': {'b': {'action': 'blackhole'}}}, config)
    config['oxidns']['native_config'] = config['oxidns']['native_config'].replace("mode: 'null'", 'mode: nxdomain')
    with pytest.raises(OxidnsRoutingLocked) as error:
        apply_rule_dns(config, deepcopy(config), {'a': 'refused'})
    assert error.value.usages[0]['code'] == 'oxidns_routing_custom'
    after = deepcopy(config)
    after['rule_configs'][0]['policy'] = 'Proxy'
    warnings = apply_rule_dns(config, after, {}, strict=False)
    assert warnings[0].startswith('OxiDNS 分流含自定义结构，DNS 未修改：黑洞插件')
    assert after['oxidns'] == config['oxidns']


def test_rebuild_keeps_hand_edited_remote_pin_without_local_hooks():
    from backend.common.rule_dns import apply_rule_dns, rule_dns_state
    from backend.converters.oxidns import generate_oxidns_config
    config = space(remote_dns='udp://203.0.113.9', remote_ttl=0, ip_selector={'enabled': True})
    key = entry_key({'addr': 'udp://203.0.113.9'}, 'remote')
    apply_rule_dns(deepcopy(config), config, {'b': 'upstream:' + key})
    document = yaml.safe_load(generate_oxidns_config(config))
    next(p for p in document['plugins'] if p['tag'] == 'cf_dns_fwd_' + key)['args']['upstreams'][0]['addr'] = \
        'udp://203.0.113.10'
    config['oxidns'].update(config_mode='native', native_config=yaml.safe_dump(document, sort_keys=False))
    assert rule_dns_state(config)['rules']['b']['upstream_label'] == '代理 · 203.0.113.10'
    after = deepcopy(config)
    apply_rule_dns(deepcopy(config), after, {'c': 'refused'})
    chains = [p for p in yaml.safe_load(after['oxidns']['native_config'])['plugins']
              if p['tag'].startswith('cf_dns_up_')]
    assert chains and all({'exec': '$ip_selector'} not in p['args'] for p in chains)
