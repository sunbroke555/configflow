#!/usr/bin/env python3
"""Golden generator for compat_test.go: runs the real Python helpers.

    CONFIGFLOW_REPO=<repo root> python compat_gen.py compat_golden.json

Every case is {"fn", "input", "raised", "ok", "error_class", "error"}.
"""

import copy
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ.get('CONFIGFLOW_REPO') or str(Path(__file__).resolve().parents[4]))

from backend.common.client_fixed_ip import validate_bindings  # noqa: E402
from backend.common.device_notes import normalize_notes  # noqa: E402
from backend.common.oxidns_device_policy import normalize_client_range  # noqa: E402
from backend.common.rule_dns import hydrate_ruleset, normalize_action  # noqa: E402
from backend.converters.mihomo import _parse_structured_proxy_string  # noqa: E402
from backend.utils.dialer_references import raw_dialer, validate_dialers, validate_shapes  # noqa: E402
from backend.utils.health_check import validate_health  # noqa: E402
from backend.utils.rule_utils import rule_cache_filename, sanitize_rule_name  # noqa: E402
from backend.utils.subscription_refresh import validate_refresh_interval  # noqa: E402


def hydrate(value):
    item = copy.deepcopy(value['item'])
    hydrate_ruleset(item, value['library'], value['profile_id'])
    return item


FUNCTIONS = {
    'validate_shapes': lambda v: validate_shapes(v['profile'], require_ids=v['require_ids']),
    'validate_dialers': validate_dialers,
    'raw_dialer': raw_dialer,
    'parse_proxy_string': _parse_structured_proxy_string,
    'validate_health': lambda v: validate_health(v['values'], provider=v['provider']),
    'validate_refresh_interval': validate_refresh_interval,
    'normalize_notes': normalize_notes,
    'normalize_client_range': normalize_client_range,
    'validate_bindings': validate_bindings,
    'normalize_action': lambda v: normalize_action(v['value'], parked=v['parked']),
    'hydrate_ruleset': hydrate,
    'sanitize_rule_name': sanitize_rule_name,
    'rule_cache_filename': rule_cache_filename,
}

CASES = []


def case(fn, value):
    CASES.append((fn, value))


def N(id, name=None, **extra):
    return {'id': id, 'name': name or id, **extra}


def G(id, name=None, **extra):
    return {'id': id, 'name': name or id, **extra}


def ref(kind, id):
    return {'type': kind, 'id': id}


def chain(id, entry, exit, **extra):
    return G(id, type='chain', chain={'entry': entry, 'exit': exit}, **extra)


# ---- validate_shapes ----
for profile, require_ids in [
    ({}, True),
    ({'nodes': [N('a')], 'proxy_groups': [G('g', manual_nodes=['a'])]}, True),
    ({'nodes': {}}, True),
    ({'nodes': None}, True),
    ({'proxy_groups': 'x'}, True),
    ({'nodes': ['a']}, True),
    ({'nodes': [None]}, True),
    ({'nodes': [{'name': 'a'}]}, True),
    ({'nodes': [{'name': 'a'}]}, False),
    ({'nodes': [{'id': '', 'name': 'a'}]}, False),
    ({'nodes': [{'id': ' \t', 'name': 'a'}]}, True),
    ({'nodes': [{'id': 5, 'name': 'a'}]}, True),
    ({'nodes': [{'id': 'a'}]}, True),
    ({'nodes': [{'id': 'a', 'name': '\x1c'}]}, True),
    ({'nodes': [{'id': 'a', 'name': '\u3000'}]}, True),
    ({'nodes': [N('a', params=None)]}, True),
    ({'nodes': [N('a', params=[])]}, True),
    ({'nodes': [N('a', params={})]}, True),
    ({'nodes': [N('a', proxy_string=5)]}, True),
    ({'nodes': [N('a', proxy_string=None)]}, True),
    ({'nodes': [N('a', proxy_string='ss://x')]}, True),
    ({'proxy_groups': [G('g', manual_nodes='a')]}, True),
    ({'proxy_groups': [G('g', manual_nodes=['a', ''])]}, True),
    ({'proxy_groups': [G('g', include_groups=[1])]}, True),
    ({'proxy_groups': [G('g', proxies=None)]}, True),
    ({'proxy_groups': [G('g', subscriptions=[' '])]}, True),
    ({'proxy_groups': [G('g', aggregations={})]}, True),
    ({'proxy_groups': [G('g', use=['x'])]}, True),
    ({'proxy_groups': [G('g', proxies_order=None)]}, True),
    ({'proxy_groups': [G('g', proxies_order=[{'type': 'node'}])]}, True),
    ({'proxy_groups': [G('g', proxies_order=[{'type': 'node', 'id': 1}])]}, True),
    ({'proxy_groups': [G('g', proxies_order=['x'])]}, True),
    ({'proxy_groups': [G('g', proxies_order=[{'type': 'node', 'id': ''}])]}, True),
    ({'proxy_groups': [{'id': 'g'}]}, False),
]:
    case('validate_shapes', {'profile': profile, 'require_ids': require_ids})

