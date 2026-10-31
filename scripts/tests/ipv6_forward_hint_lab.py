#!/usr/bin/env python3
"""Same-port IPv6 forward hint against the real VM151 RouterOS, driven from the test server.

Runs this checkout's backend in-process on a private copy of the IPv6 preview
data (as ``ipv6_ruleset_routes_lab.py``). Two short phases on VM151:

A. RouterOS's default IPv6 forward rules (``defconf: drop invalid`` and
   friends, tagged ``cf-fwhint``) are appended only for two reports, a preview
   and the fix: the report shows the hint and one copyable command for IPv6
   Fake-IP and the rule-set routes (once in the preview); the command, run
   unchanged through ``/rest/execute``, lands before ``drop invalid`` and the
   hint disappears.
B. A drop-invalid rule limited to one Fake-IPv6 probe address (so other tests
   sharing VM151 never meet it) stalls each TCP connection from CT153 to a
   server on CT152; the same accept rule makes them immediate.

Nothing is published: no route is written. /ip firewall and /ip settings are
never touched. Every tagged rule, the generated accept rule, the test address
on CT152 and the probe files are removed; the IPv6 firewall, interface lists
and IPv6 routes are compared with their state before the run.

    rt -- ~/cf-test/.venv/bin/python scripts/tests/ipv6_forward_hint_lab.py
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

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ipv6_readiness_lab as lab  # noqa: E402

SOURCE = Path(__file__).resolve().parents[2]
PREVIEW = Path('/home/sunbroke/previews/configflow-ipv6-readiness')
WORK = Path('/tmp/configflow-ipv6-forward-hint-lab')
ROUTER_ID = 'router_d10a2f2c9442'
TAG = 'cf-fwhint'
FIX_COMMENT = 'LAN to Mihomo via router'
V6 = ['2001:b28:f23d::/48', '2001:67c:4e8::/48']
FAKE = 'fc00::c0fe'
PORT = 18197
PATHS = ('/ipv6/firewall/filter', '/ipv6/firewall/address-list', '/interface/list', '/interface/list/member',
         '/ipv6/route')
VOLATILE = {'bytes', 'packets', 'invalid', 'dynamic', 'creation-time', 'timeout', 'active', 'inactive',
            'immediate-gw', 'debug.fwp-ptr', 'hw-offloaded'}
BAD = [('::/128', 'unspecified address'), ('::1/128', 'lo'), ('fec0::/10', 'site-local'),
       ('::ffff:0.0.0.0/96', 'ipv4-mapped'), ('::/96', 'ipv4 compat'), ('100::/64', 'discard only '),
       ('2001:10::/28', 'ORCHID'), ('3ffe::/16', '6bone')]
# RouterOS 7 default IPv6 forward chain, without the interface-list rule: the
# LAN/WAN lists are left alone so a parallel IPv4 firewall test is not affected.
DEFCONF = [
    {'connection-state': 'established,related,untracked', 'action': 'accept', 'comment': 'accept established,related,untracked'},
    {'connection-state': 'invalid', 'action': 'drop', 'comment': 'drop invalid'},
    {'src-address-list': 'bad_ipv6', 'action': 'drop', 'comment': 'drop packets with bad src ipv6'},
    {'dst-address-list': 'bad_ipv6', 'action': 'drop', 'comment': 'drop packets with bad dst ipv6'},
    {'protocol': 'icmpv6', 'hop-limit': 'equal:1', 'action': 'drop', 'comment': 'rfc4890 drop hop-limit=1'},
    {'protocol': 'icmpv6', 'action': 'accept', 'comment': 'accept ICMPv6'},
]
PROBE = r'''
import json, socket, subprocess, sys, time
fake, port, n = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
out = []
for i in range(n):
    subprocess.run(['ip', '-6', 'route', 'flush', 'cache'])
    s = socket.socket(socket.AF_INET6); s.settimeout(20); t0 = time.monotonic()
    try:
        s.connect((fake, port))
        s.sendall(f'GET /hint-{i} HTTP/1.1\r\nHost: lab\r\nConnection: close\r\n\r\n'.encode())
        data = b''
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        out.append({'ok': b'IPv6-node-fixture' in data, 'seconds': round(time.monotonic() - t0, 3), 'head': data[:120].decode(errors='replace')})
    except Exception as error:
        out.append({'ok': False, 'seconds': round(time.monotonic() - t0, 3), 'error': repr(error)})
    finally:
        s.close()
    time.sleep(0.5)
print(json.dumps(out))
'''
SERVER = r'''
import http.server, json, socket
class H(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def do_GET(self):
        body = json.dumps({'origin': 'IPv6-node-fixture'}).encode()
        self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass
class S(http.server.ThreadingHTTPServer):
    address_family = socket.AF_INET6; daemon_threads = True
S((%r, %d), H).serve_forever()
''' % (FAKE, PORT)


def ros_client():
    credentials = json.loads((lab.ROOT / 'private.json').read_text())
    authorization = 'Basic ' + base64.b64encode(('admin:' + credentials['router_password']).encode()).decode()

    def ros(method, path, values=None):
        assert method == 'GET' or path.startswith(('/ipv6/firewall/filter', '/ipv6/firewall/address-list',
                                                   '/execute')), path
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
    return {path: sorted(({k: v for k, v in row.items() if k not in VOLATILE} for row in ros('GET', path)
                          if path != '/ipv6/route' or row.get('dynamic') != 'true'), key=lambda row: row['.id'])
            for path in PATHS}


def measure(count):
    lab.write_remote('/tmp/cf-fwhint-probe.py', PROBE, cid=153)
    result = lab.guest(153, 'python3', '/tmp/cf-fwhint-probe.py', FAKE, PORT, count, timeout=600)
    return json.loads(result.stdout)


def server(on):
    if on:
        lab.write_remote('/tmp/cf-fwhint-srv.py', SERVER, cid=152)
        lab.pve('pct', 'exec', 152, '--', 'sh', '-c',
                f'ip -6 addr add {FAKE}/128 dev lo; nft add element inet mihomo inet6_local_address_set {{ {FAKE} }}; '
                '(nohup python3 /tmp/cf-fwhint-srv.py >/dev/null 2>&1 &); sleep 1')
        listening = lab.guest(152, 'ss', '-ltn').stdout.decode()
        assert f'[{FAKE}]:{PORT}' in listening, listening
    else:
        lab.pve('pct', 'exec', 152, '--', 'sh', '-c',
                'pkill -f "^python3 /tmp/cf-fwhint-srv[.]py"; '
                f'nft delete element inet mihomo inet6_local_address_set {{ {FAKE} }}; '
                f'ip -6 addr del {FAKE}/128 dev lo; rm -f /tmp/cf-fwhint-*', check=False)
        lab.guest(153, 'rm', '-f', '/tmp/cf-fwhint-probe.py', check=False)


def main():
    lab.guard()
    ros = ros_client()
    before = state(ros)
    assert not any(TAG in str(row.get('comment', '')) or row.get('comment') == FIX_COMMENT
                   for row in before['/ipv6/firewall/filter']), 'leftover rules from an earlier run'
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(mode=0o700, parents=True)
    shutil.copytree(PREVIEW / 'data', WORK / 'data')
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
    from backend.app import app
    from backend.utils.rule_utils import write_validated_rule_cache
    repository = store.get_repository()
    profile = repository.default_profile_id()

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

    repository.update_shared_transaction(lambda shared: shared['rule_library'].append(
        {'id': 'lab-telegram6', 'name': 'LabTelegram6', 'source_type': 'url',
         'url': 'https://rules.invalid/telegram6.list', 'behavior': 'classical', 'enabled': True}))

    def seed(config):
        config['rule_configs'].append({'id': 'lab-tg6', 'itemType': 'ruleset', 'name': 'LabTelegram6',
                                       'library_rule_id': 'lab-telegram6', 'enabled': True, 'policy': 'PROXY'})
        router = next(item for item in config['routers']['items'] if item['id'] == ROUTER_ID)
        router.update(ip_rule_sets=['lab-tg6'], ros_policy_rule_sets=[])
        document = yaml.safe_load(config['mihomo'].get('custom_config') or '') or {}
        document['ipv6'] = True
        document.setdefault('tun', {})['enable'] = True
        dns = document.setdefault('dns', {})
        dns.update({'ipv6': True, 'enhanced-mode': 'fake-ip'})
        dns.setdefault('fake-ip-range6', 'fc00::1/64')
        config['mihomo']['custom_config'] = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    repository.update_profile_transaction(profile, seed)
    store.reset_config_context()
    config = store.get_config(profile)
    source = next(rule for rule in config['rule_library'] if rule['id'] == 'lab-telegram6')
    write_validated_rule_cache(source, '\n'.join('IP-CIDR6,' + cidr for cidr in V6) + '\n', config_data=config)
    store.reset_config_context()

    results, timings = [], {}
    clean = report()
    assert clean['ipv6_fakeip']['enabled'] and not clean['ipv6_fakeip']['blockers'], clean['ipv6_fakeip']
    assert clean['ipv6_routes']['gateway'] and not clean['ipv6_routes']['skip'], clean['ipv6_routes']
    rows = ros('GET', '/ipv6/firewall/filter')
    foreign = next((row for row in rows if row.get('chain') == 'forward' and row.get('action') == 'drop'
                    and row.get('connection-state') == 'invalid' and row.get('disabled') != 'true'), None)
    if foreign is not None:
        # Another test has its own drop-invalid in place: check the report
        # against it, read-only, and leave the firewall and guests alone.
        command = clean['ipv6_fakeip']['fix_command']
        assert command == ('/ipv6 firewall filter add chain=forward action=accept in-interface=ether2 '
                           f'out-interface=ether2 comment="{FIX_COMMENT}" place-before={foreign[".id"]}'), clean
        assert clean['ipv6_routes']['fix_command'] == command
        warning = next(item for item in clean['ipv6_fakeip']['warnings'] if 'drop invalid' in item)
        preview = plan()
        assert sum(item == warning for item in preview['warnings']) == 1 and not preview['blockers'], preview
        assert state(ros) == before, 'VM151 changed during the read-only check'
        shutil.rmtree(WORK, ignore_errors=True)
        summary = {'success': True, 'mode': 'read-only', 'drop_invalid': foreign['.id'],
                   'drop_invalid_comment': foreign.get('comment'), 'command': command,
                   'cases': ['existing-drop-invalid-shows-one-shared-command-before-it']}
        lab.save('forward-hint-readonly-results.json', summary)
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        return
    assert not clean['ipv6_fakeip'].get('fix_command') and not clean['ipv6_routes'].get('fix_command'), clean
    results.append('lab-firewall-without-drop-invalid-shows-no-hint')
    try:
        # Phase A, the product check: the full default forward chain is in
        # place only for the two reports, the preview and the fix.
        for address, comment in BAD:
            ros('PUT', '/ipv6/firewall/address-list', {'list': 'bad_ipv6', 'address': address,
                                                       'comment': f'{TAG} defconf: {comment}'})
        for rule in DEFCONF:
            ros('PUT', '/ipv6/firewall/filter', {'chain': 'forward', **rule, 'comment': f'{TAG} defconf: {rule["comment"]}'})
        # RouterOS 7.25 flags connection-state rules INVALID for under a second
        # after they are added (the report rightly ignores invalid rules).
        time.sleep(2)
        drop = next(row for row in ros('GET', '/ipv6/firewall/filter')
                    if row.get('comment') == f'{TAG} defconf: drop invalid')
        assert drop['invalid'] == 'false', drop
        hinted = report()
        command = hinted['ipv6_fakeip']['fix_command']
        assert command == ('/ipv6 firewall filter add chain=forward action=accept in-interface=ether2 '
                           f'out-interface=ether2 comment="{FIX_COMMENT}" place-before={drop[".id"]}'), \
            json.dumps({'fakeip': hinted['ipv6_fakeip'], 'routes': hinted['ipv6_routes']}, ensure_ascii=False)
        assert hinted['ipv6_routes']['fix_command'] == command, hinted['ipv6_routes']
        warning = next(item for item in hinted['ipv6_fakeip']['warnings'] if 'drop invalid' in item)
        assert warning in hinted['ipv6_routes']['warnings']
        preview = plan()
        assert sum(item == warning for item in preview['warnings']) == 1, preview['warnings']
        assert not preview['blockers'], preview['blockers']
        results.append('defconf-drop-invalid-shows-one-shared-command-before-drop-invalid')
        ros('POST', '/execute', {'script': command})
        rows = ros('GET', '/ipv6/firewall/filter')
        ids = [row['.id'] for row in rows]
        fix = next(row for row in rows if row.get('comment') == FIX_COMMENT)
        assert ids.index(fix['.id']) == ids.index(drop['.id']) - 1, ids
        assert (fix['in-interface'], fix['out-interface'], fix['action']) == ('ether2', 'ether2', 'accept'), fix
        results.append('generated-command-accepted-and-placed-before-drop-invalid')
        time.sleep(2)
        fixed = report()
        assert not fixed['ipv6_fakeip'].get('fix_command') and not fixed['ipv6_routes'].get('fix_command')
        assert not any('drop invalid' in item for item in fixed['ipv6_fakeip']['warnings'] + fixed['ipv6_routes']['warnings'])
        results.append('hint-disappears-after-the-fix')
        for row in ros('GET', '/ipv6/firewall/filter'):
            if TAG in str(row.get('comment', '')) or row.get('comment') == FIX_COMMENT:
                ros('DELETE', '/ipv6/firewall/filter/' + row['.id'])

        # Phase B, the traffic: a drop-invalid rule limited to the probe address,
        # so other tests sharing VM151 never meet it; then the same accept rule.
        server(True)
        timings['lab_firewall'] = measure(2)
        assert all(item['ok'] and item['seconds'] < 1 for item in timings['lab_firewall']), timings
        scoped = ros('PUT', '/ipv6/firewall/filter', {'chain': 'forward', 'action': 'drop', 'connection-state': 'invalid',
                                                      'dst-address': FAKE + '/128', 'comment': f'{TAG} probe-only drop invalid'})
        time.sleep(2)
        timings['drop_invalid'] = measure(3)
        assert all(item['seconds'] > 5 for item in timings['drop_invalid']), timings
        results.append('drop-invalid-stalls-each-new-hairpin-tcp-connection')
        ros('POST', '/execute', {'script': command.rsplit(' place-before=', 1)[0] + ' place-before=' + scoped['.id']})
        timings['fixed'] = measure(3)
        assert all(item['ok'] and item['seconds'] < 1 for item in timings['fixed']), timings
        results.append('generated-accept-makes-connections-immediate')
    finally:
        for row in ros('GET', '/ipv6/firewall/filter'):
            if TAG in str(row.get('comment', '')) or row.get('comment') == FIX_COMMENT:
                ros('DELETE', '/ipv6/firewall/filter/' + row['.id'])
        for row in ros('GET', '/ipv6/firewall/address-list'):
            if TAG in str(row.get('comment', '')):
                ros('DELETE', '/ipv6/firewall/address-list/' + row['.id'])
        server(False)
        after = state(ros)
        for path in PATHS:
            assert after[path] == before[path], (path, before[path], after[path])
        shutil.rmtree(WORK, ignore_errors=True)
    summary = {'success': True, 'cases': results, 'restored': True,
               'seconds': {key: [item['seconds'] for item in value] for key, value in timings.items()}}
    lab.save('forward-hint-results.json', summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
