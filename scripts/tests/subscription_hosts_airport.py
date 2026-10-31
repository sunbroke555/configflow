#!/usr/bin/env python3
"""Subscription hosts against a real airport configuration, on the test server only.

Mihomo runs in the network namespace ``cfair`` (veth 10.251.0.1/30 <-> .2). The
host's own nft table ``inet cfair`` NATs it and rejects, with counters: private
and fake-IP destinations (198.18.0.0/15, also blackholed inside the namespace),
and every TCP/UDP 53 and 853 (plain DNS on this LAN is hijacked by the router to
a production resolver). Nothing else on the host changes except two
``DOCKER-USER`` ACCEPT rules commented ``cfair`` (Docker's FORWARD policy is DROP).

The airport file, private ConfigFlow data (subscription caches) and Mihomo
directories (provider caches) hold node passwords: they live only under
~/cf-airport-test/private (mode 700) and ``cleanup`` removes them. Evidence
under ~/cf-airport-test/evidence never contains a password. This file holds no
airport names, addresses or secrets; everything is read from the copied file.

Run with the shared venv from an rt checkout::

  rt -- ~/cf-test/.venv/bin/python scripts/tests/subscription_hosts_airport.py <command>

  net-up | net-down          namespace, veth, nft table, DOCKER-USER rules
  binaries                   unpack v1.19.19 / v1.19.20 / v1.19.33 into ~/cf-airport-test/bin
  versions                   -t and behaviour of proxy-server-nameserver-policy per version (loopback + DoH)
  ref                        current addresses of node, mapping-target and R4 domains (AliDNS DoH JSON)
  r1                         the airport file itself in Mihomo (fake-ip range and DNS bootstrap adjusted)
  tree main [REF]            unpack ``git archive`` (copied to ~/cf-airport-test/src/main.tar) for the main tree
  serve TREE VARIANT [--r4] [--hosts orig|none|ip]
                             private ConfigFlow from TREE (main | branch | <dir under src>) with VARIANT
                             (default | user-psn | no-psn | user-policy | redir-host), subscriptions fetched
  gen LABEL                  preview of the served ConfigFlow (content + report, if any)
  run LABEL [all|N,N] [--bin VERSION] [--from LABEL]
                             start Mihomo on LABEL's (or --from's) generated config, measure one node per server, stop
  check LABEL [VERSION]      mihomo -t on LABEL's generated config
  direct TREE LABEL          generate from the served DATA_DIR with TREE's code, without the server
  compare TREE_A TREE_B LABEL  ``direct`` for both trees, byte for byte
  stop                       stop the private ConfigFlow and subscription server
  counters                   nft counters and veth bytes
  cleanup                    stop everything, delete private copies, verify nothing with a password remains
"""
import copy
import getpass
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
BASE = Path.home() / 'cf-airport-test'
PRIVATE, EVIDENCE, STATE, BIN, SRC = (BASE / name for name in ('private', 'evidence', 'state', 'bin', 'src'))
AIRPORT = PRIVATE / 'airport-config.yaml'
USER = getpass.getuser()
NS, VETH_HOST, VETH_NS = 'cfair', 'cfair0', 'cfair1'
HOST_IP, NS_IP = '10.251.0.1', '10.251.0.2'
API, SUB = 15961, 15963
MIXED, CONTROLLER, LISTEN_PORT = 7893, 9090, 7874
PYTHON = Path.home() / 'cf-test/.venv/bin/python'
VERSIONS = ('v1.19.19', 'v1.19.20', 'v1.19.33')
MIHOMO = BIN / 'mihomo-v1.19.33'
GROUP = '代理'
R4_DOMAIN = 'one.one.one.one'
URLS = ('https://www.gstatic.com/generate_204', 'https://www.cloudflare.com/cdn-cgi/trace')
DOH = 'https://223.5.5.5/dns-query'

NFT = f'''table inet cfair {{
  counter fakeip {{}}
  counter dns {{}}
  counter private {{}}
  counter v6 {{}}
  counter egress {{}}
  counter ingress {{}}
  chain forward {{
    type filter hook forward priority -10; policy accept;
    iifname "{VETH_HOST}" ip daddr 198.18.0.0/15 counter name fakeip reject
    iifname "{VETH_HOST}" meta l4proto {{ tcp, udp }} th dport {{ 53, 853 }} counter name dns reject
    iifname "{VETH_HOST}" ip daddr {{ 0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12, 192.168.0.0/16, 224.0.0.0/3 }} counter name private reject
    iifname "{VETH_HOST}" meta nfproto ipv6 counter name v6 drop
    iifname "{VETH_HOST}" counter name egress accept
    oifname "{VETH_HOST}" counter name ingress accept
  }}
  chain postrouting {{
    type nat hook postrouting priority 100; policy accept;
    ip saddr 10.251.0.0/30 oifname != "{VETH_HOST}" masquerade
  }}
}}
'''

# ConfigFlow base configurations. Plain DNS never leaves the namespace: every
# resolver is DoH, the bootstrap one by IP.
BASE_CUSTOM = f'''mixed-port: {MIXED}
allow-lan: false
mode: rule
log-level: debug
ipv6: false
unified-delay: true
external-controller: 127.0.0.1:{CONTROLLER}
dns:
  enable: true
  ipv6: false
  listen: 127.0.0.1:{LISTEN_PORT}
  use-hosts: true
  enhanced-mode: fake-ip
  fake-ip-range: 198.19.0.0/16
  fake-ip-filter-mode: blacklist
  fake-ip-filter:
    - '*.lan'
    - '*.local'
    - '*.arpa'
    - 'time.*.com'
    - 'ntp.*.com'
    - '+.ntp.ntsc.ac.cn'
    - '+.cn.ntp.org.cn'
    - '+.market.xiaomi.com'
    - 'localhost.ptlogin2.qq.com'
    - '*.msftncsi.com'
    - 'www.msftconnecttest.com'
  default-nameserver:
    - {DOH}
  nameserver:
    - {DOH}
    - https://1.12.12.12/dns-query
  proxy-server-nameserver:
    - https://doh.pub/dns-query
    - https://dns.alidns.com/dns-query
'''


