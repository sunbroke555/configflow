#!/usr/bin/env python3
"""Exercise the managed Web listener against a private real nginx process.

Run only on the test server, for example:
    rt -- python3 scripts/tests/web_listener_integration.py

The harness uses temporary files, ephemeral high ports and its own nginx PID.
It does not modify the system nginx configuration or signal unrelated processes.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import http.client
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback


REPOSITORY = Path(__file__).resolve().parents[2]
HEALTH_PATH = '/_configflow/listener-health'


def load_listener():
    spec = importlib.util.spec_from_file_location(
        'configflow_web_listener_integration', REPOSITORY / 'backend/utils/web_listener.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def http_get(port, host='127.0.0.1', path='/health'):
    connection = http.client.HTTPConnection(host, port, timeout=1)
    try:
        connection.request('GET', path, headers={'Connection': 'close'})
        response = connection.getresponse()
        return response.status, response.read().decode()
    finally:
        connection.close()


def wait_for(predicate, message, *, timeout=5):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (OSError, http.client.HTTPException) as error:
            last_error = error
        time.sleep(.05)
    raise AssertionError(f'{message}; last error: {last_error}')


def port_closed(port, host='127.0.0.1'):
    try:
        with socket.create_connection((host, port), timeout=.2):
            return False
    except ConnectionRefusedError:
        return True


@contextmanager
def occupied_port(*, ipv6=False, port=0):
    with socket.socket(socket.AF_INET6 if ipv6 else socket.AF_INET, socket.SOCK_STREAM) as listener:
        if ipv6:
            listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        listener.bind(('::' if ipv6 else '0.0.0.0', port))
        listener.listen(1)
        yield listener.getsockname()[1]


def available_port():
    # Keep IPv4 reserved until the IPv6 bind has proved both families free.
    for _ in range(20):
        with occupied_port() as port:
            try:
                with occupied_port(ipv6=True, port=port):
                    return port
            except OSError:
                pass
    raise RuntimeError('Could not find a high port free in both address families')


def private_template(directory):
    template = (REPOSITORY / 'docker/nginx.conf').read_text()
    assert template.count('__CONFIGFLOW_LISTEN__') == 1, 'Missing listener template marker'
    assert '__CONFIGFLOW_LISTENER_TOKEN__' in template, 'Missing health token template marker'
    # The production vhost, proxy locations and managed markers stay unchanged.
    template = re.sub(r'^user\s+[^;]+;\s*', '', template, count=1, flags=re.MULTILINE)
    template = re.sub(r'^pid\s+[^;]+;', f'pid {directory}/nginx.pid;', template,
                      count=1, flags=re.MULTILINE)
    template = template.replace('root /usr/share/nginx/html;', f'root {directory}/html;')
    # Distribution defaults can point at root-owned paths even without traffic.
    temp_paths = '\n'.join(f'    {name}_temp_path {directory}/{name};'
                           for name in ('client_body', 'proxy', 'fastcgi', 'uwsgi', 'scgi'))
    template = template.replace('http {', f'http {{\n{temp_paths}', 1)
    (directory / 'html').mkdir()
    (directory / 'html/index.html').write_text('ConfigFlow disposable listener lab\n')
    return template


class Nginx:
    def __init__(self, executable, directory):
        self.executable = executable
        self.directory = directory
        self.process = None
        self.output = None

    def start(self, port):
        assert self.process is None
        self.output = (self.directory / 'nginx-output.log').open('ab')
        self.process = subprocess.Popen(
            [self.executable, '-p', str(self.directory), '-c', str(self.directory / 'nginx.conf'),
             '-g', 'daemon off;'], stdout=self.output, stderr=subprocess.STDOUT,
            start_new_session=True)
        wait_for(lambda: self.process.poll() is None and http_get(port) == (200, 'healthy\n'),
                 f'Private nginx did not start on {port}')

    def stop(self):
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGQUIT)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # Only the process group this harness created is targeted.
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
        self.process = None
        self.output.close()
        self.output = None


def run(executable, report):
    if sys.platform != 'linux':
        raise RuntimeError('Run this integration test on the Linux test server via rt')
    with tempfile.TemporaryDirectory(prefix='configflow-web-listener-') as temporary:
        directory = Path(temporary)
        module = load_listener()
        module.TEMPLATE_PATH = directory / 'nginx.conf.template'
        module.CONFIG_PATH = directory / 'nginx.conf'
        module.STATE_PATH = directory / 'web-listener.json'
        module.LOCK_PATH = directory / 'web-listener.lock'
        module.NGINX_BINARY = executable
        template = private_template(directory)
        module.TEMPLATE_PATH.write_text(template)
        initial_port = available_port()
        module.STATE_PATH.write_text(json.dumps({'port': initial_port}))
        os.environ.pop('CONFIGFLOW_WEB_PORT', None)
        server = Nginx(executable, directory)

        def passed(name, **details):
            report['checks'].append({'name': name, 'passed': True, **details})
            print(f'PASS {name}', flush=True)

        def reachable(port):
            assert http_get(port) == (200, 'healthy\n')
            assert http_get(port, '::1') == (200, 'healthy\n')
            status = module.listener_status()
            assert status['port'] == port, status
            assert status['ipv6'] is True, status
            return status

        def refused(port, message):
            state_before = module.STATE_PATH.read_bytes()
            config_before = module.CONFIG_PATH.read_bytes()
            try:
                module.update_listener(port)
            except module.WebListenerError:
                pass
            else:
                raise AssertionError(message)
            assert module.STATE_PATH.read_bytes() == state_before, 'Rejected change rewrote persisted port'
            assert module.CONFIG_PATH.read_bytes() == config_before, 'Rejected change rewrote active config'

        try:
            prepared = module.prepare_listener()
            assert prepared['port'] == initial_port, prepared
            server.start(initial_port)
            assert reachable(initial_port)['editable'] is True
            passed('saved startup serves both IPv4 and IPv6', port=initial_port)

            next_port = available_port()
            updated = module.update_listener(next_port)
            assert updated['port'] == next_port and updated['editable'] is True, updated
            reachable(next_port)
            wait_for(lambda: port_closed(initial_port), 'Old IPv4 listener remained open after reload')
            wait_for(lambda: port_closed(initial_port, '::1'), 'Old IPv6 listener remained open after reload')
            assert json.loads(module.STATE_PATH.read_text())['port'] == next_port
            passed('reload switches both listeners and persists the new port', port=next_port)

            with occupied_port() as conflicting_port:
                refused(conflicting_port, 'An occupied IPv4 port was accepted')
                reachable(next_port)
            passed('IPv4 conflict preserves existing listeners and saved settings')

            with occupied_port(ipv6=True) as conflicting_port:
                refused(conflicting_port, 'An occupied IPv6-only port was accepted')
                reachable(next_port)
            passed('IPv6-only conflict preserves existing listeners and saved settings')

            module.TEMPLATE_PATH.write_text(template + '\nnot_a_valid_nginx_directive on;\n')
            refused(available_port(), 'An invalid nginx template was activated')
            module.TEMPLATE_PATH.write_text(template)
            reachable(next_port)
            passed('nginx syntax validation failure preserves existing listeners')

            failed_port = available_port()
            atomic_write = module._atomic_write
            state_write_attempts = []

            def fail_state_write(path, content, mode=0o644):
                if Path(path) == Path(module.STATE_PATH):
                    state_write_attempts.append(True)
                    # Verify failure happens after the real reload opened both sockets.
                    if len(state_write_attempts) == 1:
                        assert http_get(failed_port) == (200, 'healthy\n')
                        assert http_get(failed_port, '::1') == (200, 'healthy\n')
                    raise PermissionError('simulated persistent state write failure')
                return atomic_write(path, content, mode)

            module._atomic_write = fail_state_write
            try:
                refused(failed_port, 'Failed persistence was reported as successful')
                assert state_write_attempts, 'Test did not reach state persistence'
                reachable(next_port)
                wait_for(lambda: port_closed(failed_port), 'Failed new IPv4 listener stayed open')
                wait_for(lambda: port_closed(failed_port, '::1'), 'Failed new IPv6 listener stayed open')
            finally:
                module._atomic_write = atomic_write
            passed('persistent state-write failure rolls back a real dual-stack reload')

            server.stop()
            module.prepare_listener()
            server.start(next_port)
            assert reachable(next_port)['source'] == 'saved'
            passed('nginx restart restores the persisted port')

            server.stop()
            recovery_port = available_port()
            os.environ['CONFIGFLOW_WEB_PORT'] = str(recovery_port)
            module.prepare_listener()
            server.start(recovery_port)
            status = reachable(recovery_port)
            assert status['source'] == 'environment' and status['editable'] is False, status
            assert json.loads(module.STATE_PATH.read_text())['port'] == next_port
            refused(available_port(), 'Web edit was accepted despite environment override')
            reachable(recovery_port)
            passed('environment override recovers access and makes Web editing read-only')

            server.stop()
            os.environ.pop('CONFIGFLOW_WEB_PORT')
            module.prepare_listener()
            server.start(next_port)
            assert reachable(next_port)['editable'] is True
            passed('removing environment override restores saved port and Web editing')
            report['success'] = True
        except Exception:
            report['success'] = False
            report['error'] = traceback.format_exc()
            output = directory / 'nginx-output.log'
            if output.exists():
                report['nginx_log_tail'] = '\n'.join(output.read_text(errors='replace').splitlines()[-60:])
            raise
        finally:
            server.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--nginx', default=shutil.which('nginx'))
    parser.add_argument('--report', type=Path)
    arguments = parser.parse_args()
    if not arguments.nginx:
        parser.error('nginx must be installed on the test server')
    report = {'checks': []}
    try:
        run(arguments.nginx, report)
    except Exception:
        print(report.get('error', traceback.format_exc()), file=sys.stderr)
        if report.get('nginx_log_tail'):
            print(report['nginx_log_tail'], file=sys.stderr)
        return 1
    finally:
        if arguments.report:
            arguments.report.parent.mkdir(parents=True, exist_ok=True)
            arguments.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(f"Passed {len(report['checks'])} real-nginx integration checks", flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
