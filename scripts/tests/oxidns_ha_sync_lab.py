#!/usr/bin/env python3
"""Real OxiDNS HA acceptance in the retained PVE laboratory, driven on Linux.

Never run on the Mac or a production host. The driver reaches PVE using its
existing test-server-only SSH key. CT130/131/132 must already have the named
pre-test snapshot; prepare refuses different identities or network attachments.
All writes stay inside these three snapshot-protected laboratory containers.
Usage (from the task worktree on 192.168.0.32):
  python scripts/tests/oxidns_ha_sync_lab.py prepare
  python scripts/tests/oxidns_ha_sync_lab.py test
  python scripts/tests/oxidns_ha_sync_lab.py cleanup
Private credentials remain in /tmp/configflow-ha-sync; report.json is sanitized.
"""
from __future__ import annotations
import argparse
import concurrent.futures
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import shlex
import socket
import struct
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path('/tmp/configflow-ha-sync')
GROOT = '/opt/cf-ha-sync'
SNAPSHOT = 'cf-ha-sync-20261027'
GUESTS = {130: ('cf-oxi-a-retest', '192.168.79.21'), 131: ('cf-oxi-b-retest', '192.168.79.22'), 132: ('cf-app-retest', '192.168.79.30')}
VIP = '192.168.79.53'
SERVER_URL = 'http://192.168.79.30:18080'
LOCAL_URL = 'http://127.0.0.1:19282'
REPO = Path(__file__).resolve().parents[2]
TERMINAL = {'succeeded', 'failed', 'rolled_back', 'rollback_failed'}


def run(args, data=None, check=True):
    result = subprocess.run(args, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode:
        raise RuntimeError(f'Command failed: {shlex.join(args[:6])}: {result.stderr.decode(errors="replace")[-1000:]}')
    return result.stdout


def pve(*args, data=None, check=True):
    return run(['ssh', '-o', 'BatchMode=yes', 'pve', shlex.join([str(x) for x in args])], data, check)


def guest(cid, *args, data=None, check=True):
    if cid not in GUESTS:
        raise RuntimeError('Only the three explicit laboratory containers are allowed')
    return pve('pct', 'exec', cid, '--', *args, data=data, check=check)


def put(cid, path, data, mode=0o600):
    if not (path.startswith(GROOT + '/') or path == '/etc/oxidns/config.yaml'
            or path.startswith('/etc/oxidns/rules/') or path.startswith('/run/systemd/system/oxidns.service.d/')):
        raise RuntimeError('Not a laboratory-owned destination')
    script = 'import os,sys; from pathlib import Path; p=Path(sys.argv[1]); p.parent.mkdir(parents=True,exist_ok=True); p.write_bytes(sys.stdin.buffer.read()); p.chmod(int(sys.argv[2]))'
    guest(cid, 'python3', '-c', script, path, str(mode), data=data if isinstance(data, bytes) else data.encode())


def private(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2)); path.chmod(0o600)


def guard():
    if sys.platform != 'linux' or socket.gethostname() == 'pve':
        raise RuntimeError('Drive this from the dedicated Linux test server')
    for cid, (name, _) in GUESTS.items():
        config = pve('pct', 'config', cid).decode()
        if f'hostname: {name}\n' not in config or 'bridge=cfgt' not in config or 'bridge=vmbr' in config:
            raise RuntimeError(f'Unexpected identity/network for CT{cid}')
        if SNAPSHOT not in pve('pct', 'listsnapshot', cid).decode():
            raise RuntimeError(f'CT{cid} lacks the pre-test recovery snapshot')


def api(method, path, body=None, allow_error=False, timeout=240):
    credentials = json.loads((ROOT / 'private.json').read_text())
    headers = {'Content-Type': 'application/json'}
    if credentials.get('admin_token'):
        headers['Authorization'] = 'Bearer ' + credentials['admin_token']
    req = urllib.request.Request(LOCAL_URL + path, method=method, headers=headers,
                                 data=None if body is None else json.dumps(body).encode())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            code, data = response.status, json.load(response)
    except urllib.error.HTTPError as error:
        code, data = error.code, json.load(error)
    if code >= 400 and not allow_error:
        raise RuntimeError(f'{method} {path}: HTTP {code}: {data.get("message", "request failed")}')
    return code, data