# ---- validate_dialers ----
a, b, c = N('a'), N('b'), N('c')
sub = {'id': 's1', 'name': 'Sub', 'enabled': True}
agg = {'id': 'ag1', 'name': 'Agg', 'nodes': ['a'], 'subscriptions': []}
for profile in [
    {},
    {'nodes': [a, b], 'proxy_groups': [G('g', manual_nodes=['a'])]},
    {'nodes': [N('a', params={'dialer-proxy': 'b'}), b]},
    {'nodes': [N('a', params={'dialer-proxy': 'missing'}), b]},
    {'nodes': [N('a', params={'dialer-proxy': 'DIRECT'})]},
    {'nodes': [N('a', params={'dialer-proxy': 'REJECT-DROP'})]},
    {'nodes': [N('a', params={'dialer-proxy': 5})]},
    {'nodes': [N('a', params={'dialer-proxy': '  '})]},
    {'nodes': [N('a', params={'dialer-proxy': ''})]},
    {'nodes': [N('a', params={'dialer-proxy': False})]},
    {'nodes': [N('a', params={'dialer-proxy': 'b'}), N('b', params={'dialer-proxy': 'a'})]},
    {'nodes': [N('a', params={'dialer-proxy': 'a'})]},
    {'nodes': [N('a', params={'dialer-proxy': 'b'}), N('b', enabled=False)]},
    {'nodes': [N('a', params={'dialer-proxy': 'b'}, enabled=False)]},
    {'nodes': [N('a', params={'dialer-proxy': 'x'}), N('b', 'x'), N('c', 'x')]},
    {'nodes': [N('a', params={'dialer-proxy': 'g'})], 'proxy_groups': [G('g', manual_nodes=['a'])]},
    {'nodes': [N('a', params={'dialer-proxy': 'g'}), b], 'proxy_groups': [G('g', manual_nodes=['b'])]},
    {'nodes': [N('a', params={'dialer-proxy': 'g'}), b], 'proxy_groups': [G('g')]},
    {'nodes': [N('a', dialer_ref=ref('node', 'b')), b]},
    {'nodes': [N('a', dialer_ref=ref('node', 'a'))]},
    {'nodes': [N('a', dialer_ref={'type': 'node', 'id': 'b', 'x': 1}), b]},
    {'nodes': [N('a', dialer_ref={'type': 'provider', 'id': 'b'}), b]},
    {'nodes': [N('a', dialer_ref={'type': 'node', 'id': ' '}), b]},
    {'nodes': [N('a', dialer_ref='b'), b]},
    {'nodes': [N('a', dialer_ref=ref('node', 'zz')), b]},
    {'nodes': [N('a', dialer_ref=ref('node', 'b')), N('b', enabled=False)]},
    {'nodes': [N('a', dialer_ref=ref('node', 'b')), N('b', subscription_id='s1')], 'subscriptions': [sub]},
    {'nodes': [N('a', dialer_ref=ref('group', 'g')), b], 'proxy_groups': [G('g', manual_nodes=['b'])]},
    {'nodes': [N('a', dialer_ref=ref('group', 'g')), b], 'proxy_groups': [G('g')]},
    {'nodes': [N('a', dialer_ref=ref('group', 'g')), b], 'proxy_groups': [G('g', manual_nodes=['b'], include_all=True)]},
    {'nodes': [N('a', dialer_ref=ref('group', 'g')), b], 'proxy_groups': [G('g', **{'include-all': 1})]},
    {'nodes': [N('a', dialer_ref=ref('group', 'g')), b],
     'proxy_groups': [G('g', follow_group='h'), G('h', manual_nodes=['b'])]},
    {'nodes': [N('a', dialer_ref=ref('group', 'g')), b], 'proxy_groups': [G('g', follow_group='zz')]},
    {'nodes': [N('a', dialer_ref=ref('group', 'g')), b],
     'proxy_groups': [G('g', include_groups=['h']), G('h', include_groups=['g'])]},
    {'nodes': [N('a', dialer_ref=ref('group', 'g')), b], 'proxy_groups': [G('g', manual_nodes=['a'])]},
    {'nodes': [N('a', dialer_ref=ref('group', 'g')), b], 'proxy_groups': [G('g', manual_nodes=['DIRECT'])]},
    {'nodes': [N('a', dialer_ref=ref('node', 'b')), N('b', 'a')]},
    {'nodes': [N('a', dialer_ref=ref('node', 'b')), b, N('b', 'other')]},
    {'nodes': [N('a', dialer_ref=ref('node', 'b')), b, N('p', 'PASS')]},
    {'nodes': [N('a', dialer_ref=ref('node', 'b')), b, N('p', 'DIRECT', enabled=False)]},
    {'nodes': [N('a', dialer_ref=ref('node', 'b')), b], 'proxy_groups': [G('g1', 'a')]},
    {'nodes': [N('a', dialer_ref=ref('group', 'g')), b], 'subscriptions': [sub],
     'proxy_groups': [G('g', subscriptions=['s1'])]},
    {'nodes': [N('a', dialer_ref=ref('group', 'g')), b], 'subscriptions': [dict(sub, enabled=False)],
     'proxy_groups': [G('g', subscriptions=['s1'])]},
    {'nodes': [N('a', dialer_ref=ref('group', 'g')), b], 'proxy_groups': [G('g', subscriptions=['s1'])]},
    {'nodes': [N('a', dialer_ref=ref('group', 'g')), b], 'subscriptions': [sub], 'proxy_groups': [G('g', use=['Sub'])]},
    {'nodes': [N('a', dialer_ref=ref('group', 'g')), b], 'subscriptions': [sub], 'proxy_groups': [G('g', use=['Nope'])]},
    {'nodes': [N('a', dialer_ref=ref('group', 'g')), b], 'subscriptions': [sub],
     'subscription_aggregations': [dict(agg, name='Sub')], 'proxy_groups': [G('g', use=['Sub'])]},
    {'nodes': [N('a', dialer_ref=ref('group', 'g')), b], 'subscription_aggregations': [agg],
     'proxy_groups': [G('g', aggregations=['ag1'])]},
    {'nodes': [N('a', dialer_ref=ref('group', 'g')), b],
     'subscription_aggregations': [{'id': 'ag1', 'name': 'Agg'}], 'proxy_groups': [G('g', aggregations=['ag1'])]},
    {'nodes': [N('a', dialer_ref=ref('group', 'g')), b], 'subscription_aggregations': [agg], 'subscriptions': [sub],
     'proxy_groups': [G('g', proxies_order=[{'type': 'aggregation', 'id': 'ag1'}, {'type': 'subscription', 'id': 's1'},
                                              {'type': 'node', 'id': 'b'}, {'type': 'strategy', 'id': 'h'}]),
                      G('h', manual_nodes=['b'])]},
    {'nodes': [N('a', dialer_ref=ref('group', 'g')), b],
     'proxy_groups': [G('g', source='node', proxies=['b'])]},
    {'nodes': [N('a', dialer_ref=ref('group', 'g')), b],
     'proxy_groups': [G('g', source='strategy', proxies=['h']), G('h', manual_nodes=['b'])]},
    {'nodes': [N('a', dialer_ref=ref('group', 'g')), b],
     'proxy_groups': [G('g', source='subscription', proxies=['s1'])]},
    {'nodes': [N('a', dialer_ref=ref('group', 'g')), b],
     'proxy_groups': [G('g', manual_nodes=['b'], enabled=False)]},
    {'nodes': [N('a', dialer_ref=ref('group', 'g')), b],
     'proxy_groups': [G('g', include_groups=['h']), G('h', manual_nodes=['b'], enabled=False)]},
    {'nodes': [N('a', dialer_ref=ref('node', 'b'), enabled=False), N('b', dialer_ref=ref('node', 'c')),
               N('c', subscription_id='s1')], 'subscriptions': [sub]},
    {'nodes': [N('a', dialer_ref=ref('node', 'b'), enabled=False), N('b', params={'dialer-proxy': 'c'}), c]},
    {'nodes': [N('a', dialer_ref=ref('node', 'b')), b], 'subscriptions': [{'name': 'no id'}]},
    {'nodes': [N('a', dialer_ref=ref('node', 'b')), b], 'subscriptions': ['x']},
    {'nodes': [N('a', dialer_ref=ref('node', 'b')), b], 'subscription_aggregations': None},
    # proxy_string dialers
    {'nodes': [N('a', proxy_string='{"type":"ss","server":"h","port":1,"dialer-proxy":"b"}'), b]},
    {'nodes': [N('a', proxy_string='type: ss\nserver: h\ndialer-proxy: b\n'), b]},
    {'nodes': [N('a', proxy_string='type: ss\nserver: h\ndialer-proxy: yes\n'), b]},
    {'nodes': [N('a', proxy_string='type: ss\nserver: h\ndialer-proxy: nope\n'), b]},
    {'nodes': [N('a', proxy_string='ss://abc@host:1#x'), b]},
    {'nodes': [N('a', proxy_string='{"type":"ss","dialer-proxy":null}', params={'dialer-proxy': 'zz'}), b]},
    # chains
    {'nodes': [a, b], 'proxy_groups': [chain('ch', ref('node', 'a'), ref('node', 'b'))]},
    {'nodes': [a, b], 'proxy_groups': [G('ch', type='chain')]},
    {'nodes': [a, b], 'proxy_groups': [G('ch', type='chain', chain={'entry': ref('node', 'a')})]},
    {'nodes': [a, b], 'proxy_groups': [G('ch', type='chain', chain=[1, 2])]},
    {'nodes': [a, b], 'proxy_groups': [chain('ch', ref('node', 'a'), 'b')]},
    {'nodes': [a, b], 'proxy_groups': [chain('ch', {'type': 'x', 'id': 'a'}, ref('node', 'b'))]},
    {'nodes': [a, b], 'proxy_groups': [chain('ch', ref('node', 'a'), ref('node', 'zz'))]},
    {'nodes': [N('a', enabled=False), b], 'proxy_groups': [chain('ch', ref('node', 'a'), ref('node', 'b'))]},
    {'nodes': [N('a', enabled=False), b], 'proxy_groups': [chain('ch', ref('node', 'a'), ref('node', 'b'), enabled=False)]},
    {'nodes': [a, N('b', enabled=False)], 'proxy_groups': [chain('ch', ref('node', 'a'), ref('node', 'b'))]},
    {'nodes': [a, N('b', enabled=False)], 'proxy_groups': [chain('ch', ref('node', 'a'), ref('node', 'b'), enabled=False)]},
    {'nodes': [a, b], 'proxy_groups': [chain('ch', ref('node', 'a'), ref('node', 'a'))]},
    {'nodes': [a, b], 'proxy_groups': [chain('ch', ref('node', 'a'), ref('node', 'b'), manual_nodes=['a'])]},
    {'nodes': [a, b], 'proxy_groups': [chain('ch', ref('node', 'a'), ref('node', 'b'), manual_nodes=[])]},
    {'nodes': [a, b], 'proxy_groups': [chain('ch', ref('node', 'a'), ref('node', 'b'), **{'include-all': True})]},
    {'nodes': [a, N('b', subscription_id='s1')], 'subscriptions': [sub],
     'proxy_groups': [chain('ch', ref('node', 'a'), ref('node', 'b'))]},
    {'nodes': [a, b], 'proxy_groups': [chain('ch', ref('node', 'a'), ref('node', 'b')), G('f', follow_group='ch')]},
    {'nodes': [a, b], 'proxy_groups': [chain('ch', ref('node', 'a'), ref('node', 'b')),
                                       G('g', include_groups=['h']), G('h', manual_nodes=['a'], enabled=False)]},
    {'nodes': [a, b], 'proxy_groups': [chain('ch', ref('node', 'a'), ref('node', 'b')), G('g', include_groups=['zz'])]},
    {'nodes': [a, b], 'proxy_groups': [chain('ch', ref('node', 'a'), ref('node', 'b')),
                                       G('g', include_groups=['zz'], enabled=False)]},
    {'nodes': [a, b], 'proxy_groups': [chain('ch', ref('node', 'a'), ref('group', 'g')), G('g', manual_nodes=['b'])]},
    {'nodes': [a, b], 'proxy_groups': [chain('ch', ref('node', 'a'), ref('group', 'g')),
                                       G('g', manual_nodes=['b', 'DIRECT'])]},
    {'nodes': [a, b], 'proxy_groups': [chain('ch', ref('group', 'g'), ref('node', 'b')),
                                       G('g', manual_nodes=['a', 'DIRECT'])]},
    {'nodes': [a, b], 'proxy_groups': [chain('ch', ref('node', 'a'), ref('group', 'g')), G('g')]},
    {'nodes': [a, b], 'subscriptions': [sub],
     'proxy_groups': [chain('ch', ref('node', 'a'), ref('group', 'g')), G('g', subscriptions=['s1'])]},
    {'nodes': [a, b],
     'proxy_groups': [chain('ch', ref('node', 'a'), ref('group', 'g')), G('g', subscriptions=['s1'])]},
    {'nodes': [a, b], 'subscription_aggregations': [{'id': 'ag1', 'name': 'Agg'}],
     'proxy_groups': [chain('ch', ref('node', 'a'), ref('group', 'g')), G('g', aggregations=['ag1'])]},
    {'nodes': [a, b], 'subscription_aggregations': [agg],
     'proxy_groups': [chain('ch', ref('node', 'b'), ref('group', 'g')), G('g', aggregations=['ag1'])]},
    {'nodes': [a, b, N('c', subscription_id='s1')], 'subscriptions': [sub],
     'proxy_groups': [chain('ch', ref('node', 'a'), ref('group', 'g')), G('g', manual_nodes=['c'])]},
    {'nodes': [a, b, c], 'proxy_groups': [chain('c1', ref('node', 'a'), ref('node', 'b')),
                                          chain('c2', ref('node', 'c'), ref('group', 'g')),
                                          G('g', include_groups=['c1'])]},
    {'nodes': [a, b, c], 'proxy_groups': [chain('c1', ref('group', 'g'), ref('node', 'b')),
                                          G('g', include_groups=['c1'])]},
    {'nodes': [a, b, c], 'proxy_groups': [chain('c1', ref('group', 'c2'), ref('node', 'b')),
                                          chain('c2', ref('group', 'c1'), ref('node', 'c'))]},
    {'nodes': [a, b, c], 'proxy_groups': [chain('c1', ref('node', 'a'), ref('group', 'c2')),
                                          chain('c2', ref('node', 'c'), ref('group', 'c1'))]},
    {'nodes': [a, b], 'proxy_groups': [chain('ch', ref('node', 'a'), ref('group', 'ch'))]},
    {'nodes': [a, b], 'proxy_groups': [chain('ch', ref('node', 'a'), ref('node', 'b'), enabled=False)]},
    {'nodes': [a, N('b', dialer_ref=ref('group', 'ch'))], 'proxy_groups': [chain('ch', ref('node', 'a'), ref('node', 'b'))]},
    {'nodes': [a, b], 'proxy_groups': [chain('ch', ref('node', 'a'), ref('node', 'b')), G('ch2', 'ch')]},
    {'nodes': [a, b], 'proxy_groups': [chain('ch', ref('node', 'a'), ref('node', 'b')),
                                       chain('ch', ref('node', 'b'), ref('node', 'a'))]},
    {'nodes': 5},
    {'proxy_groups': [G('g', manual_nodes=[5])]},
]:
    case('validate_dialers', profile)