# ------------------------------------------------------------------ helpers

def run(*args, check=True, input=None, timeout=120):
    return subprocess.run([str(a) for a in args], check=check, input=input, capture_output=True, timeout=timeout)


def sudo(*args, **kwargs):
    return run('sudo', '-n', *args, **kwargs)


def ns(*args, **kwargs):
    """A command inside the namespace, as the unprivileged user."""
    return sudo('ip', 'netns', 'exec', NS, 'runuser', '-u', USER, '--', *args, **kwargs)


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


_secrets = None


def secret_values():
    """Node passwords of the copied airport file (kept in memory only)."""
    global _secrets
    if _secrets is None:
        values = set()
        if AIRPORT.exists():
            for proxy in (yaml.safe_load(AIRPORT.read_text()) or {}).get('proxies') or []:
                for key in ('password', 'uuid', 'auth-str', 'psk', 'private-key', 'obfs-password', 'auth'):
                    if isinstance(proxy.get(key), str) and len(proxy[key]) >= 4:
                        values.add(proxy[key])
        r4 = STATE / 'r4-password'
        if r4.exists():
            values.add(r4.read_text().strip())
        _secrets = values
    return _secrets


def mask(text):
    for value in sorted(secret_values(), key=len, reverse=True):
        text = text.replace(value, '***')
    return text


def mask_document(value):
    if isinstance(value, dict):
        return {k: ('***' if k in ('password', 'uuid', 'psk', 'private-key', 'auth-str', 'obfs-password') else mask_document(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [mask_document(item) for item in value]
    return mask(value) if isinstance(value, str) else value


def save(name, value):
    private_dir(EVIDENCE)
    path = EVIDENCE / name
    path.parent.mkdir(parents=True, exist_ok=True)
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=1) + '\n'
    path.write_text(mask(text))
    path.chmod(0o600)
    return path


def state(name, value=None):
    private_dir(STATE)
    path = STATE / f'{name}.json'
    if value is None:
        return json.loads(path.read_text()) if path.exists() else None
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1))
    path.chmod(0o600)
    return value


def airport():
    assert AIRPORT.exists(), f'copy the airport file to {AIRPORT} first (mode 600)'
    return yaml.safe_load(AIRPORT.read_text())


def node_servers(document):
    """One node per distinct server, in file order: {server: node name}."""
    picked = {}
    for proxy in document.get('proxies') or []:
        picked.setdefault(str(proxy['server']).lower(), proxy['name'])
    return picked


def domain_of(value):
    return str(value).strip().lower().rstrip('.')


# ---------------------------------------------------------------- namespace

def net_up():
    if run('ip', 'netns', 'list').stdout.decode().split().count(NS):
        print(json.dumps({'net': 'already up'}))
        return
    sudo('ip', 'netns', 'add', NS)
    sudo('ip', 'link', 'add', VETH_HOST, 'type', 'veth', 'peer', 'name', VETH_NS, 'netns', NS)
    sudo('ip', 'addr', 'add', f'{HOST_IP}/30', 'dev', VETH_HOST)
    sudo('ip', 'link', 'set', VETH_HOST, 'up')
    for args in (('addr', 'add', f'{NS_IP}/30', 'dev', VETH_NS), ('link', 'set', VETH_NS, 'up'), ('link', 'set', 'lo', 'up'),
                 ('route', 'add', 'default', 'via', HOST_IP), ('route', 'add', 'blackhole', '198.18.0.0/15')):
        sudo('ip', '-n', NS, *args)
    for key in ('all', 'default', 'lo', VETH_NS):
        sudo('ip', 'netns', 'exec', NS, 'sysctl', '-qw', f'net.ipv6.conf.{key}.disable_ipv6=1', check=False)
    sudo('nft', '-f', '-', input=NFT.encode())
    for direction in ('-i', '-o'):
        sudo('iptables', '-I', 'DOCKER-USER', '1', direction, VETH_HOST, '-m', 'comment', '--comment', 'cfair', '-j', 'ACCEPT')
    print(json.dumps({'net': 'up', 'ns_routes': sudo('ip', '-n', NS, 'route').stdout.decode().splitlines()}))


def net_down():
    stop_mihomo()
    for direction in ('-i', '-o'):
        while sudo('iptables', '-D', 'DOCKER-USER', direction, VETH_HOST, '-m', 'comment', '--comment', 'cfair',
                   '-j', 'ACCEPT', check=False).returncode == 0:
            pass
    sudo('nft', 'delete', 'table', 'inet', 'cfair', check=False)
    sudo('ip', 'link', 'del', VETH_HOST, check=False)
    sudo('ip', 'netns', 'del', NS, check=False)
    left = {'netns': NS in run('ip', 'netns', 'list').stdout.decode().split(),
            'veth': run('ip', 'link', 'show', VETH_HOST, check=False).returncode == 0,
            'nft': sudo('nft', 'list', 'table', 'inet', 'cfair', check=False).returncode == 0,
            'docker_user': 'cfair' in sudo('iptables', '-S', 'DOCKER-USER').stdout.decode()}
    print(json.dumps({'net': 'down', 'left': left}))
    assert not any(left.values()), left


