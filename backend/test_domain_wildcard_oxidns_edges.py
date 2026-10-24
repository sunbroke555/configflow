"""Rare Domain label patterns and DNS query spelling retain their scope."""
import pytest

from backend.agents.deployment_bundle import convert_mosdns_rules
from backend.converters.mosdns import convert_rule_content_for_mosdns
from backend import test_domain_wildcard_delivery as native_helpers
from backend.utils.rule_matcher import RuleConfigMatcher
from backend.utils.ruleset_content import inspect_domain_content


# Expectations are explicit rather than inferred from the conversion helper.
EDGE_CASES = [
    ('*', r'regexp:^[^.]+$',
     ['localhost', 'HOST.', 'xn--fsqu00a', '_srv', '-host', 'host-', '123'],
     ['a.b', 'a.b.']),
    ('+.*', r'regexp:^(?:[^.]+\.)*[^.]+$',
     ['localhost', 'localhost.', 'a.b', 'a.b.c', 'UPPER.HOST.'], []),
    ('.*', r'regexp:^(?:[^.]+\.)+[^.]+$',
     ['a.b', 'a.b.c', 'UPPER.HOST.'], ['localhost', 'localhost.']),
    ('*.*', r'regexp:^[^.]+\.[^.]+$',
     ['a.b', 'A.B.', '_a._b'], ['a', 'a.b.c']),
    ('*.*.*', r'regexp:^[^.]+\.[^.]+\.[^.]+$',
     ['a.b.c', 'A.B.C.'], ['a.b', 'a.b.c.d']),
    ('www.*', r'regexp:^www\.[^.]+$',
     ['www.com', 'WWW.XN--P1AI.'], ['www', 'a.www.com', 'www.a.com']),
    ('+.*.*', r'regexp:^(?:[^.]+\.)*[^.]+\.[^.]+$',
     ['a.b', 'a.b.c', 'a.b.c.d'], ['a']),
    ('.*.*', r'regexp:^(?:[^.]+\.)+[^.]+\.[^.]+$',
     ['a.b.c', 'a.b.c.d'], ['a', 'a.b']),
    ('*.local', r'regexp:^[^.]+\.local$',
     ['a.local', 'A.LOCAL.', '_srv.local'], ['local', 'a.b.local']),
    ('.local', r'regexp:.+\.local$',
     ['a.local', 'A.B.LOCAL.'], ['local', 'LOCAL.']),
    ('+.local', 'domain:local',
     ['local', 'LOCAL.', 'a.b.local'], ['notlocal']),
    ('+.a.*.com', r'regexp:^(?:[^.]+\.)*a\.[^.]+\.com$',
     ['a.b.com', 'x.a.b.com', 'X.Y.A.B.COM.'], ['a.com', 'a.x.y.com']),
    ('.a.*.com', r'regexp:^(?:[^.]+\.)+a\.[^.]+\.com$',
     ['x.a.b.com', 'X.Y.A.B.COM.'], ['a.b.com', 'a.x.y.com']),
    ('a.*.b.*', r'regexp:^a\.[^.]+\.b\.[^.]+$',
     ['a.x.b.com', 'A.X.B.COM.'], ['a.b.com', 'x.a.x.b.com', 'a.x.y.b.com']),
    ('xn--fsqu00a.xn--0zwm56d', 'full:xn--fsqu00a.xn--0zwm56d',
     ['xn--fsqu00a.xn--0zwm56d', 'XN--FSQU00A.XN--0ZWM56D.'],
     ['a.xn--fsqu00a.xn--0zwm56d']),
    ('_srv.example', 'full:_srv.example',
     ['_srv.example', '_SRV.EXAMPLE.'], ['srv.example']),
    ('-host.example', 'full:-host.example', ['-host.example'], ['host.example']),
    ('host-.example', 'full:host-.example', ['host-.example'], ['host.example']),
    ('_service._tcp.example.com', 'full:_service._tcp.example.com',
     ['_service._tcp.example.com', '_SERVICE._TCP.EXAMPLE.COM.'],
     ['service.tcp.example.com', '_service._udp.example.com']),
    ('*.*.example.com', r'regexp:^[^.]+\.[^.]+\.example\.com$',
     ['_service._tcp.example.com', '_SERVICE._TCP.EXAMPLE.COM.'],
     ['_sip.example.com', 'a._service._tcp.example.com']),
]


@pytest.mark.parametrize('pattern,expected,hits,misses', EDGE_CASES)
def test_domain_edges_match_page_and_dns_conversion(pattern, expected, hits, misses):
    inspected = inspect_domain_content(pattern, 'domain')
    assert inspected['content'] == pattern
    assert inspected['errors'] == []
    assert convert_rule_content_for_mosdns(pattern, 'domain') == expected
    assert convert_mosdns_rules(pattern.encode()).decode() == expected
    matcher = RuleConfigMatcher(
        [{'id': 'domain', 'itemType': 'ruleset', 'behavior': 'domain', 'format': 'text'}],
        [], lambda *_: pattern)
    for query in hits + misses:
        assert bool(matcher.match(query)) == (query in hits), (pattern, query)


def test_native_oxidns_domain_edges(tmp_path, monkeypatch):
    # Reuse the local UDP harness without collecting its test a second time.
    # Each provider has a dedicated port; RCODE 5 means hit and RCODE 3 means miss.
    monkeypatch.setattr(native_helpers, 'CASES', EDGE_CASES)
    native_helpers.test_native_oxidns_preserves_root_and_label_boundaries(tmp_path)