# ---- raw_dialer ----
for node in [
    {}, {'params': {}}, {'params': {'dialer-proxy': 'x'}}, {'params': {'dialer-proxy': None}},
    {'params': {'dialer-proxy': 0}}, {'params': None}, {'params': []}, {'proxy_string': ''},
    {'proxy_string': '', 'params': {'dialer-proxy': 'p'}},
    {'proxy_string': 'ss://x', 'params': {'dialer-proxy': 'p'}},
    {'proxy_string': '{"type":"ss","dialer-proxy":"j"}', 'params': {'dialer-proxy': 'p'}},
    {'proxy_string': '{"type":"ss"}'},
    {'proxy_string': '{"dialer-proxy":"j"}'},
    {'proxy_string': 'type: vmess\ndialer-proxy: [a, b]'},
    {'proxy_string': 'type: vmess\ndialer-proxy: 12'},
    {'proxy_string': 5},
    {'proxy_string': ['x']},
]:
    case('raw_dialer', node)

# ---- _parse_structured_proxy_string ----
for text in [
    '', '   ', 'ss://YWVzLTI1Ni1nY206cGFzcw@host:8388#name', 'vmess://eyJ2IjoiMiJ9', '  trojan://pw@h:443  ',
    'hysteria2://pw@h:443\nsecond', 'http://x y://z', 'a b://c', 'name: x://y', 'url: http://x\ntype: ss',
    'type: ss\nurl: http://x', 'key:://x', 'a:b://c', 'YWJjZGVm', 'c3M6Ly9hYmM=',
    '{"type":"ss","server":"h","port":8388}', ' {"type":"ss"} ', '{"type":""}', '{"type":0}', '{"type":null}',
    '{"name":"x"}', '{"type":"ss",}', '{type: ss}', '{type: ss, server: h, port: 1}', "{'type': 'ss'}",
    '{"type":"ss","nested":{"a":[1,2.5,true,null]}}', '[1,2]', '"type"', 'type', 'type:', 'type: ', 'type: ~',
    'type: null', 'type: ss', 'type: ss\nserver: 1.2.3.4\nport: 443\nudp: true', 'type: yes', 'type: no',
    'type: Off', 'type: on', 'type: y', 'type: n', 'type: 0', 'type: 00', 'type: 0.0', 'type: 010', 'type: 0x1F',
    'type: 0b101', 'type: 1_000', 'type: 1:30', 'type: 1:30.5', 'type: 1e3', 'type: 1.5e+3', 'type: 1.5e3', 'type: .5',
    'type: 5.', 'type: +12', 'type: -0', 'type: .inf', 'type: -.INF', 'type: "0"', "type: '0'", 'type: 0o17',
    'type: 08', 'type: 1__0', 'type: !!str 0', 'type: !!int "12"', 'type: !!float 1', 'type: !!bool yes',
    'type: !!null x', 'type: !custom x', 'type: =', '=: 1\ntype: ss',
    'type: ss\nport: 0777\nalpn: [h2, http/1.1]\nws-opts: {path: /, headers: {Host: a.b}}',
    'type: ss\ntype: vmess', 'type: ss\nport: 1\ntype: ""', 'type: ss\n1: a\n~: c\n1.5: d',
    'base: &b {type: ss, port: 1}\n<<: *b', 'base: &b {type: ss, port: 1}\n<<: *b\nport: 2\nname: n',
    'a: &a {type: x, p: 1}\nb: &b {type: y, q: 2}\n<<: [*a, *b]\nz: 1',
    'a: &a {type: x}\n<<: *a\n<<: {type: w}', 'a: &a [1]\n<<: *a\ntype: t', '<<: 5\ntype: t', '"<<": {type: x}',
    'type: &t ss\nother: *t', 'type: *missing', '- type: ss', 'type: ss\n---\ntype: vmess',
    '--- \ntype: ss\n...', '---\ntype: ss\n---', 'type: ss\n  bad: indent', 'type: ss\n\tport: 1', 'type: [unclosed',
    'type: "unclosed', '? [a, b]\n: c\ntype: x', '? {a: b}\n: c\ntype: x', 'type: |\n  ss\n', 'type: >\n  a\n  b\n',
    'type: ss\nname: "\\u8282\\u70b9 \\x41"', "type: ss\nname: 'it''s'", 'type: ss # comment', '# only comment',
    'type: ss\nserver: 2001:db8::1', 'type: ss\nwhen: 2024-01-02', 'type: ss\nports: 8000-9000',
    'type: ss\nname: 节点 🚀', 'type: ss\nx: \x01', 'type: ss\nkey:value', '{"type":"ss","a":1}\nfoo', '{"type": "ss", "a": [}',
    '{type: ss, 1: a}', 'type: ss\nempty:\nlist:\n- a\n- b: c\n  d: e', 'TYPE: ss', 'type : ss',
]:
    case('parse_proxy_string', text)

