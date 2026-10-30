#!/usr/bin/env python3
"""Exercise the real preview backend and IPv6 Agent in the owned PVE lab.

Run only on .32. Keeps the preview and its isolated fixtures available afterward.
No production guest or network is used. Credentials never enter the report.
"""
import argparse
import base64
import json
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request
import uuid

import ipv6_readiness_lab as lab

PREVIEW = Path('/home/sunbroke/previews/configflow-ipv6-readiness')
BASE = 'http://127.0.0.1:15821'


def api(method, path, body=None, token=None):
    headers = {'Content-Type': 'application/json', 'X-ConfigFlow-Profile': 'default'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    request = urllib.request.Request(BASE + path, method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(request, timeout=100) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        data = json.load(error)
        raise RuntimeError(f'{path}: HTTP {error.code}: {data.get("message", "request failed")}') from None


def session():
    credentials = json.loads((PREVIEW / 'credentials.json').read_text())
    token = api('POST', '/api/auth/login', {k: credentials[k] for k in ('username', 'password')})['token']
    return token, credentials


def source_id(token):
    agents = api('GET', '/api/agents', token=token)
    candidates = [a for a in agents if a.get('service_type') == 'mihomo' and a.get('host') == '192.0.2.3']
    assert len(candidates) == 1
    return candidates[0]['id']


def setup(binary):
    lab.guard()
    assert json.loads((PREVIEW / 'connections.json').read_text())['owner'] == lab.OWNER
    token, credentials = session()
    # Update only the Agent executable; keep the preview's current identity.
    lab.guest(152, 'systemctl', 'stop', 'configflow-agent')
    try:
        lab.write_remote('/usr/local/bin/configflow-agent', binary.read_bytes(), mode=0o755, cid=152)
    finally:
        lab.guest(152, 'systemctl', 'start', 'configflow-agent')
    lab.write_remote('/opt/cf-ipv6-fixture/fixture.py', lab.FIXTURE, cid=154)
    lab.guest(154, 'systemctl', 'restart', 'cf-ipv6-fixture')
    private = PREVIEW / 'client-agent.json'
    previous = json.loads(private.read_text()) if private.exists() else {}
    registered = api('POST', '/api/agents/register', {
        'name': '测试客户端', 'host': '192.0.2.100', 'port': 27647,
        'profile_id': 'default', 'service_type': 'mosdns', 'deployment_method': 'shell',
        'version': '1.8.50-go', 'registration_key': credentials['registration_key']}, previous.get('token'))
    private.write_text(json.dumps(registered)); private.chmod(0o600)
    config = {'server_url': 'http://10.242.150.254:15820', 'agent_name': '测试客户端',
              'agent_host': '0.0.0.0', 'agent_port': 27647, 'agent_ip': '192.0.2.100',
              'agent_id': registered['id'], 'token': registered.get('token') or previous['token'],
              'service_type': 'mosdns', 'deployment_method': 'shell', 'service_manager': 'systemd',
              'service_name': 'cf-client-probe', 'service_unit': 'cf-client-probe.service',
              'service_binary': '/bin/true', 'config_path': '/opt/configflow-agent/client-probe.yaml',
              'agent_service_unit': 'configflow-agent', 'heartbeat_interval': 5,
              'enable_metrics': False, 'dns_watchdog_disabled': True}
    lab.guest(153, 'systemctl', 'stop', 'configflow-agent', check=False)
    lab.write_remote('/usr/local/bin/configflow-agent', binary.read_bytes(), mode=0o755, cid=153)
    lab.write_remote('/opt/configflow-agent/config-client.json', json.dumps(config), cid=153)
    lab.write_remote('/etc/systemd/system/configflow-agent.service',
                     '[Unit]\nAfter=network-online.target\n[Service]\nWorkingDirectory=/opt/configflow-agent\nExecStart=/usr/local/bin/configflow-agent -config /opt/configflow-agent/config-client.json\nRestart=on-failure\n[Install]\nWantedBy=multi-user.target\n', mode=0o644, cid=153)
    lab.guest(153, 'systemctl', 'daemon-reload')
    lab.guest(153, 'systemctl', 'enable', '--now', 'configflow-agent')
    unit = Path.home() / '.config/systemd/user/configflow-ipv6-preview-client-tunnel.service'
    unit.write_text('[Unit]\nDescription=ConfigFlow isolated IPv6 client probe tunnel\n[Service]\n'
                    'ExecStartPre=/usr/bin/sudo /usr/bin/ip address replace 192.0.2.100/32 dev lo\n'
                    'ExecStart=/usr/bin/ssh -NT -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 '
                    '-L 192.0.2.100:27647:10.242.150.3:27647 pve\nRestart=always\nRestartSec=3\n')
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', '--user', 'restart', unit.name], check=True)
    draft = api('GET', '/api/custom-config/mihomo', token=token)['config']
    draft = draft.replace('listen: 0.0.0.0:1053', 'listen: 0.0.0.0:53')
    assert 'listen: 0.0.0.0:53' in draft
    api('POST', '/api/custom-config/mihomo', {'config': draft}, token)
    lab.pve('pct', 'set', 153, '--nameserver', '192.0.2.3')
    lab.write_remote('/etc/resolv.conf', 'nameserver 192.0.2.3\n', mode=0o644, cid=153)
    # This deliberately minimal CHR has no IPv4 stateful firewall, so its
    # default "auto" mode does not track IPv4. Enable evidence in the fixture;
    # the product's verification endpoints never change connection tracking.
    private_lab = json.loads((lab.ROOT / 'private.json').read_text())
    auth = base64.b64encode(('admin:' + private_lab['router_password']).encode()).decode()
    url = 'http://127.0.0.1:19151/rest/ip/firewall/connection/tracking'
    headers = {'Authorization': 'Basic ' + auth, 'Content-Type': 'application/json'}
    old = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=8))
    if not (lab.ROOT / 'verification-fixture-settings.json').exists():
        lab.save('verification-fixture-settings.json', {'owner': lab.OWNER, 'connection_tracking_before': old['enabled']})
    with urllib.request.urlopen(urllib.request.Request(url + '/set', headers=headers, method='POST',
            data=json.dumps({'enabled': 'yes'}).encode()), timeout=8) as response:
        assert response.status == 200
    print('Updated isolated Mihomo Agent, client Agent and HTTP fixture; preview kept running.', flush=True)


