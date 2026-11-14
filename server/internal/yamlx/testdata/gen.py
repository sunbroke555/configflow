#!/usr/bin/env python3
"""Generate corpus.jsonl: what PyYAML 6 (the production version, CPython 3.11) does.

The Go tests in this package assert byte equality with every recorded output:

    cd <repo root> && PYTHONPATH=. ~/cf-test/.venv/bin/python server/internal/yamlx/testdata/gen.py

The output is deterministic (fixed seed). One JSON object per line (ASCII only):

    kind=dump     input (JSON text) and the yaml text each option set produces; "csafe"
                  is CSafeDumper's text for the safe_u set, recorded for information only
    kind=spec     value specs with shared references / flow markers (see build()) dumped
                  with the option set named in "options"
    kind=load     YAML text and what yaml.safe_load builds (as tagged JSON, see enc())
                  or the exception class and message; "native" is the same for
                  oxidns_native.NativeLoader
"""

import datetime
import json
import math
import random
import sys
from pathlib import Path

import yaml

assert sys.version_info[:2] == (3, 11), sys.version
assert yaml.__version__.startswith('6.0'), yaml.__version__

from backend.agents.docker_ha_compact import CompactDumper, FlowList, FlowMap, ImageName  # noqa: E402
from backend.common.oxidns_native import NativeLoader  # noqa: E402
from backend.converters.mihomo import IndentDumper  # noqa: E402

R = random.Random(20261114)

STRINGS = [
    '', ' ', 'a', 'id', 'name', 'port', 'z', 'A', 'Z', '_x', 'with space', 'k.e/y', 'a-b', 'a_b',
    'yes', 'Yes', 'YES', 'no', 'No', 'on', 'off', 'On', 'OFF', 'true', 'True', 'false', 'FALSE', 'y', 'n', 'Y', 'N',
    'null', 'Null', 'NULL', '~', 'nil', 'None',
    '0', '1', '-1', '+1', '123', '007', '010', '09', '0x1f', '0X1F', '0b101', '0o17', '1_000', '1__0', '1e3', '1E3', '1e+3',
    '1.5', '1.', '.5', '-.5', '+.5', '1.0e+3', '1.0e3', '1e-5', '.inf', '-.inf', '.Inf', '.nan', '.NaN', 'inf', 'nan', 'NaN',
    '12:30:45', '1:30', '190:20:30', '1:30:45.5', '-1:30', '0:0',
    '2001-01-01', '2001-1-1', '2001-12-14 21:59:43.10 -5', '2001-12-14t21:59:43.10-05:00', '2001-12-14 21:59:43', '12345-01-01',
    '<<', '<', '=', '!', '&a', '*a', '&', '*', '!tag', '!!str',
    '- a', '-a', '-', '--', '---', '--- a', '...', '.... x', '? a', '?a', '?', ': a', ':a', ':', 'a: b', 'a:b', 'a:', 'a :b',
    'a #b', 'a#b', '#c', ' #d', 'a,b', ',a', 'a, b', '[a]', '[', ']', '{a}', '{', '}', 'a{b}c', 'a[b]c', '|', '>', '|-', '>+',
    '@x', '`x', '%x', '%', "'q'", '"q"', "it's", 'say "hi"', "both ' and \"", '\\', 'a\\b', 'C:\\path\\x',
    'http://x:80/path?q=1#frag', 'host:8080', 'a=b', 'a;b', 'a!b', 'a|b', 'a>b', 'a*b', 'a&b', 'a%b', 'a@b', 'a`b', 'a$b',
    'a\nb', 'a\n', '\na', '\n', '\n\n', 'a\n\nb', 'a\n\n\nb', 'a \nb', 'a\n b', 'a\n  b', ' a\nb', 'a\nb\n', 'a\r\nb', 'a\rb', 'a\tb', '\ta', 'a\t',
    'line1\nline2\nline3', 'multi\nline\n\ntext with: colon', '  leading', 'trailing  ', ' both ', 'two  spaces', 'a   b   c',
    '例子', '节点 🚀', '中文: 带冒号', '- 中文', '#中文', 'ключ', 'é', 'ǅ', '\u00a0nbsp', 'a\u00a0b', 'line\u2028sep', 'para\u2029sep',
    'zw\u200bsp', '\u0085next', 'c1\x85x', '\ufeffbom', 'x\ufeff', '𝄞 clef', '\ue000 private', '\ufffe nonchar', '\uffff', '\ufffd repl',
    '🇨🇳', 'e\u0301 combining', '\U0010ffff', '\U0010fffe', 'ÿ', 'Ā', '😀😀',
    'bell\x07', 'nul\x00', 'esc\x1b', 'us\x1f', 'del\x7f', 'bs\x08ff\x0c', 'vt\x0b', 'cr\rend', 'c0\x01', 'x\x80y', 'x\x9fy',
    'ssh-rsa AAAAB3NzaC1yc2E user@host', 'DOMAIN-SUFFIX,example.com,Proxy', 'IP-CIDR,10.0.0.0/8,DIRECT,no-resolve',
    'https://doh.example/dns-query', 'tls://1.1.1.1:853', '1.1.1.1', '2001:db8::1', '::1', '[::1]:53', 'fe80::1%eth0',
    '/etc/mihomo/config.yaml', '${HOME}/x', '$$x', '$', 'a$$b', '{{ template }}', '%(x)s', '<html>', '\\u00e9',
    'x' * 70, 'x' * 79, 'x' * 80, 'x' * 81, 'x' * 200, ('word ' * 30).strip(), ('word ' * 30), ('ab ' * 40).strip() + ': tail',
    ('长' * 50), ('长 ' * 50).strip(), 'a ' + 'b' * 100 + ' c', ('x' * 100 + '\n') * 3, ('y ' * 45).strip() + '\n' + ('z ' * 45).strip(),
    'k' * 127, 'k' * 128, 'k' * 129, 'a b ' * 40, '"' * 3, "'" * 3, ' ' * 100, ('ab' * 45) + ' ' + ('cd' * 45),
    ('\\' * 60) + ' ' + ('é' * 60),
]
KEYS = ['a', 'b', 'id', 'name', 'port', 'proxies', 'rules', 'z', 'type', 'server', 'x-y', 'yes', 'no', 'on', '1', '10', '123',
        '例子', 'ключ', 'k e y', 'with: colon', '- dash', '?q', '#h', '', 'null', '~', 'true', '<<', '=', 'ü', 'Z', 'B', 'AA', 'aa', '_',
        'x' * 130, 'a\nb', '\ttab', 'quote\'s', '[x]', '{y}', '*s', '&t', '!u']