# ---- validate_health ----
for values, provider in [
    ({}, True), ({}, False), (None, True), ([], False), ('x', False),
    ({'enable': True, 'url': 'https://www.gstatic.com/generate_204', 'interval': 300, 'timeout': 5000,
      'lazy': True, 'expected-status': '204'}, True),
    ({'tolerance': 50}, True), ({'unknown': 1}, True), ({'unknown': 1}, False), ({'enable': 'x'}, False),
    ({'enable': 1}, True), ({'lazy': 0}, False), ({'lazy': None}, True),
    ({'interval': 0}, False), ({'interval': 1}, False), ({'interval': 86400}, False), ({'interval': 86401}, False),
    ({'interval': True}, False), ({'interval': 5.0}, False), ({'interval': '5'}, False),
    ({'interval': 10 ** 30}, False), ({'timeout': 600000}, True), ({'timeout': 600001}, True), ({'timeout': 0}, True),
    ({'tolerance': 0}, False), ({'tolerance': -1}, False), ({'tolerance': 600000}, False), ({'tolerance': 600001}, False),
    ({'expected-status': '204'}, False), ({'expected-status': '200-299/304'}, False), ({'expected-status': 204}, False),
    ({'expected-status': '20'}, False), ({'expected-status': '200-'}, False), ({'expected-status': '299-200'}, False),
    ({'expected-status': '099'}, False), ({'expected-status': '600'}, False), ({'expected-status': '200/'}, False),
    ({'expected-status': '200-300-400'}, False), ({'expected-status': '204\n'}, False),
    ({'expected-status': '２０４'}, False), ({'expected-status': '100-599/200/300-301'}, True),
] + [({'url': url}, False) for url in [
    'http://example.com', 'https://example.com/path?q=1', 'HTTP://EXAMPLE.COM', 'ftp://example.com', 'example.com',
    '//example.com', 'http:example.com', 'http:///path', 'http://', 'http://:80', 'http://example.com:80',
    'http://example.com:0', 'http://example.com:65535', 'http://example.com:65536', 'http://example.com:',
    'http://example.com:abc', 'http://example.com:8a', 'http://example.com:-1', 'http://example.com:00080',
    'http://example.com:999999999999999999999', 'http://user@example.com', 'http://user:pw@example.com',
    'http://@example.com', 'http://:@example.com', 'http://example.com#frag', 'http://example.com#',
    'http://example.com/#', 'http://example.com?x#y', 'http://exa mple.com', 'http://example.com/\tx',
    'http://example.com/\x7f', ' http://example.com', 'http://example.com\u3000', 'http://example.com\x1f',
    'http://[::1]', 'http://[::1]:8080', 'http://[::1', 'http://::1]', 'http://[::1]:x', 'http://[1.2.3.4]',
    'http://[v1.fe]', 'http://[vx]', 'http://[fe80::1%25eth0]', 'http://[]', 'http://[zz]', 'http://[::1]abc:80',
    'http://例子.测试', 'http://example.com／x', 'http://example.com：80', 'http://exa℀mple.com', 'http://ｅxample.com',
    'http://a@b@c', 'http://a:b:c', 'http://example.com:80:90', 'https://example.com:443/generate_204',
    '1http://example.com', 'ht+tp://example.com', 'http://example.com\\@evil', 'http://example.com:٣',
    'http://example.com/a b', '', 5, None, 'http://EXAMPLE.com:', 'http://?x', 'http://#x', 'http:///',
    'http://%41', 'http://-', 'http://.', 'http://a[::1]', 'http://[::1]@host', 'http://u@[::1]:80',
    'http://[x]@host/', 'http://[::1]]', 'http://[[::1]]', 'http://[::1]:80@h:1', 'http://h/[x]', 'http://[::ffff:1.2.3.4]',
]]:
    case('validate_health', {'values': values, 'provider': provider})

