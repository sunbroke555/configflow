"""Exercise rule downloads over real local HTTP, HTTPS, and proxy sockets."""

import base64
import datetime
import http.client
import ipaddress
import select
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from backend.utils.rule_fetch import request_rule


BODY = b'payload:\n  - DOMAIN,example.test\n'


@pytest.fixture(autouse=True)
def isolated_proxy_environment(monkeypatch):
    for name in ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.delenv('REQUESTS_CA_BUNDLE', raising=False)
    monkeypatch.delenv('CURL_CA_BUNDLE', raising=False)


@pytest.fixture(scope='module')
def local_certificate(tmp_path_factory):
    directory = tmp_path_factory.mktemp('rule-proxy-tls')
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName('localhost'), x509.IPAddress(ipaddress.ip_address('127.0.0.1')),
        ]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / 'cert.pem', directory / 'key.pem'
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    return str(cert_path), str(key_path)


class RecordingServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *, proxy=False):
        super().__init__(('127.0.0.1', 0), RecordingHandler)
        self.proxy = proxy
        self.seen = []
        self.routes = {}
        self.host_overrides = {}
        self.required_auth = None
        self.reject = False
        self.tls_context = None
        self.handshake_failures = 0
        self.partial_body_failures = 0
        self.chunked_faults = None
        self.accepted_connections = 0
        self.url = f'http://127.0.0.1:{self.server_port}'

    def get_request(self):
        connection, address = super().get_request()
        self.accepted_connections += 1
        if self.tls_context is None:
            return connection, address
        try:
            if self.handshake_failures:
                self.handshake_failures -= 1
                connection.settimeout(3)
                # Consume ClientHello before closing, producing a TLS EOF
                # rather than a TCP reset caused by unread incoming bytes.
                header = bytearray()
                while len(header) < 5:
                    chunk = connection.recv(5 - len(header))
                    if not chunk:
                        raise ConnectionAbortedError('Incomplete ClientHello')
                    header.extend(chunk)
                remaining = int.from_bytes(header[3:5], 'big')
                while remaining:
                    chunk = connection.recv(remaining)
                    if not chunk:
                        raise ConnectionAbortedError('Incomplete ClientHello')
                    remaining -= len(chunk)
                connection.shutdown(socket.SHUT_WR)
                raise ConnectionAbortedError('Injected TLS handshake EOF')
            return self.tls_context.wrap_socket(connection, server_side=True), address
        except BaseException:
            connection.close()
            raise


class RecordingHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_args):
        pass

    def record(self):
        self.server.seen.append({
            'method': self.command, 'url': self.path, 'headers': dict(self.headers),
        })

    def reply(self, status, body=b'', headers=None, *, truncate=False):
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body[:min(len(body) - 1, 64 * 1024 + 11)] if truncate else body)
            self.wfile.flush()
        self.close_connection = True

    def proxy_allowed(self):
        if (self.server.required_auth is not None
                and self.headers.get('Proxy-Authorization') != self.server.required_auth):
            self.reply(407, headers={'Proxy-Authenticate': 'Basic realm="rules"'})
            return False
        if self.server.reject:
            self.reply(502, b'Proxy unavailable')
            return False
        return True

    def reply_chunked(self, body, fault):
        self.send_response(200)
        self.send_header('Transfer-Encoding', 'chunked')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(f'{len(body):x}\r\n'.encode() + body + b'\r\n')
        if fault == 'invalid_length':
            self.wfile.write(b'not-a-chunk-length\r\n')
        elif fault != 'eof':
            self.wfile.write(b'0\r\n\r\n')
        # The EOF case ends cleanly between chunks, without the required zero
        # chunk. urllib3 raises ProtocolError, without an IncompleteRead cause.
        self.wfile.flush()
        self.close_connection = True

    def do_CONNECT(self):
        self.record()
        if not self.proxy_allowed():
            return
        target = urlsplit('//' + self.path)
        host = self.server.host_overrides.get(target.hostname, target.hostname)
        with socket.create_connection((host, target.port), timeout=3) as upstream:
            self.send_response(200, 'Connection established')
            self.end_headers()
            self.wfile.flush()
            peers = (self.connection, upstream)
            while True:
                ready, _, _ = select.select(peers, [], [], 3)
                if not ready:
                    break
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        self.close_connection = True
                        return
                    destination = upstream if source is self.connection else self.connection
                    destination.sendall(data)
        self.close_connection = True

    def do_GET(self):
        self.record()
        if not self.server.proxy:
            status, headers, body = self.server.routes.get(self.path, (200, {}, BODY))
            if self.server.chunked_faults is not None:
                self.reply_chunked(body, self.server.chunked_faults.pop(0))
                return
            truncate = bool(self.server.partial_body_failures)
            if truncate:
                self.server.partial_body_failures -= 1
            self.reply(status, body, headers, truncate=truncate)
            return
        if not self.proxy_allowed():
            return
        target = urlsplit(self.path)
        host = self.server.host_overrides.get(target.hostname, target.hostname)
        connection = http.client.HTTPConnection(host, target.port or 80, timeout=3)
        headers = {key: value for key, value in self.headers.items()
                   if key.lower() not in ('proxy-authorization', 'proxy-connection')}
        path = target.path or '/'
        if target.query:
            path += '?' + target.query
        try:
            connection.request(self.command, path, headers=headers)
            response = connection.getresponse()
            body = response.read()
            response_headers = {key: value for key, value in response.getheaders()
                                if key.lower() not in (
                                    'connection', 'content-length', 'transfer-encoding',
                                    'server', 'date',
                                )}
            self.reply(response.status, body, response_headers)
        finally:
            connection.close()

    do_HEAD = do_GET


