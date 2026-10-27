"""Optional real-core regression for domain-only DNS routing.

Run on the test server with OXIDNS_TEST_BINARY pointing to a real core. The
loopback upstream deliberately returns an address inside a direct IP range for
unknown names: no such answer may influence a proxy-default query.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
from pathlib import Path

import pytest
import yaml

from backend.converters.oxidns import generate_oxidns_config
from scripts.benchmark_oxidns_domain_routing import DelayedDNS, fixture_config, query, running_core


BINARY = os.environ.get('OXIDNS_TEST_BINARY')
pytestmark = pytest.mark.skipif(not BINARY, reason='Set OXIDNS_TEST_BINARY for real-core acceptance')


@contextmanager
def scenario(tmp_path, default='proxy', cache=True, native_document=None):
    local, remote = DelayedDNS(True), DelayedDNS(False)
    try:
        config = fixture_config(default, cache)
        if native_document is not None:
            config['oxidns'].update(config_mode='native', native_config=yaml.safe_dump(native_document))
        document = yaml.safe_load(generate_oxidns_config(config))
        with running_core(BINARY, document, tmp_path, local, remote) as port:
            yield port, local, remote
    finally:
        local.close()
        remote.close()


@pytest.mark.parametrize('default', ['direct', 'proxy'])
@pytest.mark.parametrize('qtype', [1, 28])
@pytest.mark.parametrize('tcp', [False, True])
def test_unmatched_domains_use_only_default_even_if_local_answer_would_match_ip(tmp_path, default, qtype, tcp):
    with scenario(tmp_path, default) as (port, local, remote):
        for name in ('poison.unlisted.test', 'ordinary.unlisted.test'):
            reply = query(port, name, qtype=qtype, tcp=tcp)
            if default == 'proxy':
                expected = '198.51.100.20' if qtype == 1 else '2001:db8:ffff::20'
            elif name.startswith('poison.'):
                expected = '10.1.2.3' if qtype == 1 else '2001:db8:1::3'
            else:
                expected = '203.0.114.44' if qtype == 1 else '2001:db8:2::44'
            assert reply['rcode'] == 0 and reply['ips'] == [expected]
            assert local.count(name) == int(default == 'direct')
            assert remote.count(name) == int(default == 'proxy')


@pytest.mark.parametrize('qtype', [1, 28])
def test_domain_choices_and_mixed_ruleset_domain_part_remain_effective(tmp_path, qtype):
    with scenario(tmp_path) as (port, local, remote):
        for name, direct in [('one.direct.test', True), ('one.proxy.test', False), ('one.mixed.test', True)]:
            reply = query(port, name, qtype=qtype)
            expected = ('10.1.2.3' if direct else '198.51.100.20') if qtype == 1 else (
                '2001:db8:1::3' if direct else '2001:db8:ffff::20')
            assert reply['rcode'] == 0 and reply['ips'] == [expected]
            assert local.count(name) == int(direct)
            assert remote.count(name) == int(not direct)


@pytest.mark.parametrize('cache', [False, True])
def test_repeated_unmatched_queries_do_not_populate_or_use_local_probe_cache(tmp_path, cache):
    with scenario(tmp_path, cache=cache) as (port, local, remote):
        for _ in range(3):
            assert query(port, 'poison.repeated.test')['ips'] == ['198.51.100.20']
            assert query(port, 'cached.direct.test')['ips'] == ['10.1.2.3']
        assert local.count('poison.repeated.test') == 0
        assert remote.count('poison.repeated.test') == 3
        assert local.count('cached.direct.test') == (1 if cache else 3)


@pytest.mark.parametrize('default', ['direct', 'proxy'])
def test_failed_default_does_not_implicitly_try_the_other_upstream(tmp_path, default):
    with scenario(tmp_path, default) as (port, local, remote):
        name = 'failed.unlisted.test'
        selected, other = (local, remote) if default == 'direct' else (remote, local)
        selected.fail_names.add(name)
        reply = query(port, name)
        assert reply['rcode'] == 2 and reply['ips'] == []
        assert selected.count(name) >= 1
        assert other.count(name) == 0


def test_concurrent_unmatched_queries_never_contact_local_upstream(tmp_path):
    with scenario(tmp_path) as (port, local, remote):
        names = [f'poison.parallel-{i}.test' for i in range(48)]
        before = local.count()
        with ThreadPoolExecutor(max_workers=12) as pool:
            replies = list(pool.map(lambda name: query(port, name), names))
        assert all(reply['rcode'] == 0 and reply['ips'] == ['198.51.100.20'] for reply in replies)
        assert local.count() == before
        assert all(remote.count(name) == 1 for name in names)


@pytest.mark.skipif(not os.environ.get('OXIDNS_LEGACY_FIXTURE_DIR'), reason='Set OXIDNS_LEGACY_FIXTURE_DIR for old native migration acceptance')
def test_old_generated_native_config_is_migrated_before_real_core_start(tmp_path):
    old = yaml.safe_load((Path(os.environ['OXIDNS_LEGACY_FIXTURE_DIR']) / 'proxy-cache-1.yaml').read_text())
    with scenario(tmp_path, native_document=old) as (port, local, remote):
        reply = query(port, 'poison.legacy-unlisted.test')
        assert reply['rcode'] == 0 and reply['ips'] == ['198.51.100.20']
        assert local.count('poison.legacy-unlisted.test') == 0
        assert remote.count('poison.legacy-unlisted.test') == 1