# ---- validate_refresh_interval ----
for value in [0, 60, 59, 1, -1, 604800, 604801, 3600, True, False, None, '60', 60.0, 10 ** 40, [], {}]:
    case('validate_refresh_interval', value)

# ---- normalize_client_range ----
RANGES = [
    '192.168.1.10', ' 192.168.1.10 ', '192.168.1.10/32', '192.168.1.10/24', '192.168.1.0/24', '10.0.0.0/8', '0.0.0.0/0',
    '0.0.0.0', '0.0.0.0/32', '0.0.0.0/31', '255.255.255.255', '224.0.0.1', '224.0.0.0/4', '224.0.0.0/3', '239.1.2.3/24',
    '192.168.01.1', '192.168.1', '192.168.1.1.1', '256.1.1.1', '1.2.3.4/33', '1.2.3.4/', '1.2.3.4/a', '1.2.3.4/024',
    '1.2.3.4/0032', '1.2.3.4/24/1', '1.2.3.4 /24', '1.2.3.4/ 24', '00.0.0.0', '0.0.0.00', '1.2.3.+4', '1.2.3.4.',
    '.1.2.3.4', '1..2.3', '::', '::/0', '::/128', '::1', '::1/128', '::1/127', 'fe80::1', 'FE80::1', 'fe80::1/64',
    '2001:db8::/32', '2001:DB8:0:0:0:0:0:1', '2001:db8:0:0:1:0:0:1', '2001:0db8:0000:0000:0000:0000:0000:0001',
    '1:2:3:4:5:6:7:8', '1:2:3:4:5:6:7::', '::2:3:4:5:6:7:8', '1::3:4:5:6:7:8', '1:2:3:4:5:6:7:8:9', '1:2:3:4:5:6:7',
    '1::2::3', ':1::2', '1::2:', ':::', '12345::', 'g::1', '::ffff:192.168.1.1', '::FFFF:192.168.1.1/128',
    '::ffff:192.168.1.0/120', '::ffff:192.168.1.1/96', '::ffff:192.168.1.1/95', '::ffff:0.0.0.0', '::ffff:0.0.0.0/96',
    '::ffff:224.0.0.1', '::ffff:c0a8:101', '::192.168.1.1', '::192.168.1.1/128', '1:2:3:4:5:6:1.2.3.4',
    '1:2:3:4:5:6:7:1.2.3.4', '::1.2.3', '::01.2.3.4', 'ff02::1', 'ff00::/8', 'ff00::/7', 'fe80::1%eth0', 'fe80::1/129',
    '2001:db8::1/0', '0:0:0:0:0:0:0:0', '0:0:0:0:0:0:0:1', '1:0:0:2:0:0:0:3', '1:0:0:0:2:0:0:3', '0:0:1::', '::1:0:0',
    '', ' ', '/', '/24', 'abc', '1', 'ff', '1.2.3.4\n', '１.2.3.4', '1.2.3.4/２４', '::ffff:1.2.3.4/127',
    '64:ff9b::1.2.3.4', '100::/64', 'a:b:c:d:e:f:0:1', '::ffff:ffff:ffff', '0::ffff:1.2.3.4', '::fffe:1.2.3.4',
]
for raw in RANGES + [None, 5, ['1.2.3.4'], True]:
    case('normalize_client_range', raw)