def document(address, workers=2):
    return {'runtime': {'worker_threads': workers}, 'log': {'level': 'info'}, 'plugins': [
        {'tag': 'lab_domains', 'type': 'domain_set', 'args': {'files': ['rules/lab.txt']}},
        {'tag': 'lab_answer', 'type': 'black_hole', 'args': {'mode': 'custom', 'ips': [address], 'short_circuit': True}},
        {'tag': 'main', 'type': 'sequence', 'args': [{'exec': '$lab_answer'}]},
        {'tag': 'udp', 'type': 'udp_server', 'args': {'entry': 'main', 'listen': '0.0.0.0:53'}},
        {'tag': 'tcp', 'type': 'tcp_server', 'args': {'entry': 'main', 'listen': '0.0.0.0:53'}}]}


def server():
    root = Path(GROOT)
    creds = json.loads((root / 'credentials.json').read_text())
    os.environ.update(DATA_DIR=str(root / 'data'), LOG_FILE=str(root / 'backend.log'), LOG_LEVEL='WARNING',
                      ADMIN_USERNAME=creds['username'], ADMIN_PASSWORD=creds['password'],
                      JWT_SECRET_KEY=creds['jwt_secret'], AGENT_REGISTRATION_KEY=creds['registration_key'],
                      SUB_STORE_BUILTIN_DIR=str(root / 'no-sub-store'), NO_PROXY='*')
    for key in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy'):
        os.environ.pop(key, None)
    os.chdir(root)
    sys.path.insert(0, str(REPO))
    from backend.common import config as store
    from backend.common.config_repository import ProfileRepository
    store.set_repository(ProfileRepository(root / 'data', default_config_factory=store.get_default_config,
                                           initial_config_factory=store.get_default_config))
    from backend.app import app
    from werkzeug.serving import make_server
    make_server('0.0.0.0', 18080, app, threaded=True).serve_forever()


def stage_source():
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as archive:
        for name in ('backend', 'scripts', 'config_template.json', 'version.json', 'requirements.txt'):
            path = REPO / name
            if path.exists():
                archive.add(path, arcname=name, filter=lambda info: None if '__pycache__' in info.name or info.name.endswith('.pyc') else info)
    guest(132, 'mkdir', '-p', GROOT + '/source')
    guest(132, 'tar', '-xzf', '-', '-C', GROOT + '/source', data=stream.getvalue())


def start_tunnel():
    pidfile = ROOT / 'tunnel.pid'
    if pidfile.exists():
        try:
            os.kill(int(pidfile.read_text()), 0)
            return
        except ProcessLookupError:
            pass
    proc = subprocess.Popen(['ssh', '-N', '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes',
                             '-L', '127.0.0.1:19282:10.97.0.74:18080', 'pve'], stdout=subprocess.DEVNULL,
                            stderr=(ROOT / 'tunnel.log').open('ab'))
    pidfile.write_text(str(proc.pid))
    time.sleep(.5)
    if proc.poll() is not None:
        raise RuntimeError('Could not establish laboratory HTTP tunnel')


