#!/usr/bin/env python3
"""Run only on .32: compare real ss/proc capture, then exercise the deployed API.

No daemon/container is started and no DNS listener or PVE guest is modified.
Raw captures stay private; export removes host fingerprints. The optional NAS
input must be supplied separately and its provenance recorded explicitly.
"""
import argparse
import base64
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request

ROOT = Path('/home/sunbroke/configflow-docker-preflight-nas-lab')
SOURCE = ROOT / 'source/docker_ha_preflight.py'
BASE = 'http://192.168.0.32:15810'
CREDS = Path('/home/sunbroke/previews/configflow-oxidns-ha-guide/credentials.json')
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def run(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, timeout=60, **kw)


def guard():
    assert sys.platform == 'linux'
    assert '192.168.0.32' in run(['ip', '-j', '-4', 'address']).stdout


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    path.chmod(0o600)


def module():
    spec = importlib.util.spec_from_file_location('preflight_under_test', SOURCE)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def baseline():
    return {'docker': run(['systemctl', 'is-active', 'docker']).stdout.strip(),
            'docker_socket': run(['systemctl', 'is-active', 'docker.socket']).stdout.strip(),
            'udp53': sorted(run(['ss', '-Hlun', 'sport = :53']).stdout.splitlines()),
            'tcp53': sorted(run(['ss', '-Hltn', 'sport = :53']).stdout.splitlines())}


def decode(raw):
    values = {}
    for line in raw.splitlines():
        if not line.startswith('CFHA1\t'): continue
        _, key, rc, encoded = line.split('\t', 3)
        values[key] = (int(rc), '' if encoded == '-' else base64.b64decode(encoded).decode())
    return values


def ports(value, proto):
    rc, text = value
    assert rc == 0, (proto, rc)
    lines = text.splitlines()
    if lines[0] == 'source=ss':
        result = []
        for line in lines[1:]:
            cols = line.split()
            if cols[0] in ('tcp', 'udp'): cols = cols[1:]
            addr = cols[3].rsplit(':', 1)[0].split('%')[0].strip('[]')
            result.append((proto, str(ipaddress.ip_address(addr))))
        return sorted(result)
    assert lines[0] == 'source=proc-net-v1'
    assert lines[1] == 'tables=' + proto + ',' + proto + '6'
    result = []
    for line in lines[2:]:
        protocol, family, address, state = line.split()
        assert protocol == proto and address.endswith(':0035')
        assert state in (('0A',) if proto == 'tcp' else ('01', '07'))
        data = bytes.fromhex(address.split(':')[0])
        # Linux /proc represents each 32-bit address word in native little-endian.
        data = b''.join(data[i:i + 4][::-1] for i in range(0, len(data), 4))
        addr = ipaddress.ip_address(data)
        assert addr.version == int(family)
        result.append((proto, str(addr)))
    return sorted(result)


def sample(name, raw, origin, tested):
    path = ROOT / 'samples' / (name + '.txt')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(raw); path.chmod(0o600)
    return {'name': name, 'origin': origin, 'sha256': hashlib.sha256(raw.encode()).hexdigest(),
            'reports': {mode: tested.parse_report(raw, mode) for mode in ('host', 'macvlan')}}


def collect():
    guard(); tested = module(); before = baseline()
    assert before['docker'] == before['docker_socket'] == 'inactive'
    script = tested.collector_script()
    env = os.environ.copy(); env.pop('DOCKER_CONTEXT', None)
    env['DOCKER_HOST'] = 'unix:///var/run/docker.sock'
    normal = run(['/usr/bin/bash', '-s'], input=script, env=env)
    assert normal.returncode == 0
    restricted = ROOT / 'without-ss'; restricted.mkdir(mode=0o700, exist_ok=True)
    for name in ('base64', 'tr', 'timeout', 'docker', 'uname', 'ip', 'systemd-detect-virt', 'grep', 'sha256sum', 'cat', 'awk'):
        executable = shutil.which(name)
        if executable and not (restricted / name).exists(): (restricted / name).symlink_to(executable)
    env['PATH'] = str(restricted)
    assert run(['/usr/bin/bash', '-c', 'command -v ss'], env=env).returncode != 0
    fallback = run(['/usr/bin/bash', '-s'], input=script, env=env)
    assert fallback.returncode == 0
    normal_values, fallback_values = decode(normal.stdout), decode(fallback.stdout)
    compared = {}
    for proto in ('udp', 'tcp'):
        left, right = ports(normal_values[proto + '53'], proto), ports(fallback_values[proto + '53'], proto)
        assert left == right and left, (left, right)
        compared[proto] = {'same_bound_addresses': True, 'ss_count': len(left), 'proc_count': len(right)}
    after = baseline(); assert after == before
    report = {'module_sha256': hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
              'collector_script_sha256': hashlib.sha256(script.encode()).hexdigest(),
              'baseline': before, 'after_capture': after, 'comparison': compared,
              'daemon_started': False, 'pve_modified': False, 'actual_nas_contacted': False,
              'samples': [sample('real-ss', normal.stdout, 'Real Arch Linux .32, normal ss, default Docker inactive', tested),
                          sample('real-proc', fallback.stdout, 'Same real Linux /proc tables; PATH fixture removes ss only', tested)]}
    write(ROOT / 'report.json', report)
    print(json.dumps(compared))


