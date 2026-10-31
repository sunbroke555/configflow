#!/usr/bin/env python3
"""Same-port IPv4 forward hint against the real VM151 RouterOS, driven from the test server.

Runs this checkout's backend in-process on a private copy of the IPv6
readiness preview data (as ``ipv6_forward_hint_lab.py``), with the lab router
set to plain static routes (no return routing, no rule sets): its existing
198.18.0.0/16 route points at Mihomo (CT152, 192.0.2.3) over ether2.

1. Without IPv4 filter rules the report has no hint, and CT153 reaches a TCP
   server on a Fake-IP address of CT152 through ROS at once.
2. RouterOS's default IPv4 forward chain (``defconf``, with its LAN/WAN
   interface lists, all tagged ``cf-fw4hint``) is added. The report shows the
   hint and a command placed before ``drop invalid``, also after that rule's
   comment is changed; the preview lists it once and is not blocked. New
   connections from CT153 stall for seconds.
3. The command, run unchanged through ``/rest/execute``, is accepted and lands
   right before ``drop invalid``; the hint disappears and connections are
   immediate again.

Nothing is published. Only the forward chain is used (input-chain defaults are
irrelevant to the hint and would affect other tests sharing VM151), and the
client's own sysctl settings stay as they are (it forwards, so it ignores
redirects). Every tagged object, the accept rule, the test address on CT152 and
the probe files are removed; the IPv4 firewall, interface lists, static routes,
/ip settings and the guests' touched state are compared with the start.

    rt -- ~/cf-test/.venv/bin/python scripts/tests/ipv4_forward_hint_lab.py
"""
import base64
import json
import os
from pathlib import Path
import shutil
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ipv6_readiness_lab as lab  # noqa: E402

SOURCE = Path(__file__).resolve().parents[2]
PREVIEW = Path('/home/sunbroke/previews/configflow-ipv6-readiness')
WORK = Path('/tmp/configflow-ipv4-forward-hint-lab')
ROUTER_ID = 'router_d10a2f2c9442'
TAG = 'cf-fw4hint'
FIX_COMMENT = 'LAN to Mihomo via router'
FAKE_NET = '198.18.240.0/20'
PORT = 18091
PATHS = ('/ip/firewall/filter', '/ip/firewall/nat', '/ip/firewall/mangle', '/ip/firewall/raw',
         '/ip/firewall/address-list', '/interface/list', '/interface/list/member', '/ip/route', '/ip/settings')
VOLATILE = {'bytes', 'packets', 'invalid', 'creation-time', 'timeout', 'active', 'inactive', 'immediate-gw',
            'debug.fwp-ptr', 'hw-offloaded'}
D = 'defconf: '
# RouterOS 7 default IPv4 forward chain, in order.
DEFCONF = [
    {'action': 'accept', 'ipsec-policy': 'in,ipsec', 'comment': D + 'accept in ipsec policy'},
    {'action': 'accept', 'ipsec-policy': 'out,ipsec', 'comment': D + 'accept out ipsec policy'},
    {'action': 'fasttrack-connection', 'connection-state': 'established,related', 'hw-offload': 'yes',
     'comment': D + 'fasttrack'},
    {'action': 'accept', 'connection-state': 'established,related,untracked',
     'comment': D + 'accept established,related, untracked'},
    {'action': 'drop', 'connection-state': 'invalid', 'comment': D + 'drop invalid'},
    {'action': 'drop', 'connection-state': 'new', 'connection-nat-state': '!dstnat', 'in-interface-list': 'WAN',
     'comment': D + 'drop all from WAN not DSTNATed'},
]
# ether1 is the lab management network; LAN as in the earlier defconf measurement.
MEMBERS = (('LAN', 'ether1'), ('LAN', 'ether2'), ('WAN', 'ether3'))
PROBE = r'''
import json, socket, subprocess, sys, threading, time
base, port, seq, burst = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
def one(ip):
    s = socket.socket(); s.settimeout(25); t0 = time.monotonic()
    try:
        s.connect((ip, port))
        s.sendall(f'GET / HTTP/1.1\r\nHost: lab\r\nConnection: close\r\n\r\n'.encode())
        data = b''
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        return {'ip': ip, 'ok': b'IPv4-hint-fixture' in data, 'seconds': round(time.monotonic() - t0, 3)}
    except Exception as error:
        return {'ip': ip, 'ok': False, 'seconds': round(time.monotonic() - t0, 3), 'error': repr(error)}
    finally:
        s.close()
out = {'seq': [], 'burst': [None] * burst}
for i in range(seq):
    subprocess.run(['ip', 'route', 'flush', 'cache'])
    out['seq'].append(one(f'{base}.{i + 1}')); time.sleep(0.5)
def run(j):
    out['burst'][j] = one(f'{base}.{101 + j}')
threads = [threading.Thread(target=run, args=(j,)) for j in range(burst)]
[thread.start() for thread in threads]; [thread.join() for thread in threads]
print(json.dumps(out))
'''
SERVER = r'''
import http.server, json
class H(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def do_GET(self):
        body = json.dumps({'origin': 'IPv4-hint-fixture'}).encode()
        self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass
class S(http.server.ThreadingHTTPServer):
    daemon_threads = True
S(('0.0.0.0', %d), H).serve_forever()
''' % PORT