@pytest.fixture
def servers(local_certificate):
    running = []

    def start(*, proxy=False, tls=False, handshake_failures=0, partial_body_failures=0):
        server = RecordingServer(proxy=proxy)
        server.handshake_failures = handshake_failures
        server.partial_body_failures = partial_body_failures
        if tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(*local_certificate)
            server.tls_context = context
            server.url = server.url.replace('http:', 'https:', 1)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={'poll_interval': 0.02}, daemon=True)
        thread.start()
        running.append((server, thread))
        return server

    yield start
    for server, thread in reversed(running):
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def config(proxy='', server_domain=''):
    return {'system_config': {'rule_fetch_proxy': proxy, 'server_domain': server_domain}}


@pytest.mark.parametrize('method', ['get', 'head'])
@pytest.mark.parametrize('proxy_tls', [False, True])
@pytest.mark.parametrize('origin_tls', [False, True])
@pytest.mark.filterwarnings('error::urllib3.exceptions.InsecureRequestWarning')
def test_get_and_head_use_configured_http_or_https_proxy(
        servers, local_certificate, method, proxy_tls, origin_tls):
    origin = servers(tls=origin_tls)
    proxy = servers(proxy=True, tls=proxy_tls)
    url = origin.url + '/rules.yaml'

    response = request_rule(url, method=method, config_data=config(proxy.url),
                            verify=local_certificate[0], timeout=3)

    assert response.status_code == 200
    assert response.content == (BODY if method == 'get' else b'')
    assert [(item['method'], item['url']) for item in origin.seen] == [
        (method.upper(), '/rules.yaml')]
    assert len(proxy.seen) == 1
    assert proxy.seen[0]['method'] == ('CONNECT' if origin_tls else method.upper())
    assert proxy.seen[0]['url'] == (urlsplit(origin.url).netloc if origin_tls else url)


@pytest.mark.parametrize('origin_tls', [False, True])
@pytest.mark.filterwarnings('error::urllib3.exceptions.InsecureRequestWarning')
def test_untrusted_https_proxy_certificate_is_rejected(servers, origin_tls, monkeypatch):
    origin, proxy = servers(tls=origin_tls), servers(proxy=True, tls=True)
    delays = []
    monkeypatch.setattr('backend.utils.rule_fetch.time.sleep', delays.append)

    with pytest.raises((requests.exceptions.ProxyError, requests.exceptions.SSLError)):
        request_rule(origin.url + '/rules', config_data=config(proxy.url), timeout=3)

    assert origin.seen == []
    assert proxy.seen == []
    assert proxy.accepted_connections == 1
    assert delays == []


@pytest.mark.parametrize('bundle_variable', ['REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE'])
@pytest.mark.filterwarnings('error::urllib3.exceptions.InsecureRequestWarning')
def test_https_proxy_uses_environment_certificate_bundle(
        servers, local_certificate, monkeypatch, bundle_variable):
    origin, proxy = servers(), servers(proxy=True, tls=True)
    monkeypatch.setenv(bundle_variable, local_certificate[0])

    response = request_rule(origin.url + '/rules', config_data=config(proxy.url), timeout=3)

    assert response.content == BODY
    assert len(origin.seen) == len(proxy.seen) == 1


