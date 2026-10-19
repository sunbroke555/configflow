"""The standalone gateway probe reads fake iproute2 output without changing the host."""
import json
import os
from pathlib import Path
import subprocess

import pytest
from flask import Flask

from backend.agents.linux_gateway import _spec
from backend.routes.agents import get_gateway_network_probe
from backend.routes.auth import setup_before_request

SCRIPT = Path(__file__).parent / 'agents' / 'scripts' / 'detect-gateway-network.sh'


def probe(tmp_path, route='default via 192.168.8.1 dev ens18 proto dhcp metric 100',
          addresses='2: ens18 inet 192.168.8.23/24 brd 192.168.8.255 scope global dynamic ens18',
          *, interface='ens18', route_error=False, address_error=False):
    commands = tmp_path / 'commands'
    commands.mkdir()
    (commands / 'ip').write_text('''#!/bin/sh
printf "%s\\n" "$*" >> "$PROBE_LOG"
case "$*" in
    '-4 -o route show default')
        [ "$PROBE_ROUTE_ERROR" = 0 ] || exit 1
        cat "$PROBE_ROUTES_FILE" ;;
    "-4 -o addr show dev $PROBE_INTERFACE")
        [ "$PROBE_ADDRESS_ERROR" = 0 ] || exit 1
        cat "$PROBE_ADDRESSES_FILE" ;;
    *) printf 'unexpected ip command: %s\\n' "$*" >&2; exit 91 ;;
esac
''')
    (commands / 'ip').chmod(0o755)
    (tmp_path / 'routes').write_text(route + '\n')
    (tmp_path / 'addresses').write_text(addresses + '\n')
    env = {**os.environ, 'PATH': str(commands) + ':/usr/bin:/bin',
           'PROBE_LOG': str(tmp_path / 'commands.log'), 'PROBE_INTERFACE': interface,
           'PROBE_ROUTES_FILE': str(tmp_path / 'routes'), 'PROBE_ADDRESSES_FILE': str(tmp_path / 'addresses'),
           'PROBE_ROUTE_ERROR': str(int(route_error)), 'PROBE_ADDRESS_ERROR': str(int(address_error))}
    result = subprocess.run(['sh', str(SCRIPT)], env=env, capture_output=True, text=True, timeout=5)
    calls = (tmp_path / 'commands.log').read_text().splitlines()
    assert calls == ['-4 -o route show default'] or calls == [
        '-4 -o route show default', f'-4 -o addr show dev {interface}']
    return result


@pytest.mark.parametrize('interface', ['eth0', 'enp2s0', 'ens18', 'br0', 'bond0.10', 'vlan-20', '_lan0'])
def test_probe_uses_actual_default_route_interface_without_name_prefix_guessing(tmp_path, interface):
    result = probe(tmp_path, f'default via 192.168.8.1 dev {interface} proto static',
                   f'5: {interface}@if4 inet 192.168.8.23/24 brd 192.168.8.255 scope global {interface}',
                   interface=interface)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ''
    body = json.loads(result.stdout)
    assert body == {'version': 1, 'interface': interface, 'address': '192.168.8.23',
                    'lan_cidr': '192.168.8.0/24', 'upstream_gateway': '192.168.8.1'}
    assert _spec({key: value for key, value in body.items() if key != 'version'})


@pytest.mark.parametrize('address,prefix,gateway,network', [
    ('10.42.20.7', 8, '10.0.0.1', '10.0.0.0'),
    ('172.20.31.23', 20, '172.20.16.1', '172.20.16.0'),
    ('192.168.8.6', 30, '192.168.8.5', '192.168.8.4'),
])
def test_probe_calculates_canonical_network_for_supported_prefixes(tmp_path, address, prefix, gateway, network):
    result = probe(tmp_path, f'default via {gateway} dev ens18', f'2: ens18 inet {address}/{prefix} scope global ens18')
    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout)
    assert body['lan_cidr'] == f'{network}/{prefix}'
    assert _spec({key: value for key, value in body.items() if key != 'version'})


@pytest.mark.parametrize('route,message', [
    ('', '未找到'),
    ('default via 192.168.8.1 dev ens18\ndefault via 10.0.0.1 dev br0 metric 200', '多个'),
    ('default nexthop via 192.168.8.1 dev ens18 weight 1 nexthop via 10.0.0.1 dev br0 weight 1', '多路径'),
    ('default nhid 11 via 192.168.8.1 dev ens18', '多路径'),
    ('default dev ens18 scope link', '缺少'),
    ('default via 192.168.8.1', '缺少'),
    ('default via 192.168.8.1 dev lo', '网卡名'),
    ('default via 192.168.8.1 dev ens18;id', '网卡名'),
    ('default via 192.168.8.1 dev abcdefghijklmnop', '网卡名'),
    ('default via 192.168.8.1 dev ens18 linkdown', '未连接'),
])
def test_probe_refuses_missing_or_ambiguous_routes_without_success_output(tmp_path, route, message):
    result = probe(tmp_path, route=route)
    assert result.returncode != 0
    assert result.stdout == ''
    assert message in result.stderr