INTS = [0, 1, -1, 2, 7, 80, 443, 65535, -32768, 2 ** 31, 2 ** 53, 2 ** 63 - 1, 2 ** 64, 10 ** 22, -(10 ** 30),
        123456789012345678901234567890, 8080, 7890, 9090]
FLOATS = [0.0, -0.0, 1.0, -1.0, 1.5, 0.1, 0.2, 0.1 + 0.2, 1 / 3, 2.5e-5, 1e-4, 1e-5, 0.00012345, 1e15, 1e16, 1e17,
          123456789012345680.0, 1e22, 1e23, 1.7976931348623157e308, 5e-324, 3.141592653589793, -2.718281828459045, 100.0,
          1e100, 1.5e300, 4.35, 0.5, 2.0, 1e21, 12345678.9, 0.001, 1e-7,
          float('inf'), float('-inf'), float('nan')]


def scalar_pool():
    return (R.choice(STRINGS), R.choice(INTS), R.choice(FLOATS), True, False, None)


def random_value(depth=0):
    kind = R.random()
    if depth > 4 or kind < 0.45:
        pool = scalar_pool()
        weights = [5, 2, 2, 1, 1, 1]
        return R.choices(pool, weights)[0]
    if kind < 0.72:
        return [random_value(depth + 1) for _ in range(R.choice([0, 1, 1, 2, 3, 4, 6]))]
    out = {}
    for _ in range(R.choice([0, 1, 2, 3, 4, 5])):
        out[R.choice(KEYS)] = random_value(depth + 1)
    return out


