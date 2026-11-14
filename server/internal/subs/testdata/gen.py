#!/usr/bin/env python3
"""Generate the differential corpora of package subs from the real Python code.

Run from the repository root on the test server (CPython 3.11, the production
dependencies; nothing here touches the network):

    rt -- bash -lc 'PYTHONPATH=. ~/cf-test/.venv/bin/python server/internal/subs/testdata/gen.py'
    rt --back server/internal/subs/testdata

Every value is synthetic (example.com / RFC 5737 / RFC 3849 addresses, random
UUIDs from a fixed seed). Output: one JSON object per line,
{"fn": name, "in": input, "out": output}; an exception is
{"error": str(exc), "type": ClassName} in "out". Files are written with
ensure_ascii=False; key order is the Python dict order.
"""
import base64
import copy
import json
import os
import random
import re
import sys
import tempfile
import uuid
from pathlib import Path

assert sys.version_info[:2] == (3, 11), sys.version
HERE = Path(__file__).parent
R = random.Random(20261115)


def uid():
    return str(uuid.UUID(int=R.getrandbits(128), version=4))


def dump(path, rows):
    with open(HERE / path, 'w', encoding='utf-8') as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')
    print(path, len(rows))


def call(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except BaseException as exc:  # noqa: BLE001
        return {'error': str(exc), 'type': type(exc).__name__}


# --------------------------------------------------------------------- parser
from backend.utils import subscription_parser as sp  # noqa: E402

HOSTS = ['example.com', 'node1.example.com', '192.0.2.10', '198.51.100.7', '203.0.113.5', '[2001:db8::1]',
         '2001:db8::2', '例子.test', 'a-b.example.org', 'EXAMPLE.COM', '', 'host with space.example']
PORTS = ['443', '8388', '1', '65535', '0', '70000', '-1', ' 443 ', '4_43', '٤٤٣', '443/', '443?x=1', 'abc', '',
         '+80', '00443', '443.0', '0x1bb', '1' * 30]
NAMES = ['Node 1', 'HK-01', '%E9%A6%99%E6%B8%AF%2001', '香港 01', 'a#b', '', '🚀 fast', 'name%20with%20space',
         'x' * 50, 'tab\there']
METHODS = ['aes-128-gcm', 'chacha20-ietf-poly1305', '2022-blake3-aes-256-gcm', 'none']
PASSWORDS = ['pw', 'p@ss:word', 'pass%40word', '密码', 'a:b:c', '', 'x' * 40, 'with/slash+plus=eq']


def b64(text, mode=None):
    raw = text.encode('utf-8') if isinstance(text, str) else text
    mode = mode or R.choice(['std', 'nopad', 'url', 'urlnopad', 'std', 'nl'])
    if mode == 'std':
        return base64.b64encode(raw).decode()
    if mode == 'nopad':
        return base64.b64encode(raw).decode().rstrip('=')
    if mode == 'url':
        return base64.urlsafe_b64encode(raw).decode()
    if mode == 'urlnopad':
        return base64.urlsafe_b64encode(raw).decode().rstrip('=')
    s = base64.b64encode(raw).decode()
    return s[:10] + '\n' + s[10:] if len(s) > 10 else s


def pick(seq):
    return R.choice(seq)


def qs(items):
    parts = []
    for k, v in items:
        parts.append(f'{k}={v}')
    return '&'.join(parts)


def gen_ss():
    m, p, h, port, n = pick(METHODS), pick(PASSWORDS), pick(HOSTS), pick(PORTS[:6]), pick(NAMES)
    forms = [
        f'ss://{b64(m + ":" + p)}@{h}:{port}#{n}',
        f'ss://{m}:{p}@{h}:{port}#{n}',
        f'ss://{b64(m + ":" + p + "@" + h + ":" + port)}#{n}',
        f'ss://{b64(m + ":" + p + "@" + h + ":" + port + "#" + n)}',
        f'ss://{b64(m + ":" + p)}@{h}:{port}/?plugin=obfs-local%3Bobfs%3Dtls#{n}',
        f'ss://{b64(m + ":" + p)}@{h}:{pick(PORTS)}#{n}',
        f'ss://{b64(m)}@{h}:{port}#{n}',
        f'ss://{b64(m + ":" + p)}@{h}#{n}',
        f'ss://{b64(m + ":" + p)}@{h}:{port}',
        f'ss://{b64(m + ":" + p)}',
        f'ss://{m}:{p}@@{h}:{port}#{n}',
        f'ss://@{h}:{port}#{n}',
        f'ss://{b64(m + ":" + p)}@{h}:{port}#',
        f'ss://{b64(b"".join([bytes([R.randrange(256)]) for _ in range(12)]))}@{h}:{port}#{n}',
    ]
    return pick(forms)


def gen_ssr():
    h, port = pick(HOSTS), pick(PORTS[:6] + [pick(PORTS)])
    proto, method, obfs = pick(['origin', 'auth_aes128_md5']), pick(['aes-256-cfb', 'chacha20']), pick(['plain', 'tls1.2_ticket_auth'])
    pw = b64(pick(PASSWORDS))
    main = f'{h}:{port}:{proto}:{method}:{obfs}:{pw}'
    params = []
    if R.random() < .8:
        params.append(('obfsparam', b64('obfs.example.com')))
    if R.random() < .6:
        params.append(('protoparam', b64('1:pw')))
    if R.random() < .8:
        params.append(('remarks', b64(pick(NAMES))))
    if R.random() < .3:
        params.append(('group', b64('grp')))
    if R.random() < .1:
        params.append(('remarks', 'not base64!'))
    tail = ''
    if params or R.random() < .3:
        tail = '/?' + qs(params)
    body = main + tail
    if R.random() < .1:
        body = main.rsplit(':', 1)[0]
    return 'ssr://' + b64(body)


def gen_vmess():
    d = {'v': '2', 'ps': pick(NAMES), 'add': pick(HOSTS[:6]), 'port': pick([443, '443', 8080, 443.0, '443', None, 'x', True, 0]),
         'id': uid(), 'aid': pick([0, '0', 2, '2', 64, None, '', 1.5]), 'scy': pick(['auto', 'aes-128-gcm', 'none']),
         'net': pick(['tcp', 'ws', 'grpc', 'h2', 'kcp', 'ws']), 'type': 'none', 'host': pick(['', 'example.com']),
         'path': pick(['/', '/ws', '/a b', '']), 'tls': pick(['tls', '', 'none', 'TLS']), 'sni': 'example.com'}
    for key in list(d):
        if R.random() < .15:
            del d[key]
    if R.random() < .05:
        d = pick([[1, 2], 'str', 5, None])
    text = json.dumps(d, ensure_ascii=R.random() < .5)
    return 'vmess://' + b64(text)


def gen_vless():
    sec = pick(['none', 'tls', 'reality', 'xtls', None, 'tls'])
    typ = pick(['tcp', 'ws', 'grpc', 'h2', 'http', None, 'tcp'])
    items = []
    if sec is not None:
        items.append(('security', sec))
    if typ is not None:
        items.append(('type', typ))
    for key, vals in [('encryption', ['none', 'mlkem']), ('sni', ['example.com', '%E4%BE%8B%E5%AD%90.test']), ('fp', ['chrome', 'firefox']),
                      ('pbk', ['pubkey' + uid()[:8]]), ('sid', ['ab12', '']), ('spx', ['%2F', '/']), ('ech', ['echconfig', '']),
                      ('path', ['%2Fws', '/p', '']), ('host', ['example.com', '']), ('serviceName', ['grpcsvc', '']),
                      ('headerType', ['none', 'http']), ('flow', ['xtls-rprx-vision', '']), ('alpn', ['h2,http/1.1', 'h2', '']),
                      ('extra', ['1', 'a+b', 'a%20b'])]:
        if R.random() < .4:
            items.append((key, pick(vals)))
    if R.random() < .15 and items:
        items.append(pick(items))  # duplicate key -> list value
    if R.random() < .1:
        items.append(('alpn', 'h2'))
        items.append(('alpn', 'http/1.1'))
    R.shuffle(items)
    q = ('?' + qs(items)) if items or R.random() < .1 else ''
    port = pick(PORTS[:5] + [pick(PORTS)])
    h = pick(HOSTS)
    forms = [f'vless://{uid()}@{h}:{port}{q}#{pick(NAMES)}', f'vless://{uid()}@{h}:{port}{q}', f'vless://{uid()}@{h}{q}#{pick(NAMES)}',
             f'vless://{uid()}{h}:{port}{q}', f'vless://{uid()}@{h}:{port}{q}#{pick(NAMES)}']
    return pick(forms)


def gen_trojan():
    items = []
    for key, vals in [('sni', ['example.com', 'a.example.com']), ('allowInsecure', ['true', '1', 'false', '0']), ('type', ['ws']),
                      ('peer', ['example.com'])]:
        if R.random() < .5:
            items.append((key, pick(vals)))
    if R.random() < .1 and items:
        items.append(pick(items))
    q = ('?' + qs(items)) if items else ''
    h, port = pick(HOSTS), pick(PORTS[:5] + [pick(PORTS)])
    forms = [f'trojan://{pick(PASSWORDS)}@{h}:{port}{q}#{pick(NAMES)}', f'trojan://{pick(PASSWORDS)}@{h}:{port}{q}',
             f'trojan://{pick(PASSWORDS)}{h}:{port}', f'trojan://{pick(PASSWORDS)}@{h}{q}#{pick(NAMES)}',
             f'trojan://{pick(PASSWORDS)}@{h}:{port}#{pick(NAMES)}{q}']
    return pick(forms)


def gen_hy():
    v2 = R.random() < .6
    items = []
    for key, vals in [('obfs', ['salamander', '']), ('obfs-password', ['pw']), ('sni', ['example.com']), ('peer', ['peer.example.com']),
                      ('insecure', ['1', '0', 'true']), ('auth', ['x']), ('upmbps', ['100']), ('alpn', ['h3'])]:
        if R.random() < .4:
            items.append((key, pick(vals)))
    if R.random() < .1 and items:
        items.append(pick(items))
    q = ('?' + qs(items)) if items else ''
    h, port = pick(HOSTS), pick(PORTS[:5] + [pick(PORTS)])
    pre = 'hysteria2://' if v2 else 'hysteria://'
    forms = [f'{pre}{pick(PASSWORDS)}@{h}:{port}{q}#{pick(NAMES)}', f'{pre}{h}:{port}{q}#{pick(NAMES)}', f'{pre}{pick(PASSWORDS)}@{h}:{port}{q}',
             f'{pre}{h}{q}', f'{pre}@{h}:{port}/{q}']
    return pick(forms)


def gen_object():
    base = {'name': pick(NAMES) or 'n', 'type': pick(['ss', 'vmess', 'trojan', 'tuic', 'snell', 'wireguard', 'anytls', 'hysteria2', 'http']),
            'server': pick(HOSTS[:6]), 'port': pick([443, 8080, '443', 65535])}
    extra = {'cipher': 'aes-128-gcm', 'password': 'pw', 'udp': True, 'tls': True, 'sni': 'example.com', 'skip-cert-verify': False,
             'alpn': ['h2', 'h3'], 'ws-opts': {'path': '/', 'headers': {'Host': 'example.com'}}, 'dialer-proxy': 'Relay',
             'version': 5, 'psk': 'abcd', 'obfs-opts': {'mode': 'tls', 'host': 'example.com'}, 'weight': 1.5, 'ip': '192.0.2.1',
             'private-key': 'k', 'reserved': [1, 2, 3], 'enabled': 'yes', 'tilde': '~', 'octal': '0o17', 'nullv': None}
    for k in list(extra):
        if R.random() < .35:
            base[k] = extra[k]
    style = R.random()
    if style < .3:
        return json.dumps(base, ensure_ascii=False)
    if style < .6:
        def flow(v):
            if isinstance(v, dict):
                return '{' + ', '.join(f'{k}: {flow(x)}' for k, x in v.items()) + '}'
            if isinstance(v, list):
                return '[' + ', '.join(flow(x) for x in v) + ']'
            if v is None:
                return 'null'
            if isinstance(v, bool):
                return 'true' if v else 'false'
            if isinstance(v, str):
                return json.dumps(v, ensure_ascii=False) if R.random() < .5 else (v if re.fullmatch(r'[A-Za-z0-9_.\-/]+', v) and v not in ('yes', 'on', '~') else json.dumps(v, ensure_ascii=False))
            return str(v)
        text = flow(base)
        return ('- ' + text) if R.random() < .2 else text
    import yaml
    text = yaml.safe_dump(base, allow_unicode=True, sort_keys=False)
    if style < .8:
        return text
    return '- ' + text.replace('\n', '\n  ', text.count('\n') - 1)


JUNK = ['', '   ', '# comment', 'tuic://' + uid() + ':pw@example.com:443?congestion_control=bbr#t', 'snell://x', 'wireguard://x', 'anytls://pw@example.com:443#a',
        'http://example.com', 'plain text', 'proxies:', '- ', '-', '- {', '{}', '{"a":', ']', '﻿ss://x', 'ss:// ', 'vless://', 'trojan://', 'vmess://', 'ssr://',
        'hysteria://', 'hysteria2://', '- ss://', '-  trojan://p@example.com:443#x']
GENS = [gen_ss, gen_ssr, gen_vmess, gen_vless, gen_trojan, gen_hy, gen_object]


def mutate(s):
    if not s:
        return s
    kind = R.randrange(7)
    i = R.randrange(len(s))
    if kind == 0:
        return s[:i]
    if kind == 1:
        return s[:i] + s[i + 1:]
    if kind == 2:
        return s[:i] + pick(['@', ':', '#', '?', '&', '=', '%', '/', ' ', '"', '{', '\n', ' ', '　', '﻿']) + s[i:]
    if kind == 3:
        return s.replace(':', '', 1)
    if kind == 4:
        return s.replace('@', '', 1)
    if kind == 5:
        return s.replace('#', '', 1)
    return s + pick(['=', '==', '\r', '\t', ' ', ' ', '%2', '%zz', '#', '?'])


def parser_rows():
    rows = []
    fixtures = [
        'ss://YWVzLTEyOC1nY206cGFzc3dvcmQ@example.com:8388#Test%20Node',
        'ss://YWVzLTEyOC1nY206cGFzc3dvcmQ=@example.com:8388#n',
        'trojan://password@example.com:443?sni=example.com&allowInsecure=1#T',
        'hysteria2://auth@example.com:443?sni=example.com&insecure=1#H2',
        'name: x\ntype: ss\nserver: example.com\nport: 443\ncipher: aes-128-gcm\npassword: p\n',
        '- name: a\n  type: ss\n  server: example.com\n  port: 1\n- name: b\n  type: trojan\n',
        '- name: a\n  type: ss\n- 5\n- [1]\n',
        '- name:\n',
        '{"name":"x","type":"ss","server":"example.com","port":1}',
        '{name: x, type: ss, server: example.com, port: 443}\n{name: y, type: ss, server: example.com, port: 444}',
        '[1,2,3]', '{"a":1}', 'a: [', '{\n  "name": "j",\n  "type": "vmess"\n}',
    ]
    inputs = list(fixtures)
    for _ in range(2200):
        parts = []
        for _ in range(R.choice([1, 1, 1, 2, 3, 5])):
            r = R.random()
            if r < .12:
                parts.append(pick(JUNK))
            else:
                g = pick(GENS)
                s = g()
                if R.random() < .25:
                    s = mutate(s)
                if R.random() < .1:
                    s = '- ' + s
                if R.random() < .1:
                    s = '  ' + s + pick(['  ', '\t', ''])
                parts.append(s)
        sep = pick(['\n', '\n', '\r\n', '\n\n'])
        text = sep.join(parts)
        if R.random() < .05:
            text = '\n' + text + '\n'
        inputs.append(text)
    for text in inputs:
        rows.append({'fn': 'parse_uri_list', 'in': text, 'out': call(sp.parse_uri_list, text)})
    # per-scheme single lines (more weight on parameter variations)
    for g in GENS[:6]:
        for _ in range(250):
            s = g()
            if R.random() < .3:
                s = mutate(s)
            rows.append({'fn': 'parse_uri_list', 'in': s, 'out': call(sp.parse_uri_list, s)})
    # base64 decoding semantics (CPython 3.11 binascii)
    alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
    for _ in range(2500):
        n = R.randrange(0, 14)
        chars = [pick(alphabet) if R.random() < .85 else pick(['=', '-', '_', ' ', '\n', '\r', '.', '!', 'é', ' ']) for _ in range(n)]
        s = ''.join(chars)
        try:
            out = {'hex': base64.b64decode(s).hex()}
        except BaseException as exc:  # noqa: BLE001
            out = {'error': type(exc).__name__}
        rows.append({'fn': 'b64decode', 'in': s, 'out': out})
    # str.strip / int() semantics through the parsers' helpers
    for s in ['', ' 12 ', '1_000', '1__0', '_1', '1_', '+5', '-5', '٣٤', '１２', '0x10', '1e3', '  1　', '1\x1c', '9' * 4301, '9' * 40, '-0', '+', '١٢٣', '1 2', '²']:
        try:
            out = int(s)
        except BaseException as exc:  # noqa: BLE001
            out = {'error': type(exc).__name__}
        rows.append({'fn': 'int', 'in': s, 'out': out})
    return rows


dump('parser.jsonl', parser_rows())


# --------------------------------------------------------------------- filter
from backend.utils import subscription_filter as sf  # noqa: E402

PATTERNS = ['香港|HK', '^(?!.*剩余).*$', '(', '[a-z', '(?i)^hk', r'\d+', '(?<=a)b', r'(a)\1', '*', 'a{2,1}', '(?P<n>x)', '^(?!.*(过期|流量)).*$',
            ' x ', '\\', '(?x) a b', '.*', 'a++', '(?>a)', r'\p{L}', '日本|JP', '^HK', 'HK$', r'(?i)jp\s*\d', '(?!x)', '(?<!a)b', 'x{3}', 'a{1,2}?',
            '[^\\w]', r'\bhk\b', '(?s).', '^$', '', '   ', '(?P<a>x)(?P=a)', '[[:alpha:]]', '\\Qa\\E', '(?i)香港', 'a|', '|', '(?:)', '^(?=.*香港)(?!.*IPLC).*$',
            'x{1001}', '(?#c)x', r'\N{DIGIT ONE}', '(?(1)a|b)', 'a**', '+', '?', '(?', '(?P<1>a)', r'\8', r'[\d-x]', '(a)(b)', '(?i:a)b', '(?-i:A)']
NAMES_F = ['香港 01', '日本 01', 'HK-02', 'hk 03', 'JP 5', '剩余流量 100G', '过期时间 2026', 'IPLC 香港', 'a', 'b', 'ab', 'x', 'xxx', 'axb', '', ' ',
           'Name With Spaces', '123', 'abcabc', 'ÀÉ', 'ǅ', '\n', 'a\nb', 'AbA', '_', '😀']


def filter_rows():
    rows = []
    values = [None, 123, 1.5, True, [], {}, '', ' ', '\t']
    for p in PATTERNS:
        values += [p, ' ' + p + ' ', p + '\n']
    for v in values:
        rows.append({'fn': 'validate_regex_filter', 'in': v, 'out': call(sf.validate_regex_filter, v)})
    for p in PATTERNS:
        for variant in (p, ' ' + p + ' '):
            for sub in ({'regex_filter': variant, 'name': 'S'}, ):
                items = [{'name': n} for n in NAMES_F] + ['str', None, 5, {'nom': 'x'}, {'name': 5}, {'name': None}, {'name': ['香港']}]
                R.shuffle(items)
                rows.append({'fn': 'filter_subscription_items', 'in': {'sub': sub, 'items': items},
                             'out': call(sf.filter_subscription_items, sub, items)})
    for sub in (None, 5, 'x', [], {}, {'regex_filter': 5}, {'regex_filter': None}, {'regex_filter': ['a']}):
        items = [{'name': 'a'}, 'x']
        rows.append({'fn': 'filter_subscription_items', 'in': {'sub': sub, 'items': items}, 'out': call(sf.filter_subscription_items, sub, items)})
    rows.append({'fn': 'filter_subscription_items', 'in': {'sub': {'regex_filter': 'a'}, 'items': None}, 'out': call(sf.filter_subscription_items, {'regex_filter': 'a'}, None)})
    return rows


dump('filter.jsonl', filter_rows())

# ---------------------------------------------------------------------- hosts
from backend.utils import subscription_hosts as sh  # noqa: E402

DOMS = ['jp-node.example.net', 'hk-node.example.net', 'Node.Example.Org.', 'a.test', 'b.test', 'c.test', 'x.example.com', 'node_1.test',
        'bad_label-.test', 'localhost', '198.51.100.1', 'example', '例子.test', '-a.test', 'a' * 64 + '.test', ('a.' * 130) + 'com', '1.2.3']
VALS = ['198.51.100.12', '2001:db8::12', '2001:DB8:0:0:0:0:0:12', '::ffff:192.0.2.1', 'fe80::1%eth0', '0.0.0.0', '::', '224.0.0.1', 'ff02::1',
        'jp-entry.example.org', 'lan', 'Entry.Example.ORG.', '192.0.2.1,192.0.2.2', '010.0.0.1', '1.2.3', ' 192.0.2.5 ', 'node.test', '~', '']


def hosts_value():
    r = R.random()
    if r < .55:
        return pick(VALS)
    if r < .8:
        return [pick(VALS) for _ in range(R.choice([0, 1, 1, 2, 3, 9]))]
    return pick([{'a': 1}, 5, None, True, [1, 2], [None], [['x']], 1.5])


def yaml_scalar(v):
    import yaml
    return yaml.safe_dump(v, default_flow_style=True, allow_unicode=True).strip().removesuffix('\n...').strip()


def hosts_text():
    n = R.choice([0, 1, 2, 3, 5, 12])
    keys = []
    for _ in range(n):
        k = pick(DOMS + ['*.example.net', '+.example.net', '.example.net', 'google.com', 'ok.test', 'dup.test', '"quoted.test"', "'single.test'"])
        keys.append(k)
    style = R.randrange(5)
    key_word = pick(['hosts', 'hosts', 'hosts', '"hosts"', "'hosts'", 'hosts '])
    pre = pick(['proxies: []\n', 'mixed-port: 7890\nproxies:\n  - {name: a, type: http, server: a.test, port: 80}\n', '', '# hosts: x\n', '\n',
                'proxies:\n  - name: n\n    type: http\n    hosts: ignored\n', 'dns:\n  hosts: nope\n'])
    post = pick(['', 'proxy-groups: []\n', 'rules: []\n', '# trailing\n', 'x: 1\n'])
    nl = pick(['\n', '\n', '\n', '\r\n', ' ', '\x1c'])
    if style == 0:
        body = key_word + ': {' + ', '.join(f'{k}: {yaml_scalar(hosts_value())}' for k in keys) + '}\n'
    elif style == 1:
        body = key_word + ':\n' + ''.join(f'  {k}: {yaml_scalar(hosts_value())}\n' for k in keys)
    elif style == 2:
        body = key_word + ':\n' + ''.join(f'  {k}:\n' + ''.join(f'  - {pick(VALS)}\n' for _ in range(R.choice([1, 2]))) for k in keys)
    elif style == 3:
        body = key_word + ':\n' + ''.join(f'  # c\n  {k}: {pick(VALS)}\n\n' for k in keys)
    else:
        body = key_word + pick([': [198.51.100.1]\n', ': 5\n', ':\n', ': ~\n', ': "text"\n', ':\n  a.test: [unclosed\n', ': {a: 1\n', ':\n\t bad: tab\n'])
    text = pre + body + post
    if nl != '\n':
        text = text.replace('\n', nl)
    return text


def hosts_rows():
    rows = []
    texts = ['no hosts here', '', 'hosts', 'c3M6Ly9ZV1Z6TFRJMU5pMW5ZMjA2Y0dGemMzZHZjbVE9QDE5OC41MS4xMDAuMTo4Mzg4I2hvc3Rz',
             'proxies: []\nhosts: {node.test: 198.51.100.4}\n', 'hosts:\n  node.test:\n  - 198.51.100.4\nproxies: []\n']
    texts += [hosts_text() for _ in range(900)]
    big = {f'n{i}.test': '198.51.100.1' for i in range(1030)}
    import yaml
    texts.append(yaml.safe_dump({'proxies': [], 'hosts': big}))
    texts.append(yaml.safe_dump({'proxies': [], 'hosts': {'a.test': 'b.test', 'b.test': 'a.test'}}))
    records = []
    for t in texts:
        out = call(sh.extract_subscription_hosts, t)
        records.append(out)
        rows.append({'fn': 'extract_subscription_hosts', 'in': t, 'out': out})
    good = [r for r in records if isinstance(r, dict) and 'entries' in r]
    for _ in range(300):
        a, b = R.choice(good + [None, {}]), R.choice(good + [None, {}])
        rows.append({'fn': 'hosts_digest', 'in': a, 'out': call(sh.hosts_digest, a)})
        rows.append({'fn': 'describe_change', 'in': {'previous': a, 'current': b}, 'out': call(sh.describe_change, a, b)})
    for v in ['*.a.test', '+.a.test', '.a.test', 'a.test', 'A.TEST.', 'x.*.test', '*.*.test', '+.', '.', '', 5, None, 'a.test '] :
        for d in ['a.test', 'x.a.test', 'y.x.a.test', 'b.test', 'x.b.test', 'a.x.test']:
            rows.append({'fn': '_matches', 'in': {'pattern': v, 'domain': d}, 'out': call(sh._matches, v, d)})
    rows += merge_rows(good)
    return rows


def merge_rows(good):
    rows = []
    for case in range(700):
        nsub = R.choice([1, 1, 2, 3])
        subs, caches = [], {}
        for i in range(nsub):
            sid = f's{i}'
            rec = R.choice(good) if good and R.random() < .9 else None
            servers = []
            if rec:
                servers = list(rec['entries'])
            nodes = []
            for j in range(R.choice([1, 2, 4])):
                server = pick(servers) if servers and R.random() < .75 else pick(['other.example.com', '203.0.113.9', 'jp-node.example.net', ''])
                nodes.append({'name': pick(['HK 01', 'JP 01', 'SG 01', 'X', f'n{j}']), 'type': 'socks5', 'server': server, 'port': 1})
            if R.random() < .1:
                nodes.append('junk')
            url = f'https://sub.test/{sid}' if R.random() < .9 else None
            sub = {'id': sid, 'name': pick(['机场', 'Air', f'Sub {i}', '']), 'url': url, 'enabled': R.random() < .9}
            if R.random() < .3:
                sub['regex_filter'] = pick(['HK', 'JP|HK', '^(?!.*SG).*$', '(', ' ', 'zzz'])
            subs.append(sub)
            meta = {'url': url if R.random() < .9 else 'https://sub.test/rotated'}
            if rec is not None:
                meta['hosts'] = rec
            caches[sid] = {'nodes': nodes, 'metadata': meta} if R.random() < .95 else None
        aggs = []
        if R.random() < .4:
            aggs.append({'id': 'agg', 'name': 'Agg', 'enabled': R.random() < .9, 'subscriptions': [s['id'] for s in subs if R.random() < .7],
                         'nodes': ['imp'] if R.random() < .5 else [], 'regex_filter': pick(['', 'JP|Imported', '(', 'HK', None])})
        cfg_nodes = [{'id': 'imp', 'name': 'Imported', 'enabled': True, 'server': pick(['jp-node.example.net', 'x.example.com']),
                      'subscription_id': pick([s['id'] for s in subs])},
                     {'id': 'own', 'name': 'Own', 'enabled': True, 'server': 'google.com'}]
        config = {'subscriptions': subs, 'subscription_aggregations': aggs, 'nodes': cfg_nodes}
        providers = {}
        for s in subs:
            if R.random() < .85:
                q = pick(['', '?token=t', '?regex=HK', '?regex=%E9%A6%99&exclude_regex=SG', '?regex=(&x=1', '?exclude_regex=JP', '?regex=+'])
                providers[s['name'] or s['id']] = {'type': 'http', 'url': f"https://panel.test/api/subscriptions/{s['id']}/proxies{q}"}
        for a in aggs:
            if R.random() < .85:
                q = pick(['', '?regex=JP', '?exclude_regex=HK'])
                providers['Agg'] = {'type': 'http', 'url': f"https://panel.test/api/aggregations/{a['id']}/proxies{q}"}
        if R.random() < .1:
            providers['Odd'] = pick([5, {'url': 5}, {'url': 'https://x/api/subscriptions/nope/proxies'}, {'url': 'https://x/%73ubscriptions/s0'}])
        main = {'proxy-providers': providers}
        if R.random() < .6:
            main['hosts'] = pick([{'jp-node.example.net': '192.0.2.10', '+.wild.example.net': '192.0.2.11'}, {'+.example.org': 'jp-node.example.net'},
                                  {}, 'notdict', {'a': ['1.1.1.1']}])
        inline = []
        for n in cfg_nodes:
            if R.random() < .5:
                inline.append({'node': n, 'proxy': R.choice([None, {'server': 'hk-node.example.net'}, {}, {'server': 'jp-node.example.net'}])})
        user_keys = [pick(['jp-node.example.net', '+.wild.example.net', '*.example.net', 'x.example.com'])] if R.random() < .3 else []
        mosdns_keys = [pick(['hk-node.example.net', 'x.example.com'])] if R.random() < .3 else []
        scenario = {'main': main, 'config': config, 'caches': caches, 'user_keys': user_keys, 'mosdns_keys': mosdns_keys, 'inline': inline}
        rows.append({'fn': 'merge_subscription_hosts', 'in': scenario, 'out': run_merge(scenario)})
    return rows


def run_merge(sc):
    import backend.utils.subscription_cache as sc_mod
    tmp = tempfile.mkdtemp()
    paths = {}
    for sid in sc['caches']:
        p = os.path.join(tmp, sid)
        open(p, 'w').write(sid)
        paths[sid] = p
    saved = (sc_mod._get_cache_path, sc_mod.load_subscription_cache)
    sc_mod._get_cache_path = lambda sid: paths.get(sid, os.path.join(tmp, 'missing-' + str(sid)))
    sc_mod.load_subscription_cache = lambda sid: copy.deepcopy(sc['caches'].get(sid))
    sh._index.clear()
    main = copy.deepcopy(sc['main'])
    merged = {}
    try:
        inline = [(copy.deepcopy(i['node']), copy.deepcopy(i['proxy'])) for i in sc['inline']]
        report = call(sh.merge_subscription_hosts, main, copy.deepcopy(sc['config']), user_keys=sc['user_keys'],
                      mosdns_keys=sc['mosdns_keys'], inline_proxies=inline, merged_out=merged)
    finally:
        sc_mod._get_cache_path, sc_mod.load_subscription_cache = saved
        sh._index.clear()
    return {'report': report, 'main': main, 'merged': merged}


dump('hosts.jsonl', hosts_rows())
