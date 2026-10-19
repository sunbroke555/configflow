"""Bounded retries for interrupted downloads, without retrying persistent errors."""
import errno
from http.client import IncompleteRead, RemoteDisconnected
import ssl
from types import SimpleNamespace

import pytest
import requests
from urllib3 import exceptions as urllib_errors
from urllib3.util.ssl_match_hostname import CertificateError

from backend.utils.rule_fetch import RuleFetchError, request_rule


SOURCE = 'https://raw.githubusercontent.com/owner/repo/main/rules.list'
MIRROR = 'https://mirror.test/'
CONFIG = {'system_config': {}}


def response(status=200, content=b'complete rule file'):
    result = requests.Response()
    result.status_code = status
    result.url = SOURCE
    result._content = content
    result._content_consumed = True
    return result


def tls_eof():
    return requests.exceptions.SSLError(urllib_errors.MaxRetryError(
        None, SOURCE, urllib_errors.SSLError(ssl.SSLEOFError(
            8, '[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol'))))


@pytest.fixture
def sleeps(monkeypatch):
    delays = []
    monkeypatch.setattr('backend.utils.rule_fetch.time.sleep', delays.append)
    return delays


@pytest.mark.parametrize('method', ['get', 'head'])
@pytest.mark.parametrize('proxy', ['', 'http://proxy.test:7890'])
def test_tls_eof_recovers_on_third_attempt_with_same_options(monkeypatch, sleeps, method, proxy):
    calls = []

    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        if len(calls) < 3:
            raise tls_eof()
        return response()

    monkeypatch.setattr(requests, method, fetch)
    config = {'system_config': {'rule_fetch_proxy': proxy, 'github_proxy_domain': MIRROR}}
    result = request_rule(SOURCE, method=method, config_data=config, timeout=(10, 60))
    assert result.content == b'complete rule file'
    assert sleeps == [1, 2]
    assert [url for url, _ in calls] == [SOURCE] * 3
    assert all(options['verify'] is True and options['timeout'] == (10, 60)
               and options['proxies'] == {'http': proxy, 'https': proxy}
               for _, options in calls)


@pytest.mark.parametrize('failure', [
    requests.ConnectionError(urllib_errors.ProtocolError('aborted', ConnectionResetError(errno.ECONNRESET, 'reset'))),
    requests.ConnectionError(ConnectionAbortedError(errno.ECONNABORTED, 'aborted')),
    requests.ConnectionError(BrokenPipeError(errno.EPIPE, 'broken pipe')),
    requests.ConnectionError(urllib_errors.ProtocolError('aborted', RemoteDisconnected('no response'))),
    requests.exceptions.ChunkedEncodingError(urllib_errors.ProtocolError('broken', IncompleteRead(b'partial', 10))),
    requests.exceptions.ChunkedEncodingError(urllib_errors.ProtocolError('Response ended prematurely')),
    requests.exceptions.ProxyError(urllib_errors.MaxRetryError(None, SOURCE,
        urllib_errors.ProxyError('proxy disconnected', urllib_errors.SSLError(ssl.SSLEOFError(8, 'EOF'))))),
    requests.exceptions.SSLError('[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol'),
])
def test_disconnect_retries_are_bounded_and_preserve_final_error(monkeypatch, sleeps, failure):
    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        raise failure

    monkeypatch.setattr(requests, 'get', fetch)
    with pytest.raises(type(failure)) as caught:
        request_rule(SOURCE, config_data=CONFIG, timeout=(10, 60))
    assert caught.value is failure
    assert calls == [SOURCE] * 3
    assert sleeps == [1, 2]


@pytest.mark.parametrize('failure', [
    requests.exceptions.SSLError(urllib_errors.MaxRetryError(None, SOURCE,
        urllib_errors.SSLError(ssl.SSLCertVerificationError(1, 'certificate verify failed')))),
    requests.exceptions.ProxyError(urllib_errors.MaxRetryError(None, SOURCE,
        urllib_errors.ProxyError('proxy failed', ssl.SSLCertVerificationError(1, 'hostname mismatch')))),
    requests.exceptions.SSLError('[SSL: WRONG_VERSION_NUMBER] wrong version number'),
    requests.exceptions.ConnectTimeout('connect timeout'),
    requests.exceptions.ReadTimeout('read timeout'),
    requests.ConnectionError(urllib_errors.ReadTimeoutError(None, SOURCE, 'read timeout')),
    requests.ConnectionError(ConnectionRefusedError(errno.ECONNREFUSED, 'refused')),
    requests.ConnectionError('name resolution failed'),
    requests.exceptions.ProxyError('proxy authentication failed'),
    requests.exceptions.ContentDecodingError('invalid compressed content'),
    requests.exceptions.ChunkedEncodingError('invalid chunk length'),
    requests.exceptions.ChunkedEncodingError(urllib_errors.ProtocolError('broken',
        urllib_errors.InvalidChunkLength(SimpleNamespace(tell=lambda: 0, length_remaining=None), b'bad\r\n'))),
])
def test_persistent_errors_and_timeouts_do_not_retry(monkeypatch, sleeps, failure):
    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        raise failure

    monkeypatch.setattr(requests, 'get', fetch)
    with pytest.raises(type(failure)):
        request_rule(SOURCE, config_data=CONFIG)
    assert calls == [SOURCE]
    assert sleeps == []