# ---- normalize_notes ----
for value in [
    {}, None, [], 'x', {'192.168.1.2': 'PC'}, {'192.168.1.2': '  PC  '}, {'192.168.1.2': ''},
    {' 192.168.1.2': 'a', '192.168.1.3': 'b'}, {'192.168.1.2': 'a', '192.168.1.2/32': 'b'},
    {'::ffff:192.168.1.2': 'a', '192.168.1.2': 'b'}, {'192.168.1.0/24': 'LAN', 'fe80::1': 'v6'},
    {'bad': 'a'}, {'192.168.1.2': None}, {'192.168.1.2': 5}, {'192.168.1.2': 'a\nb'}, {'192.168.1.2': 'a\x7fb'},
    {'192.168.1.2': 'x' * 80}, {'192.168.1.2': 'x' * 81}, {'192.168.1.2': '备' * 80}, {'192.168.1.2': '🚀' * 80},
    {'192.168.1.2': '🚀' * 81}, {'192.168.1.2': '\u3000名\u3000'},
    {f'10.{i // 256}.{i % 256}.1': 'n' for i in range(2048)},
    {f'10.{i // 256}.{i % 256}.1': 'n' for i in range(2049)},
]:
    case('normalize_notes', value)

# ---- validate_bindings ----
GOOD = {'router_id': 'r1', 'vendor': 'routeros', 'id': '*1', 'ip': '192.168.1.2', 'mac': 'AA:BB:CC:DD:EE:FF',
        'comment': 'cf-client:pc'}