def wait_report(token, ident, deployment=None):
    path = f'/api/agents/{ident}/ipv6-readiness/verify?profile_id=default'
    if deployment:
        path += '&deployment_id=' + deployment
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        report = api('GET', path, token=token).get('verification')
        if report and report['status'] != 'running':
            return report
        time.sleep(2)
    raise RuntimeError('Verification did not finish')


def verify(client=False, publish=False, fail=False):
    lab.guard()
    token, _ = session()
    ident = source_id(token)
    settings = {'client_agent_id': json.loads((PREVIEW / 'client-agent.json').read_text())['id'] if client else '',
                'test_url': f'http://fixture.lab.invalid:{18081 if fail else 18080}/acceptance'}
    path = f'/api/agents/{ident}/ipv6-readiness'
    api('PUT', path + '/verification-settings', settings, token)
    if publish:
        deployment = uuid.uuid4().hex
        result = api('POST', f'/api/agents/{ident}/push-config',
                     {'deployment_id': deployment, 'restart': True,
                      'base_url': 'http://10.242.150.254:15820'}, token)
        print(json.dumps({'publication': result.get('status'), 'deployment_id': deployment}), flush=True)
        # Deliberately never call POST /verify or poll deployment: backend must
        # monitor publication and enroll verification without a browser.
        report = wait_report(token, ident, deployment)
    else:
        api('POST', path + '/verify', {}, token)
        report = wait_report(token, ident)
    name = ('automatic' if publish else 'manual') + ('-client' if client else '-host') + ('-closed-port' if fail else '')
    lab.save('verification-' + name + '.json', report)
    checks = {c['id']: c['status'] for c in report.get('checks', [])}
    if fail:
        assert report['status'] == 'error' and checks.get('runtime_proxy_http') == 'error', checks
    else:
        required = {'runtime_deployed_config', 'runtime_runtime_core', 'runtime_runtime_ipv6',
                    'runtime_runtime_tun', 'runtime_dns_a', 'runtime_dns_aaaa',
                    'runtime_proxy_http', 'runtime_node_ipv6_transport'}
        if client:
            required.update(('client_http_ipv4', 'client_mihomo_path', 'client_ipv6_transport', 'client_ros_path'))
            if 'client_http_ipv6' in checks:
                required.add('client_http_ipv6')
        assert all(checks.get(key) == 'ok' for key in required), {k: checks.get(k) for k in required}
        assert report['status'] != 'error', checks
    print(json.dumps({'scenario': name, 'status': report['status'], 'checks': [
        {k: c.get(k) for k in ('id', 'status', 'message')} for c in report.get('checks', [])]}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('setup', 'verify'))
    parser.add_argument('--agent-binary', type=Path, default=Path('/tmp/configflow-ipv6-agent'))
    parser.add_argument('--client', action='store_true')
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--fail', action='store_true')
    args = parser.parse_args()
    setup(args.agent_binary) if args.command == 'setup' else verify(args.client, args.publish, args.fail)