def ros_client():
    credentials = json.loads((lab.ROOT / 'private.json').read_text())
    authorization = 'Basic ' + base64.b64encode(('admin:' + credentials['router_password']).encode()).decode()

    def ros(method, path, values=None):
        assert method == 'GET' or path.startswith(('/ip/firewall/filter', '/interface/list', '/execute')), path
        request = urllib.request.Request('http://127.0.0.1:19151/rest' + path, method=method,
                                         headers={'Authorization': authorization, 'Content-Type': 'application/json'},
                                         data=json.dumps(values).encode() if values is not None else None)
        try:
            with urllib.request.urlopen(request, timeout=30) as reply:
                text = reply.read()
                return json.loads(text) if text else None
        except urllib.error.HTTPError as error:
            raise RuntimeError(f'{method} {path}: {error.code} {error.read()[:300]!r}') from error
    return ros


def state(ros):
    result = {}
    for path in PATHS:
        rows = ros('GET', path)
        rows = rows if isinstance(rows, list) else [dict(rows, **{'.id': 'settings'})]
        result[path] = sorted(({k: v for k, v in row.items() if k not in VOLATILE} for row in rows
                               if path != '/ip/route' or row.get('dynamic') != 'true'), key=lambda row: row['.id'])
    return result


def guests():
    def sh(cid, command):
        return lab.pve('pct', 'exec', cid, '--', 'sh', '-c', command, check=False).stdout.decode()
    return {'152_lo': sh(152, 'ip -4 addr show dev lo'),
            '152_set': sh(152, 'nft list set inet mihomo inet4_local_address_set'),
            '152_listen': sh(152, f'ss -ltn | grep -c ":{PORT} " || true'),
            '153_sysctl': sh(153, 'sysctl net.ipv4.ip_forward net.ipv4.conf.all.accept_redirects')}


def server(on):
    if on:
        lab.write_remote('/tmp/cf-fw4hint-srv.py', SERVER, cid=152)
        lab.pve('pct', 'exec', 152, '--', 'sh', '-c',
                f'ip addr add 198.18.240.1/20 dev lo; nft add element inet mihomo inet4_local_address_set {{ {FAKE_NET} }}; '
                '(nohup python3 /tmp/cf-fw4hint-srv.py >/dev/null 2>&1 &); sleep 1')
        assert f':{PORT} ' in lab.guest(152, 'ss', '-ltn').stdout.decode()
    else:
        lab.pve('pct', 'exec', 152, '--', 'sh', '-c',
                'pkill -f "^python3 /tmp/cf-fw4hint-srv[.]py"; '
                f'nft delete element inet mihomo inet4_local_address_set {{ {FAKE_NET} }}; '
                'ip addr del 198.18.240.1/20 dev lo; rm -f /tmp/cf-fw4hint-*', check=False)
        lab.pve('pct', 'exec', 153, '--', 'sh', '-c', 'rm -f /tmp/cf-fw4hint-*; ip route flush cache', check=False)


