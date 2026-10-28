#!/usr/bin/env python3
"""Build and verify a focused recovery image on the Linux test server.

Run through rt; requires sudo Docker. Uses a new disposable container and
occupies the old internal ports to reproduce NAS Host network conflicts.
Existing listeners are left untouched. Never uses production data.
"""
import argparse
import contextlib
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--base-image', required=True)
parser.add_argument('--image', default='configflow:host-ports-check')
parser.add_argument('--report', type=Path)
args = parser.parse_args()
IMAGE, BASE = args.image, args.base_image
NAME = f'configflow-host-ports-check-{os.getpid()}'

def docker(*args, **kwargs):
    return subprocess.run(['sudo', 'docker', *args], check=True, text=True, **kwargs)

def request(port, path, data=None, host='127.0.0.1'):
    conn = http.client.HTTPConnection(host, port, timeout=40)
    try:
        conn.request('POST' if data is not None else 'GET', path,
                     body=json.dumps(data) if data is not None else None,
                     headers={'Content-Type': 'application/json', 'Connection': 'close'})
        res = conn.getresponse()
        body = res.read().decode()
        assert res.status == 200, (path, res.status, body[:300])
        return json.loads(body)
    finally:
        conn.close()

def available_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]

def wait_ready(port):
    deadline = time.monotonic() + 45
    while True:
        try:
            return request(port, '/api/auth/status')
        except (OSError, AssertionError):
            if time.monotonic() > deadline:
                raise
            time.sleep(.25)

with tempfile.TemporaryDirectory(prefix='cf-host-ports-') as directory, contextlib.ExitStack() as stack:
    temporary = Path(directory)
    files = ['backend/app.py', 'backend/http_server.py', 'backend/common/config.py',
             'backend/utils/dependencies.py', 'backend/utils/sub_store_client.py',
             'backend/utils/sub_store_runtime.py', 'backend/utils/web_listener.py']
    lines = ['FROM ' + BASE]
    for file in files:
        target = temporary / file
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / file).read_bytes())
        lines.append(f'COPY {file} /app/{file}')
    (temporary / 'nginx.conf').write_bytes((ROOT / 'docker/nginx.conf').read_bytes())
    lines += ['COPY backend/utils/web_listener.py /usr/local/bin/configflow-web-listener.py',
              'COPY nginx.conf /etc/nginx/configflow-nginx.conf.template']
    (temporary / 'Dockerfile').write_text('\n'.join(lines) + '\n')
    docker('build', '-t', IMAGE, str(temporary))
    defaults = []
    for port in (5001, 3001):
        sock = socket.socket()
        try:
            sock.bind(('0.0.0.0', port))
            sock.listen(1)
            stack.callback(sock.close)
            defaults.append({'port': port, 'occupied': 'test socket'})
        except OSError:
            sock.close()
            defaults.append({'port': port, 'occupied': 'existing listener'})
    web, backend, substore = (available_port() for _ in range(3))
    assert len({web, backend, substore}) == 3
    try:
        docker('run', '-d', '--name', NAME, '--network', 'host',
               '-e', f'CONFIGFLOW_WEB_PORT={web}',
               '-e', 'CONFIGFLOW_BACKEND_HOST=127.0.0.1',
               '-e', f'CONFIGFLOW_BACKEND_PORT={backend}',
               '-e', f'SUB_STORE_BACKEND_API_PORT={substore}', IMAGE)
        wait_ready(web)
        for path in ['/api/stats/overview', '/api/subscriptions', '/api/proxy-groups',
                     '/api/rule-sets', '/api/nodes', '/api/profiles']:
            request(web, path)
        request(web, '/api/auth/status', host='::1')
        converted = request(web, '/api/nodes/parse-lines',
                            {'lines': ['probe = snell, 192.0.2.1, 443, psk=example, version=4']})
        assert converted['results'][0]['proxy']['type'] == 'snell', converted
        status = docker('exec', NAME, 'supervisorctl', '-c',
                        '/etc/supervisor/conf.d/supervisord.conf', 'status', capture_output=True).stdout
        assert all(line.split()[1] == 'RUNNING' for line in status.splitlines()), status
        docker('restart', NAME, stdout=subprocess.DEVNULL)
        wait_ready(web)
        request(web, '/api/stats/overview')
        report = {'image': IMAGE, 'defaults': defaults, 'web': web, 'backend': backend,
                  'substore': substore, 'dashboard_endpoints': 6, 'ipv6': True,
                  'real_substore_conversion': True, 'restart': True, 'success': True}
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report), flush=True)
    except Exception:
        docker('logs', '--tail', '60', NAME)
        raise
    finally:
        docker('rm', '-f', NAME, stdout=subprocess.DEVNULL)
