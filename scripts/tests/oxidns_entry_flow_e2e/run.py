#!/usr/bin/env python3
"""Real browser -> authenticated API -> saved native YAML -> real OxiDNS.

Run on the test server only. No production services or configuration are read.
"""
from __future__ import annotations
import argparse
import ast
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request

import yaml
from fixtures import fixtures, reserve_port

REPOSITORY = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY))
from scripts.verify_oxidns_native import MockDNS, query, read_name


def private_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    path.chmod(0o600)


def core_binary(cache):
    tree = ast.parse((REPOSITORY / 'backend/agents/gateway_install_script.py').read_text())
    releases = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'CORE_RELEASES' for target in node.targets))
    spec = releases['oxidns']['linux-amd64']
    root = cache / ('oxidns-' + releases['oxidns']['version'])
    root.mkdir(parents=True, exist_ok=True)
    archive = root / 'release.tar.gz'
    if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != spec['sha256']:
        subprocess.run(['curl', '--fail', '--location', '--retry', '3', '--silent', '--show-error', spec['url'], '-o', str(archive)], check=True, timeout=180)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert digest == spec['sha256'], 'Official release archive checksum mismatch'
    with tarfile.open(archive) as source:
        members = [member for member in source.getmembers() if Path(member.name).name == 'oxidns' and member.isfile()]
        assert len(members) == 1, 'Expected one OxiDNS binary in pinned release'
        binary = root / 'oxidns'
        binary.write_bytes(source.extractfile(members[0]).read())
        binary.chmod(0o755)
    return binary, {'version': releases['oxidns']['version'], 'archive_sha256': digest, 'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest()}


class Api:
    def __init__(self, url):
        self.url, self.token = url, ''
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, method, path, body=None, profile=None, headers=None, expected_error=None):
        fields = {'Content-Type': 'application/json', **(headers or {})}
        if self.token:
            fields['Authorization'] = 'Bearer ' + self.token
        if profile:
            fields['X-ConfigFlow-Profile'] = profile
        request = urllib.request.Request(self.url + path, data=json.dumps(body).encode() if body is not None else None, headers=fields, method=method)
        try:
            with self.opener.open(request, timeout=30) as response:
                assert expected_error is None, f'{method} {path} unexpectedly succeeded'
                return json.load(response), dict(response.headers)
        except urllib.error.HTTPError as error:
            detail = json.load(error)
            if expected_error is not None:
                assert error.code == expected_error, (error.code, detail)
                return detail, dict(error.headers)
            raise AssertionError(f'{method} {path} returned {error.code}: {detail}') from None


def create_app(root):
    credentials = {'username': 'entry-flow-e2e', 'password': secrets.token_urlsafe(24)}
    os.environ.update(DATA_DIR=str(root / 'data'), LOG_FILE=str(root / 'backend.log'), LOG_LEVEL='WARNING',
        ADMIN_USERNAME=credentials['username'], ADMIN_PASSWORD=credentials['password'], JWT_SECRET_KEY=secrets.token_urlsafe(48),
        AGENT_REGISTRATION_KEY=secrets.token_urlsafe(24), SUB_STORE_BUILTIN_DIR=str(root / 'disabled-sub-store'))
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        os.environ.pop(key, None)
    os.environ['NO_PROXY'] = '127.0.0.1,localhost'
    os.chdir(root)
    from backend.common import config as store
    from backend.common.config_repository import ProfileRepository
    store.set_repository(ProfileRepository(root / 'data', default_config_factory=store.get_default_config, initial_config_factory=store.get_default_config))
    from backend.app import app
    from backend.utils.sub_store_runtime import runtime
    assert runtime._thread is None, 'Unexpected Sub-Store monitor in isolated test'
    app.config.update(TESTING=False, DEBUG=False)
    from werkzeug.serving import make_server
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    api = Api(f'http://127.0.0.1:{server.server_port}')
    login, _ = api.call('POST', '/api/auth/login', credentials)
    api.token = login['token']
    return server, api