def counters():
    names = ('fakeip', 'dns', 'private', 'v6', 'egress', 'ingress')
    result = {}
    output = sudo('nft', '-j', 'list', 'counters', 'table', 'inet', 'cfair', check=False).stdout
    for item in (json.loads(output or b'{}').get('nftables') or []):
        counter = item.get('counter')
        if counter and counter.get('name') in names:
            result[counter['name']] = {'packets': counter['packets'], 'bytes': counter['bytes']}
    link = json.loads(run('ip', '-s', '-j', 'link', 'show', VETH_HOST, check=False).stdout or b'[{}]')[0]
    stats = link.get('stats64') or link.get('stats') or {}
    result['veth'] = {'rx_bytes': (stats.get('rx') or {}).get('bytes'), 'tx_bytes': (stats.get('tx') or {}).get('bytes')}
    return result


def counters_cmd():
    print(json.dumps(counters(), indent=1))


# ----------------------------------------------------------------- Mihomo

def ns_pids():
    return [int(pid) for pid in sudo('ip', 'netns', 'pids', NS, check=False).stdout.decode().split()]


def stop_mihomo():
    if NS not in run('ip', 'netns', 'list').stdout.decode().split():
        return
    for pid in ns_pids():
        try:
            comm = Path(f'/proc/{pid}/comm').read_text().strip()
        except OSError:
            continue
        if comm.startswith('mihomo'):
            sudo('kill', str(pid), check=False)
    for _ in range(40):
        if not any(Path(f'/proc/{pid}/comm').exists() and Path(f'/proc/{pid}/comm').read_text().startswith('mihomo')
                   for pid in ns_pids()):
            break
        time.sleep(0.25)


def controller(method, path, body=None, *, port=CONTROLLER):
    args = ['curl', '-sS', '--max-time', '6', '-X', method, f'http://127.0.0.1:{port}{path}']
    if body is not None:
        args += ['-H', 'Content-Type: application/json', '--data', json.dumps(body)]
    result = ns(*args, check=False, timeout=20)
    text = result.stdout.decode()
    try:
        return json.loads(text) if text.strip() else None
    except ValueError:
        return {'raw': text[:300]}


