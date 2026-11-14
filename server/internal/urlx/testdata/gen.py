#!/usr/bin/env python3
"""Generate corpus.jsonl: backend.utils.url_utils.safe_url_for_log on many URLs, plus
urllib's unquote/quote_plus on strings. Run on the test server from the repository root:

    PYTHONPATH=. ~/cf-test/.venv/bin/python server/internal/urlx/testdata/gen.py

One JSON object per line (ASCII only): {"kind": "url", "in": ..., "out": ...} or
{"kind": "unquote"|"quote_plus", "in": ..., "out": ...}.
"""
import json
import random
import sys
from pathlib import Path
from urllib.parse import quote_plus, unquote, unquote_plus

from backend.utils.url_utils import safe_url_for_log

assert sys.version_info[:2] == (3, 11)
R = random.Random(20261114)

SCHEMES = ['http', 'https', 'HTTP', 'ftp', 'file', 'ws', 'socks5', 'ss', 'vmess', '', 'a+b-c.d', '1http', 'tel']
USERINFO = ['', 'alice:hunter2@', 'bob@', ':pw@', 'u:p:q@', '%75ser:%70ass@', 'a@b@']
HOSTS = ['example.test', 'EXAMPLE.Test', '例子.test', 'a.b.c.d.e', '1.2.3.4', '[::1]', '[2001:db8::1]', '[fe80::1%25eth0]',
         '[fe80::1%eth0]', '[1.2.3.4]', '[v1.x]', '[vz.x]', '[broken', 'broken]', '[::1]x', 'x[::1]', '', 'ex ample', 'a%2Fb',
         'host:80', 'host:99999', 'host:abc', 'host:', 'host:0', 'host:٣', '℀.test', 'a／b.test', 'a＠b', 'a⁇b', '︓x', 'xx⩴yy',
         'Ünïcode.test', 'İstanbul.test', 'ΑΣ.TEST', 'ΣΑΣ.test', 'ΑΣΑ.test', 'Σ.test', 'ǅx.test', 'ẞ.test', 'ﬃ.TEST', 'a..b', '-x-', 'localhost', '192.168.0.1:8080', '[::1]:9090', '[::1]:', '[::1]:abc', '[::1]:70000']
PATHS = ['', '/', '/p', '/a/b/c', '/a b', '/%E4%BE%8B', '/a%2Fb', '/a%252Fb', '/路径', '/a;b=c', '/..', '//x', '/p/', 'p', '%2F']
KEYS = ['token', 'Token', 'TOKEN', 'access_token', 'api-key', 'api_key', 'apikey', 'API%2DKEY', 'auth', 'authorization', 'secret', 'client_secret',
        'password', 'passwd', 'pwd', 'credential', 'credentials', 'key', 'monkey', 'ok', 'name', 'next', 'redirect', 'url', 'q', '', 'a+b',
        'to%6Ben', 'to%256Ben', 'k e y', 'tokеn', 'keyword', 'sig', 'sign', 'session', 'user', 'a%20b', '键']
VALUES = ['', 'v', 'visible', 'raw-secret', '1', 'a b', 'a+b', 'a%20b', '%E4%BD%A0', '你好', 'https://x.test/p?token=abc', 'https%3A%2F%2Fx.test%2Fp%3Ftoken%3Dabc',
          'https%253A%252F%252Fx.test%252Fp%253Ftoken%253Dabc', '/p?key=1', '/p?x=1&y=2', 'token%3Dabc', 'token%253Dabc', 'a=b', 'api%2Bkey%3Dx', 'x&token=y', 'x;token=y',
          'x?auth=y', '%00', '%0d%0a', '%250d%250a', '%2500', '%25', '%', '%zz', '%2', '%E4', '%E4%BD', '%ED%A0%80', '%F0%9F%98%80', '%C0%80', '%FF', 'a%',
          '%2525252525252525252525', '%25' * 9 + '41', 'x' * 300, '😀', 'é', '=', '==', '&&', 'a=b=c', '\t', ' ', 'a\nb']