def measure(base, seq=5, burst=10):
    lab.write_remote('/tmp/cf-fw4hint-probe.py', PROBE, cid=153)
    result = json.loads(lab.guest(153, 'python3', '/tmp/cf-fw4hint-probe.py', base, PORT, seq, burst,
                                  timeout=600).stdout)
    return result['seq'] + result['burst']


def main():
    lab.guard()
    ros = ros_client()
    before, guests_before = state(ros), guests()
    assert not any(TAG in str(row.get('comment', '')) or row.get('comment') == FIX_COMMENT
                   for path in PATHS for row in before[path]), 'leftover objects from an earlier run'
    assert not any(row.get('name') in ('LAN', 'WAN') for row in before['/interface/list']), 'LAN/WAN lists exist'
    assert not any(row.get('dynamic') != 'true' for row in before['/ip/firewall/filter']), 'IPv4 filter not empty'
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(mode=0o700, parents=True)
    shutil.copytree(PREVIEW / 'data', WORK / 'data')
    (WORK / 'backup.json').write_text(json.dumps({'ros': before, 'guests': guests_before}, indent=1))
    credentials = json.loads((PREVIEW / 'credentials.json').read_text())
    os.environ.update(DATA_DIR=str(WORK / 'data'), LOG_FILE=str(WORK / 'server.log'), LOG_LEVEL='WARNING',
                      ADMIN_USERNAME=credentials['username'], ADMIN_PASSWORD=credentials['password'],
                      JWT_SECRET_KEY=credentials['jwt_secret'], AGENT_REGISTRATION_KEY=credentials['registration_key'])
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        os.environ.pop(name, None)
    sys.path.insert(0, str(SOURCE))
    from backend.common import config as store
    from backend.common.config_repository import ProfileRepository
    store.set_repository(ProfileRepository(WORK / 'data', default_config_factory=store.get_default_config,
                                           initial_config_factory=store.get_default_config))
    from backend.routes import routers as views
    from backend.routers.forward_hint import IPV4_DROP
    from backend.app import app
    repository = store.get_repository()
    profile = repository.default_profile_id()

    def seed(config):
        router = next(item for item in config['routers']['items'] if item['id'] == ROUTER_ID)
        router.update(ip_rule_sets=[], ros_policy_rule_sets=[], manage_return_routing=False)
    repository.update_profile_transaction(profile, seed)
    store.reset_config_context()

    def call(view, method, path):
        with app.test_request_context(path, method=method, json={}):
            reply = view.__wrapped__(ROUTER_ID)
        response, status = reply if isinstance(reply, tuple) else (reply, reply.status_code)
        assert status == 200, response.get_json()
        return response.get_json()

    def report():
        return call(views.router_report, 'GET', f'/api/routers/{ROUTER_ID}/report')['report']

    def plan():
        return call(views.router_plan, 'POST', f'/api/routers/{ROUTER_ID}/plan')['plan']

    def tagged(path):
        return [row for row in ros('GET', path) if TAG in str(row.get('comment', ''))]

    results, timings, evidence = [], {}, {}
    try:
        clean = report()
        fake = next(item for item in clean['routes']['items'] if item['cidr'] == '198.18.0.0/16')
        assert fake['status'] in ('synced', 'adoptable', 'missing'), fake
        assert 'forward_hint' not in clean['routes'], clean['routes']
        results.append('no-ipv4-filter-shows-no-hint')
        server(True)
        timings['no_filter'] = measure('198.18.241')
        assert all(item['ok'] and item['seconds'] < 1 for item in timings['no_filter']), timings

        for name in ('LAN', 'WAN'):
            ros('PUT', '/interface/list', {'name': name, 'comment': f'{TAG} defconf'})
        for name, interface in MEMBERS:
            ros('PUT', '/interface/list/member', {'list': name, 'interface': interface, 'comment': f'{TAG} defconf'})
        for rule in DEFCONF:
            values = {'chain': 'forward', **rule, 'comment': f'{TAG} {rule["comment"]}'}
            try:
                ros('PUT', '/ip/firewall/filter', values)
            except RuntimeError:
                if 'hw-offload' not in values:
                    raise
                ros('PUT', '/ip/firewall/filter', {k: v for k, v in values.items() if k != 'hw-offload'})
        # RouterOS 7.25 flags connection-state rules INVALID for under a second after adding them.
        time.sleep(2)
        drop = next(row for row in tagged('/ip/firewall/filter') if row['comment'].endswith('drop invalid'))
        assert drop.get('invalid') != 'true', drop
        command = ('/ip firewall filter add chain=forward action=accept in-interface=ether2 out-interface=ether2 '
                   f'comment="{FIX_COMMENT}" place-before={drop[".id"]}')
        hinted = report()
        hint = hinted['routes'].get('forward_hint')
        assert hint == {'status': 'drop', 'warning': IPV4_DROP, 'fix_command': command}, hint
        preview = plan()
        assert preview['warnings'].count(IPV4_DROP) == 1 and not preview['blockers'], preview
        assert not any('syn-sent' in item for item in preview['warnings']), preview['warnings']
        evidence['command'] = command
        evidence['filter'] = [{k: row.get(k) for k in ('.id', 'chain', 'action', 'comment', 'dynamic')}
                              for row in ros('GET', '/ip/firewall/filter')]
        results.append('defconf-shows-hint-with-command-before-drop-invalid-preview-not-blocked')
        # A changed comment does not move the target: the rule is found by what it does.
        ros('PATCH', '/ip/firewall/filter/' + drop['.id'], {'comment': f'{TAG} 自己的规则'})
        time.sleep(1)
        renamed = report()['routes'].get('forward_hint') or {}
        assert renamed.get('fix_command') == command, renamed
        results.append('renamed-drop-rule-still-targeted')

        timings['defconf'] = measure('198.18.242')
        stalled = [item for item in timings['defconf'] if item['seconds'] > 5]
        assert len(stalled) >= 10, timings['defconf']  # all 5 sequential and most of the burst
        results.append(f'defconf-stalls-new-hairpin-connections-{len(stalled)}-of-{len(timings["defconf"])}')

        ros('POST', '/execute', {'script': command})
        rows = ros('GET', '/ip/firewall/filter')
        ids = [row['.id'] for row in rows]
        fix = next(row for row in rows if row.get('comment') == FIX_COMMENT)
        assert ids.index(fix['.id']) == ids.index(drop['.id']) - 1, ids
        assert (fix['chain'], fix['action'], fix['in-interface'], fix['out-interface']) == \
            ('forward', 'accept', 'ether2', 'ether2'), fix
        evidence['after_fix'] = [{k: row.get(k) for k in ('.id', 'chain', 'action', 'comment')} for row in rows]
        results.append('command-accepted-unchanged-and-placed-right-before-drop-invalid')
        time.sleep(2)
        fixed = report()
        assert 'forward_hint' not in fixed['routes'], fixed['routes']
        assert IPV4_DROP not in plan()['warnings']
        results.append('hint-disappears-after-the-fix')
        timings['fixed'] = measure('198.18.243')
        assert all(item['ok'] and item['seconds'] < 1 for item in timings['fixed']), timings['fixed']
        results.append('fixed-connections-immediate')
    finally:
        for row in ros('GET', '/ip/firewall/filter'):
            if TAG in str(row.get('comment', '')) or row.get('comment') == FIX_COMMENT:
                ros('DELETE', '/ip/firewall/filter/' + row['.id'])
        for row in tagged('/interface/list/member'):
            ros('DELETE', '/interface/list/member/' + row['.id'])
        for row in tagged('/interface/list'):
            ros('DELETE', '/interface/list/' + row['.id'])
        server(False)
        after, guests_after = state(ros), guests()
        differences = [path for path in PATHS if after[path] != before[path]]
        differences += [key for key in guests_before if guests_after[key] != guests_before[key]]
        (WORK.parent / 'configflow-ipv4-forward-hint-lab-restore.json').write_text(
            json.dumps({'differences': differences, 'before': before, 'after': after,
                        'guests_before': guests_before, 'guests_after': guests_after}, indent=1))
        assert not differences, differences
        shutil.rmtree(WORK, ignore_errors=True)
    summary = {'success': True, 'cases': results, 'restored': True, **evidence,
               'seconds': {key: [item['seconds'] for item in value] for key, value in timings.items()}}
    lab.save('ipv4-forward-hint-results.json', summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
