#!/usr/bin/env python3
"""Run the real ConfigFlow app against a disposable gateway-lab repository.

Loopback smoke check (no installation or host-network changes):
    .venv/bin/python scripts/tests/linux_gateway_lab_server.py --check-only

Inside the marked disposable VM and router network namespace:
    ip netns exec cfgl-router python3 scripts/tests/linux_gateway_lab_server.py \
      --host 192.0.2.1 --port 8000 --workspace /tmp/configflow-gateway-lab-server \
      --agent-binaries-dir /tmp/configflow-gateway-lab-agents

The private workspace/manifest.json contains test-only login credentials and API
fixture IDs. Seeding uses actual authenticated production routes. The app's
onboarding, installer, enrollment, publication, gateway preview/apply/remove and
Agent requests are NOT mocked. This file never runs installation/network commands.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import ipaddress
import json
import os
from pathlib import Path
import platform
import secrets
import sys
import tempfile

REPOSITORY = Path(__file__).resolve().parents[2]
PROFILE_ID = 'gateway-lab'
WORKSPACE_MARKER = 'ConfigFlow disposable Linux gateway API lab v1'
LAB_NETWORKS = (ipaddress.ip_network('192.0.2.0/24'), ipaddress.ip_network('203.0.113.0/24'))
INSTALL_DOWNLOAD_DOMAINS = ('github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com')


def private_json(path: Path, value):
    fd, temporary = tempfile.mkstemp(prefix='.lab-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def private_text(path: Path, value):
    fd, temporary = tempfile.mkstemp(prefix='.lab-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(value + '\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def workspace_path(raw):
    temporary_root = Path('/tmp').resolve()
    path = Path(raw).expanduser() if raw else Path(tempfile.mkdtemp(prefix='configflow-gateway-lab-', dir='/tmp'))
    if path.is_symlink():
        raise ValueError('Lab workspace must not be a symlink')
    path = path.resolve()
    if (temporary_root not in path.parents or not path.name.startswith('configflow-gateway-lab-')
            or path == REPOSITORY or REPOSITORY in path.parents):
        raise ValueError('Lab workspace must be a dedicated /tmp/configflow-gateway-lab-* directory outside the repository')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker = path / '.gateway-api-lab'
    if marker.exists():
        if marker.read_text().strip() != WORKSPACE_MARKER:
            raise ValueError('The existing directory does not belong to this lab harness')
    elif any(path.iterdir()):
        raise ValueError('Refusing to use a nonempty directory without the lab marker')
    else:
        marker.write_text(WORKSPACE_MARKER + '\n')
        marker.chmod(0o600)
    path.chmod(0o700)
    return path


def bind_address(raw):
    address = ipaddress.ip_address(raw)
    if address.version != 4 or address.is_unspecified or address.is_multicast:
        raise ValueError('Choose one explicit IPv4 loopback or isolated lab-interface address')
    if address.is_loopback:
        return str(address)
    if not any(address in network for network in LAB_NETWORKS):
        raise ValueError('Non-loopback binding is limited to the documented isolated lab subnets')
    if sys.platform != 'linux' or not Path('/root/.configflow-disposable-lab').is_file():
        raise ValueError('Non-loopback binding requires the marked disposable Linux VM')
    from linux_gateway_lab_support import guard
    guard(require_setup=False)
    router_namespace = Path('/run/netns/cfgl-router')
    if (not router_namespace.exists()
            or router_namespace.stat().st_ino != os.stat('/proc/self/ns/net').st_ino):
        raise ValueError('Run non-loopback lab binding inside the dedicated cfgl-router network namespace')
    return str(address)


def environment(workspace, agent_directory):
    credentials_path = workspace / 'credentials.json'
    if credentials_path.is_file():
        credentials = json.loads(credentials_path.read_text())
    else:
        credentials = {'username': 'gateway-lab-admin', 'password': secrets.token_urlsafe(24),
                       'jwt_secret': secrets.token_urlsafe(48), 'config_token': secrets.token_urlsafe(24),
                       'registration_key': secrets.token_urlsafe(24),
                       'controller_secret': secrets.token_urlsafe(24)}
        private_json(credentials_path, credentials)
    if 'controller_secret' not in credentials:
        credentials['controller_secret'] = secrets.token_urlsafe(24)
        private_json(credentials_path, credentials)
    settings = {
        'DATA_DIR': str(workspace / 'data'), 'LOG_FILE': str(workspace / 'server.log'),
        'LOG_LEVEL': 'WARNING', 'ADMIN_USERNAME': credentials['username'],
        'ADMIN_PASSWORD': credentials['password'], 'JWT_SECRET_KEY': credentials['jwt_secret'],
        'AGENT_REGISTRATION_KEY': credentials['registration_key'],
        # No built-in file means initialize_app() starts no Sub-Store monitor.
        'SUB_STORE_BUILTIN_DIR': str(workspace / 'disabled-sub-store'),
        'AGENTS_STATIC_DIR': str(agent_directory),
    }
    os.environ.update(settings)
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        os.environ.pop(name, None)
    os.environ['NO_PROXY'] = '127.0.0.1,localhost,192.0.2.0/24,203.0.113.0/24'
    os.chdir(workspace)
    sys.path.insert(0, str(REPOSITORY))
    return credentials


def create_app(workspace):
    # Use empty defaults, not the developer's initial config_template.json.
    from backend.common import config as store
    from backend.common.config_repository import ProfileRepository
    store.set_repository(ProfileRepository(workspace / 'data',
        default_config_factory=store.get_default_config, initial_config_factory=store.get_default_config))
    from backend.app import app
    from backend.utils.sub_store_runtime import runtime
    if runtime._thread is not None:
        raise RuntimeError('An unrelated background monitor started; aborting lab server')
    app.config.update(TESTING=False, DEBUG=False)

    @app.get('/__gateway_lab__/health')
    def lab_health():
        return {'ready': True, 'disposable_lab': True, 'profile_id': PROFILE_ID}

    return app, store.get_repository()


def seed_fixture(app, repository, credentials, server_url, proxy_host, proxy_port):
    """Exercise actual authenticated APIs, preserving real validation and hooks."""
    client = app.test_client()
    login = client.post('/api/auth/login', json={'username': credentials['username'], 'password': credentials['password']})
    if login.status_code != 200:
        raise RuntimeError('Lab administrator login failed')
    admin_token = login.get_json()['token']
    headers = {'Authorization': 'Bearer ' + admin_token}

    def api(method, path, body=None):
        response = client.open(path, method=method, json=body, headers=headers)
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f'Fixture API {method} {path} failed ({response.status_code}): {response.get_json()}')
        return response.get_json()

    api('POST', '/api/server-domain', {'new_domain': server_url})
    api('POST', '/api/config-token', {'token': credentials['config_token']})
    profiles = api('GET', '/api/profiles')
    if not any(item['id'] == PROFILE_ID for item in profiles):
        api('POST', '/api/profiles', {'id': PROFILE_ID, 'name': 'Disposable gateway laboratory'})
        for rule in (
            {'id': 'lab-lib-ip', 'name': 'LAB selected IPv4', 'behavior': 'ipcidr', 'content': '203.0.113.20/32\n'},
            {'id': 'lab-lib-direct', 'name': 'LAB direct domains', 'behavior': 'domain',
             'content': '+.direct.lab.invalid\nexample.com\n' + '\n'.join(INSTALL_DOWNLOAD_DOMAINS) + '\n'},
            {'id': 'lab-lib-proxy', 'name': 'LAB proxy domains', 'behavior': 'domain', 'content': '+.proxy.lab.invalid\n'},
        ):
            api('POST', '/api/rule-library', {**rule, 'source_type': 'content', 'format': 'text', 'enabled': True})
        api('POST', '/api/nodes', {'id': 'lab-socks', 'name': 'LAB-SOCKS', 'enabled': True,
            'type': 'socks5', 'server': proxy_host, 'port': proxy_port, 'params': {'udp': False}})
        api('POST', f'/api/profiles/{PROFILE_ID}/proxy-groups', {'id': 'lab-proxy-group', 'name': 'LAB-PROXY',
            'type': 'select', 'enabled': True, 'manual_nodes': ['lab-socks']})
        # POST prepends; create the final MATCH first to preserve intended order.
        for rule in (
            {'id': 'lab-match', 'itemType': 'rule', 'rule_type': 'MATCH', 'policy': 'LAB-PROXY'},
            {'id': 'lab-ip', 'itemType': 'ruleset', 'library_rule_id': 'lab-lib-ip', 'policy': 'LAB-PROXY'},
            {'id': 'lab-proxy', 'itemType': 'ruleset', 'library_rule_id': 'lab-lib-proxy', 'policy': 'LAB-PROXY'},
            {'id': 'lab-direct', 'itemType': 'ruleset', 'library_rule_id': 'lab-lib-direct', 'policy': 'DIRECT'},
        ):
            api('POST', f'/api/profiles/{PROFILE_ID}/rules', {**rule, 'enabled': True})
        # The fixture SOCKS endpoint intentionally accepts only isolated test
        # targets. A second installer must still fetch verified release assets
        # while the first session's Mihomo configuration remains active.
        # POST prepends these real rule_configs ahead of LAB-PROXY's MATCH.
        for index, domain in enumerate(INSTALL_DOWNLOAD_DOMAINS):
            api('POST', f'/api/profiles/{PROFILE_ID}/rules', {
                'id': 'lab-install-download-' + str(index), 'itemType': 'rule',
                'rule_type': 'DOMAIN', 'value': domain, 'policy': 'DIRECT', 'enabled': True})
        import yaml
        base = {'mixed-port': 0, 'allow-lan': False, 'log-level': 'debug',
                'external-controller': '192.0.2.2:9090', 'secret': credentials['controller_secret'],
                'dns': {'nameserver': ['https://223.5.5.5/dns-query'],
                        'nameserver-policy': {'proxy.lab.invalid': ['udp://203.0.113.2:5353']},
                        'fake-ip-filter': ['+.lan', '+.local', *INSTALL_DOWNLOAD_DOMAINS]}}
        response = client.post('/api/custom-config/mihomo',
            json={'config': yaml.safe_dump(base, allow_unicode=True, sort_keys=False)},
            headers={**headers, 'X-ConfigFlow-Profile': PROFILE_ID})
        if response.status_code != 200:
            raise RuntimeError('Unable to save lab Mihomo base configuration: ' + str(response.get_json()))
    # Upgrade only this disposable fixture on restart, so an existing lab also
    # exercises a real direct-domain answer through OxiDNS's IP-address DoH.
    direct = api('GET', '/api/rule-library/lab-lib-direct/draft-content')
    missing_direct = [domain for domain in ('example.com', *INSTALL_DOWNLOAD_DOMAINS)
                      if domain not in direct['rule']['content'].splitlines()]
    if missing_direct:
        api('PUT', '/api/rule-library/lab-lib-direct/draft-content', {
            'revision': direct['revision'], 'content': direct['rule']['content'].rstrip() + '\n' + '\n'.join(missing_direct) + '\n'})
    state = api('GET', f'/api/profiles/{PROFILE_ID}/gateway-onboarding')
    onboarding = {'rule_set_ids': ['lab-ip'], 'direct_rule_set_ids': ['lab-direct'],
                  'proxy_rule_set_ids': ['lab-proxy'], 'local_dns': 'https://223.5.5.5/dns-query',
                  'expected_config_revision': state['config_revision']}
    before = deepcopy(repository.export_all())
    preview = api('POST', f'/api/profiles/{PROFILE_ID}/gateway-onboarding/preview', onboarding)
    if not preview.get('ready'):
        raise RuntimeError('The real onboarding API rejected the fixture: ' + str(preview.get('blockers')))
    if repository.export_all() != before:
        raise RuntimeError('Read-only onboarding preview changed the fixture repository')
    return {'profile_id': PROFILE_ID, 'onboarding_body': onboarding,
            'rule_ids': {'ip': 'lab-ip', 'direct': 'lab-direct', 'proxy': 'lab-proxy', 'match': 'lab-match'},
            'network': {'interface': 'lan0', 'address': '192.0.2.2', 'lan_cidr': '192.0.2.0/24',
                        'upstream_gateway': '192.0.2.1'},
            'installation_name': 'cfgl-gateway',
            'proxy': {'host': proxy_host, 'port': proxy_port, 'policy': 'LAB-PROXY'},
            'targets': {'direct': '203.0.113.10', 'selected_ip': '203.0.113.20',
                        'proxy_domain': 'proxy.lab.invalid', 'proxy_origin': '203.0.113.30', 'http_port': 18080,
                        'direct_dns_domain': 'example.com', 'direct_dns_upstream': 'https://223.5.5.5/dns-query'},
            'preview_ready': True}, admin_token


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--workspace', help='Dedicated /tmp/configflow-gateway-lab-* directory; never production data')
    parser.add_argument('--agent-binaries-dir', type=Path,
                        help='Directory holding actual configflow-agent-linux-{amd64,arm64} downloads')
    parser.add_argument('--proxy-host', default='203.0.113.2')
    parser.add_argument('--proxy-port', type=int, default=1080)
    parser.add_argument('--check-only', action='store_true', help='Run authenticated fixture + read-only onboarding smoke check, then exit')
    args = parser.parse_args()
    host = bind_address(args.host)
    if not 1024 <= args.port <= 65535 or not 1 <= args.proxy_port <= 65535:
        parser.error('Invalid server or proxy port')
    ipaddress.ip_address(args.proxy_host)
    workspace = workspace_path(args.workspace)
    agent_directory = (args.agent_binaries_dir or workspace / 'agents').resolve()
    if Path('/tmp').resolve() not in agent_directory.parents:
        parser.error('Agent artifacts must live in a disposable directory under /tmp')
    # Prevent the production download route's fallback to checkout artifacts.
    agent_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    credentials = environment(workspace, agent_directory)
    app, repository = create_app(workspace)
    server_url = f'http://{host}:{args.port}'
    fixture, admin_token = seed_fixture(app, repository, credentials, server_url, args.proxy_host, args.proxy_port)
    private_text(workspace / 'admin-token', admin_token)
    private_text(workspace / 'controller-token', credentials['controller_secret'])
    architecture = {'aarch64': 'arm64', 'arm64': 'arm64', 'x86_64': 'amd64', 'amd64': 'amd64'}.get(platform.machine().lower())
    artifacts = {architecture: str(agent_directory / ('configflow-agent-linux-' + architecture))
                 for architecture in ('amd64', 'arm64', 'armv7')}
    manifest = {'workspace': str(workspace), 'server_url': server_url, 'base_url': server_url,
                'profile_id': PROFILE_ID, 'rule_ids': fixture['rule_ids'],
                'admin_token_file': str(workspace / 'admin-token'),
                'controller_token_file': str(workspace / 'controller-token'),
                'controller_url': 'http://192.0.2.2:9090',
                'username': credentials['username'], 'password': credentials['password'],
                'fixture': fixture, 'agent_binaries_dir': str(agent_directory),
                'agent_artifacts': artifacts, 'agent_artifact': artifacts.get(architecture), 'pid': os.getpid()}
    private_json(workspace / 'manifest.json', manifest)

    # Binding is performed before announcing readiness. No Flask reloader or
    # production scheduler is involved; request-triggered publish threads remain.
    if args.check_only:
        print(json.dumps({'check': 'passed', 'manifest': str(workspace / 'manifest.json'),
                          'profile_id': PROFILE_ID, 'preview_ready': fixture['preview_ready']}), flush=True)
        return
    from werkzeug.serving import make_server
    server = make_server(host, args.port, app, threaded=True)
    print(json.dumps({'ready': True, 'server_url': server_url,
                      'manifest': str(workspace / 'manifest.json')}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
