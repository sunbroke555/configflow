"""A publication shares raw downloads, without retaining responses for the next one."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import tarfile
from threading import Barrier, Event, Lock, get_ident
import time
from urllib.parse import urlencode

import pytest
import requests
import yaml

from backend.agents import deployment_bundle as bundles


BASE_URL = 'https://config.test'
SOURCE_URL = 'https://rules.test/shared.list'


def config():
    return {'profile_id': 'default', 'rule_library': [],
            'system_config': {'server_domain': BASE_URL}}


def response(content):
    result = requests.Response()
    result.status_code = 200
    result._content = content
    result._content_consumed = True
    return result


def contents(bundle):
    with tarfile.open(fileobj=io.BytesIO(bundle.archive), mode='r:gz') as archive:
        return {item['path']: archive.extractfile('files/' + item['path']).read()
                for item in bundle.manifest['files']}


def raw_bundle(urls, *, progress=None):
    main = {'rule-providers': {
        str(index): {'type': 'http', 'format': 'mrs', 'behavior': 'domain',
                     'url': url, 'path': f'./rules/{index}.mrs'}
        for index, url in enumerate(urls)}}
    return bundles.build_deployment_bundle(
        {'id': 'fixture', 'service_type': 'mihomo', 'profile_id': 'default'},
        config(), yaml.safe_dump(main), base_url=BASE_URL, progress=progress)


def synchronize_consumers(monkeypatch, count=8):
    """Start all consumers together without making the network fetch wait on peers."""
    barrier = Barrier(count, timeout=5)
    original = bundles.materialize_rule

    def materialize(*args, **kwargs):
        if kwargs.get('depth', 0) == 0:
            barrier.wait()
        return original(*args, **kwargs)

    monkeypatch.setattr(bundles, 'materialize_rule', materialize)


@pytest.mark.parametrize('service', ['mosdns', 'oxidns'])
@pytest.mark.parametrize('yaml_payload', [False, True])
def test_callback_aliases_download_one_source_for_each_domain_and_ip_consumer(
        monkeypatch, service, yaml_payload):
    snapshot = config()
    snapshot['rule_library'] = [{'id': 'shared', 'name': 'Shared',
                                'source_type': 'url', 'url': SOURCE_URL}]
    lines = ['DOMAIN-SUFFIX,example.test', 'DOMAIN,exact.test',
             'IP-CIDR,192.0.2.0/24', 'IP-CIDR6,2001:db8::/32']
    original = (yaml.safe_dump({'payload': lines}) if yaml_payload else '\n'.join(lines)).encode()
    calls = []

    def fetch(url, **_kwargs):
        calls.append(url)
        return response(original)

    monkeypatch.setattr(bundles, 'request_rule', fetch)
    main = {'plugins': [
        {'tag': 'domains', 'type': 'domain_set', 'args': {'files': ['./rules/domains.txt']}},
        {'tag': 'ips', 'type': 'ip_set', 'args': {'files': ['./rules/ips.txt']}},
        {'tag': 'domains2', 'type': 'domain_set', 'args': {'files': ['./rules/domains2.txt']}},
    ]}
    downloads = [
        {'name': 'domain', 'local_path': './rules/domains.txt', 'url':
         BASE_URL + '/api/profiles/default/oxidns/rule-proxy?' + urlencode({
             'url': BASE_URL + '/api/profiles/default/rules/local/Shared', 'part': 'domain'})},
        {'name': 'ip', 'local_path': './rules/ips.txt', 'url':
         BASE_URL + '/api/profiles/default/mosdns/rule-proxy?' + urlencode({
             'url': BASE_URL + '/api/profiles/default/rule-library/content/shared', 'part': 'ip'})},
        {'name': 'direct', 'local_path': './rules/domains2.txt', 'url': SOURCE_URL},
    ]
    bundle = bundles.build_deployment_bundle(
        {'id': 'fixture', 'service_type': service, 'profile_id': 'default'}, snapshot,
        yaml.safe_dump(main), ruleset_downloads=downloads, base_url=BASE_URL)
    files = contents(bundle)
    assert calls == [SOURCE_URL]
    assert files['rules/domains.txt'] == files['rules/domains2.txt'] == b'domain:example.test\nfull:exact.test'
    assert files['rules/ips.txt'] == b'192.0.2.0/24\n2001:db8::/32'
    for item in bundle.manifest['files']:
        assert item['sha256'] == hashlib.sha256(files[item['path']]).hexdigest()


def test_concurrent_consumers_share_binary_response_and_report_progress_on_caller(monkeypatch):
    synchronize_consumers(monkeypatch)
    original = b'\x00MRS\xff\xfe\x80\r\n'
    calls = []
    progress = []
    caller = get_ident()

    def fetch(url, **_kwargs):
        calls.append(url)
        time.sleep(.02)
        return response(original)

    monkeypatch.setattr(bundles, 'request_rule', fetch)
    bundle = raw_bundle([SOURCE_URL] * 8,
                        progress=lambda done, total: progress.append((done, total, get_ident())))
    assert calls == [SOURCE_URL]
    assert all(contents(bundle)[f'rules/{index}.mrs'] == original for index in range(8))
    assert progress == [(done, 8, caller) for done in range(9)]


def test_concurrent_failure_is_shared_and_does_not_poison_next_publication(monkeypatch):
    synchronize_consumers(monkeypatch)
    calls = []
    fail = True
    progress = []

    def fetch(url, **_kwargs):
        calls.append(url)
        time.sleep(.02)
        if fail:
            raise requests.exceptions.ProxyError('http://secret:password@proxy.test')
        return response(b'new bytes')

    monkeypatch.setattr(bundles, 'request_rule', fetch)
    with pytest.raises(bundles.DeploymentPreparationError, match='代理连接失败') as exc:
        raw_bundle([SOURCE_URL] * 8, progress=lambda *value: progress.append(value))
    assert calls == [SOURCE_URL]
    assert 'password' not in str(exc.value)
    assert progress == [(0, 8)]
    fail = False
    bundle = raw_bundle([SOURCE_URL] * 8)
    assert calls == [SOURCE_URL, SOURCE_URL]
    assert contents(bundle)['rules/0.mrs'] == b'new bytes'


@pytest.mark.parametrize('error, reason', [
    (requests.exceptions.ConnectionError, '连接失败'),
    (requests.exceptions.ConnectTimeout, '下载超时'),
    (requests.exceptions.ReadTimeout, '下载超时'),
    (requests.exceptions.ProxyError, '代理连接失败'),
    (requests.exceptions.SSLError, '无法建立 HTTPS 安全连接'),
    (requests.exceptions.ChunkedEncodingError, '下载失败'),
    (ValueError, '下载失败'),
])
def test_download_errors_explain_cause_without_exposing_request_details(monkeypatch, error, reason):
    def fetch(*_args, **_kwargs):
        raise error('https://user:password@rules.test/list?token=secret')

    monkeypatch.setattr(bundles, 'request_rule', fetch)
    with pytest.raises(bundles.DeploymentPreparationError) as exc:
        raw_bundle([SOURCE_URL])
    message = str(exc.value)
    assert '无法下载「0」规则集' in message
    assert reason in message
    assert '本次发布已停止，设备仍使用原配置。' in message
    for private in ('password', 'token', 'secret', 'rules.test'):
        assert private not in message


@pytest.mark.parametrize('status', [403, 404, 503])
def test_http_download_failure_shows_status_and_closes_response(monkeypatch, status):
    result = response(b'')
    result.status_code = status
    result.url = 'https://user:password@rules.test/list?token=secret'
    closed = []
    monkeypatch.setattr(result, 'close', lambda: closed.append(True))
    monkeypatch.setattr(bundles, 'request_rule', lambda *_a, **_k: result)
    with pytest.raises(bundles.DeploymentPreparationError) as exc:
        raw_bundle([SOURCE_URL])
    assert f'HTTP {status}' in str(exc.value)
    assert 'password' not in str(exc.value)
    assert 'secret' not in str(exc.value)
    assert closed == [True]


def test_distinct_urls_and_separate_publications_do_not_share_bytes(monkeypatch):
    calls = []
    version = 'first'
    first, second = SOURCE_URL + '?part=a', SOURCE_URL + '?part=b'

    def fetch(url, **_kwargs):
        calls.append(url)
        return response((version + ':' + url).encode())

    monkeypatch.setattr(bundles, 'request_rule', fetch)
    old = raw_bundle([first, second, first])
    version = 'second'
    new = raw_bundle([first, second, first])
    assert Counter(calls) == {first: 2, second: 2}
    for bundle, prefix in ((old, 'first'), (new, 'second')):
        files = contents(bundle)
        assert files['rules/0.mrs'] == files['rules/2.mrs'] == (prefix + ':' + first).encode()
        assert files['rules/1.mrs'] == (prefix + ':' + second).encode()


def test_deduplicated_destinations_still_count_toward_total_size(monkeypatch):
    monkeypatch.setattr(bundles, 'MAX_TOTAL_BYTES', 100)
    calls = []

    def fetch(url, **_kwargs):
        calls.append(url)
        return response(b'x' * 40)

    monkeypatch.setattr(bundles, 'request_rule', fetch)
    with pytest.raises(bundles.DeploymentPreparationError, match='Deployment exceeds size limit'):
        raw_bundle([SOURCE_URL] * 3)
    assert calls == [SOURCE_URL]


def test_source_file_size_limit_still_applies_before_sharing(monkeypatch):
    synchronize_consumers(monkeypatch, count=3)
    monkeypatch.setattr(bundles, 'MAX_FILE_BYTES', 8)
    calls = []

    def fetch(url, **_kwargs):
        calls.append(url)
        time.sleep(.02)
        return response(b'x' * 9)

    monkeypatch.setattr(bundles, 'request_rule', fetch)
    with pytest.raises(bundles.DeploymentPreparationError, match='Rule exceeds deployment file size limit'):
        raw_bundle([SOURCE_URL] * 3)
    assert calls == [SOURCE_URL]


def test_distinct_downloads_remain_limited_to_eight_workers(monkeypatch):
    lock = Lock()
    active = maximum = 0

    def fetch(_url, **_kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            time.sleep(.02)
            return response(b'content')
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(bundles, 'request_rule', fetch)
    raw_bundle([SOURCE_URL + '/' + str(index) for index in range(19)])
    assert 1 < maximum <= 8


def test_empty_download_progress(monkeypatch):
    monkeypatch.setattr(bundles, 'request_rule', lambda *_a, **_k: pytest.fail('unexpected network'))
    progress = []
    raw_bundle([], progress=lambda *value: progress.append(value))
    assert progress == [(0, 0)]


def test_completed_download_reports_progress_while_first_source_is_still_waiting(monkeypatch):
    completed = Event()
    progress = []

    def fetch(url, **_kwargs):
        if url.endswith('/slow'):
            assert completed.wait(3), 'completed sources must not wait behind the first source'
        return response(url.encode())

    def report(done, total):
        progress.append((done, total))
        if done == 1:
            completed.set()

    monkeypatch.setattr(bundles, 'request_rule', fetch)
    raw_bundle([SOURCE_URL + '/slow', SOURCE_URL + '/fast'], progress=report)
    assert progress == [(0, 2), (1, 2), (2, 2)]


def sequential_downloads(monkeypatch):
    # Serial consumption makes the cache's retention/eviction order observable
    # separately from the overlapping requests covered by the singleflight test.
    monkeypatch.setattr(bundles, 'ThreadPoolExecutor', lambda **_kwargs: ThreadPoolExecutor(max_workers=1))


def test_raw_response_byte_budget_evicts_least_recently_used_source(monkeypatch):
    sequential_downloads(monkeypatch)
    monkeypatch.setattr(bundles, 'MAX_RAW_CACHE_BYTES', 6)
    calls = []
    first, second, third = (SOURCE_URL + '/' + key for key in ('a', 'b', 'c'))

    def fetch(url, **_kwargs):
        calls.append(url)
        return response(url[-1:].encode() * 3)

    monkeypatch.setattr(bundles, 'request_rule', fetch)
    urls = [first, second, first, third, second]
    bundle = raw_bundle(urls)
    assert calls == [first, second, third, second]
    files = contents(bundle)
    assert [files[f'rules/{index}.mrs'] for index in range(len(urls))] == [b'aaa', b'bbb', b'aaa', b'ccc', b'bbb']


def test_single_response_larger_than_cache_budget_is_not_retained(monkeypatch):
    sequential_downloads(monkeypatch)
    monkeypatch.setattr(bundles, 'MAX_RAW_CACHE_BYTES', 2)
    calls = []

    def fetch(url, **_kwargs):
        calls.append(url)
        return response(b'big')

    monkeypatch.setattr(bundles, 'request_rule', fetch)
    bundle = raw_bundle([SOURCE_URL] * 3)
    assert calls == [SOURCE_URL] * 3
    assert [contents(bundle)[f'rules/{index}.mrs'] for index in range(3)] == [b'big'] * 3


def test_zero_byte_responses_are_also_bounded_by_cache_entry_count(monkeypatch):
    sequential_downloads(monkeypatch)
    monkeypatch.setattr(bundles, 'MAX_RAW_CACHE_ENTRIES', 2)
    calls = []
    urls = [SOURCE_URL + '/' + key for key in ('a', 'b', 'c', 'a')]

    def fetch(url, **_kwargs):
        calls.append(url)
        return response(b'')

    monkeypatch.setattr(bundles, 'request_rule', fetch)
    raw_bundle(urls)
    assert calls == urls


def test_oversized_response_still_shares_with_in_flight_consumers(monkeypatch):
    synchronize_consumers(monkeypatch)
    monkeypatch.setattr(bundles, 'MAX_RAW_CACHE_BYTES', 2)
    calls = []

    def fetch(url, **_kwargs):
        calls.append(url)
        time.sleep(.02)
        return response(b'big')

    monkeypatch.setattr(bundles, 'request_rule', fetch)
    bundle = raw_bundle([SOURCE_URL] * 8)
    assert calls == [SOURCE_URL]
    assert contents(bundle)['rules/7.mrs'] == b'big'