def start_mihomo(directory, binary=MIHOMO, *, port=CONTROLLER):
    stop_mihomo()
    log = open(directory / 'mihomo.log', 'ab')
    process = subprocess.Popen(['sudo', '-n', 'ip', 'netns', 'exec', NS, 'runuser', '-u', USER, '--', str(binary), '-d',
                                str(directory)], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(80):
        version = controller('GET', '/version', port=port)
        if isinstance(version, dict) and version.get('version'):
            return process, version
        if process.poll() is not None:
            raise SystemExit('mihomo exited: ' + mask((directory / 'mihomo.log').read_text()[-2000:]))
        time.sleep(0.25)
    raise SystemExit('mihomo controller did not come up')


OBSERVE = r'''import json, sys, time, urllib.request
port, seconds, node = sys.argv[1], float(sys.argv[2]), sys.argv[3]
seen = {}; end = time.monotonic() + seconds
while time.monotonic() < end:
    try:
        for c in json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}/connections', timeout=1)).get('connections') or []:
            if node in (c.get('chains') or []):
                seen[c['id']] = c
    except Exception:
        pass
    time.sleep(0.1)
print(json.dumps(list(seen.values()), ensure_ascii=False))
'''


def mihomo_log_lines(directory, needles):
    lines = (directory / 'mihomo.log').read_text(errors='replace').splitlines()
    return [mask(line) for line in lines if any(needle in line for needle in needles)]


def measure(directory, label, group, servers, hosts):
    """One node per server: request, then DNS log lines, /connections and SYN destinations."""
    results = {}
    before = counters()
    for server, node in servers.items():
        selected = controller('PUT', '/proxies/' + urllib.parse.quote(group, safe=''), {'name': node})
        now = (controller('GET', '/proxies/' + urllib.parse.quote(group, safe='')) or {}).get('now')
        controller('DELETE', '/connections')
        port = None
        holder, capture = {}, {}

        def observe():
            holder['connections'] = json.loads(ns('python3', '-c', OBSERVE, CONTROLLER, '9', node, timeout=40).stdout or b'[]')

        def dump():
            capture['syn'] = sudo('timeout', '10', 'tcpdump', '-nn', '-tt', '-l', '-i', VETH_HOST,
                                  'tcp[tcpflags] & (tcp-syn|tcp-ack) == tcp-syn', check=False,
                                  timeout=40).stdout.decode().splitlines()
            capture['syn'] = [line for line in capture['syn'] if line.strip()]

        threads = [threading.Thread(target=observe), threading.Thread(target=dump)]
        for thread in threads:
            thread.start()
        time.sleep(1.5)
        requests = []
        for url in URLS:
            result = ns('curl', '-sS', '--max-time', '15', '-x', f'http://127.0.0.1:{MIXED}', '-o', '-', '-w',
                        '\n%{http_code} %{size_download} %{time_total}', url, check=False, timeout=40)
            body, _, meta = result.stdout.decode(errors='replace').rpartition('\n')
            entry = {'url': url, 'ok': result.returncode == 0, 'meta': meta.strip(), 'error': result.stderr.decode()[:200]}
            if url.endswith('/trace'):
                # Keep the location fields only; the exit address is the airport's.
                entry['trace'] = {k: v for k, v in (line.split('=', 1) for line in body.splitlines() if '=' in line)
                                  if k in ('colo', 'loc', 'http', 'tls', 'warp')}
            requests.append(entry)
        for thread in threads:
            thread.join()
        connections = holder.get('connections') or []
        remote = sorted({c['metadata'].get('remoteDestination') for c in connections if c.get('metadata')} - {None, ''})
        target = hosts.get(server)
        results[node] = {
            'server': server, 'mapping_target': target, 'selected': now == node, 'requests': requests,
            'connections': [{'chains': c.get('chains'), 'host': c['metadata'].get('host'),
                             'remoteDestination': c['metadata'].get('remoteDestination'), 'rule': c.get('rule')}
                            for c in connections],
            'remote_destinations': remote,
            'syn': capture.get('syn', []),
        }
        time.sleep(0.5)
    after = counters()
    needles = list(servers) + [hosts[s] for s in servers if hosts.get(s)] + ['198.18.', '198.19.']
    log = mihomo_log_lines(directory, needles)
    for node, entry in results.items():
        names = [entry['server']] + ([entry['mapping_target']] if entry['mapping_target'] else [])
        entry['dns_log'] = [line for line in log if any(name in line for name in names) and '[DNS]' in line][:12]
        entry['dial_log'] = [line for line in log if entry['server'] in line and '[DNS]' not in line][:6]
        ports = {tuple(line.split(' > ')[1].split(':')[0].rsplit('.', 1)) for line in entry['syn'] if ' > ' in line}
        entry['syn_destinations'] = sorted({f'{host}:{port}' for host, port in ports})
    fake = [line for line in log if re.search(r'\b198\.1[89]\.\d+\.\d+', line)]
    traffic = {key: {'packets': after[key]['packets'] - before.get(key, {}).get('packets', 0),
                     'bytes': after[key]['bytes'] - before.get(key, {}).get('bytes', 0)}
               for key in after if key != 'veth' and key in before}
    traffic['veth'] = {k: (after['veth'][k] or 0) - (before['veth'][k] or 0) for k in after['veth']}
    return {'nodes': results, 'fake_ip_log_lines': fake[:20], 'traffic': traffic}


def wait_group(group, names, seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        info = controller('GET', '/proxies/' + urllib.parse.quote(group, safe='')) or {}
        if set(names) <= set(info.get('all') or []):
            return info
        time.sleep(1)
    raise SystemExit(f'group {group} did not get {names}')


def run_config(label, document, group, servers, hosts, binary=MIHOMO):
    directory = private_dir(PRIVATE / f'run-{label}')
    for child in directory.iterdir():
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    (directory / 'config.yaml').write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False))
    (directory / 'config.yaml').chmod(0o600)
    save(f'{label}/config.yaml', yaml.safe_dump(mask_document(document), allow_unicode=True, sort_keys=False))
    fakeip = {}

    def watch():
        fakeip['packets'] = sudo('timeout', str(len(servers) * 13 + 30), 'tcpdump', '-nn', '-l', '-i', VETH_HOST,
                                 'net 198.18.0.0/15', check=False, timeout=400).stdout.decode().split('\n')
        fakeip['packets'] = [line for line in fakeip['packets'] if line.strip()]

    watcher = threading.Thread(target=watch)
    watcher.start()
    try:
        _process, version = start_mihomo(directory, binary)
        wait_group(group, list(servers.values()))
        result = measure(directory, label, group, servers, hosts)
        result['mihomo'] = version
        result['config_dns'] = mask_document(document.get('dns'))
        result['config_hosts'] = document.get('hosts')
    finally:
        stop_mihomo()
        watcher.join()
    result['tcpdump_198_18_0_0_15_on_veth'] = fakeip.get('packets', [])
    result['counters_after'] = counters()
    save(f'{label}/mihomo.log', (directory / 'mihomo.log').read_text(errors='replace'))
    save(f'{label}/result.json', result)
    print(json.dumps(summary(result), ensure_ascii=False, indent=1))
    return result


def summary(result):
    return {'mihomo': (result.get('mihomo') or {}).get('version'),
            'nodes': {node: {'server': entry['server'], 'target': entry['mapping_target'],
                             'requests': [f"{r['meta']}{' ' + json.dumps(r.get('trace')) if r.get('trace') else ''}"
                                          for r in entry['requests']],
                             'remote': entry['remote_destinations'], 'syn': entry['syn_destinations'],
                             'dns': entry['dns_log'][:4]}
                      for node, entry in result['nodes'].items()},
            'fake_ip_log_lines': result['fake_ip_log_lines'][:4],
            'tcpdump_198_18': len(result.get('tcpdump_198_18_0_0_15_on_veth', [])),
            'traffic': result['traffic']}


# ------------------------------------------------------------------ R1 / ref

def ref():
    document = airport()
    hosts = {domain_of(k): domain_of(v) for k, v in (document.get('hosts') or {}).items() if isinstance(v, str)}
    names = sorted(set(node_servers(document)) | set(hosts.values()) | {R4_DOMAIN})
    answers = {}
    for name in names:
        url = 'https://223.5.5.5/resolve?' + urllib.parse.urlencode({'name': name, 'type': 'A'})
        body = json.loads(run('curl', '-sS', '--max-time', '10', url).stdout)
        answers[name] = sorted(item['data'] for item in body.get('Answer') or [] if item.get('type') == 1)
    save('ref.json', {'at': time.strftime('%Y-%m-%d %H:%M:%S'), 'source': 'https://223.5.5.5/resolve', 'answers': answers})
    print(json.dumps(answers, indent=1))


