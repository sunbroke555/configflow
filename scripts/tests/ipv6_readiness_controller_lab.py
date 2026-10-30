#!/usr/bin/env python3
"""Real controller orchestration against the dedicated IPv6 PVE laboratory.

Run with the backend Python environment on .32 AFTER ipv6_readiness_lab.py test.
Only profile/Agent lookup is supplied by a private in-memory test fixture. All
Go Agent calls, RouterOS REST reads/writes and client transfers are real.
The isolated lab guard refuses production guests or changed network identity.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from unittest.mock import patch
from urllib.parse import quote

import ipv6_readiness_lab as lab


def require(value, message):
    if not value:
        raise RuntimeError(message)


def run():
    lab.guard()
    require((lab.ROOT / 'agent-report.json').is_file(), 'Complete the real Go Agent rounds first')
    require(json.loads((lab.ROOT / 'agent-report.json').read_text()).get('state') == 'passed',
            'The three real Go Agent rounds must pass before controller acceptance')
    data = lab.ROOT / 'controller-data'
    data.mkdir(mode=0o700, exist_ok=True)
    os.environ['DATA_DIR'] = str(data)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from backend.agents import ipv6_readiness as service
    from backend.agents.oxidns_monitor import MonitorError
    from backend.routers.routeros import RouterOSDriver

    credentials = json.loads((lab.ROOT / 'private.json').read_text())
    for port in (19151, 19152):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', port))
    tunnel_log = (lab.ROOT / 'tunnel.log').open('ab')
    os.chmod(lab.ROOT / 'tunnel.log', 0o600)
    tunnel = subprocess.Popen(['ssh', '-N', '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes',
                               '-L', '127.0.0.1:19151:10.242.150.1:80',
                               '-L', '127.0.0.1:19152:10.242.150.2:27647', 'pve'],
                              stdout=subprocess.DEVNULL, stderr=tunnel_log)
    driver = None
    report = None
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if tunnel.poll() is not None:
                raise RuntimeError('Task-only SSH tunnel failed')
            try:
                with socket.create_connection(('127.0.0.1', 19151), .5):
                    break
            except OSError:
                time.sleep(.1)
        router = {'id': 'ipv6-lab-router', 'name': lab.GUESTS[151], 'type': 'routeros',
                  'endpoint': 'http://127.0.0.1:19151', 'username': 'admin',
                  'password': credentials['router_password'], 'verify_tls': False,
                  'proxy_gateway_agent_id': 'ipv6-readiness-lab', 'dns_agent_id': '',
                  'manage_return_routing': False}
        agent = {'id': 'ipv6-readiness-lab', 'name': lab.GUESTS[152], 'host': '192.0.2.3',
                 'port': 27647, 'profile_id': 'ipv6-lab', 'service_type': 'mihomo',
                 'token': credentials['agent_token'], 'deployment_method': 'shell',
                 'version': '1.8.50-go', 'status': 'online'}
        config = {'mihomo': {'custom_config': lab.MIHOMO}, 'routers': {'items': [router]}, 'nodes': []}

        class Manager:
            def get_agent_by_id(self, ident):
                return copy.deepcopy(agent) if ident == agent['id'] else None

        manager = Manager()
        driver = RouterOSDriver(router['endpoint'], router['username'], router['password'], verify_tls=False)
        version = driver._get('/system/resource').get('version')
        require(str(version).startswith('7.25'), 'Unexpected RouterOS version')

        def static_routes():
            return [r for r in driver._list('/ipv6/route') if r.get('dst-address') == 'fc00::/64']

        def unchanged_settings():
            dns = driver._get('/ip/dns')
            return {**{key: [{field: value for field, value in row.items() if field not in ('bytes', 'packets')}
                            for row in driver._list(path)] for key, path in {
                'nat': '/ipv6/firewall/nat', 'filter': '/ipv6/firewall/filter', 'nd': '/ipv6/nd',
                'dhcp': '/ipv6/dhcp-client', 'ipv4_nat': '/ip/firewall/nat',
                'ipv4_filter': '/ip/firewall/filter'}.items()},
                'dns': {key: dns.get(key) for key in ('servers', 'use-doh-server', 'allow-remote-requests')}}

        report = {'scope': 'Controller service + real Go Agent + real CHR REST; fixture profile lookup',
                  'state': 'running',
                  'routeros_version': version, 'checks': [], 'started': time.time()}
        lab.save('controller-report.json', report)
        original_settings = unchanged_settings()
        original_files = lab.metadata()
        if '--reset-owned-route' in sys.argv:
            for row in static_routes():
                require(str(row.get('comment', '')).startswith('cf-ipv6-readiness:'),
                        'Only this lab workflow-owned route may be reset')
                driver._request('DELETE', '/ipv6/route/' + quote(row['.id'], safe='*'))
        require(not static_routes(), 'Lab must start without an IPv6 Fake-IP route')
        with patch.object(service, 'get_config', lambda profile: copy.deepcopy(config)), \
             patch.object(service, 'reset_config_context', lambda: None), \
             patch.object(service, '_agent_origin', lambda _: 'http://127.0.0.1:19152'):
            # The user's preferred mode: clients keep IPv4 split routing while
            # only the Mihomo-to-node connection uses IPv6. No Fake6 route is
            # required, even if an inactive fake-ip-range6 remains in YAML.
            deployed = lab.guest(152, 'cat', '/etc/mihomo/config.yaml').stdout.decode()
            require(deployed == lab.MIHOMO, 'Unexpected lab Mihomo config; refusing to replace it')
            ipv4_only = deployed.replace('\n  ipv6: true\n', '\n  ipv6: false\n')
            require(ipv4_only != deployed, 'DNS IPv6 fixture setting was not found')

            def reload_lab_core(content):
                lab.write_remote('/etc/mihomo/config.yaml', content, cid=152)
                script = '''import json,urllib.request
r=urllib.request.Request('http://127.0.0.1:9090/configs?force=true',method='PUT',headers={'Content-Type':'application/json'},data=json.dumps({'path':'/etc/mihomo/config.yaml'}).encode())
with urllib.request.urlopen(r,timeout=15) as response:assert response.status==204
'''
                lab.guest(152, 'python3', '-c', script)

            try:
                config['mihomo']['custom_config'] = ipv4_only
                reload_lab_core(ipv4_only)
                primary = service.inspect(manager, agent['id'], 'ipv6-lab')
                require(primary['configuration']['ipv6'] is True
                        and primary['configuration']['dns_ipv6'] is False
                        and primary['routers'][0]['plan'] is None,
                        'IPv4-client mode incorrectly planned an IPv6 Fake-IP route')
                result = service.prepare(manager, agent['id'], 'ipv6-lab', {
                    'interface': 'eth1', 'check_token': primary['check_token']})
                require(result['changed_routes'] == 0 and not static_routes(),
                        'IPv4-client mode added an unnecessary IPv6 Fake-IP route')
                require(lab.fake_address(6, allow_empty=True) is None,
                        'DNS IPv6 disabled but the actual core still returned AAAA')
                report['ipv4_only_mode'] = {
                    'ipv6': True, 'dns_ipv6': False, 'fake6_route_count': 0,
                    'aaaa_answer_empty': True,
                    'transfer': lab.transfer('controller-ipv4-only-via-ipv6'),
                }
                report['checks'].append('ipv4_clients_ipv6_node_without_fake6_route')
            finally:
                config['mihomo']['custom_config'] = deployed
                reload_lab_core(deployed)
                lab.wait_ready()
            first = service.inspect(manager, agent['id'], 'ipv6-lab')
            lab.save('controller-first-inspection.json', first)
            require(first['can_prepare'], 'Controller did not offer preparation for the isolated gateway')
            require(first['routers'][0]['plan'] is not None, 'Missing IPv6 Fake-IP route was not detected')
            require(not static_routes() and unchanged_settings() == original_settings
                    and lab.metadata() == original_files, 'Inspection mutated configuration')
            report['checks'].append('read_only_inspection')
            try:
                service.prepare(manager, agent['id'], 'ipv6-lab', {'interface': 'eth1', 'check_token': '0' * 64})
            except MonitorError as error:
                require(error.code == 'CHECK_CHANGED', 'Wrong stale-check error')
            else:
                raise RuntimeError('Stale check_token was accepted')
            require(not static_routes(), 'Stale-check request created a route')
            report['checks'].append('stale_token_rejected')
            actual_request = service._request

            def fail_host_prepare(current_agent, operation='', payload=None):
                if operation == 'prepare':
                    raise MonitorError('LAB_INJECTED_FAILURE', 502, 'Injected Agent prepare failure after real ROS add')
                return actual_request(current_agent, operation, payload)

            with patch.object(service, '_request', fail_host_prepare):
                try:
                    service.prepare(manager, agent['id'], 'ipv6-lab',
                                    {'interface': 'eth1', 'check_token': first['check_token']})
                except MonitorError as error:
                    require(error.code == 'PREPARE_FAILED', 'Wrong compensation error')
                else:
                    raise RuntimeError('Injected host preparation failure was ignored')
            require(not static_routes() and lab.metadata() == original_files,
                    'Agent preparation failure did not compensate its new RouterOS route')
            report['checks'].append('real_router_undo_after_injected_agent_failure')
            first = service.inspect(manager, agent['id'], 'ipv6-lab')
            prepared = service.prepare(manager, agent['id'], 'ipv6-lab',
                                       {'interface': 'eth1', 'check_token': first['check_token']})
            rows = static_routes()
            require(prepared['changed_routes'] == 1 and len(rows) == 1, 'Preparation did not create exactly one route')
            owned = copy.deepcopy(rows[0])
            require(owned.get('active') == 'true', 'Created IPv6 route is not active')
            require(unchanged_settings() == original_settings, 'Preparation modified unrelated router settings')
            require(lab.metadata() == original_files, 'Already prepared host was rewritten')
            report['checks'].append('one_missing_route_created_and_active')
            latest = service.inspect(manager, agent['id'], 'ipv6-lab')
            repeat = service.prepare(manager, agent['id'], 'ipv6-lab',
                                     {'interface': 'eth1', 'check_token': latest['check_token']})
            require(repeat['changed_routes'] == 0 and static_routes() == rows, 'Repeated preparation changed the route')
            report['checks'].append('repeat_prepare_does_not_duplicate')
            node = service.probe(manager, agent['id'], 'ipv6-lab')
            require(node['connectivity']['status'] == 'ok', 'Controller IPv6 TCP probe failed')
            report['probe'] = node
            report['ipv4_transfer'] = lab.transfer('controller-ipv4-via-ipv6')
            report['ipv6_transfer'] = lab.transfer('controller-fake6-via-ipv6', family=6)
            route_path = '/ipv6/route/' + quote(owned['.id'], safe='*')
            driver._request('PATCH', route_path, {'comment': 'lab-user-existing-ipv6-route'})
            user_owned = static_routes()
            known = service.inspect(manager, agent['id'], 'ipv6-lab')
            require(known['routers'][0]['plan'] is None, 'Existing correct foreign route was not recognized')
            service.prepare(manager, agent['id'], 'ipv6-lab', {'interface': 'eth1', 'check_token': known['check_token']})
            require(static_routes() == user_owned, 'Correct foreign route was changed or adopted')
            report['checks'].append('existing_user_route_preserved')
            driver._request('PATCH', route_path, {'gateway': 'fdda:150::dead%ether2'})
            conflict = static_routes()
            try:
                blocked = service.inspect(manager, agent['id'], 'ipv6-lab')
                require(blocked['can_prepare'] is False, 'Conflicting route did not block preparation')
                try:
                    service.prepare(manager, agent['id'], 'ipv6-lab', {'interface': 'eth1', 'check_token': blocked['check_token']})
                except MonitorError as error:
                    require(error.code == 'PREPARE_UNAVAILABLE', 'Wrong conflicting-route error')
                else:
                    raise RuntimeError('Conflicting route was overwritten')
                after_conflict = static_routes()
                after_settings = unchanged_settings()
                if after_conflict != conflict or after_settings != original_settings:
                    lab.save('controller-conflict-diff.json', {'before_routes': conflict, 'after_routes': after_conflict,
                                                              'before_settings': original_settings, 'after_settings': after_settings})
                require(after_conflict == conflict and after_settings == original_settings, 'Conflict handling mutated configuration')
            finally:
                driver._request('PATCH', route_path, {'gateway': owned['gateway'], 'comment': owned['comment']})
            report['checks'].append('conflicting_route_blocked_and_preserved')
            report['final_transfer'] = lab.transfer('after-conflict-restored', family=6)
        report['unrelated_router_settings_preserved'] = unchanged_settings() == original_settings
        report['completed'] = time.time()
        report['state'] = 'passed'
        lab.save('controller-report.json', report)
        print(json.dumps({'passed': report['checks'], 'ipv4_and_ipv6_transfers': True,
                          'report': str(lab.ROOT / 'controller-report.json')}, indent=2))
    finally:
        if report is not None and sys.exc_info()[0] is not None:
            report.update(state='failed', error=str(sys.exc_info()[1]), completed=time.time())
            lab.save('controller-report.json', report)
        if driver:
            driver.close()
        tunnel.terminate()
        try:
            tunnel.wait(timeout=5)
        except subprocess.TimeoutExpired:
            tunnel.kill()
            tunnel.wait()
        tunnel_log.close()


if __name__ == '__main__':
    run()
