"""Shared proxy settings for server-side rule downloads.

Rule sources deliberately use normal hostname resolution, including Fake-IP
networks. Only URL syntax is checked here; this is not a public-IP-only gate.
"""
import errno
from http.client import IncompleteRead, RemoteDisconnected
import logging
import ssl
import time
from urllib.parse import unquote, urljoin, urlsplit
import requests
from urllib3.exceptions import InvalidChunkLength, ProtocolError, TimeoutError as Urllib3TimeoutError
from urllib3.util.ssl_match_hostname import CertificateError


logger = logging.getLogger(__name__)


class _RuleSession(requests.Session):
    def resolve_redirects(self, *args, **kwargs):
        # Requests otherwise buffers redirect bodies to prepare response.next,
        # even with allow_redirects=False. request_rule owns all redirect hops.
        return iter(())


def _origin(parsed):
    return (parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80))


def validate_rule_url(url: str):
    if not isinstance(url, str) or any(ord(c) < 32 or ord(c) == 127 for c in url):
        raise ValueError('Invalid HTTP/HTTPS URL')
    try:
        parsed = urlsplit(url)
        port = parsed.port
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            raise ValueError
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        if any(c.isspace() for c in parsed.netloc) or '\\' in parsed.netloc:
            raise ValueError
    except (ValueError, UnicodeError) as exc:
        raise ValueError('Invalid HTTP/HTTPS URL') from exc
    return parsed