def r1():
    document = copy.deepcopy(airport())
    deviations = []
    dns = document['dns']
    deviations.append(f"dns.fake-ip-range: {dns.get('fake-ip-range')} -> 198.19.0.0/16")
    dns['fake-ip-range'] = '198.19.0.0/16'
    deviations.append(f"dns.default-nameserver: {dns.get('default-nameserver', '(absent, Mihomo default: plain DNS)')} -> [{DOH}]")
    dns['default-nameserver'] = [DOH]
    deviations.append(f"log-level: {document.get('log-level')} -> debug")
    document['log-level'] = 'debug'
    for key, value in (('mixed-port', MIXED), ('external-controller', f'127.0.0.1:{CONTROLLER}')):
        if document.get(key) != value:
            deviations.append(f'{key}: {document.get(key)} -> {value}')
            document[key] = value
    save('r1/deviations.json', deviations)
    hosts = {domain_of(k): domain_of(v) for k, v in (document.get('hosts') or {}).items() if isinstance(v, str)}
    group = next(g['name'] for g in document['proxy-groups'] if g.get('type') == 'select')
    result = run_config('r1', document, group, node_servers(document), hosts)
    result['deviations'] = deviations
    save('r1/result.json', result)


# ------------------------------------------------------------ versions

VERSION_CONFIG = '''mixed-port: 17893
external-controller: 127.0.0.1:19190
log-level: debug
ipv6: false
mode: rule
hosts:
  a.test: 127-0-0-2.sslip.io
proxies:
  - {name: A, type: socks5, server: a.test, port: 1080}
proxy-groups:
  - {name: G, type: select, proxies: [A]}
rules:
  - MATCH,G
dns:
  enable: true
  ipv6: false
  listen: 127.0.0.1:15358
  use-hosts: true
  enhanced-mode: fake-ip
  fake-ip-range: 198.19.0.0/16
  fake-ip-filter: ['+.sslip.io']
  default-nameserver: [https://223.5.5.5/dns-query]
  nameserver: [https://223.5.5.5/dns-query]
  proxy-server-nameserver: [https://223.5.5.5/dns-query]
  proxy-server-nameserver-policy:
    'a.test': 'udp://127.0.0.1:15358'
'''


def binaries():
    private_dir(BIN)
    for version in VERSIONS:
        target = BIN / f'mihomo-{version}'
        if target.exists():
            continue
        archive = BIN / f'mihomo-linux-amd64-{version}.gz'
        if not archive.exists():
            local = Path.home() / f'dl/mihomo/mihomo-linux-amd64-{version}.gz'
            if local.exists():
                shutil.copy(local, archive)
            else:
                run('curl', '-sSLf', '--max-time', '120', '-o', archive,
                    f'https://github.com/MetaCubeX/mihomo/releases/download/{version}/mihomo-linux-amd64-{version}.gz',
                    timeout=180)
        import gzip
        target.write_bytes(gzip.decompress(archive.read_bytes()))
        target.chmod(0o755)
    print(json.dumps({version: run(BIN / f'mihomo-{version}', '-v').stdout.decode().strip()
                      for version in VERSIONS}, indent=1))


def versions():
    directory = private_dir(BASE / 'versions')
    full = yaml.safe_load(VERSION_CONFIG)
    no_psn = copy.deepcopy(full)
    del no_psn['dns']['proxy-server-nameserver']
    files = {'policy-with-psn': full, 'policy-without-psn': no_psn}
    for name, document in files.items():
        (directory / f'{name}.yaml').write_text(yaml.safe_dump(document, sort_keys=False))
    result = {'configs': files}
    for version in VERSIONS:
        binary = BIN / f'mihomo-{version}'
        entry = {'v': run(binary, '-v').stdout.decode().strip()}
        for name in files:
            check = run(binary, '-t', '-d', directory, '-f', directory / f'{name}.yaml', check=False, timeout=60)
            entry[f'-t {name}'] = {'exit': check.returncode,
                                   'output': [line for line in check.stdout.decode().splitlines()
                                              if 'proxy-server' in line or 'test is successful' in line
                                              or 'failed' in line.lower() or 'error' in line.lower()][-4:]}
        # Behaviour: dial node A (server a.test) once; the target needs no answer.
        run_dir = private_dir(directory / f'run-{version}')
        for child in run_dir.iterdir():
            shutil.rmtree(child) if child.is_dir() else child.unlink()
        (run_dir / 'config.yaml').write_text(VERSION_CONFIG)
        try:
            start_mihomo(run_dir, binary, port=19190)
            curl = ns('curl', '-sS', '--max-time', '8', '-x', 'http://127.0.0.1:17893', 'http://example.invalid/',
                      check=False, timeout=30)
            time.sleep(1)
        finally:
            stop_mihomo()
        lines = (run_dir / 'mihomo.log').read_text(errors='replace').splitlines()
        entry['behaviour'] = {'curl_error': curl.stderr.decode().strip()[:160],
                              'log': [line for line in lines if 'a.test' in line or 'sslip.io' in line
                                      or '127.0.0.2' in line][:10]}
        result[version] = entry
    save('versions.json', result)
    print(json.dumps({v: {k: val for k, val in result[v].items()} for v in VERSIONS}, ensure_ascii=False, indent=1))


# --------------------------------------------------------- private ConfigFlow