def curated():
    """Shapes the application writes: mihomo/oxidns-like documents and rule lists."""
    proxy = {'name': '节点 🚀 HK-01', 'type': 'ss', 'server': 'hk.example.com', 'port': 8388, 'cipher': 'aes-256-gcm',
             'password': 'p@ss:w0rd #1', 'udp': True, 'plugin-opts': {'mode': 'tls', 'host': 'x.example'}}
    tun = {'enable': True, 'stack': 'mixed', 'dns-hijack': ['any:53', 'tcp://any:53'], 'auto-route': False,
           'route-address': ['0.0.0.0/1', '128.0.0.0/1'], 'mtu': 9000, 'gso-max-size': 65536}
    dns = {'enable': True, 'ipv6': False, 'listen': '0.0.0.0:1053', 'enhanced-mode': 'fake-ip',
           'fake-ip-range': '198.18.0.1/16', 'fake-ip-filter': ['*.lan', '+.local', 'time.*.com'],
           'nameserver': ['https://doh.pub/dns-query', 'tls://223.5.5.5:853'], 'fallback': [], 'default-nameserver': ['223.5.5.5'],
           'nameserver-policy': {'geosite:cn': ['223.5.5.5'], '+.example.com': '1.1.1.1'}, 'proxy-server-nameserver': {}}
    doc = {'mixed-port': 7890, 'allow-lan': True, 'mode': 'rule', 'log-level': 'info', 'external-controller': '0.0.0.0:9090',
           'secret': '', 'tun': tun, 'dns': dns, 'proxies': [proxy, dict(proxy, name='b', port=443)],
           'proxy-groups': [{'name': '🚀 节点选择', 'type': 'select', 'proxies': ['DIRECT', 'a', 'b']},
                            {'name': 'auto', 'type': 'url-test', 'url': 'http://www.gstatic.com/generate_204', 'interval': 300,
                             'tolerance': 50, 'proxies': []}],
           'rule-providers': {'ads': {'type': 'http', 'behavior': 'domain', 'url': 'https://x.example/a.yaml',
                                      'path': './ruleset/ads.yaml', 'interval': 86400}},
           'rules': ['DOMAIN-SUFFIX,google.com,🚀 节点选择', 'GEOIP,CN,DIRECT', 'MATCH,🚀 节点选择']}
    oxidns = {'log': {'level': 'info', 'file': ''}, 'plugins': [
        {'tag': 'cache', 'type': 'cache', 'args': {'size': 8192, 'lazy_cache_ttl': 3600}},
        {'tag': 'main', 'type': 'sequence', 'args': [{'exec': 'query_summary x'}, {'matches': ['qname $ads'], 'exec': 'reject 3'}]},
        {'tag': 'udp', 'type': 'udp_server', 'args': {'entry': 'main', 'listen': ':53'}}]}
    return [doc, oxidns, {'payload': ['+.google.com', 'DOMAIN,x.example', '192.168.0.0/16', '例子.cn']}, {'proxies': [proxy]},
            {'services': {'agent': {'image': 'thsrite/config-flow-agent:latest', 'environment': {'AGENT_PORT': '8080', 'X': '$$y'},
                                    'volumes': ['/etc/machine-id:/etc/machine-id:ro'], 'cap_add': ['NET_ADMIN']}}},
            [], {}, [[]], [{}], {'a': []}, {'a': {}}, [[1, 2], [3, [4, 5]]], [{'a': 1}, {'b': [1, 2]}], {'a': [{'b': [{'c': 1}]}]}]


def build_inputs():
    values = [s for s in STRINGS] + [i for i in INTS] + [f for f in FLOATS] + [True, False, None]
    values += [[s] for s in STRINGS] + [{'k': s} for s in STRINGS] + [{s: 1} for s in KEYS] + [[s, s] for s in STRINGS[:60]]
    values += curated()
    for _ in range(420):
        values.append(random_value())
    return values


# -- option sets -------------------------------------------------------------