def add_nas(path, origin, name):
    guard(); tested = module(); report = json.loads((ROOT / 'report.json').read_text())
    raw = Path(path).read_text()
    assert name in ('nas-redacted', 'nas-markdown')
    value = sample(name, raw, origin, tested)
    checks = lambda mode: {item['id']: item['status'] for item in value['reports'][mode]['checks']}
    assert checks('host')['compose'] == 'warning'
    assert checks('host')['udp53'] == checks('host')['tcp53'] == 'unknown'
    assert checks('macvlan')['udp53'] == checks('macvlan')['tcp53'] == 'pass'
    assert value['reports']['host']['status'] == 'unknown'
    assert value['reports']['macvlan']['status'] == 'warning'
    report['samples'] = [v for v in report['samples'] if v['name'] != value['name']] + [value]
    write(ROOT / 'report.json', report)
    print(json.dumps({'sample': value['name'], 'host': 'unknown', 'macvlan': 'warning', 'source': origin}))


def request(path, data, token=None):
    headers = {'Content-Type': 'application/json'}
    if token: headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(BASE + path, json.dumps(data).encode(), headers)
    with HTTP.open(req, timeout=30) as response: return response.status, json.load(response)


def api():
    guard(); tested = module(); report = json.loads((ROOT / 'report.json').read_text())
    _, login = request('/api/auth/login', json.loads(CREDS.read_text()))
    token = login['token']
    _, inspect = request('/api/agents/docker-oxidns-ha-guide', {'action': 'inspect'}, token)
    assert inspect['inspection_command'] == tested.collector_script()
    results = []
    for item in report['samples']:
        raw = (ROOT / 'samples' / (item['name'] + '.txt')).read_text()
        for mode in ('host', 'macvlan'):
            status, response = request('/api/agents/docker-oxidns-ha-guide', {'action': 'check', 'output': raw, 'network_mode': mode}, token)
            actual = response['preflight_report']; expected = tested.parse_report(raw, mode)
            assert actual == expected
            results.append({'sample': item['name'], 'mode': mode, 'http': status, 'status': actual['status'], 'checks': len(actual['checks'])})
    report['api'] = {'authenticated_real_api': True, 'mocked_responses': False, 'cases': results}
    write(ROOT / 'report.json', report)
    print(json.dumps({'api_cases': len(results)}))


def export():
    guard(); report = json.loads((ROOT / 'report.json').read_text())
    browser = ROOT / 'browser/report.json'
    if browser.exists(): report['browser'] = json.loads(browser.read_text())
    def clean(value):
        if isinstance(value, dict): return {k: clean(v) for k, v in value.items() if k != 'host_fingerprint'}
        if isinstance(value, list): return [clean(v) for v in value]
        return value
    report = clean(report); report.update(raw_samples_exported=False, fingerprints_exported=False)
    write(ROOT / 'public-report.json', report)


def cleanup():
    guard(); report = json.loads((ROOT / 'report.json').read_text())
    after = baseline(); assert after == report['baseline']
    urls = {}
    for port in (15800, 15810):
        with HTTP.open(f'http://192.168.0.32:{port}/', timeout=10) as response: urls[str(port)] = response.status
    assert CREDS.is_file()
    result = {'default_docker_and_dns_unchanged': True, 'after': after, 'previews_http': urls,
              'pve_modified': False, 'daemon_started': False, 'preview_credentials_preserved': True}
    assert str(ROOT) == '/home/sunbroke/configflow-docker-preflight-nas-lab'
    shutil.rmtree(ROOT); assert not ROOT.exists()
    result['private_lab_and_raw_samples_removed'] = True
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['collect', 'nas', 'api', 'export', 'cleanup'])
    parser.add_argument('--sample'); parser.add_argument('--origin'); parser.add_argument('--name', default='nas-redacted')
    args = parser.parse_args()
    if args.command == 'nas':
        assert args.sample and args.origin
        add_nas(args.sample, args.origin, args.name)
    else: globals()[args.command]()