def test_authenticated_proxy_does_not_pass_credentials_to_origin(servers):
    origin, proxy = servers(), servers(proxy=True)
    expected = 'Basic ' + base64.b64encode(b'rule-user:test-secret').decode()
    proxy.required_auth = expected
    authenticated_url = proxy.url.replace('://', '://rule-user:test-secret@', 1)

    response = request_rule(origin.url + '/rules', config_data=config(authenticated_url), timeout=3)

    assert response.status_code == 200
    assert proxy.seen[0]['headers']['Proxy-Authorization'] == expected
    assert 'Proxy-Authorization' not in origin.seen[0]['headers']


def test_clearing_proxy_uses_direct_connection_despite_environment(servers, monkeypatch):
    origin = servers()
    configured, environment = servers(proxy=True), servers(proxy=True)
    monkeypatch.setenv('HTTP_PROXY', environment.url)
    settings = config(configured.url)
    assert request_rule(origin.url + '/first', config_data=settings, timeout=3).status_code == 200
    settings['system_config']['rule_fetch_proxy'] = ''
    assert request_rule(origin.url + '/second', config_data=settings, timeout=3).status_code == 200

    assert [entry['url'] for entry in configured.seen] == [origin.url + '/first']
    assert environment.seen == []
    assert [entry['url'] for entry in origin.seen] == ['/first', '/second']


def test_empty_proxy_without_environment_connects_directly(servers):
    origin = servers()
    assert request_rule(origin.url + '/rules', config_data=config(), timeout=3).content == BODY
    assert len(origin.seen) == 1


@pytest.mark.parametrize('use_proxy', [False, True])
def test_github_fallback_uses_same_transport_for_both_real_requests(servers, monkeypatch, use_proxy):
    origin, mirror, proxy = servers(), servers(), servers(proxy=True)
    origin.routes['/rules/custom.yaml'] = (503, {}, b'unavailable')
    original_resolve = socket.getaddrinfo
    def resolve(host, *args, **kwargs):
        return original_resolve('127.0.0.1' if host == 'raw.githubusercontent.com' else host, *args, **kwargs)
    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    source = f'http://raw.githubusercontent.com:{origin.server_port}/rules/custom.yaml'
    settings = config(proxy.url if use_proxy else '')
    settings['system_config']['github_proxy_domain'] = mirror.url
    result = request_rule(source, config_data=settings, stream=True, max_bytes=1024, timeout=3)
    assert result.content == BODY
    assert [entry['url'] for entry in origin.seen] == ['/rules/custom.yaml']
    assert [entry['url'] for entry in mirror.seen] == ['/' + source]
    assert [entry['url'] for entry in proxy.seen] == ([source, mirror.url + '/' + source] if use_proxy else [])


def test_proxy_error_does_not_retry_directly(servers):
    origin, proxy = servers(), servers(proxy=True)
    proxy.reject = True

    response = request_rule(origin.url + '/rules', config_data=config(proxy.url), timeout=3)

    assert response.status_code == 502
    assert len(proxy.seen) == 1
    assert origin.seen == []


def test_unavailable_proxy_does_not_retry_directly(servers):
    origin = servers()
    # A bound, non-listening socket keeps the port reserved and refuses connects.
    with socket.socket() as unavailable:
        unavailable.bind(('127.0.0.1', 0))
        proxy_url = f'http://127.0.0.1:{unavailable.getsockname()[1]}'
        with pytest.raises(requests.exceptions.ProxyError):
            request_rule(origin.url + '/rules', config_data=config(proxy_url), timeout=1)
    assert origin.seen == []


@pytest.mark.parametrize('server_prefix', ['', '/configflow'])
def test_self_api_bypasses_configured_and_environment_proxy(servers, monkeypatch, server_prefix):
    origin, proxy = servers(), servers(proxy=True)
    monkeypatch.setenv('HTTP_PROXY', proxy.url)
    url = origin.url + server_prefix + '/api/rules?token=test-token'

    response = request_rule(url, config_data=config(proxy.url, origin.url + server_prefix), timeout=3)

    assert response.status_code == 200
    assert proxy.seen == []
    assert origin.seen[0]['url'].endswith('/api/rules?token=test-token')


def test_external_redirect_to_self_api_recomputes_proxy(servers):
    origin, proxy = servers(), servers(proxy=True)
    origin.routes['/rules'] = (302, {'Location': origin.url + '/api/rules'}, b'')

    response = request_rule(origin.url + '/rules',
                            config_data=config(proxy.url, origin.url), timeout=3)

    assert response.content == BODY
    assert [entry['url'] for entry in origin.seen] == ['/rules', '/api/rules']
    assert [entry['url'] for entry in proxy.seen] == [origin.url + '/rules']