def binding(**changes):
    result = dict(GOOD)
    for key, value in changes.items():
        if value is ...:
            result.pop(key, None)
        else:
            result[key] = value
    return result


for value in [
    {}, None, [], {'192.168.1.2': GOOD}, {'192.168.1.2': binding(vendor='ikuai')},
    {'192.168.1.2': binding(server='dhcp1', interface='bridge', gateway='192.168.1.1')},
    {'192.168.1.2': binding(extra='x')}, {'192.168.1.2': binding(router_id=...)}, {'192.168.1.2': binding(comment=...)},
    {'192.168.1.3': GOOD}, {'192.168.01.2': binding(ip='192.168.01.2')}, {' 192.168.1.2 ': binding(ip=' 192.168.1.2 ')},
    {'::1': binding(ip='::1')}, {'192.168.1.0/24': binding(ip='192.168.1.0/24')}, {'0.0.0.0': binding(ip='0.0.0.0')},
    {'192.168.1.2': binding(vendor='openwrt')}, {'192.168.1.2': binding(vendor=None)},
    {'192.168.1.2': binding(router_id='')}, {'192.168.1.2': binding(id='')}, {'192.168.1.2': binding(id=5)},
    {'192.168.1.2': binding(server=None)}, {'192.168.1.2': binding(server='x' * 512)},
    {'192.168.1.2': binding(server='x' * 513)}, {'192.168.1.2': binding(server='a\tb')},
    {'192.168.1.2': binding(server='a\x7fb')}, {'192.168.1.2': binding(mac='aa:bb:cc:dd:ee:ff')},
    {'192.168.1.2': binding(mac='AA-BB-CC-DD-EE-FF')}, {'192.168.1.2': binding(mac='A:B:C:D:E:F')},
    {'192.168.1.2': binding(mac='0A:0B:0C:0D:0E:0F')}, {'192.168.1.2': binding(mac='AA:BB:CC:DD:EE')},
    {'192.168.1.2': binding(mac='AA:BB:CC:DD:EE:GG')}, {'192.168.1.2': binding(mac='')},
    {'192.168.1.2': binding(mac=' AA:BB:CC:DD:EE:FF')}, {'192.168.1.2': binding(mac='+A:BB:CC:DD:EE:FF')},
    {'192.168.1.2': binding(comment='pc')}, {'192.168.1.2': binding(comment='cf-client:')},
    {'192.168.1.2': 'x'}, {'192.168.1.2': None}, {'192.168.1.2': GOOD, '192.168.1.9': binding(ip='192.168.1.9', mac='x')},
    {f'10.{i // 256}.{i % 256}.1': 1 for i in range(2049)},
]:
    case('validate_bindings', value)

