#!/usr/bin/env python3
"""Python timing of the input of bench_test.go (run from the repo root):

    PYTHONPATH=. ~/cf-test/.venv/bin/python server/internal/rules/testdata/bench.py
"""
import time

from backend.utils.rule_matcher import RuleConfigMatcher

lines = []
for i in range(300_000):
    k = i % 10
    if k <= 4:
        lines.append(f'DOMAIN-SUFFIX,site{i}.example')
    elif k <= 7:
        lines.append(f'DOMAIN,host{i}.example.org')
    elif k == 8:
        lines.append(f'IP-CIDR,10.{i // 256 % 256}.{i % 256}.0/24,no-resolve')
    elif i % 1000 == 9:
        lines.append(f'DOMAIN-KEYWORD,kw{i}')
    else:
        lines.append(f'DOMAIN-SUFFIX,other{i}.test')
body = '\n'.join(lines) + '\n'
item = {'id': 'set', 'itemType': 'ruleset', 'behavior': 'classical', 'policy': 'PROXY'}
t = time.perf_counter()
m = RuleConfigMatcher([item], [], lambda *_: body)
m.match('warmup.test')
print('index build: %.3f s' % (time.perf_counter() - t))
queries = ['www.site4000.example', 'a.b.other3999.test', 'nothing.test', '10.5.5.9', 'host4005.example.org', 'zz.kw9009.com']
n = 20000
t = time.perf_counter()
for i in range(n):
    m.match(queries[i % len(queries)])
print('match: %.2f us/op' % ((time.perf_counter() - t) / n * 1e6))