def query_listener(listener, name, timeout=3):
    if listener['type'] != 'http_server':
        return query(listener['port'], name, tcp=listener['type'] == 'tcp_server', timeout=timeout)
    wire_name = b''.join(bytes([len(label)]) + label.encode() for label in name.split('.')) + b'\0'
    request = struct.pack('!HHHHHH', 0x2345, 0x0100, 1, 0, 0, 0) + wire_name + struct.pack('!HH', 1, 1)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = f"http://127.0.0.1:{listener['port']}{listener['path']}"
    with opener.open(urllib.request.Request(url, data=request, headers={'Content-Type': 'application/dns-message', 'Accept': 'application/dns-message'}, method='POST'), timeout=timeout) as response:
        assert response.status == 200
        assert 'application/dns-message' in response.headers.get('Content-Type', '')
        reply = response.read()
    header = struct.unpack('!HHHHHH', reply[:12])
    assert header[0] == 0x2345
    _, offset = read_name(reply, 12)
    offset += 4
    ips, ttls = [], []
    for _ in range(header[3]):
        if reply[offset] & 0xc0 == 0xc0:
            offset += 2
        else:
            _, offset = read_name(reply, offset)
        typ, _, ttl, size = struct.unpack('!HHIH', reply[offset:offset + 10])
        offset += 10
        if typ == 1:
            ips.append(socket.inet_ntoa(reply[offset:offset + size]))
            ttls.append(ttl)
        offset += size
    return {'rcode': header[1] & 15, 'ips': ips, 'ttls': ttls, 'answers': header[3], 'authority': header[4]}


def core_run(root, binary, document, listeners, expectations):
    root.mkdir(parents=True, exist_ok=True)
    config = root / 'config.yaml'
    config.write_text(document)
    checked = subprocess.run([str(binary), 'check', '-c', str(config), '-d', str(root)], capture_output=True, text=True, timeout=30)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    process = None
    replies = []
    try:
        with (root / 'runtime.log').open('w') as log:
            process = subprocess.Popen([str(binary), 'start', '-c', str(config), '-d', str(root)], stdout=log, stderr=subprocess.STDOUT)
            for listener in listeners:
                tcp = listener['type'] == 'tcp_server'
                for _ in range(100):
                    assert process.poll() is None, (root / 'runtime.log').read_text()[-3000:]
                    try:
                        query_listener(listener, 'ready.test', timeout=.1)
                        break
                    except OSError:
                        time.sleep(.05)
                else:
                    raise AssertionError('Temporary OxiDNS listener did not become ready')
                for name in ('order.test', 'unmatched.test'):
                    expected = expectations[listener['tag']][name]
                    for _ in range(3):
                        reply = query_listener(listener, name)
                        assert reply['rcode'] == 0 and reply['ips'] == [expected], (listener['tag'], name, expected, reply)
                        replies.append({'listener': listener['tag'], 'transport': 'http-dns-post' if listener['type'] == 'http_server' else 'tcp' if tcp else 'udp', 'name': name, 'reply': reply})
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    return {'check': checked.stdout.strip(), 'queries': replies}