SEED = r'''
import json, sys
from pathlib import Path
data, sub_url, server_domain, custom, token, r4 = (Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5],
                                                   sys.argv[6] == '1')
from backend.common import config as store
from backend.common.config_repository import ProfileRepository
store.set_repository(ProfileRepository(data, default_config_factory=store.get_default_config,
                                       initial_config_factory=store.get_default_config))
repo = store.get_repository()
subs = [{'id': 'air', 'name': '真实机场', 'url': sub_url + 'airport.yaml', 'enabled': True, 'interval': 86400}]
if r4:
    subs.append({'id': 'r4', 'name': 'R4 无 hosts', 'url': sub_url + 'r4.yaml', 'enabled': True, 'interval': 86400})
def shared(s):
    s['subscriptions'] = subs
    s['subscription_aggregations'] = []
def profile(p):
    p['proxy_groups'] = [{'id': 'air-group', 'name': '代理', 'type': 'select',
                          'subscriptions': [s['id'] for s in subs], 'enabled': True}]
    # Mihomo fetches providers through its own rules: keep the panel off the airport.
    p['rule_configs'] = [{'id': 'panel', 'itemType': 'rule', 'rule_type': 'IP-CIDR', 'value': '10.251.0.1/32',
                          'policy': 'DIRECT', 'no_resolve': True, 'enabled': True},
                         {'id': 'match', 'itemType': 'rule', 'rule_type': 'MATCH', 'policy': '代理', 'enabled': True}]
    p.setdefault('mihomo', {}).update(custom_config=custom, provider_update_interval=86400)
repo.update_system_transaction(lambda c: c['system_config'].update(server_domain=server_domain, config_token=token))
repo.update_shared_transaction(shared)
repo.update_profile_transaction(repo.default_profile_id(), profile)
print(json.dumps({'profile': repo.default_profile_id(), 'subscriptions': [s['id'] for s in subs]}))
'''

SERVE = r'''
import os, sys
sys.path.insert(0, os.getcwd())
from pathlib import Path
from backend.common import config as store
from backend.common.config_repository import ProfileRepository
store.set_repository(ProfileRepository(Path(os.environ['DATA_DIR']), default_config_factory=store.get_default_config,
                                       initial_config_factory=store.get_default_config))
from backend.app import app
from backend.http_server import run_http_server
run_http_server(app)
'''

DIRECT = r'''
import hashlib, json, sys
from pathlib import Path
from backend.common import config as store
from backend.common.config_repository import ProfileRepository
store.set_repository(ProfileRepository(Path(sys.argv[1]), default_config_factory=store.get_default_config,
                                       initial_config_factory=store.get_default_config))
from backend.converters.mihomo import generate_mihomo_config
text = generate_mihomo_config(store.get_config(), base_url=sys.argv[2], preflight_providers=False)
Path(sys.argv[3]).write_text(text)
print(json.dumps({'sha256': hashlib.sha256(text.encode()).hexdigest(), 'bytes': len(text.encode())}))
'''


def credentials():
    value = state('credentials')
    if value is None:
        value = state('credentials', {'username': 'admin', 'password': secrets.token_urlsafe(18),
                                      'jwt_secret': secrets.token_hex(32), 'registration_key': secrets.token_urlsafe(24),
                                      'config_token': secrets.token_hex(16)})
    return value


def environment(data):
    c = credentials()
    env = {key: value for key, value in os.environ.items() if key.lower() not in ('http_proxy', 'https_proxy', 'all_proxy')}
    env.update(DATA_DIR=str(data), LOG_LEVEL='INFO', ADMIN_USERNAME=c['username'], ADMIN_PASSWORD=c['password'],
               JWT_SECRET_KEY=c['jwt_secret'], AGENT_REGISTRATION_KEY=c['registration_key'],
               SUB_STORE_BUILTIN_DIR=str(PRIVATE / 'no-sub-store'), SUB_STORE_URL='http://127.0.0.1:9',
               CONFIGFLOW_BACKEND_HOST=HOST_IP, CONFIGFLOW_BACKEND_PORT=str(API))
    return env


def tree_path(tree):
    if tree == 'branch':
        return ROOT
    path = SRC / tree
    assert (path / 'backend').is_dir(), f'unpack {tree} first (tree {tree})'
    return path


def tree(name, ref='upstream/main'):
    archive = SRC / f'{name}.tar'
    assert archive.exists(), f'copy `git archive {ref}` to {archive} first'
    target = SRC / name
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    with tarfile.open(archive) as tar:
        tar.extractall(target, filter='data')
    print(json.dumps({'tree': name, 'ref': ref, 'path': str(target), 'tar_sha256': hashlib.sha256(archive.read_bytes()).hexdigest()[:16]}))


def variant_custom(variant):
    document = yaml.safe_load(BASE_CUSTOM)
    dns = document['dns']
    if variant == 'user-psn':
        dns['proxy-server-nameserver'] = ['https://223.6.6.6/dns-query']
    elif variant == 'no-psn':
        del dns['proxy-server-nameserver']
    elif variant == 'user-policy':
        first = sorted(domain_of(k) for k, v in (airport().get('hosts') or {}).items() if isinstance(v, str))[0]
        dns['proxy-server-nameserver-policy'] = {first: 'https://223.6.6.6/dns-query'}
    elif variant == 'redir-host':
        dns['enhanced-mode'] = 'redir-host'
        for key in ('fake-ip-range', 'fake-ip-filter-mode', 'fake-ip-filter'):
            dns.pop(key)
    elif variant != 'default':
        raise SystemExit(f'unknown variant {variant}')
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False)