def prepare():
    guard()
    ROOT.mkdir(mode=0o700, exist_ok=True)
    if (ROOT / 'private.json').exists():
        raise RuntimeError('Existing private fixture; use update-source or test instead of reseeding')
    creds = {'username': 'ha-sync-lab', 'password': secrets.token_urlsafe(30),
             'jwt_secret': secrets.token_urlsafe(48), 'registration_key': secrets.token_urlsafe(30)}
    private(ROOT / 'private.json', creds)
    stage_source()
    put(132, GROOT + '/credentials.json', json.dumps(creds))
    guest(132, 'systemd-run', '--unit=cf-ha-sync-backend', '--property=WorkingDirectory=' + GROOT,
          '/opt/cf-retest/venv/bin/python', GROOT + '/source/scripts/tests/oxidns_ha_sync_lab.py', 'server')
    start_tunnel()
    for _ in range(30):
        try:
            _, logged = api('POST', '/api/auth/login', {'username': creds['username'], 'password': creds['password']})
            creds['admin_token'] = logged['token']; break
        except (OSError, RuntimeError):
            time.sleep(1)
    else:
        raise RuntimeError('Laboratory ConfigFlow did not become ready')
    private(ROOT / 'private.json', creds)
    api('POST', '/api/server-domain', {'new_domain': SERVER_URL})
    members = {}
    binary = Path('/tmp/configflow-ha-sync-agent').read_bytes()
    for cid, initial in ((130, '198.51.100.10'), (131, '198.51.100.11')):
        name, host = GUESTS[cid]
        guest(cid, 'systemctl', 'stop', 'configflow-agent', 'oxidns', 'keepalived', check=False)
        # Original journals/configuration are protected by the stopped pre-test snapshot.
        guest(cid, 'mkdir', '-p', GROOT, '/etc/oxidns/rules')
        # Retained lab images have /opt owned by a former fixture UID.
        # keepalived correctly requires every executable ancestor to be root-owned.
        # The pre-test snapshot restores that original ownership afterwards.
        guest(cid, 'chown', 'root:root', '/opt')
        put(cid, '/etc/oxidns/config.yaml', json.dumps(document(initial)))
        put(cid, '/etc/oxidns/rules/lab.txt', 'full:initial-' + str(cid) + '.invalid\n')
        put(cid, GROOT + '/configflow-agent', binary, 0o755)
        _, registered = api('POST', '/api/agents/register', {'name': 'HA lab ' + str(cid), 'host': host, 'port': 18081,
                                                  'registration_key': creds['registration_key'],
                                                  'service_type': 'oxidns', 'deployment_method': 'shell', 'profile_id': 'default'})
        member = registered; members[str(cid)] = member
        assert len(member['token']) >= 24 and member['token'] != '[REDACTED]', 'Enrollment must return a real Agent credential'
        config = {'server_url': SERVER_URL, 'agent_name': 'HA lab ' + str(cid), 'agent_host': host,
                  'agent_port': 18081, 'service_type': 'oxidns', 'deployment_method': 'shell',
                  'service_name': 'oxidns', 'config_path': '/etc/oxidns/config.yaml',
                  'service_manager': 'systemd', 'service_binary': '/usr/local/bin/oxidns',
                  'service_unit': 'oxidns.service', 'restart_command': 'systemctl restart oxidns',
                  'stop_command': 'systemctl stop oxidns', 'start_command': 'systemctl start oxidns',
                  'status_command': 'systemctl is-active --quiet oxidns', 'health_dns_address': '127.0.0.1:53',
                  'health_dns_name': 'sync.lab.invalid', 'deployment_health_timeout': 8,
                  'dns_watchdog_disabled': True, 'heartbeat_interval': 2,
                  'agent_id': member['id'], 'token': member['token'], 'enable_metrics': False}
        put(cid, GROOT + '/agent.json', json.dumps(config))
        guest(cid, 'systemctl', 'start', 'oxidns')
        guest(cid, 'systemd-run', '--unit=cf-ha-sync-agent', '--property=WorkingDirectory=' + GROOT,
              GROOT + '/configflow-agent', '-config', GROOT + '/agent.json')
    creds['members'] = members
    private(ROOT / 'private.json', creds)
    print(json.dumps({'prepared': True, 'guests': list(GUESTS), 'vip': VIP, 'source_sha256': hashlib.sha256(binary).hexdigest()}))


def dns_query(host, name='sync.lab.invalid', timeout=.3):
    ident = os.urandom(2)
    question = ident + b'\x01\x00\x00\x01' + b'\0' * 6
    question += b''.join(bytes([len(part)]) + part.encode() for part in name.split('.')) + b'\0\0\x01\0\x01'
    started = time.monotonic()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout); sock.sendto(question, (host, 53)); response = sock.recv(4096)
        if response[:2] != ident or not response[2] & 0x80:
            raise ValueError('invalid DNS reply')
        return {'ok': True, 'rcode': response[3] & 15, 'ip': socket.inet_ntoa(response[-4:]) if response[7] else None,
                'ms': round((time.monotonic() - started) * 1000, 3)}
    except (OSError, ValueError) as error:
        return {'ok': False, 'error': type(error).__name__, 'ms': round((time.monotonic() - started) * 1000, 3)}