def expectations(item, edited):
    return {listener['tag']: {'order.test': '198.18.0.2' if edited and (item['case'] == 'forward' or item['id'] not in ('e2e-independent', 'e2e-http') or listener['entry'] == 'entry_a') else '10.11.0.1',
        'unmatched.test': '198.18.0.2' if edited and item['case'] == 'forward' else '10.11.0.1'} for listener in item['listeners']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cache', type=Path, default=Path('/tmp/configflow-entry-flow-e2e-runtime'))
    parser.add_argument('--bootstrap-only', action='store_true', help='Validate full app/API/core fixture baseline before new UI lands')
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if (root / 'data/configflow.sqlite3').exists():
        raise RuntimeError('Use a fresh output directory; refusing to reuse a database')
    server, vite, local, remote = None, None, None, None
    report = {'success': False, 'cases': {}, 'scope': 'full ConfigFlow application, real HTTP API, disposable SQLite, pinned real OxiDNS'}
    try:
        binary, report['core'] = core_binary(args.cache)
        local, remote = MockDNS('10.11.0.1'), MockDNS('198.18.0.2')
        server, api = create_app(root)
        data = fixtures(local.server_address[1], remote.server_address[1])
        initial = {}
        for item in data:
            api.call('POST', '/api/profiles', {'id': item['id'], 'name': item['name']})
            saved, _ = api.call('POST', '/api/oxidns/settings', item['settings'], item['id'])
            initial[item['id']] = saved['settings']
            native, _ = api.call('POST', '/api/oxidns/render', {'settings': saved['settings']}, item['id'])
            report['cases'][item['id']] = {'baseline': core_run(root / item['id'] / 'before', binary, native['config'], item['listeners'], expectations(item, False))}
        info = {'repository': str(REPOSITORY), 'backend_url': api.url, 'frontend_port': reserve_port(), 'token': api.token,
            'profiles': [{key: value for key, value in item.items() if key != 'settings'} for item in data], 'remote_upstream': f'udp://127.0.0.1:{remote.server_address[1]}', 'output': str(root), 'bootstrap_only': args.bootstrap_only}
        report['ports'] = {'backend': server.server_port, 'frontend': info['frontend_port'], 'dns': sorted({listener['port'] for item in data for listener in item['listeners']})}
        private_json(root / 'browser-info.json', info)
        private_json(root / 'initial-settings.json', initial)
        with (root / 'vite.log').open('w') as log:
            vite = subprocess.Popen(['node', str(Path(__file__).with_name('vite-server.mjs')), str(root / 'browser-info.json')], cwd=REPOSITORY, stdout=log, stderr=subprocess.STDOUT)
            url = f"http://127.0.0.1:{info['frontend_port']}"
            for _ in range(150):
                assert vite.poll() is None, (root / 'vite.log').read_text()[-3000:]
                try:
                    with urllib.request.urlopen(url, timeout=.2) as response:
                        if response.status == 200:
                            break
                except OSError:
                    time.sleep(.1)
            else:
                raise AssertionError('Temporary frontend did not become ready')
            subprocess.run(['node', str(Path(__file__).with_name('browser.mjs')), str(root / 'browser-info.json')], cwd=REPOSITORY, check=True, timeout=300)
        report['browser'] = json.loads((root / 'browser-report.json').read_text())
        if not args.bootstrap_only:
            for item in data:
                saved, headers = api.call('GET', '/api/oxidns/settings', profile=item['id'])
                original = initial[item['id']]
                verify_locality(item, original, saved)
                validation, _ = api.call('POST', '/api/oxidns/validate', {'settings': saved}, item['id'])
                native, _ = api.call('POST', '/api/oxidns/render', {'settings': saved}, item['id'])
                private_json(root / item['id'] / 'saved-settings.json', saved)
                report['cases'][item['id']]['after'] = core_run(root / item['id'] / 'after', binary, native['config'], item['listeners'], expectations(item, True))
                report['cases'][item['id']].update(locality_preserved=True, saved_etag=headers.get('ETag'), validation=validation)
            report['negative_checks'] = negative_checks(api, binary, root)
        report['success'] = True
    except BaseException as error:
        report['failure'] = str(error)
        raise
    finally:
        if vite and vite.poll() is None:
            vite.terminate()
            try:
                vite.wait(timeout=5)
            except subprocess.TimeoutExpired:
                vite.kill(); vite.wait(timeout=5)
        if server:
            server.shutdown(); server.server_close()
        if local:
            local.close()
        if remote:
            remote.close()
        for private_name in ('browser-info.json', 'initial-settings.json'):
            (root / private_name).unlink(missing_ok=True)
        report['screenshot_count'] = len(list(root.glob('*.png')))
        report['dns_result_assertions'] = sum(len(stage['queries']) for case in report['cases'].values() for stage in case.values() if isinstance(stage, dict) and 'queries' in stage)
        closed_ports = {}
        for name in ('backend', 'frontend'):
            port = report.get('ports', {}).get(name)
            if port:
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.2):
                        closed_ports[name] = False
                except OSError:
                    closed_ports[name] = True
        report['cleanup'] = {'vite_process_stopped': vite is None or vite.poll() is not None,
            'http_ports_closed': closed_ports, 'temporary_credentials_removed': not (root / 'browser-info.json').exists(),
            'core_and_dns_stubs': 'owned processes/servers terminated and waited/closed in finally'}
        (root / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'success': True, 'cases': len(report['cases']), 'screenshots': report['screenshot_count'], 'report': str(root / 'report.json')}))