@pytest.mark.parametrize('certificate_error', [
    ssl.SSLCertVerificationError(1, 'certificate verify failed'),
    CertificateError("hostname 'wrong.test' doesn't match 'expected.test'"),
])
def test_certificate_error_takes_precedence_over_disconnect_in_exception_chain(
        monkeypatch, sleeps, certificate_error):
    failure = tls_eof()
    failure.__cause__ = certificate_error
    failure.__cause__.__context__ = failure  # Defensive traversal of cyclic wrappers.
    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        raise failure

    monkeypatch.setattr(requests, 'get', fetch)
    with pytest.raises(requests.exceptions.SSLError):
        request_rule(SOURCE, config_data=CONFIG)
    assert calls == [SOURCE]
    assert sleeps == []


def test_read_timeout_is_not_retried_even_with_disconnect_context(monkeypatch, sleeps):
    timeout = urllib_errors.ReadTimeoutError(None, SOURCE, 'read timeout')
    timeout.__context__ = ConnectionResetError(errno.ECONNRESET, 'reset')
    failure = requests.ConnectionError(timeout)
    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        raise failure

    monkeypatch.setattr(requests, 'get', fetch)
    with pytest.raises(requests.ConnectionError):
        request_rule(SOURCE, config_data=CONFIG)
    assert calls == [SOURCE]
    assert sleeps == []


@pytest.mark.parametrize('mirror_fails', [False, True])
def test_exhaust_source_before_mirror_and_bound_each_candidate(monkeypatch, sleeps, mirror_fails):
    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        if url == SOURCE or mirror_fails:
            raise tls_eof()
        return response()

    monkeypatch.setattr(requests, 'get', fetch)
    config = {'system_config': {'github_proxy_domain': MIRROR}}
    if mirror_fails:
        with pytest.raises(RuleFetchError) as caught:
            request_rule(SOURCE, config_data=config)
        assert [label for label, _ in caught.value.attempts] == ['原始地址', 'GitHub 代理地址']
    else:
        assert request_rule(SOURCE, config_data=config).content == b'complete rule file'
    assert calls == [SOURCE] * 3 + [MIRROR + SOURCE] * (3 if mirror_fails else 1)
    assert sleeps == [1, 2] * (2 if mirror_fails else 1)


@pytest.mark.parametrize('status', [403, 404, 429, 503])
def test_http_failures_go_to_mirror_without_backoff(monkeypatch, sleeps, status):
    calls, closed = [], []

    def fetch(url, **kwargs):
        calls.append(url)
        result = response(status if url == SOURCE else 200)
        monkeypatch.setattr(result, 'close', lambda: closed.append(url))
        return result

    monkeypatch.setattr(requests, 'get', fetch)
    config = {'system_config': {'github_proxy_domain': MIRROR}}
    assert request_rule(SOURCE, config_data=config).status_code == 200
    assert calls == [SOURCE, MIRROR + SOURCE]
    assert closed == [SOURCE]
    assert sleeps == []


def test_single_url_without_bounded_body_preserves_http_error_response(monkeypatch, sleeps):
    result = response(503)
    monkeypatch.setattr(requests, 'get', lambda *a, **kw: result)
    assert request_rule(SOURCE, config_data=CONFIG) is result
    assert sleeps == []


def test_partial_response_is_closed_before_backoff_and_bytes_are_discarded(monkeypatch):
    events = []

    def broken_body(**kwargs):
        yield b'partial content that must not appear in the final file'
        raise requests.exceptions.ChunkedEncodingError(
            urllib_errors.ProtocolError('truncated', IncompleteRead(b'', 10)))

    def fetch(self, url, **kwargs):
        events.append('fetch')
        result = response()
        if events.count('fetch') < 3:
            monkeypatch.setattr(result, 'iter_content', broken_body)
            monkeypatch.setattr(result, 'close', lambda: events.append('close'))
        return result

    monkeypatch.setattr('backend.utils.rule_fetch._RuleSession.get', fetch)
    monkeypatch.setattr('backend.utils.rule_fetch.time.sleep', events.append)
    result = request_rule(SOURCE, config_data=CONFIG, stream=True, max_bytes=1024)
    assert result.content == b'complete rule file'
    assert events == ['fetch', 'close', 1, 'fetch', 'close', 2, 'fetch']


def test_oversize_body_closes_without_retry_or_mirror(monkeypatch, sleeps):
    calls, closed = [], []

    def fetch(self, url, **kwargs):
        calls.append(url)
        result = response(content=b'oversize')
        monkeypatch.setattr(result, 'close', lambda: closed.append(url))
        return result

    monkeypatch.setattr('backend.utils.rule_fetch._RuleSession.get', fetch)
    with pytest.raises(ValueError, match='size limit'):
        request_rule(SOURCE, config_data={'system_config': {'github_proxy_domain': MIRROR}},
                     stream=True, max_bytes=1)
    assert calls == closed == [SOURCE]
    assert sleeps == []