def probe():
    destination = Path(GROOT + '/dns-probe.jsonl')
    stop = Path(GROOT + '/stop-probe')
    stop.unlink(missing_ok=True)
    with destination.open('w') as output, concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        while not stop.exists():
            started = time.time()
            responses = list(pool.map(dns_query, [GUESTS[130][1], GUESTS[131][1], VIP]))
            output.write(json.dumps({'time': started, 'nodes': responses}) + '\n'); output.flush()
            time.sleep(max(0, .1 - (time.time() - started)))



def snapshot(cid):
    script = 'import hashlib,json,subprocess; from pathlib import Path; paths=["/etc/oxidns/config.yaml","/etc/oxidns/rules/lab.txt"]; print(json.dumps({"hashes":{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths},"pid":subprocess.check_output(["systemctl","show","oxidns","-p","MainPID","--value"],text=True).strip(),"active":subprocess.check_output(["systemctl","is-active","oxidns"],text=True).strip()}))'
    return json.loads(guest(cid, 'python3', '-c', script))


def dns_snapshot():
    script = 'import sys,json; sys.path.insert(0,"/opt/cf-ha-sync/source/scripts/tests"); from oxidns_ha_sync_lab import dns_query; print(json.dumps({host:dns_query(host) for host in ["192.168.79.21","192.168.79.22","192.168.79.53"]}))'
    return json.loads(guest(132, '/opt/cf-retest/venv/bin/python', '-c', script))