def negative_checks(api, binary, root):
    """Actual HTTP draft conflicts and final validation without storing invalid data."""
    profile = 'e2e-independent'
    saved, _ = api.call('GET', '/api/oxidns/settings', profile=profile)
    projection, _ = api.call('POST', '/api/oxidns/common-settings', {'settings': saved}, profile)
    node = next(item for item in projection['entry_flow']['plugins'] if item['tag'] == 'entry_a')
    patch = {'source': node['source'], 'index': node['index'], 'before': deepcopy(node['value']), 'value': deepcopy(node['value'])}
    checks = []
    for kind in ('stale-before', 'tag-change', 'type-change'):
        bad = deepcopy(patch)
        if kind == 'stale-before':
            bad['before']['e2e_extension'] = 'stale'
        else:
            bad['value']['tag' if kind == 'tag-change' else 'type'] = 'unauthorized-replacement'
        payload, _ = api.call('POST', '/api/oxidns/common-settings/apply', {'settings': saved, 'changes': {'entry_flow': bad}}, profile, expected_error=400)
        checks.append({'case': kind, 'http_status': 400, 'message': payload['message']})
    for name, target in [('missing-reference', 'e2e_no_such_target'), ('dependency-cycle', 'entry_a')]:
        bad = deepcopy(patch)
        bad['value']['args'][0]['exec'] = 'goto ' + target
        result, _ = api.call('POST', '/api/oxidns/common-settings/apply', {'settings': saved, 'changes': {'entry_flow': bad}}, profile)
        candidate = result['settings']
        validation, _ = api.call('POST', '/api/oxidns/validate', {'settings': candidate}, profile)
        assert validation['valid'] is False, (name, validation)
        api.call('POST', '/api/oxidns/render', {'settings': candidate}, profile, expected_error=400)
        directory = root / 'negative' / name
        directory.mkdir(parents=True)
        config = directory / 'config.yaml'
        config.write_text(candidate['native_config'])
        result = subprocess.run([str(binary), 'check', '-c', str(config), '-d', str(directory)], capture_output=True, text=True, timeout=30)
        assert result.returncode != 0, name + ' was accepted by the real core'
        (directory / 'check.log').write_text(result.stdout + result.stderr)
        checks.append({'case': name, 'rules_valid': False, 'render_http_status': 400, 'real_core_check_exit': result.returncode})
    unchanged, _ = api.call('GET', '/api/oxidns/settings', profile=profile)
    assert json.dumps(unchanged, sort_keys=True) == json.dumps(saved, sort_keys=True)
    return checks


def verify_locality(item, before, after):
    source = item['edit_source']
    old_root, new_root = yaml.safe_load(before['native_config']), yaml.safe_load(after['native_config'])
    old_files = {file['path']: file for file in before['additional_files']}
    new_files = {file['path']: file for file in after['additional_files']}
    if source == 'config.yaml':
        old_doc, new_doc = old_root, new_root
        assert old_files == new_files, 'Unrelated attachments changed'
    else:
        assert before['native_config'] == after['native_config'], 'Editing include changed root document'
        assert old_files.keys() == new_files.keys()
        old_doc, new_doc = yaml.safe_load(old_files[source]['content']), yaml.safe_load(new_files[source]['content'])
        assert {key: value for key, value in old_files.items() if key != source} == {key: value for key, value in new_files.items() if key != source}
    tag = item['edit_tag']
    expected = deepcopy(old_doc)
    edited = next(plugin for plugin in expected['plugins'] if plugin['tag'] == tag)
    actual = next(plugin for plugin in new_doc['plugins'] if plugin['tag'] == tag)
    if item['case'] == 'forward':
        edited['args']['upstreams'][0]['addr'] = actual['args']['upstreams'][0]['addr']
        assert edited['args']['upstreams'][0]['addr'] != next(plugin for plugin in old_doc['plugins'] if plugin['tag'] == tag)['args']['upstreams'][0]['addr']
    else:
        target = next(step for step in edited['args'] if step.get('exec') == 'goto local_flow')
        target['exec'] = 'goto remote_flow'
    assert json.dumps(expected, sort_keys=True) == json.dumps(new_doc, sort_keys=True), 'Changes escaped the one edited field; order, shape, extensions and unrelated plugins must survive'


if __name__ == '__main__':
    main()
