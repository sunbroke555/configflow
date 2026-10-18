"""Source-first mirror fallback, including failures while reading a resource."""
import pytest
import requests

from backend.utils.rule_fetch import RuleFetchError, request_rule

SOURCE = 'https://github.com/owner/arbitrary-repo/releases/download/v2/custom.bin'
MIRROR = 'https://gh-proxy.com/'


def reply(status=200, content=b'complete file'):
    response = requests.Response()
    response.status_code = status
    response._content = content
    response._content_consumed = True
    response.url = SOURCE
    return response


@pytest.mark.parametrize('proxy', ['', 'http://127.0.0.1:7890'])
@pytest.mark.parametrize('prefixed', [False, True])
@pytest.mark.parametrize('failure', [404, 503, requests.ConnectionError('EOF')])
def test_arbitrary_github_assets_try_original_then_mirror(monkeypatch, proxy, prefixed, failure):
    calls = []
    closed = []
    def fetch(url, **kwargs):
        calls.append((url, kwargs['proxies']))
        if url == SOURCE:
            if isinstance(failure, Exception):
                raise failure
            result = reply(failure)
            monkeypatch.setattr(result, 'close', lambda: closed.append(True))
            return result
        return reply()
    monkeypatch.setattr(requests, 'get', fetch)
    cfg = {'system_config': {'github_proxy_domain': MIRROR, 'rule_fetch_proxy': proxy}}
    result = request_rule(MIRROR + SOURCE if prefixed else SOURCE, config_data=cfg)
    assert result.content == b'complete file'
    assert calls == [(url, {'http': proxy, 'https': proxy}) for url in (SOURCE, MIRROR + SOURCE)]
    assert closed == ([] if isinstance(failure, Exception) else [True])


def test_successful_original_does_not_request_mirror(monkeypatch):
    calls = []
    def fetch(url, **kwargs):
        calls.append(url)
        return reply()
    monkeypatch.setattr(requests, 'get', fetch)
    request_rule(SOURCE, config_data={'system_config': {'github_proxy_domain': MIRROR}})
    assert calls == [SOURCE]


def test_incomplete_stream_is_retried_without_retaining_partial_bytes(monkeypatch):
    calls, closed = [], []
    def broken_stream(**kwargs):
        yield b'partial file'
        raise requests.exceptions.ChunkedEncodingError('incomplete body')
    def fetch(self, url, **kwargs):
        calls.append(url)
        result = reply()
        if url == SOURCE:
            monkeypatch.setattr(result, 'iter_content', broken_stream)
            monkeypatch.setattr(result, 'close', lambda: closed.append(True))
        return result
    monkeypatch.setattr('backend.utils.rule_fetch._RuleSession.get', fetch)
    result = request_rule(SOURCE, config_data={'system_config': {'github_proxy_domain': MIRROR}},
                          stream=True, max_bytes=1024)
    assert result.content == b'complete file'
    assert calls == [SOURCE, MIRROR + SOURCE]
    assert closed == [True]


def test_both_failures_are_reported_without_credentials(monkeypatch):
    def fetch(*args, **kwargs):
        raise requests.exceptions.ProxyError('failed via http://user:private@proxy.test:7890')
    monkeypatch.setattr(requests, 'get', fetch)
    with pytest.raises(RuleFetchError) as caught:
        request_rule(SOURCE, config_data={'system_config': {'github_proxy_domain': MIRROR}})
    text = str(caught.value)
    assert '原始地址' in text and 'GitHub 代理地址' in text
    assert 'private' not in text and 'user:' not in text


def test_non_github_source_never_uses_mirror(monkeypatch):
    calls = []
    def fetch(url, **kwargs):
        calls.append(url)
        raise requests.ConnectionError('unavailable')
    monkeypatch.setattr(requests, 'get', fetch)
    with pytest.raises(requests.ConnectionError):
        request_rule('https://custom.test/data', config_data={'system_config': {'github_proxy_domain': MIRROR}})
    assert calls == ['https://custom.test/data']


def test_mirror_error_redacts_userinfo_nested_in_path(monkeypatch):
    source = 'https://private-user:private-password@github.com/owner/repo/raw/main/rules'
    def fetch(url, **kwargs):
        raise requests.ConnectionError('failed to download ' + url)
    monkeypatch.setattr(requests, 'get', fetch)
    with pytest.raises(RuleFetchError) as caught:
        request_rule(source, config_data={'system_config': {'github_proxy_domain': MIRROR}})
    assert 'private-user' not in str(caught.value)
    assert 'private-password' not in str(caught.value)