OPTION_SETS = {
    'safe': lambda v: yaml.safe_dump(v),
    'safe_u': lambda v: yaml.safe_dump(v, allow_unicode=True, sort_keys=False),
    'safe_u_sorted': lambda v: yaml.safe_dump(v, allow_unicode=True),
    'indent': lambda v: yaml.dump(v, Dumper=IndentDumper, allow_unicode=True, default_flow_style=False, sort_keys=False, indent=2),
    'indent_sorted': lambda v: yaml.dump(v, Dumper=IndentDumper, default_flow_style=False),
    'ruleset': lambda v: yaml.safe_dump(v, allow_unicode=True, sort_keys=False, default_flow_style=False, width=2 ** 31 - 1),
    'compose': lambda v: yaml.dump(v, Dumper=CompactDumper, sort_keys=False, allow_unicode=True, width=1000),
    'flow_true': lambda v: yaml.safe_dump(v, allow_unicode=True, sort_keys=False, default_flow_style=True),
    'flow_auto': lambda v: yaml.safe_dump(v, allow_unicode=True, default_flow_style=None),
    'indent4_w30': lambda v: yaml.safe_dump(v, allow_unicode=True, sort_keys=False, indent=4, width=30),
    'explicit': lambda v: yaml.safe_dump(v, allow_unicode=True, explicit_start=True, explicit_end=True),
}


def dump_all(value):
    out = {}
    for name, fn in OPTION_SETS.items():
        try:
            out[name] = fn(value)
        except Exception as exc:  # recorded, the Go side must fail too
            out[name] = {'error': type(exc).__name__}
    try:
        out['csafe'] = yaml.dump(value, Dumper=yaml.CSafeDumper, allow_unicode=True, sort_keys=False)
    except Exception as exc:
        out['csafe'] = {'error': type(exc).__name__}
    return out


# -- specs with sharing and flow markers --------------------------------------

def build(spec, env):
    if isinstance(spec, list):
        return [build(item, env) for item in spec]
    if isinstance(spec, dict):
        if '$ref' in spec:
            return env[spec['$ref']]
        if '$define' in spec:
            env[spec['$define']] = build(spec['value'], env)
            return env[spec['$define']]
        if '$flowmap' in spec:
            return FlowMap({k: build(v, env) for k, v in spec['$flowmap'].items()})
        if '$flowlist' in spec:
            return FlowList([build(v, env) for v in spec['$flowlist']])
        if '$image' in spec:
            return ImageName(spec['$image'])
        return {k: build(v, env) for k, v in spec.items()}
    return spec


def D(name, value):
    return {'$define': name, 'value': value}


def ref(name):
    return {'$ref': name}


SPECS = [
    ('safe_u', {'a': D('x', {'k': 1}), 'b': ref('x')}),
    ('safe_u', [D('x', [1, 2]), ref('x'), ref('x')]),
    ('safe_u', [D('x', {}), ref('x')]),
    ('safe_u', [D('x', []), ref('x')]),
    ('safe_u', {'a': D('p', {'n': 'v'}), 'b': D('q', [1]), 'c': ref('q'), 'd': ref('p'), 'e': [ref('p'), ref('q')]}),
    ('safe_u', [D('x', {'inner': D('y', [1])}), ref('y'), ref('x')]),
    ('safe_u', {'k': [D('x', {'a': 1}), {'w': ref('x')}], 'z': ref('x')}),
    ('safe', {'b': D('x', [1]), 'a': ref('x')}),
    ('indent', {'proxies': [D('n', {'name': 'a', 'port': 1}), ref('n')], 'g': {'proxies': [ref('n')]}}),
    ('safe_u', {'a': D('s', 'text'), 'b': ref('s')}),
    ('safe_u', [D('d', {'a': D('e', [1, 2])}), ref('e')]),
    ('compose', {'a': {'$flowmap': {'x': 1, 'y': 'z'}}, 'b': {'$flowlist': ['NET_ADMIN', 'NET_RAW']}}),
    ('compose', {'logging': {'driver': 'json-file', 'options': {'$flowmap': {'max-size': '10m', 'max-file': '3'}}}}),
    ('compose', {'bind': {'$flowmap': {'create_host_path': False}}, 'cap_add': {'$flowlist': []}, 'e': {'$flowmap': {}}}),
    ('compose', {'$flowlist': [{'$flowmap': {'a': [1, 2]}}, {'$flowlist': [1, {'b': 2}]}]}),
    ('compose', {'services': {'a': {'image': D('img', {'$image': 'thsrite/config-flow-agent:latest'}), 'n': 1},
                              'b': {'image': ref('img'), 'volumes': [{'type': 'bind', 'source': '/x', 'target': '/y',
                                                                      'bind': {'$flowmap': {'create_host_path': True}}}]}}}),
    ('compose', {'a': D('img', {'$image': 'x:1'}), 'b': ref('img'), 'c': ref('img')}),
    ('compose', {'a': {'$image': 'plain'}, 'b': {'$image': 'plain'}}),
    ('compose', {'a': {'$flowlist': [1]}, 'b': [1, 2], 'c': {'k': [1]}}),
    ('compose', {'k': {'$flowmap': {'a b': 'c d', 'long': 'x' * 400, 'z': ['p', 'q']}}}),
    ('compose', {'k': {'$flowlist': ['x' * 300, 'y ' * 300, 'z']}}),
]