def subscription_bodies(hosts_mode, r4):
    text = AIRPORT.read_text()
    document = yaml.safe_load(text)
    if hosts_mode == 'none':
        document.pop('hosts', None)
        text = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    elif hosts_mode == 'ip':
        # Every mapping replaced by documentation addresses: no domain-to-domain entry remains.
        document['hosts'] = {key: f'198.51.100.{10 + index}' for index, key in enumerate(document.get('hosts') or {})}
        text = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    elif hosts_mode != 'orig':
        raise SystemExit(f'unknown hosts mode {hosts_mode}')
    bodies = {'airport.yaml': text}
    if r4:
        password_file = STATE / 'r4-password'
        if not password_file.exists():
            private_dir(STATE)
            password_file.write_text(secrets.token_hex(12))
            password_file.chmod(0o600)
        bodies['r4.yaml'] = yaml.safe_dump({'proxies': [{
            'name': 'R4 无 hosts 测试节点', 'type': 'ss', 'server': R4_DOMAIN, 'port': 443, 'cipher': 'aes-128-gcm',
            'password': password_file.read_text().strip(), 'udp': False}]}, allow_unicode=True, sort_keys=False)
    return bodies


def _kill(name):
    path = STATE / name
    if not path.exists():
        return
    try:
        os.killpg(int(path.read_text()), signal.SIGTERM)
    except (ProcessLookupError, ValueError):
        pass
    path.unlink()
    time.sleep(1)


def stop():
    _kill('server.pid')
    _kill('sub.pid')


_token = None


def api(method, path, body=None, *, timeout=300):
    global _token
    if _token is None:
        c = credentials()
        request = urllib.request.Request(f'http://{HOST_IP}:{API}/api/auth/login', method='POST',
                                         data=json.dumps({'username': c['username'], 'password': c['password']}).encode(),
                                         headers={'Content-Type': 'application/json'})
        _token = json.load(urllib.request.urlopen(request, timeout=30))['token']
    request = urllib.request.Request(f'http://{HOST_IP}:{API}/api{path}', method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {_token}'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b'null')
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b'null')