def normalize_rule_fetch_proxy(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError('规则下载代理必须是 HTTP 或 HTTPS 地址')
    value = value.strip()
    if not value:
        return ''
    try:
        parsed = validate_rule_url(value)
        if parsed.path not in ('', '/') or parsed.query or parsed.fragment:
            raise ValueError
    except ValueError as exc:
        raise ValueError('规则下载代理必须是 HTTP 或 HTTPS 地址，不能包含路径、查询参数或片段') from exc
    return parsed._replace(path='').geturl()


def rule_fetch_settings(config_data=None):
    if config_data is None:
        # Import lazily: configuration validation also uses this module.
        from backend.common.config import get_system_config
        config_data = get_system_config()
    return config_data.get('system_config', {})


def _internal_rule_base(url: str, config_data=None):
    settings = rule_fetch_settings(config_data)
    parsed = urlsplit(url)
    from flask import has_request_context, request
    bases = [settings.get('server_domain', '')]
    if has_request_context():
        bases.append(request.host_url)
    for base in bases:
        if not base:
            continue
        origin = urlsplit(base)
        if _origin(parsed) != _origin(origin):
            continue
        prefix = origin.path.rstrip('/') + '/api/'
        if parsed.path.startswith(prefix):
            return origin
    return None


def is_internal_rule_url(url: str, config_data=None) -> bool:
    """Keep requests to ConfigFlow itself away from the download proxy."""
    return _internal_rule_base(url, config_data) is not None


def get_rule_fetch_proxy(config_data=None) -> str:
    return normalize_rule_fetch_proxy(rule_fetch_settings(config_data).get('rule_fetch_proxy', ''))


def rule_request_options(url: str, config_data=None) -> dict:
    if is_internal_rule_url(url, config_data):
        return {'proxies': {'http': '', 'https': ''}}
    proxy = get_rule_fetch_proxy(config_data)
    return {'proxies': {'http': proxy, 'https': proxy}}


def _request_with_https_proxy(url, method, options, kwargs):
    """Verify the proxy's TLS even when the rule URL itself uses plain HTTP."""
    import os
    import ssl
    import requests

    with _RuleSession() as session:
        settings = session.merge_environment_settings(
            url, dict(options['proxies']), kwargs.get('stream', False),
            kwargs.get('verify', True), kwargs.get('cert'),
        )
        verify = settings['verify']
        if verify is False:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        else:
            bundle = verify if isinstance(verify, (str, os.PathLike)) else requests.certs.where()
            context = ssl.create_default_context(**{
                'capath' if os.path.isdir(bundle) else 'cafile': bundle,
            })

        class HTTPSProxyAdapter(requests.adapters.HTTPAdapter):
            def proxy_manager_for(self, proxy, **proxy_kwargs):
                proxy_kwargs['proxy_ssl_context'] = context
                return super().proxy_manager_for(proxy, **proxy_kwargs)

        adapter = HTTPSProxyAdapter()
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        return getattr(session, method)(url, **kwargs, **options)


def _request_rule_once(url: str, *, method='get', config_data=None, max_redirects=3, **kwargs):
    """Fetch a rule by hostname and choose its proxy again on every redirect.

    An empty setting explicitly selects a direct connection. Explicit
    proxies never fall back to a direct connection. Internal API requests are
    direct and may only redirect within that origin's API paths.
    """
    import requests
    if config_data is None:
        from backend.common.config import get_system_config
        config_data = get_system_config()
    if method not in ('get', 'head'):
        raise ValueError('Rule requests only support GET and HEAD')
    kwargs['allow_redirects'] = False
    kwargs.setdefault('verify', True)
    current = url
    for hop in range(max_redirects + 1):
        parsed = validate_rule_url(current)
        internal = is_internal_rule_url(current, config_data)
        options = rule_request_options(current, config_data)
        proxy = options.get('proxies', {}).get(parsed.scheme, '')
        response = None
        if internal:
            from backend.utils.local_http import request_local
            base = _internal_rule_base(current, config_data)
            response = request_local(current, method=method, prefix=base.path.rstrip('/'), **kwargs)
        if response is not None:
            pass
        elif urlsplit(proxy).scheme == 'https':
            response = _request_with_https_proxy(current, method, options, kwargs)
        elif kwargs.get('stream'):
            with _RuleSession() as session:
                response = getattr(session, method)(current, **kwargs, **options)
        else:
            response = getattr(requests, method)(current, **kwargs, **options)
        if response.status_code not in (301, 302, 303, 307, 308):
            return response
        try:
            location = response.headers.get('Location')
            if not location:
                raise ValueError('Redirect without Location')
            target = urljoin(current, location)
            target_parts = validate_rule_url(target)
            if internal and (_origin(parsed) != _origin(target_parts)
                             or not is_internal_rule_url(target, config_data)):
                raise ValueError('Internal API redirect leaves its origin')
            if hop == max_redirects:
                raise ValueError('Too many redirects')
            current = target
        finally:
            response.close()


class RuleFetchError(requests.RequestException):
    """Both source and configured GitHub mirror failed, with safe diagnostics."""

    def __init__(self, attempts, secrets=()):
        from backend.agents.diagnostics import failure_text
        self.attempts = attempts
        self.last_error = attempts[-1][1]
        super().__init__(failure_text('\n'.join(
            f'{label}：{type(error).__name__}: {error}' for label, error in attempts), secrets))


def github_download_candidates(url, config_data):
    """Keep original paths intact, including GitHub Release assets.

    Generated configurations may already contain this mirror's prefix. Unwrap
    only that exact configured prefix and only when the remainder is GitHub.
    """
    from backend.converters.mihomo import apply_github_proxy_domain
    if is_internal_rule_url(url, config_data):
        return [('原始地址', url)]
    mirror = rule_fetch_settings(config_data).get('github_proxy_domain', '')
    if isinstance(mirror, str) and mirror.strip():
        prefix = mirror.strip()
        if not prefix.startswith(('http://', 'https://')):
            prefix = 'https://' + prefix
        prefix = prefix.rstrip('/') + '/'
        if url.startswith(prefix):
            original = url[len(prefix):]
            if apply_github_proxy_domain(original, config_data) == url:
                url = original
    mirrored = apply_github_proxy_domain(url, config_data)
    return [('原始地址', url)] + ([('GitHub 代理地址', mirrored)] if mirrored != url else [])


def _is_transient_disconnect(error):
    """Recognize interrupted connections through Requests/urllib3 wrappers.

    Do not retry all SSLError/ConnectionError instances: certificate failures,
    DNS errors, refused connections and timeouts keep their existing behavior.
    """
    pending, seen = [error], set()
    disconnected = False
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        message = str(current).upper()
        # Certificate errors take precedence even if another wrapper mentions
        # an EOF/reset. Never relax TLS verification to recover a connection.
        if (isinstance(current, (ssl.SSLCertVerificationError, CertificateError))
                or 'CERTIFICATE_VERIFY_FAILED' in message
                or 'CERTIFICATE VERIFY FAILED' in message
                or 'HOSTNAME MISMATCH' in message):
            return False
        if isinstance(current, (requests.exceptions.Timeout, Urllib3TimeoutError, TimeoutError,
                                requests.exceptions.HTTPError, requests.exceptions.ContentDecodingError,
                                InvalidChunkLength)):
            return False
        if (isinstance(current, (ssl.SSLEOFError, ConnectionResetError,
                                 ConnectionAbortedError, BrokenPipeError,
                                 RemoteDisconnected, IncompleteRead))
                or (isinstance(current, OSError)
                    and current.errno in (errno.ECONNRESET, errno.ECONNABORTED, errno.EPIPE))
                or (isinstance(current, (requests.exceptions.SSLError, ssl.SSLError))
                    and ('UNEXPECTED_EOF_WHILE_READING' in message
                         or 'EOF OCCURRED IN VIOLATION OF PROTOCOL' in message))
                or (isinstance(current, ProtocolError)
                    and current.args == ('Response ended prematurely',))):
            disconnected = True
        children = list(current.args)
        children.extend(getattr(current, name, None)
                        for name in ('reason', 'original_error', '__cause__', '__context__'))
        pending.extend(child for child in children if isinstance(child, BaseException))
    return disconnected


def _request_rule_with_retries(url, *, check_status, max_bytes, **kwargs):
    delays = (1, 2)
    for attempt in range(len(delays) + 1):
        response = None
        try:
            response = _request_rule_once(url, **kwargs)
            if check_status:
                response.raise_for_status()
            if max_bytes is not None:
                body = bytearray()
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise ValueError('Resource exceeds deployment file size limit')
                response._content = bytes(body)
                response._content_consumed = True
            return response
        except BaseException as exc:
            if response is not None:
                response.close()
            if (not isinstance(exc, requests.RequestException)
                    or attempt == len(delays) or not _is_transient_disconnect(exc)):
                raise
            from backend.utils.url_utils import safe_url_for_log
            logger.info('规则下载连接中断（%s），%s 秒后重试第 %d 次：%s', type(exc).__name__,
                        delays[attempt], attempt + 1, safe_url_for_log(url))
            time.sleep(delays[attempt])


def request_rule(url: str, *, method='get', config_data=None, max_redirects=3,
                 max_bytes=None, **kwargs):
    """Try the source first, then its configured GitHub mirror on failure.

    The same explicit forward proxy (or direct transport when empty) applies
    to every attempt. Each candidate retries transient disconnects after 1s
    and 2s, with unchanged certificate checks and per-request timeouts.
    max_bytes consumes a bounded body inside the retry so a broken stream is
    retried as well, never returning a partial resource. Without max_bytes,
    a streamed response remains lazy; errors after returning belong to its caller.
    """
    if config_data is None:
        from backend.common.config import get_system_config
        config_data = get_system_config()
    attempts = []
    candidates = github_download_candidates(url, config_data)
    for index, (label, candidate) in enumerate(candidates):
        try:
            # Keep the established response API for callers of a single URL.
            return _request_rule_with_retries(
                candidate, method=method, config_data=config_data, max_redirects=max_redirects,
                check_status=len(candidates) > 1 or max_bytes is not None,
                max_bytes=max_bytes, **kwargs)
        except requests.RequestException as exc:
            attempts.append((label, exc))
            if index + 1 < len(candidates):
                from backend.utils.url_utils import safe_url_for_log
                logger.warning('规则下载失败（%s），改用%s：%s', type(exc).__name__,
                               candidates[index + 1][0], safe_url_for_log(url))
            if index + 1 == len(candidates):
                if len(attempts) == 1:
                    raise
                # A mirror embeds the original URL in its path. Redact
                # source userinfo there too, not just the outer URL's host.
                secrets = []
                for _, source in candidates:
                    parsed = urlsplit(source)
                    secrets.extend(unquote(value) for value in (parsed.username, parsed.password) if value)
                raise RuleFetchError(attempts, secrets) from None