FRAGMENTS = ['', '#', '#frag', '#token=abc', '#a=b&secret=c', '#%74oken=1']


def build():
    scheme = R.choice(SCHEMES)
    out = (scheme + ':' if scheme else '') + ('//' if R.random() < 0.9 else '') + R.choice(USERINFO) + R.choice(HOSTS) + R.choice(PATHS)
    if R.random() < 0.85:
        pairs = []
        for _ in range(R.choice([0, 1, 1, 2, 3, 5])):
            pairs.append(R.choice(KEYS) + R.choice(['=', '=', '=', '']) + R.choice(VALUES))
        out += '?' + R.choice(['&', '&', ';']).join(pairs) if pairs else '?'
    out += R.choice(FRAGMENTS)
    return out


FIXED = [
    'https://alice:hunter2@example.test/path?config_token=raw-secret&api-key=key-secret&ok=visible',
    'https%253A%252F%252Fbob%253Apass%2540example.test%252Fp%253F%252574oken%253Ddeep-secret',
    'https://user:password@[broken?token=raw-secret',
    'https://example.test/p?next=https%253A%252F%252Fnested.test%252Fp%253Ftoken%253Dnested-url-secret',
    'https://example.test/p?next=token%253Dnested-expression-secret', 'https://example.test/p?next=api%252Bkey%253Dplus-layer-secret',
    'https://example.test/p?next=ok%250d%250atoken=secret', 'https://example.test/p?next=ok%2500token=secret',
    'https://example.test/path#token=plain-fragment-secret', 'https://example.test/path#outer=https%253A%252F%252Fexample.test%252F%253Ftoken%253Dnested-secret',
    'https://example.test', 'https://example.test/', 'http://example.test:8080/a?b=c', '/relative/path?token=x&a=b', 'relative', '', ' ', 'http://', 'https:///path',
    '  https://example.test/p?a=1', 'https://exa\tmple.test/p', 'https://example.test/p?a=1\n&token=2', 'HTTPS://EXAMPLE.TEST:443/Path?A=b',
    'https://[2001:db8::1]:8443/p?q=1', 'https://[::1]/', 'https://example.test:/p', 'https://example.test:0/p', 'https://example.test:65536/p',
    'ss://YWVzLTI1Ni1nY206cGFzc3dvcmQ@example.test:8388#name', 'vmess://abc', 'mailto:a@b.c', 'data:text/plain,hello', 'javascript:alert(1)',
    'https://example.test/p?a=1&&b=2&', 'https://example.test/p?=x&y=', 'https://example.test/p?a=1#', 'https://example.test/p?a%3D1=2',
    'https://example.test/p?a=b%26token%3Dc', 'https://example.test/?x=%2F%2Fevil.test%2F%3Ftoken%3D1',
]


def url_out(url):
    return safe_url_for_log(url)


def main():
    lines = []
    seen = set()
    for url in FIXED + [build() for _ in range(1600)]:
        if url in seen:
            continue
        seen.add(url)
        lines.append({'kind': 'url', 'in': url, 'out': url_out(url)})
    strings = [v for v in VALUES] + [h for h in HOSTS] + ['%41%42', '%e4%bd%a0', 'a%2Bb', '%c3%a9', '%E2%82', '%E2%82%AC', '%E2%82%AC%80', '%80', '%F4%90%80%80',
                                                          '%F0%80%80%80', '%E0%80%80', '%ED%9F%BF', '%C2', '%C2%C2%A9', 'é%C3', '%C3é', '%F0%9F', '%F0%9F%98']
    for text in strings:
        lines.append({'kind': 'unquote', 'in': text, 'out': unquote(text)})
        lines.append({'kind': 'unquote_plus', 'in': text, 'out': unquote_plus(text)})
        lines.append({'kind': 'quote_plus', 'in': text, 'out': quote_plus(text)})
    path = Path(__file__).with_name('corpus.jsonl')
    with open(path, 'w', encoding='ascii', newline='\n') as handle:
        for line in lines:
            handle.write(json.dumps(line, ensure_ascii=True, separators=(',', ':')) + '\n')
    print(len(lines))


main()