def serve(tree_name, variant, *flags):
    r4 = '--r4' in flags
    hosts_mode = flags[flags.index('--hosts') + 1] if '--hosts' in flags else 'orig'
    stop()
    src = tree_path(tree_name)
    sub_dir = private_dir(PRIVATE / 'sub')
    for child in sub_dir.iterdir():
        child.unlink()
    for name, body in subscription_bodies(hosts_mode, r4).items():
        (sub_dir / name).write_text(body)
        (sub_dir / name).chmod(0o600)
    process = subprocess.Popen([sys.executable, '-m', 'http.server', str(SUB), '--bind', '127.0.0.1'], cwd=sub_dir,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    (STATE / 'sub.pid').write_text(str(process.pid))
    data = PRIVATE / 'data'
    if data.exists():
        shutil.rmtree(data)
    private_dir(data)
    custom = variant_custom(variant)
    seeded = subprocess.run([str(PYTHON), '-c', SEED, str(data), f'http://127.0.0.1:{SUB}/', f'http://{HOST_IP}:{API}',
                             custom, credentials()['config_token'], '1' if r4 else '0'],
                            cwd=src, env=environment(data), capture_output=True, text=True)
    if seeded.returncode:
        raise SystemExit('seed failed: ' + mask(seeded.stderr[-2000:]))
    log = open(PRIVATE / 'server.log', 'ab')
    server = subprocess.Popen([str(PYTHON), '-c', SERVE], cwd=src, env=environment(data), stdout=log,
                              stderr=subprocess.STDOUT, start_new_session=True)
    (STATE / 'server.pid').write_text(str(server.pid))
    deadline = time.monotonic() + 90
    while True:
        try:
            urllib.request.urlopen(f'http://{HOST_IP}:{API}/api/auth/status', timeout=2)
            break
        except Exception:
            if server.poll() is not None or time.monotonic() > deadline:
                raise SystemExit('server did not start; see ' + str(PRIVATE / 'server.log'))
            time.sleep(0.5)
    fetched = {}
    for sub_id in ['air'] + (['r4'] if r4 else []):
        status, body = api('POST', f'/subscriptions/{sub_id}/fetch', {'preview': True})
        assert status == 200, mask(json.dumps(body, ensure_ascii=False))[:500]
        fetched[sub_id] = body.get('count')
    info = state('served', {'tree': tree_name, 'src': str(src), 'variant': variant, 'r4': r4, 'hosts': hosts_mode,
                            'custom': custom, 'fetched': fetched})
    print(json.dumps({key: info[key] for key in ('tree', 'variant', 'r4', 'hosts', 'fetched')}, ensure_ascii=False))


def gen(label):
    served = state('served')
    status, body = api('POST', '/generate/mihomo/preview', {'base_url': ''})
    assert status == 200, body
    content = body['content']
    document = yaml.safe_load(content)
    save(f'{label}/generated.yaml', content)
    report = body.get('subscription_hosts')
    out = {'served': {key: served[key] for key in ('tree', 'variant', 'r4', 'hosts')}, 'custom': served['custom'],
           'sha256': hashlib.sha256(content.encode()).hexdigest(), 'bytes': len(content.encode()),
           'dns': document.get('dns'), 'hosts': document.get('hosts'), 'report': report,
           'providers': list(document.get('proxy-providers') or {})}
    save(f'{label}/generate.json', out)
    print(json.dumps({key: out[key] for key in ('served', 'sha256', 'bytes', 'dns', 'hosts')}
                     | {'report_dns': (report or {}).get('dns')}, ensure_ascii=False, indent=1))


def run_label(label, *args):
    """run LABEL [servers|all|names] [--bin VERSION] [--from LABEL]"""
    args = list(args)
    binary, source = MIHOMO, label
    if '--bin' in args:
        index = args.index('--bin')
        binary = BIN / f'mihomo-{args[index + 1]}'
        del args[index:index + 2]
    if '--from' in args:
        index = args.index('--from')
        source = args[index + 1]
        del args[index:index + 2]
    which = args[0] if args else 'servers'
    path = EVIDENCE / source / 'generated.yaml'
    assert path.exists(), f'gen {source} first'
    document = yaml.safe_load(path.read_text())
    served = state('served')
    servers = {}
    sources = [airport()] + ([yaml.safe_load((PRIVATE / 'sub/r4.yaml').read_text())] if served.get('r4') else [])
    for item in sources:
        servers.update(node_servers(item))
    if which not in ('servers', 'all'):
        wanted = set(which.split(','))
        servers = {s: n for s, n in servers.items() if n in wanted or s in wanted}
    hosts = {domain_of(k): domain_of(v) for k, v in (airport().get('hosts') or {}).items() if isinstance(v, str)}
    result = run_config(label, document, GROUP, servers, hosts, binary)
    result['served'] = {key: served[key] for key in ('tree', 'variant', 'r4', 'hosts')}
    result['generated_from'] = source
    result['binary'] = binary.name
    save(f'{label}/result.json', result)


def check(label, version='v1.19.33'):
    """mihomo -t on LABEL's generated configuration (inside the namespace)."""
    directory = private_dir(PRIVATE / f'check-{label}')
    for child in directory.iterdir():
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    shutil.copy(EVIDENCE / label / 'generated.yaml', directory / 'config.yaml')
    result = ns(BIN / f'mihomo-{version}', '-t', '-d', directory, check=False, timeout=60)
    out = {'version': version, 'exit': result.returncode,
           'output': [mask(line) for line in result.stdout.decode().splitlines()
                      if 'test is successful' in line or 'failed' in line or 'level=error' in line][-4:]}
    shutil.rmtree(directory)
    save(f'{label}/check-{version}.json', out)
    print(json.dumps(out, ensure_ascii=False))


def direct(tree_name, label):
    out = PRIVATE / f'direct-{label}-{tree_name}.yaml'
    result = subprocess.run([str(PYTHON), '-c', DIRECT, str(PRIVATE / 'data'), f'http://{HOST_IP}:{API}', str(out)],
                            cwd=tree_path(tree_name), env=environment(PRIVATE / 'data'), capture_output=True, text=True)
    if result.returncode:
        raise SystemExit('direct generation failed: ' + mask(result.stderr[-2000:]))
    info = json.loads(result.stdout.strip().splitlines()[-1])
    save(f'{label}/direct-{tree_name}.yaml', out.read_text())
    out.unlink()
    info['tree'] = tree_name
    print(json.dumps(info))
    return info


def compare(first, second, label):
    served = state('served')
    infos = {tree_name: direct(tree_name, label) for tree_name in (first, second)}
    texts = [(EVIDENCE / label / f'direct-{tree_name}.yaml').read_text() for tree_name in (first, second)]
    out = {'served': {key: served[key] for key in ('tree', 'variant', 'r4', 'hosts')}, **infos,
           'identical': texts[0] == texts[1]}
    if not out['identical']:
        import difflib
        out['diff'] = list(difflib.unified_diff(texts[0].splitlines(), texts[1].splitlines(), first, second, lineterm=''))[:120]
    save(f'{label}/compare.json', out)
    print(json.dumps(out, ensure_ascii=False, indent=1))


# ------------------------------------------------------------------ cleanup

def cleanup():
    stop()
    stop_mihomo()
    removed = []
    for path in (AIRPORT, PRIVATE / 'sub', PRIVATE / 'data', PRIVATE / 'server.log', STATE / 'r4-password'):
        if path.exists():
            shutil.rmtree(path) if path.is_dir() else path.unlink()
            removed.append(str(path.relative_to(BASE)))
    for path in list(PRIVATE.glob('run-*')) + list(PRIVATE.glob('direct-*')) + list(PRIVATE.glob('check-*')):
        shutil.rmtree(path) if path.is_dir() else path.unlink()
        removed.append(str(path.relative_to(BASE)))
    leftovers = [str(p) for p in PRIVATE.rglob('*')] if PRIVATE.exists() else []
    print(json.dumps({'removed': removed, 'private_left': leftovers}, ensure_ascii=False, indent=1))


def scan(*needles):
    """Fail when any saved evidence contains a node password (or one of the given strings)."""
    values = sorted(secret_values()) + list(needles)
    assert secret_values(), 'scan needs the airport copy to know what to look for'

    hits = []
    for path in EVIDENCE.rglob('*'):
        if path.is_file():
            text = path.read_text(errors='replace')
            hits += [str(path) for value in values if value in text]
    print(json.dumps({'files': sum(1 for p in EVIDENCE.rglob('*') if p.is_file()), 'hits': hits}))
    assert not hits


if __name__ == '__main__':
    action, args = sys.argv[1], sys.argv[2:]
    {'net-up': net_up, 'net-down': net_down, 'counters': counters_cmd, 'binaries': binaries, 'versions': versions,
     'ref': ref, 'r1': r1, 'tree': tree, 'serve': serve, 'gen': gen, 'run': run_label, 'direct': direct,
     'compare': compare, 'check': check, 'stop': stop, 'cleanup': cleanup, 'scan': scan}[action](*args)