# -- loading --------------------------------------------------------------------

def enc(value):
    """JSON-able form of a loaded value; ints and floats stay distinguishable through json."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError('nonstring')
            out[key] = enc(item)
        return out
    if isinstance(value, list):
        return [enc(item) for item in value]
    if isinstance(value, datetime.datetime):
        return {'$ts': value.isoformat(' ')}
    if isinstance(value, datetime.date):
        return {'$ts': value.isoformat()}
    if isinstance(value, bytes):
        return {'$bytes': value.hex()}
    if isinstance(value, (set, tuple)):
        raise TypeError('unsupported')
    return value


def load_outcome(text, loader):
    try:
        value = yaml.load(text, Loader=loader)
    except yaml.YAMLError as exc:
        return {'error': type(exc).__name__, 'message': str(exc)}
    except Exception as exc:
        return {'error': type(exc).__name__, 'message': str(exc)}
    try:
        return {'value': json.dumps(enc(value), ensure_ascii=True, separators=(',', ':'))}
    except (TypeError, ValueError, RecursionError) as exc:
        return {'special': str(exc)[:60]}


LOAD_TEXTS = [
    '', '\n', '# comment only\n', '---\n', '--- \n...\n', 'a', 'a\n', '1', '1.5', 'yes', 'No', 'on', 'OFF', 'true', 'False', 'null', '~', 'Null',
    '0', '-0', '+5', '007', '010', '09', '0x1F', '0b11', '0o17', '1_000', '+1_0', '-0x10', '12:30:45', '1:30', '-1:30', '190:20:30', '1:30:45.5',
    '1e3', '1.0e+3', '1.e+3', '.5', '-.5', '1.', '.inf', '-.inf', '+.inf', '.nan', '.NaN', '1_0.5', '3.14', '1e400', '1.0e+400', '-0.0',
    '2001-12-14', '2001-12-14t21:59:43.10-05:00', '2001-12-14 21:59:43.10 -5', '2001-12-14 21:59:43', '2001-1-1', '2002-12-14 1:02:03Z',
    '2001-13-01', '2001-02-30', '2001-01-01 25:00:00', '0000-01-01', '2001-01-01 10:00:00 +23:59', '2001-01-01 10:00:00.1234567',
    "'123'", '"yes"', "'null'", '!!str 123', '!!int "42"', '!!float 1', '!!bool yes', '!!null x', '!!binary aGVsbG8=', '!!str', '!!int abc',
    '!!timestamp 2001-01-01', '!!set {a, b}', '!!omap [a: 1]', '!!seq [1]', '!!map {a: 1}', '!foo bar', '!foo {a: 1}', '!<tag:yaml.org,2002:str> 5',
    '<<', '=', '!', 'a: <<', 'a: =', '- <<', '= : 1', '"=": 1', '{=: 1}', "'<<': 1", '? <<\n: 1',
    'a: 1', 'a: 1\nb: 2\n', 'b: 1\na: 2\n', 'a: 1\na: 2\n', 'a: 1\nb: 2\na: 3\n', 'a:\n  - 1\n  - 2\n', 'a:\n- 1\n- 2\n', 'a: [1, 2, {b: c}]', '{a: 1, b: [x, y]}',
    '[a, b,]', '[]', '{}', '[[]]', 'a: []\nb: {}\n', '- - 1\n  - 2\n- - 3\n', '- a: 1\n  b: 2\n- c: 3\n',
    'a: |\n  line1\n  line2\n', 'a: |-\n  x\n', 'a: |+\n  x\n\n', 'a: >\n  folded\n  text\n\n  para\n', 'a: >-\n  x\n  y\n', 'a: |2\n    x\n',
    'a: "x\\ny\\u00e9\\U0001F600\\x41\\t"', "a: 'it''s'", 'a: "line1\n  line2"', "a: 'line1\n\n  line2'", 'a: plain\n  continued\n', 'a: "\\/"',
    '? a\n: b\n', '? [a, b]\n: c\n', '? {a: 1}\n: c\n', '[a]: 1', '{a: 1}: 2', '1: a', 'true: a', 'yes: a', '~: a', 'null: a', '1.5: a', '2001-01-01: a',
    '1: a\n2: b\n', '-1: a', '0x10: a', 'a: &x {b: 1}\nc: *x\n', 'a: &x [1, 2]\nc: *x\nd: [*x, *x]\n', '- &a 1\n- *a\n', '- &s foo\n- *s\n',
    'base: &b {x: 1, y: 2}\nd:\n  <<: *b\n  y: 3\n', 'd:\n  <<: [{a: 1}, {a: 2, b: 3}]\n  c: 4\n', 'd:\n  <<: {a: 1}\n  <<: {b: 2}\n', 'd:\n  <<: 5\n',
    'd:\n  <<: [1, 2]\n', 'd:\n  <<: &m {a: 1}\ne:\n  <<: *m\n', 'a: &x\n  b: *x\n', '&a [*a]', '*a', '&a a\n&a b', 'a: *x',
    'a: b: c', 'a: [1, 2', '{a: 1', 'a:\n\t- 1', '- a\n b', 'a: 1\n  b: 2', '"unterminated', "'unterminated", 'a: @x', 'a: `x', '%YAML 1.1\n---\na: 1\n', '%YAML 1.2\n---\na: 1\n',
    '--- a\n--- b\n', 'a: 1\n---\nb: 2\n', '--- a\n...\n--- b\n', 'a\n---\n', '---\n---\n',
    'a: 例子\nb: "节点 🚀"\n', 'key with spaces: value with spaces\n', 'a: b # comment\n# c\nc: d\n', 'a: b#c\n', 'url: http://x:80/p?q=1#f\n',
    '- ! 5\n', '- !!str\n- !!int 5\n', '- !!float 1\n', '- !!null\n', '- !!binary |\n    aGVs\n    bG8=\n', '- !!binary "!!"', '- !!binary "é"',
    '\ufeffa: 1\n', 'a: 1\r\nb: 2\r\n', 'a: 1\rb: 2\r', 'a: \u0085x\n', 'a:\u00a01\n', 'a:\n  - b\n  -\n  - c\n', 'a:\n  b:\n    c:\n', '-\n-\n',
    'k: !!str 1_0', 'k: 0o17', 'k: 0O17', 'k: 1e5', 'k: 1E+5', 'k: +1e5', 'k: 1.5e5', 'k: 0.5E-3', 'k: 0x', 'k: 0b', 'k: 0b_', 'k: 0_', 'k: _1', 'k: 1_', 'k: -', 'k: +', 'k: .',
    'k: 0.', 'k: 00', 'k: 00.5', 'k: -0o7', 'k: +0x1f', 'k: 08', 'k: 1:60', 'k: 1:59', 'k: 1:5', 'k: 0:0:0', 'k: 1:0:0.0', 'k: 1_0:3_0', 'k: -.5e+3', 'k: 12e03',
    'a: !!python/tuple [1]', 'a: !!python/name:os.system', 'a: !!python/object:os.system {}',
    '- yes\n- no\n- on\n- off\n- y\n- n\n- Y\n- N\n- TRUE\n- tRUE\n', '- nUll\n- NULL\n- ~\n-\n- ""\n',
]


def random_document(rng):
    scalars = ['a', 'on', 'off', 'yes', '~', 'null', '1', '0x1f', '0o17', '1_000', '1e3', '.inf', '.nan', '-0.0', '2026-11-10',
               '2026-11-10 08:00:00', '1:20', '"q"', "'s'", 'a b', 'a: b', '', '🇭🇰 香港', 'é', '&a x', '*a', '!!str 1', '!x y',
               '<<', '=', '[1, 2]', '{a: 1}', '|\n    text', '>\n    folded', 'a#b', 'a #c', '\ttab', 'x\ty', '@at', '`tick', '%p']
    lines = []
    for _ in range(rng.randrange(1, 9)):
        key = rng.choice(['k', 'on', '1', '1.5', 'a b', '? x', '"quoted"', '🙂', 'k'])
        shape = rng.randrange(6)
        if shape == 0:
            lines.append(f'{key}: {rng.choice(scalars)}')
        elif shape == 1:
            lines.append(f'{key}:')
            lines.extend(f'{rng.choice(["  ", "", "    ", chr(9)])}- {rng.choice(scalars)}' for _ in range(rng.randrange(1, 4)))
        elif shape == 2:
            lines.append(f'{key}:')
            lines.extend(f'  {rng.choice(["x", "y", "x"])}: {rng.choice(scalars)}' for _ in range(rng.randrange(1, 4)))
        elif shape == 3:
            lines.append(f'{key}: [{", ".join(rng.choice(scalars[:20]) for _ in range(rng.randrange(0, 4)))}]')
        elif shape == 4:
            lines.append(f'{key}: &anchor {rng.choice(scalars[:12])}')
            lines.append(f'{key}2: *anchor')
        else:
            lines.append(rng.choice(['---', '...', '# comment', '', '- top', '%YAML 1.1', '﻿bom: 1', 'k: "unterminated']))
    return rng.choice(['', '', '﻿']) + '\n'.join(lines) + rng.choice(['\n', '', '\n\n', '\r\n'])


def load_cases():
    texts = list(LOAD_TEXTS)
    # round trips and the texts the application emits
    for value in build_inputs()[::5]:
        try:
            texts.append(yaml.safe_dump(value, allow_unicode=True, sort_keys=False))
            texts.append(yaml.safe_dump(value))
            texts.append(yaml.dump(value, default_flow_style=True))
        except Exception:
            pass
    rng = random.Random(20261110)
    texts.extend(random_document(rng) for _ in range(900))
    seen, out = set(), []
    for text in texts:
        if text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def main():
    lines = []
    for value in build_inputs():
        text = json.dumps(value, ensure_ascii=True, allow_nan=True)
        value = json.loads(text)
        lines.append({'kind': 'dump', 'input': text, 'out': dump_all(value)})
    for options, spec in SPECS:
        value = build(spec, {})
        lines.append({'kind': 'spec', 'options': options, 'spec': spec, 'out': OPTION_SETS[options](value)})
    for text in load_cases():
        lines.append({'kind': 'load', 'text': text, 'safe': load_outcome(text, yaml.SafeLoader),
                      'native': load_outcome(text, NativeLoader)})
    path = Path(__file__).with_name('corpus.jsonl')
    with open(path, 'w', encoding='ascii', newline='\n') as handle:
        for line in lines:
            handle.write(json.dumps(line, ensure_ascii=True, allow_nan=True, separators=(',', ':')) + '\n')
    counts = {}
    for line in lines:
        counts[line['kind']] = counts.get(line['kind'], 0) + 1
    print(counts)


if __name__ == '__main__':
    main()
