#!/usr/bin/env python3
"""Real RA DNS release/readback in owned VM151/CT153; restores all fixtures.

The temporary OxiDNS registration represents the existing lab DNS listener;
this tests router publication, not OxiDNS installation or resolver correctness.
"""
import base64
import json
import urllib.error
import urllib.request

import ipv6_readiness_lab as lab
import ipv6_verification_lab as runtime

ROUTER_ID = 'router_d10a2f2c9442'
RA_PROBE = '''import socket,struct,time,json
s=socket.socket(socket.AF_INET6,socket.SOCK_RAW,socket.IPPROTO_ICMPV6)
s.setsockopt(socket.SOL_SOCKET,socket.SO_BINDTODEVICE,b"eth1\\0")
i=socket.if_nametoindex("eth1")
s.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_MULTICAST_IF,i)
s.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_MULTICAST_HOPS,255)
s.settimeout(8)
s.sendto(bytes([133,0,0,0,0,0,0,0]),("ff02::2",0,0,i))
end=time.monotonic()+8
while time.monotonic()<end:
 b,src=s.recvfrom(65535)
 if b[0]!=134:continue
 options=[];n=16
 while n+2<=len(b) and b[n+1]:
  size=b[n+1]*8;options.append({"type":b[n],"bytes":b[n:n+size].hex()});n+=size
 print(json.dumps({"source":src[0],"lifetime":struct.unpack("!H",b[6:8])[0],"options":options}));break
else:raise RuntimeError("No RA received")
'''