def wait_ha(agent_id, expected='succeeded', timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, state = api('GET', f'/api/agents/{agent_id}/oxidns-ha')
        job = state.get('publication') or {}
        if job.get('status') in {'succeeded', 'failed', 'rollback_failed', expected}:
            if job['status'] != expected:
                raise AssertionError('HA publication ended as ' + job['status'] + ': ' + job.get('message', ''))
            return state
        time.sleep(.5)
    raise AssertionError('HA publication did not finish within timeout')


def set_draft(address, *, workers=2, rule='full:shared.lab.invalid\n'):
    _, settings = api('GET', '/api/oxidns/settings')
    settings.update(config_mode='native', native_config=json.dumps(document(address, workers)),
                    additional_files=[{'path': 'rules/lab.txt', 'encoding': 'text', 'content': rule}])
    return api('POST', '/api/oxidns/settings', settings)[1]


def publish(agent_id, deployment_id=None, *, allow_error=False, restart=True):
    return api('POST', f'/api/agents/{agent_id}/push-config', {
        'base_url': SERVER_URL, 'deployment_id': deployment_id or uuid.uuid4().hex,
        'restart': restart}, allow_error=allow_error)


def matching_files():
    states = {str(cid): snapshot(cid) for cid in (130, 131)}
    assert states['130']['hashes'] == states['131']['hashes'], 'Main configuration or attached rule file differs between members'
    return states


def service_events(started):
    result = {}
    for cid in (130, 131):
        raw = guest(cid, 'journalctl', '-u', 'oxidns', '--since', '@' + str(started), '-o', 'json', '--no-pager')
        events = []
        for line in raw.splitlines():
            entry = json.loads(line)
            message = str(entry.get('MESSAGE', ''))
            if entry.get('_COMM') == 'systemd' and any(message.startswith(prefix) for prefix in ('Stopping ', 'Stopped ', 'Starting ', 'Started ', 'Failed to start ')):
                events.append({'time': int(entry['__REALTIME_TIMESTAMP']) / 1e6, 'message': message})
        result[str(cid)] = events
    return result


def assert_serial(events):
    # A core is unavailable from its Stop request until systemd confirms Start.
    stopped = set()
    for moment, cid, message in sorted((event['time'], cid, event['message']) for cid, entries in events.items() for event in entries):
        if message.startswith('Stopping '): stopped.add(cid)
        if message.startswith('Started '): stopped.discard(cid)
        assert len(stopped) < 2, f'Both OxiDNS services were stopped at {moment}'


def test():
    guard(); start_tunnel()
    credentials = json.loads((ROOT / 'private.json').read_text())
    ids = {cid: credentials['members'][str(cid)]['id'] for cid in (130, 131)}
    report = {'started_at': time.time(), 'passed': False,
              'scope': {'containers': list(GUESTS), 'network': 'isolated cfgt VLAN91', 'vip': VIP},
              'baseline': {str(cid): snapshot(cid) for cid in (130, 131)}, 'scenarios': []}
    assert report['baseline']['130']['hashes'] != report['baseline']['131']['hashes'], 'Initial fixtures must differ'
    guest(132, 'systemd-run', '--unit=cf-ha-sync-probe', '/opt/cf-retest/venv/bin/python',
          GROOT + '/source/scripts/tests/oxidns_ha_sync_lab.py', 'probe')
    def record(name, started, **details):
        events = service_events(started)
        assert_serial(events)
        item = {'name': name, 'started_at': started, 'finished_at': time.time(), 'service_events': events, **details}
        report['scenarios'].append(item); private(ROOT / 'report.json', report)
        return item
    try:
        started = time.time()
        set_draft('198.51.100.20')
        code, enabled = api('PUT', f'/api/agents/{ids[130]}/oxidns-ha', {'peer_id': ids[131], 'vip': VIP})
        assert code in (200, 202) and enabled['success']
        complete = wait_ha(ids[130])
        states = matching_files()
        answers = dns_snapshot()
        assert all(answer.get('ip') == '198.51.100.20' for answer in answers.values()), answers
        assert complete.get('config', {}).get('enabled') is True
        record('enable-synchronizes-different-initial-configurations', started, publication=complete['publication'],
               files=states, answers=answers, ha_health=complete.get('health'))

        for cid, address, workers in ((130, '198.51.100.30', 3), (131, '198.51.100.40', 4)):
            started = time.time()
            set_draft(address, workers=workers, rule=f'full:updated-from-{cid}.invalid\n')
            _, accepted = publish(ids[cid])
            complete = wait_ha(ids[cid])
            states = matching_files(); answers = dns_snapshot()
            assert all(answer.get('ip') == address for answer in answers.values()), answers
            record(f'publish-from-{cid}-updates-both-members', started, publication=complete['publication'],
                   files=states, answers=answers)

        started = time.time()
        previous = {cid: snapshot(cid) for cid in (130, 131)}
        set_draft('198.51.100.45', workers=8, rule='full:staged-candidate.invalid\n')
        publish(ids[130], restart=False)
        ready = wait_ha(ids[130], 'ready')
        assert {cid: snapshot(cid) for cid in (130, 131)} == previous, 'Staging changed live files or restarted a core'
        secondary = next(member for member in ready['publication']['members'] if member['agent_id'] == ids[131])
        api('POST', f'/api/agents/{ids[131]}/deployments/{secondary["deployment_id"]}/activate', {})
        complete = wait_ha(ids[131]); states = matching_files()
        assert dns_snapshot()[VIP].get('ip') == '198.51.100.45'
        record('stage-both-without-restart-then-activate-from-secondary', started,
               ready_publication=ready['publication'], publication=complete['publication'], files=states)

        started = time.time()
        put(131, '/run/systemd/system/oxidns.service.d/ha-sync-delay.conf', '[Service]\nExecStartPre=/bin/sleep 8\n')
        guest(131, 'systemctl', 'daemon-reload')
        try:
            set_draft('198.51.100.48', workers=9, rule='full:recover-after-backend-restart.invalid\n')
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                accepted = pool.submit(publish, ids[130], uuid.uuid4().hex, allow_error=True)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    state = guest(131, 'systemctl', 'is-active', 'oxidns', check=False).decode().strip()
                    if state == 'activating': break
                    time.sleep(.1)
                else: raise AssertionError('Standby did not enter delayed startup before backend restart')
                guest(132, 'systemctl', 'restart', 'cf-ha-sync-backend')
                try:
                    first_code = accepted.result()[0]
                except (OSError, urllib.error.URLError):
                    first_code = 'connection-closed-during-restart'
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    api('GET', '/api/agents?summary=1'); break
                except (OSError, urllib.error.URLError): time.sleep(.3)
            complete = wait_ha(ids[130]); states = matching_files()
            assert dns_snapshot()[VIP].get('ip') == '198.51.100.48'
            item = record('backend-restart-resumes-publication-without-restarting-a-member-twice', started,
                          initial_http_status=first_code, publication=complete['publication'], files=states)
            for entries in item['service_events'].values():
                assert sum(event['message'].startswith('Stopping ') for event in entries) <= 1
        finally:
            guest(131, 'rm', '-f', '/run/systemd/system/oxidns.service.d/ha-sync-delay.conf')
            guest(131, 'systemctl', 'daemon-reload')

        # A real failure of the next standby startup. Removing its sentinel in
        # ExecStartPre lets the Agent's normal automatic rollback start the old core.
        started = time.time()
        previous = {cid: snapshot(cid) for cid in (130, 131)}
        dropin = '[Service]\nRestart=no\nExecStartPre=/bin/sh -c "if test -f /opt/cf-ha-sync/fail-next; then rm /opt/cf-ha-sync/fail-next; exit 1; fi"\n'
        put(131, '/run/systemd/system/oxidns.service.d/ha-sync-failure.conf', dropin)
        put(131, GROOT + '/fail-next', 'Fail only the next test start\n')
        guest(131, 'systemctl', 'daemon-reload')
        try:
            set_draft('198.51.100.50', workers=5, rule='full:failed-candidate.invalid\n')
            publish(ids[130])
            complete = wait_ha(ids[130], 'failed')
            after = {cid: snapshot(cid) for cid in (130, 131)}
            assert after[130] == previous[130], 'Primary was touched after standby failed'
            assert after[131]['hashes'] == previous[131]['hashes'], 'Standby did not restore old files'
            assert dns_snapshot()[VIP].get('ip') == '198.51.100.48'
            record('standby-start-failure-rolls-back-and-leaves-primary-untouched', started,
                   publication=complete['publication'], before=previous, after=after)
        finally:
            guest(131, 'rm', '-f', '/run/systemd/system/oxidns.service.d/ha-sync-failure.conf', GROOT + '/fail-next')
            guest(131, 'systemctl', 'daemon-reload'); guest(131, 'systemctl', 'reset-failed', 'oxidns')

        started = time.time()
        publish(ids[131]); complete = wait_ha(ids[131]); states = matching_files()
        assert dns_snapshot()[VIP].get('ip') == '198.51.100.50'
        record('retry-after-failure-updates-both-members', started, publication=complete['publication'], files=states)

        # Slow the actual standby start to hold an observable in-flight transaction.
        # Duplicate request IDs must not cause another restart; another request
        # entering from the peer must not run concurrently.
        started = time.time()
        put(131, '/run/systemd/system/oxidns.service.d/ha-sync-delay.conf', '[Service]\nExecStartPre=/bin/sleep 4\n')
        guest(131, 'systemctl', 'daemon-reload')
        try:
            set_draft('198.51.100.60', workers=6, rule='full:concurrent-candidate.invalid\n')
            deployment_id = uuid.uuid4().hex
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                first = pool.submit(publish, ids[130], deployment_id, allow_error=True)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    _, state = api('GET', f'/api/agents/{ids[130]}/oxidns-ha')
                    if (state.get('publication') or {}).get('status') not in (None, 'succeeded', 'failed', 'rollback_failed'):
                        break
                    time.sleep(.1)
                duplicate = pool.submit(publish, ids[130], deployment_id, allow_error=True)
                competing = pool.submit(publish, ids[131], uuid.uuid4().hex, allow_error=True)
                results = [future.result() for future in (first, duplicate, competing)]
            assert results[0][0] < 300, results[0][0]
            assert results[2][0] == 409, results[2][0]
            complete = wait_ha(ids[130]); states = matching_files()
            event = record('duplicates-and-concurrent-peer-publications', started,
                           http_statuses=[item[0] for item in results], publication=complete['publication'], files=states)
            for entries in event['service_events'].values():
                assert sum(item['message'].startswith('Stopping ') for item in entries) <= 1, 'Duplicate publication restarted a member twice'
        finally:
            guest(131, 'rm', '-f', '/run/systemd/system/oxidns.service.d/ha-sync-delay.conf')
            guest(131, 'systemctl', 'daemon-reload')

        started = time.time()
        guest(130, 'systemctl', 'stop', 'oxidns')
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            _, state = api('GET', f'/api/agents/{ids[130]}/oxidns-ha')
            holders = [member['agent_id'] for member in state.get('members', []) if (member.get('runtime') or {}).get('vip_present')]
            if holders == [ids[131]]: break
            time.sleep(.5)
        else: raise AssertionError('Standby did not take over VIP after primary core stop')
        assert dns_snapshot()[VIP].get('ip') == '198.51.100.60'
        guest(130, 'systemctl', 'start', 'oxidns')
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            _, state = api('GET', f'/api/agents/{ids[130]}/oxidns-ha')
            if state.get('health') == 'ok': break
            time.sleep(.5)
        else: raise AssertionError('Pair health did not recover after primary restart')
        record('real-core-failure-and-recovery-retain-dns-service', started, health=state.get('health'), answers=dns_snapshot())
        report['passed'] = True
    except Exception as error:
        report['failure'] = str(error)
        raise
    finally:
        guest(130, 'systemctl', 'start', 'oxidns', check=False)
        guest(132, 'touch', GROOT + '/stop-probe')
        time.sleep(.5)
        raw = guest(132, 'cat', GROOT + '/dns-probe.jsonl')
        (ROOT / 'dns-probe.jsonl').write_bytes(raw)
        samples = [json.loads(line) for line in raw.splitlines()]
        report['dns_probe'] = {'sample_count': len(samples),
             'both_nodes_unavailable_samples': sum(not item['nodes'][0]['ok'] and not item['nodes'][1]['ok'] for item in samples),
             'sha256': hashlib.sha256(raw).hexdigest()}
        for item in report['scenarios']:
            subset = [sample for sample in samples if item['started_at'] <= sample['time'] <= item['finished_at']]
            item['dns_probe'] = {'samples': len(subset), 'vip_failed_samples': sum(not sample['nodes'][2]['ok'] for sample in subset),
                                'both_nodes_unavailable_samples': sum(not sample['nodes'][0]['ok'] and not sample['nodes'][1]['ok'] for sample in subset)}
        report['finished_at'] = time.time()
        private(ROOT / 'report.json', report)
        print(json.dumps({'passed': report['passed'], 'scenarios': len(report['scenarios']), 'report': str(ROOT / 'report.json'),
                          'dns_probe': report['dns_probe']}))



def edge_tests():
    """Final HTTP guard checks against an already completed laboratory run."""
    guard(); start_tunnel()
    credentials = json.loads((ROOT / 'private.json').read_text())
    ids = {cid: credentials['members'][str(cid)]['id'] for cid in (130, 131)}
    report = json.loads((ROOT / 'report.json').read_text())
    results = []
    try:
        # A disabled pair must not keep showing a finished publication as active HA.
        api('DELETE', f'/api/agents/{ids[130]}/oxidns-ha')
        for cid in (130, 131):
            _, state = api('GET', f'/api/agents/{ids[cid]}/oxidns-ha')
            assert state.get('config') is None and not state.get('publication'), state
        results.append({'name': 'disable-clears-config-and-publication-for-both-members', 'passed': True})

        # Pin an in-flight first enable before keepalived has been configured.
        put(131, '/run/systemd/system/oxidns.service.d/ha-sync-delay.conf', '[Service]\nExecStartPre=/bin/sleep 8\n')
        guest(131, 'systemctl', 'daemon-reload')
        try:
            set_draft('198.51.100.70', workers=10, rule='full:guard-check.invalid\n')
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                enabling = pool.submit(api, 'PUT', f'/api/agents/{ids[130]}/oxidns-ha', {'peer_id': ids[131], 'vip': VIP})
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    _, state = api('GET', f'/api/agents/{ids[130]}/oxidns-ha')
                    publication = state.get('publication') or {}
                    if publication.get('status') and publication['status'] not in TERMINAL:
                        assert not state.get('config'), 'Expected pre-keepalived initial enable'
                        break
                    time.sleep(.1)
                else: raise AssertionError('Initial enable was not observed in progress')
                blocked = [api('POST', f'/api/agents/{ids[cid]}/oxidns/rules/update', {}, allow_error=True)[0] for cid in (130, 131)]
                assert blocked == [409, 409], blocked
                enabling.result()
            complete = wait_ha(ids[130])
            results.append({'name': 'manual-native-rules-blocked-during-initial-enable', 'passed': True,
                            'http_statuses': blocked, 'publication': complete['publication']})
        finally:
            guest(131, 'rm', '-f', '/run/systemd/system/oxidns.service.d/ha-sync-delay.conf')
            guest(131, 'systemctl', 'daemon-reload')

        # The settings endpoint accepts either member, while the pair remains equal.
        settings_checks = []
        for cid, settings in ((131, {'library': True, 'rules': True}), (130, {'library': False, 'rules': False})):
            api('PUT', f'/api/agents/{ids[cid]}/rule-sync', settings)
            actual = {str(member): api('GET', f'/api/agents/{ids[member]}/rule-sync')[1]['settings'] for member in (130, 131)}
            assert all(value == settings for value in actual.values()), actual
            settings_checks.append({'request_member': cid, 'settings': actual})
        results.append({'name': 'rule-sync-settings-propagate-from-either-member', 'passed': True, 'checks': settings_checks})

        started = time.time()
        code, requested = api('POST', f'/api/agents/{ids[131]}/oxidns/rules/update', {})
        assert requested.get('success') is True, requested
        complete = wait_ha(ids[131])
        _, task = api('GET', f'/api/agents/{ids[131]}/oxidns/rules/update')
        assert task.get('success') is True and task['task']['status'] == 'succeeded', task
        events = service_events(started); assert_serial(events)
        results.append({'name': 'manual-rules-keep-success-contract-and-complete-for-pair', 'passed': True,
                        'http_status': code, 'task': task, 'publication': complete['publication'],
                        'files': matching_files(), 'service_events': events})

        api('DELETE', f'/api/agents/{ids[131]}/oxidns-ha')
        for cid in (130, 131):
            _, state = api('GET', f'/api/agents/{ids[cid]}/oxidns-ha')
            assert state.get('config') is None and not state.get('publication'), state
        results.append({'name': 'disable-after-rules-clears-both-members', 'passed': True})
    finally:
        paths = ('backend/agents/oxidns_ha_publish.py', 'backend/agents/oxidns_ha.py',
                 'backend/agents/oxidns_rule_update.py', 'backend/agents/rule_sync.py',
                 'backend/routes/agents.py', 'scripts/tests/oxidns_ha_sync_lab.py')
        report['final_guard_checks'] = {'source_sha256': {path: hashlib.sha256((REPO / path).read_bytes()).hexdigest() for path in paths},
                                      'checks': results, 'finished_at': time.time()}
        private(ROOT / 'report.json', report)
    print(json.dumps({'passed': True, 'final_guard_checks': len(results)}))


def cleanup(*, reset=False):
    guard()
    path = ROOT / 'tunnel.pid'
    if path.exists():
        try: os.kill(int(path.read_text()), 15)
        except ProcessLookupError: pass
        path.unlink()
    for cid in (130, 131, 132):
        pve('pct', 'stop', cid, check=False)
        pve('pct', 'rollback', cid, SNAPSHOT)
        if not reset: pve('pct', 'delsnapshot', cid, SNAPSHOT)
    if reset:
        for name in ('report.json', 'dns-probe.jsonl'):
            path = ROOT / name
            if path.exists(): path.rename(ROOT / ('previous-' + str(int(time.time())) + '-' + name))
        (ROOT / 'private.json').unlink(missing_ok=True)
        for cid in GUESTS: pve('pct', 'start', cid)
    print(json.dumps({'restored_guests': list(GUESTS), 'snapshot': SNAPSHOT, 'reset_for_new_run': reset}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare','update-source','server','probe','test','edge-tests','cleanup','reset'])
    args = parser.parse_args()
    if args.command == 'server': server()
    elif args.command == 'probe': probe()
    elif args.command == 'prepare': prepare()
    elif args.command == 'update-source':
        guard(); stage_source(); guest(132, 'systemctl', 'restart', 'cf-ha-sync-backend'); start_tunnel()
    elif args.command == 'cleanup': cleanup()
    elif args.command == 'reset': cleanup(reset=True)
    elif args.command == 'test': test()
    elif args.command == 'edge-tests': edge_tests()

if __name__ == '__main__':
    main()