def test_self_api_redirect_cannot_leak_token_to_external_origin(servers):
    origin, external, proxy = servers(), servers(), servers(proxy=True)
    origin.routes['/api/rules?token=test-token'] = (
        302, {'Location': external.url + '/rules?token=test-token'}, b'')

    with pytest.raises((ValueError, requests.exceptions.RequestException)):
        request_rule(origin.url + '/api/rules?token=test-token',
                     config_data=config(proxy.url, origin.url), timeout=3)

    assert len(origin.seen) == 1
    assert external.seen == []
    assert proxy.seen == []


def test_redirect_revalidates_destination_scheme_before_request(servers):
    origin, proxy = servers(), servers(proxy=True)
    origin.routes['/rules'] = (302, {'Location': 'file:///etc/hosts'}, b'')

    with pytest.raises((ValueError, requests.exceptions.RequestException)):
        request_rule(origin.url + '/rules', config_data=config(proxy.url), timeout=3)

    assert len(origin.seen) == len(proxy.seen) == 1


def test_relative_redirect_preserves_head_and_honors_hop_limit(servers):
    origin, proxy = servers(), servers(proxy=True)
    origin.routes['/rules'] = (302, {'Location': '/next'}, b'')
    origin.routes['/next'] = (302, {'Location': '/final'}, b'')

    with pytest.raises((ValueError, requests.exceptions.RequestException)):
        request_rule(origin.url + '/rules', method='head', config_data=config(proxy.url),
                     max_redirects=1, timeout=3)

    assert [(entry['method'], entry['url']) for entry in origin.seen] == [
        ('HEAD', '/rules'), ('HEAD', '/next')]
    assert len(proxy.seen) == 2


def test_proxy_resolves_source_without_local_fake_ip_preflight(servers, monkeypatch):
    origin, proxy = servers(), servers(proxy=True)
    hostname = 'fake-ip.rules.test'
    proxy.host_overrides[hostname] = '127.0.0.1'
    real_getaddrinfo = socket.getaddrinfo
    local_resolutions = []

    def fake_ip_resolution(host, port, *args, **kwargs):
        if host == hostname:
            local_resolutions.append(host)
            return real_getaddrinfo('198.18.0.123', port, *args, **kwargs)
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, 'getaddrinfo', fake_ip_resolution)
    url = f'http://{hostname}:{origin.server_port}/rules'

    response = request_rule(url, config_data=config(proxy.url), timeout=3)

    assert response.content == BODY
    assert local_resolutions == []
    assert proxy.seen[0]['url'] == url
    assert len(origin.seen) == 1


@pytest.mark.parametrize('failure_at', ['origin', 'https_proxy'])
@pytest.mark.filterwarnings('error::urllib3.exceptions.InsecureRequestWarning')
def test_tls_handshake_eof_retries_same_source_with_backoff(
        servers, local_certificate, monkeypatch, failure_at):
    from backend.utils import rule_fetch

    origin = servers(tls=True, handshake_failures=2 if failure_at == 'origin' else 0)
    proxy = servers(proxy=True, tls=True, handshake_failures=2) if failure_at == 'https_proxy' else None
    delays = []
    monkeypatch.setattr('backend.utils.rule_fetch.time.sleep', delays.append)
    failures = []
    request_once = rule_fetch._request_rule_once

    def record_failure(*args, **kwargs):
        try:
            return request_once(*args, **kwargs)
        except requests.RequestException as error:
            failures.append(error)
            raise

    monkeypatch.setattr(rule_fetch, '_request_rule_once', record_failure)

    response = request_rule(origin.url + '/rules', config_data=config(proxy.url if proxy else ''),
                            verify=local_certificate[0], stream=True, max_bytes=1024, timeout=3)

    assert response.content == BODY
    assert delays == [1, 2]
    assert len(failures) == 2
    assert all(isinstance(error, (requests.exceptions.SSLError, requests.exceptions.ProxyError))
               and 'EOF' in str(error).upper() for error in failures)
    assert [entry['url'] for entry in origin.seen] == ['/rules']
    failing_server = proxy or origin
    assert failing_server.accepted_connections == 3
    assert failing_server.handshake_failures == 0
    if proxy:
        assert [(entry['method'], entry['url']) for entry in proxy.seen] == [
            ('CONNECT', urlsplit(origin.url).netloc)]
        assert origin.accepted_connections == 1