# ---- normalize_action ----
UP = {'addr': 'udp://1.1.1.1', 'bootstrap': '223.5.5.5'}
for value in [
    None, 'refused', {}, {'action': 'refused'}, {'action': 'nxdomain'}, {'action': 'nodata'}, {'action': 'blackhole'},
    {'action': 'direct'}, {'action': 'proxy'}, {'action': 'other'}, {'action': None}, {'action': ['refused']},
    {'action': 'refused', 'x': 1}, {'action': 'direct', 'source': 'local'},
    {'action': 'upstream'}, {'action': 'upstream', 'source': 'local', 'upstream': UP},
    {'action': 'upstream', 'source': 'remote', 'upstream': UP}, {'action': 'upstream', 'source': 'fallback', 'upstream': UP},
    {'action': 'upstream', 'source': 'other', 'upstream': UP}, {'action': 'upstream', 'source': None, 'upstream': UP},
    {'action': 'upstream', 'source': 'local', 'upstream': UP, 'x': 1}, {'action': 'upstream', 'source': 'local'},
    {'action': 'upstream', 'source': 'local', 'upstream': 'udp://1.1.1.1'},
    {'action': 'upstream', 'source': 'local', 'upstream': {}},
    {'action': 'upstream', 'source': 'local', 'upstream': {'addr': '  '}},
    {'action': 'upstream', 'source': 'local', 'upstream': {'addr': 5}},
    {'upstream': {'z': 1, 'addr': 'tls://dns.example'}, 'source': 'local', 'action': 'upstream'},
]:
    for parked in (False, True):
        case('normalize_action', {'value': value, 'parked': parked})

# ---- hydrate_ruleset ----
LIB = {'id': 'lib1', 'name': 'Ads', 'url': 'https://example.com/a.list', 'behavior': 'classical',
       'source_type': 'url', 'format': 'text', 'enabled': False, 'extra': 1}
for item, library, profile_id in [
    ({'id': 'r1', 'itemType': 'ruleset', 'library_rule_id': 'lib1', 'policy': 'P'}, LIB, 'default'),
    ({'name': 'old', 'id': 'r1', 'url': 'u', 'library_enabled': True, 'policy': 'P', 'behavior': 'ipcidr'}, LIB, 'p2'),
    ({'id': 'r1'}, {'id': 'lib2', 'name': 'N'}, 'p'),
    ({'id': 'r1', 'behavior': 'ipcidr'}, {'id': 'lib2', 'name': 'N'}, 'p'),
    ({'id': 'r1', 'url': 'keep-position', 'z': 1}, {'id': 'lib3', 'name': 'C', 'source_type': 'content', 'content': 'a\nb'}, 'p3'),
    ({'id': 'r1'}, {'id': 'lib3', 'name': 'C', 'source_type': 'content', 'content': 'a', 'url': 'x'}, ''),
    ({'id': 'r1'}, {'id': 'lib3', 'name': 'C', 'source_type': 'content'}, None),
    ({'id': 'r1'}, {'id': 7, 'name': 'C', 'source_type': 'content', 'enabled': None}, 'p'),
    ({'id': 'r1'}, {'name': 'C', 'source_type': 'content'}, 'p'),
    ({'id': 'r1'}, {'id': 'l', 'name': {'deep': [1]}, 'enabled': {'x': 1}}, 'p'),
]:
    case('hydrate_ruleset', {'item': item, 'library': library, 'profile_id': profile_id})

# ---- sanitize_rule_name ----
for name in ['', None, 'Ads', 'a/b\\c:d*e?f"g<h>i|j', '  .name. ', '...', ' . ', '. /. ', 'x' * 250, '规' * 201,
             '🚀' * 201 + '.', ' ' + 'y' * 200 + '.', 'a\tb', '\tname\t', 'a.b', '/']:
    case('sanitize_rule_name', name)

# ---- rule_cache_filename ----
for rule in [
    {}, {'name': 'Ads'}, {'id': 'r1'}, {'id': 'r1', 'library_rule_id': 'lib1'}, {'id': 'r1', 'library_rule_id': ''},
    {'id': '', 'name': 'n'}, {'id': 0, 'name': 'n'}, {'id': 5}, {'id': None, 'name': '规则 🚀'},
    {'id': 'r1', 'url': 'https://example.com/a.list'}, {'id': 'r1', 'url': 'https://example.com/a.list', 'source_type': 'url'},
    {'id': 'r1', 'url': 'https://example.com/a.list', 'source_type': 'content'},
    {'id': 'r1', 'url': '', 'source_type': 'url'}, {'id': 'r1', 'url': 'https://例子.test/规则.list', 'behavior': 'ipcidr'},
    {'id': 'r1', 'url': 'u', 'behavior': 'classical', 'format': 'yaml'}, {'id': 'r1', 'url': 'u', 'format': None},
    {'id': 'r1', 'url': 'u', 'format': ''}, {'id': 'r1', 'url': 'u', 'behavior': None},
    {'name': 'n', 'url': 'u'}, {'id': 12, 'url': 'u "quoted" \\ /'},
    {'library_rule_id': 'lib1', 'id': 'r1', 'url': 'u', 'source_type': None},
]:
    case('rule_cache_filename', rule)


def main():
    results = []
    for fn, value in CASES:
        record = {'fn': fn, 'input': value, 'raised': False, 'ok': None, 'error_class': None, 'error': None}
        try:
            result = FUNCTIONS[fn](copy.deepcopy(value))
            # A YAML timestamp (a date object) is written as [text], see compat_proxystring.go.
            record['ok'] = json.loads(json.dumps(result, default=lambda o: [str(o)]))
        except Exception as error:  # noqa: BLE001 - the class is the expectation
            record.update(raised=True, ok=None, error_class=type(error).__name__, error=str(error))
        results.append(record)
    lines = ',\n'.join(json.dumps(record, ensure_ascii=False) for record in results)
    text = '{"python": %s, "cases": [\n%s\n]}\n' % (json.dumps(sys.version.split()[0]), lines)
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(text, encoding='utf-8')
    else:
        sys.stdout.write(text)
    print(f'{len(results)} cases', file=sys.stderr)


if __name__ == '__main__':
    main()