def main():
    lab.guard()
    assert json.loads((runtime.PREVIEW / 'connections.json').read_text())['owner'] == lab.OWNER
    token, _ = runtime.session()
    private = json.loads((lab.ROOT / 'private.json').read_text())
    auth = 'Basic ' + base64.b64encode(('admin:' + private['router_password']).encode()).decode()

    def ros(method, path, value=None):
        assert path.startswith(('/ipv6/nd', '/ip/dhcp-server', '/ip/route'))
        request = urllib.request.Request('http://127.0.0.1:19151/rest' + path, method=method,
            headers={'Authorization': auth, 'Content-Type': 'application/json'},
            data=json.dumps(value).encode() if value is not None else None)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.load(response) if response.status != 204 else None
        except urllib.error.HTTPError as error:
            raise RuntimeError(f'{method} {path}: {error.read().decode()}') from None

    def api(method, path, value=None):
        return runtime.api(method, path, value, token)

    root = '/api/routers/' + ROUTER_ID
    routers = api('GET', '/api/routers')
    if isinstance(routers, dict):
        routers = routers['routers']
    original_router = next(row for row in routers if row['id'] == ROUTER_ID)
    assert not original_router.get('dns_agent_id'), 'Existing DNS association must not be replaced'
    before = ros('GET', '/ipv6/nd')
    assert len(before) == 1 and before[0]['interface'] == 'ether2'
    assert before[0]['advertise-dns'] == 'no'
    assert not ros('GET', '/ip/dhcp-server/network') and not ros('GET', '/ip/dhcp-server')
    prefixes = ros('GET', '/ipv6/nd/prefix')
    routes = ros('GET', '/ip/route')
    owned = []
    agent_id = None
    linked = False
    cases = []
    nd_path = '/ipv6/nd/' + before[0]['.id']
    lab.save('router-ra-dns-original.json', {'nd': before, 'prefixes': prefixes, 'routes': routes}, private=True)

    def packet():
        return json.loads(lab.guest(153, 'python3', '-c', RA_PROBE, timeout=15).stdout)

    def prefix_options(value):
        return sorted(row['bytes'] for row in value['options'] if row['type'] == 3)

    def plan():
        value = api('POST', root + '/plan', {})['plan']
        assert not value['blockers'], value['blockers']
        for stage in value['stages']:
            for op in stage['ops']:
                assert op['kind'] in ('route_comment', 'dhcp_dns_update', 'ipv6_dns_update'), op['kind']
                if op['kind'] == 'route_comment':
                    old = next(row for row in routes if row['.id'] == op['id'])
                    assert old.get('comment') == 'ipv6-readiness-lab' and old['dst-address'] == '198.18.0.0/16'
        return value

    def publish(value):
        result = api('POST', root + '/apply', {'fingerprint': value['fingerprint']})
        assert result['success'] and result['result']['ok'], result

    try:
        for path, values in [('/ip/dhcp-server/network', {'address': '192.0.2.0/24', 'gateway': '192.0.2.1',
                                'dns-server': '192.0.2.1', 'comment': lab.OWNER + '-ra-dns'}),
                             ('/ip/dhcp-server', {'name': 'cf-ra-dns-test', 'interface': 'ether2',
                                'address-pool': 'static-only', 'disabled': 'false', 'comment': lab.OWNER + '-ra-dns'})]:
            row = ros('PUT', path, values)
            owned.append((path, row['.id']))
        registration = {'name': 'RA DNS 临时验收目标', 'host': '192.0.2.3',
                        'port': 27649, 'service_type': 'oxidns', 'deployment_method': 'shell', 'profile_id': 'default'}
        result = api('POST', '/api/agents', registration)
        agent_id = result['data']['id']
        api('PUT', root, {'dns_agent_id': agent_id})
        linked = True
        for mode in ('yes', 'self'):
            dns_value = 'fdda:150::3' if mode == 'yes' else ''
            ros('PATCH', nd_path, {'advertise-dns': mode, 'dns': dns_value})
            advertised = packet()
            assert any(row['type'] == 25 for row in advertised['options']), advertised
            report = api('GET', root + '/report')['report']['ipv6_dns']
            assert report['status'] == 'pending' and report['interface'] == 'ether2', report
            planned = plan()
            assert next(s for s in planned['stages'] if s['key'] == 'ipv6_dns')['ops'][0]['values'] == {'advertise-dns': 'no'}
            assert ros('GET', '/ipv6/nd')[0]['advertise-dns'] == mode, 'Preview wrote to router'
            if mode == 'yes':
                ros('PATCH', nd_path, {'ra-preference': 'high'})
                try:
                    api('POST', root + '/apply', {'fingerprint': planned['fingerprint']})
                except RuntimeError as error:
                    assert 'HTTP 409' in str(error)
                else:
                    raise AssertionError('Changed ND accepted stale preview')
                assert ros('GET', '/ipv6/nd')[0]['advertise-dns'] == mode
                ros('PATCH', nd_path, {'ra-preference': before[0]['ra-preference']})
                cases.append('changed-ra-rejects-stale-preview-without-write')
                planned = plan()
            publish(planned)
            current = ros('GET', '/ipv6/nd')[0]
            expected = {**before[0], 'advertise-dns': 'no'}
            if dns_value:
                expected['dns'] = dns_value
            assert current == expected, current
            assert ros('GET', '/ip/dhcp-server/network')[0]['dns-server'] == '192.0.2.3'
            observed = packet()
            assert not any(row['type'] == 25 for row in observed['options']), observed
            assert observed['source'] == advertised['source'] and observed['lifetime'] == advertised['lifetime'] > 0
            assert prefix_options(observed) == prefix_options(advertised) and prefix_options(observed)
            assert ros('GET', '/ipv6/nd/prefix') == prefixes
            cases.append(mode + '-publish-stops-DNS-keeps-prefix-and-gateway')
            repeated = plan()
            assert repeated['empty']
            publish(repeated)
            cases.append(mode + '-repeat-publish-no-changes')
            print(mode + ': publish and repeat passed; RA prefix and gateway retained', flush=True)
        ros('PATCH', nd_path, {'interface': 'all', 'advertise-dns': 'yes'})
        blocked = api('POST', root + '/plan', {})['plan']
        assert blocked['blockers'] and not next(s for s in blocked['stages'] if s['key'] == 'ipv6_dns')['ops']
        cases.append('shared-all-without-explicit-LAN-blocks-global-change')
        ros('PATCH', nd_path, {'interface': 'ether2', 'advertise-dns': 'no'})
        api('PUT', root, {'dns_agent_id': ''})
        linked = False
        assert 'ipv6_dns' not in api('GET', root + '/report')['report']
        assert ros('GET', '/ipv6/nd')[0]['advertise-dns'] == 'no'
        cases.append('unlink-keeps-present-router-config')
    finally:
        cleanup_errors = []

        def cleanup(label, action):
            try:
                action()
            except Exception as error:
                cleanup_errors.append(f'{label}: {error}')

        def unlink():
            current = api('GET', '/api/routers')
            current = current['routers'] if isinstance(current, dict) else current
            assert next(r for r in current if r['id'] == ROUTER_ID)['dns_agent_id'] == agent_id
            api('PUT', root, {'dns_agent_id': ''})

        if linked:
            cleanup('unlink temporary DNS', unlink)
        cleanup('restore original ND', lambda: ros('PATCH', nd_path,
            {'interface': before[0]['interface'], 'advertise-dns': before[0]['advertise-dns'],
             'ra-preference': before[0]['ra-preference'], 'dns': before[0].get('dns', '')}))
        for path, resource_id in reversed(owned):
            def remove_owned(path=path, resource_id=resource_id):
                row = next(r for r in ros('GET', path) if r['.id'] == resource_id)
                assert row.get('comment') == lab.OWNER + '-ra-dns'
                ros('DELETE', path + '/' + resource_id)
            cleanup('remove ' + path, remove_owned)
        if agent_id:
            cleanup('remove temporary DNS registration', lambda: api('DELETE', '/api/agents/' + agent_id))
        for old in routes:
            def restore_comment(old=old):
                current = next((row for row in ros('GET', '/ip/route') if row['.id'] == old['.id']), None)
                if current and current.get('comment') != old.get('comment'):
                    assert old.get('comment') == 'ipv6-readiness-lab' and current.get('comment') == 'cf-ipv6-readiness-lab'
                    ros('PATCH', '/ip/route/' + old['.id'], {'comment': old['comment']})
            cleanup('restore route comment', restore_comment)
        assert not cleanup_errors, cleanup_errors
        assert ros('GET', '/ipv6/nd') == before
        assert ros('GET', '/ipv6/nd/prefix') == prefixes
        assert not ros('GET', '/ip/dhcp-server/network') and not ros('GET', '/ip/dhcp-server')
    ident = runtime.source_id(token)
    api('POST', f'/api/agents/{ident}/ipv6-readiness/verify', {})
    verification = runtime.wait_report(token, ident)
    statuses = {row['id']: row['status'] for row in verification['checks']}
    for key in ('client_http_ipv4', 'client_mihomo_path', 'client_ipv6_transport', 'client_ros_path'):
        assert statuses.get(key) == 'ok', {key: statuses.get(key)}
    cases.append('restored-original-client-ROS-Mihomo-IPv6-node-path')
    result = {'success': True, 'cases': cases, 'fixtures_restored': True}
    lab.save('router-ra-dns-results.json', result)
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