@pytest.mark.parametrize('https_proxy', [False, True])
@pytest.mark.filterwarnings('error::urllib3.exceptions.InsecureRequestWarning')
def test_interrupted_tls_body_retries_without_partial_content(
        servers, local_certificate, monkeypatch, https_proxy):
    origin = servers(tls=True, partial_body_failures=2)
    proxy = servers(proxy=True, tls=True) if https_proxy else None
    # Larger than iter_content's chunk size: the first attempt has already
    # yielded a complete chunk before its declared response body is cut short.
    complete = BODY * 8192
    origin.routes['/rules'] = (200, {}, complete)
    delays = []
    monkeypatch.setattr('backend.utils.rule_fetch.time.sleep', delays.append)

    response = request_rule(origin.url + '/rules', config_data=config(proxy.url if proxy else ''),
                            verify=local_certificate[0], stream=True,
                            max_bytes=len(complete), timeout=3)

    assert response.content == complete
    assert delays == [1, 2]
    assert [entry['url'] for entry in origin.seen] == ['/rules'] * 3
    assert origin.accepted_connections == 3
    if proxy:
        assert [(entry['method'], entry['url']) for entry in proxy.seen] == [
            ('CONNECT', urlsplit(origin.url).netloc)] * 3


@pytest.mark.parametrize('https_proxy', [False, True])
@pytest.mark.filterwarnings('error::urllib3.exceptions.InsecureRequestWarning')
def test_untrusted_origin_certificate_is_not_retried_or_bypassed(
        servers, local_certificate, monkeypatch, https_proxy):
    origin = servers(tls=True)
    proxy = servers(proxy=True, tls=True) if https_proxy else None
    delays = []
    monkeypatch.setattr('backend.utils.rule_fetch.time.sleep', delays.append)
    if proxy:
        # Trust only the HTTPS proxy's TLS layer, leaving the origin certificate
        # untrusted. This exercises certificate failure after a successful tunnel.
        context = ssl.create_default_context(cafile=local_certificate[0])
        original = requests.adapters.HTTPAdapter.proxy_manager_for

        def trusted_proxy(self, proxy_url, **kwargs):
            kwargs['proxy_ssl_context'] = context
            return original(self, proxy_url, **kwargs)

        monkeypatch.setattr(requests.adapters.HTTPAdapter, 'proxy_manager_for', trusted_proxy)

    with pytest.raises(requests.exceptions.SSLError):
        request_rule(origin.url + '/rules', config_data=config(proxy.url if proxy else ''),
                     stream=True, max_bytes=1024, timeout=3)

    assert origin.seen == []
    assert origin.accepted_connections == 1
    assert delays == []
    if proxy:
        assert [(entry['method'], entry['url']) for entry in proxy.seen] == [
            ('CONNECT', urlsplit(origin.url).netloc)]


@pytest.mark.parametrize('https_proxy', [False, True])
@pytest.mark.parametrize('fault', ['eof', 'invalid_length'])
@pytest.mark.filterwarnings('error::urllib3.exceptions.InsecureRequestWarning')
def test_chunked_eof_is_retried_but_invalid_length_is_not(
        servers, local_certificate, monkeypatch, https_proxy, fault):
    origin = servers(tls=True)
    proxy = servers(proxy=True, tls=True) if https_proxy else None
    origin.chunked_faults = ['eof', 'eof', None] if fault == 'eof' else ['invalid_length']
    delays = []
    monkeypatch.setattr('backend.utils.rule_fetch.time.sleep', delays.append)
    options = dict(config_data=config(proxy.url if proxy else ''),
                   verify=local_certificate[0], stream=True, max_bytes=len(BODY), timeout=3)

    if fault == 'eof':
        response = request_rule(origin.url + '/rules', **options)
        assert response.content == BODY
        assert delays == [1, 2]
        expected_attempts = 3
    else:
        with pytest.raises(requests.exceptions.ChunkedEncodingError, match='InvalidChunkLength'):
            request_rule(origin.url + '/rules', **options)
        assert delays == []
        expected_attempts = 1

    assert [entry['url'] for entry in origin.seen] == ['/rules'] * expected_attempts
    assert origin.accepted_connections == expected_attempts
    assert origin.chunked_faults == []
    if proxy:
        assert [(entry['method'], entry['url']) for entry in proxy.seen] == [
            ('CONNECT', urlsplit(origin.url).netloc)] * expected_attempts