@pytest.mark.parametrize('addresses,message', [
    ('', '没有可用'),
    ('2: ens18 inet 192.168.8.23/24 scope global ens18\n2: ens18 inet 192.168.8.24/24 scope global secondary ens18', '多个'),
    ('2: ens18 inet 192.168.8.23/24 scope global deprecated ens18', '尚未可用'),
    ('2: ens18 inet 192.168.8.23/24 scope global tentative ens18', '尚未可用'),
    ('2: ens18 inet 192.168.8.23/24 scope global dadfailed ens18', '尚未可用'),
    ('2: ens18 inet 192.168.8.23/31 scope global ens18', '掩码'),
    ('2: ens18 inet 192.168.8.23/7 scope global ens18', '掩码'),
    ('2: ens18 inet 192.168.8.0/24 scope global ens18', '网络或广播'),
    ('2: ens18 inet 192.168.8.255/24 scope global ens18', '网络或广播'),
    ('2: ens18 inet 192.168.8.1/24 scope global ens18', '不能相同'),
    ('2: ens18 inet 192.168.08.23/24 scope global ens18', '有效'),
    ('2: ens18 inet 192.168.8.256/24 scope global ens18', '有效'),
])
def test_probe_refuses_ambiguous_or_unusable_addresses(tmp_path, addresses, message):
    result = probe(tmp_path, addresses=addresses)
    assert result.returncode != 0
    assert result.stdout == ''
    assert message in result.stderr


@pytest.mark.parametrize('address,gateway', [
    ('192.168.8.23', '10.0.0.1'), ('192.168.8.23', '192.168.8.0'),
    ('192.168.8.23', '192.168.8.255'), ('192.168.8.23', 'bad'),
    ('127.0.0.2', '127.0.0.1'), ('169.254.8.2', '169.254.8.1'),
    ('224.0.0.2', '224.0.0.1'), ('0.0.0.0', '0.0.0.1'),
    ('0.1.2.3', '0.1.2.1'), ('240.0.0.2', '240.0.0.1'),
])
def test_probe_rejects_unusable_or_unsupported_host_addresses(tmp_path, address, gateway):
    result = probe(tmp_path, f'default via {gateway} dev ens18', f'2: ens18 inet {address}/24 scope global ens18')
    assert result.returncode != 0
    assert result.stdout == ''


@pytest.mark.parametrize('failure', ['route_error', 'address_error'])
def test_probe_reports_ip_command_failures(tmp_path, failure):
    result = probe(tmp_path, **{failure: True})
    assert result.returncode != 0
    assert result.stdout == ''
    assert '无法读取' in result.stderr


def test_probe_has_posix_shell_syntax_and_no_runtime_python_jq_or_root_check():
    result = subprocess.run(['sh', '-n', str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    source = SCRIPT.read_text()
    assert all(command not in source for command in ('python', 'jq ', 'id -u', 'sudo ', 'sysctl ', 'nft ', 'ip route add'))


def test_probe_download_is_public_fixed_content_without_request_values(monkeypatch):
    app = Flask(__name__)
    app.add_url_rule('/api/agents/gateway-network-probe.sh', view_func=get_gateway_network_probe)
    app.add_url_rule('/api/agents/gateway-network-probe.sh/extra', endpoint='extra', view_func=lambda: 'private')
    monkeypatch.setattr('backend.routes.auth.is_auth_enabled', lambda: True)
    monkeypatch.setattr('backend.routes.auth.is_internal_call', lambda: False)
    setup_before_request(app)
    response = app.test_client().get('/api/agents/gateway-network-probe.sh', query_string={
        'token': 'secret-must-not-appear', 'interface': 'do-not-use-this', 'path': '/etc/passwd',
    })
    assert response.status_code == 200
    assert response.mimetype == 'text/plain'
    assert response.text == SCRIPT.read_text()
    assert 'secret-must-not-appear' not in response.text
    assert response.headers['Content-Disposition'] == 'attachment; filename="detect-gateway-network.sh"'
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    assert response.headers['Cache-Control'] == 'no-store'
    assert app.test_client().get('/api/agents/gateway-network-probe.sh/extra').status_code == 401


def test_probe_download_fails_when_fixed_script_is_unavailable(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise FileNotFoundError('unavailable')

    monkeypatch.setattr('backend.routes.agents.open', unavailable, raising=False)
    app = Flask(__name__)
    app.add_url_rule('/api/agents/gateway-network-probe.sh', view_func=get_gateway_network_probe)
    response = app.test_client().get('/api/agents/gateway-network-probe.sh')
    assert response.status_code == 503
    assert response.json['success'] is False
