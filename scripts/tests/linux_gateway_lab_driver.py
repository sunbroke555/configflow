#!/usr/bin/env python3
"""Exercise the real gateway installer/API in the explicitly marked disposable VM.

Run AFTER linux_gateway_lab_network.sh setup and linux_gateway_lab_server.py:
  ip netns exec cfgl-router /opt/configflow-lab/venv/bin/python \
    scripts/tests/linux_gateway_lab_driver.py \
    --manifest /tmp/configflow-gateway-lab-server/manifest.json

This is a destructive test ONLY for the lab VM's four dedicated services. It does
not create its own VM permission marker, change initial-namespace networking, or
call production ConfigFlow endpoints. Network setup/cleanup remain separate.
All request evidence and process logs are private files under a fresh /tmp folder.
"""
from __future__ import annotations

import argparse
import datetime as dt
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import re
import select
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import termios
import time
import urllib.error
import urllib.parse
import urllib.request

# The independently guarded support script is part of this same test harness.
from linux_gateway_lab_support import guard

ROOT = ['nsenter', '--net=/proc/1/ns/net', '--']
GATEWAY = [*ROOT, 'ip', 'netns', 'exec', 'cfgl-gateway']
SERVICES = ('mihomo', 'oxidns')
CORE_FILES = tuple(Path(path) for service in SERVICES for path in (
    f'/etc/{service}/config.yaml', f'/usr/local/bin/{service}',
    f'/etc/systemd/system/{service}.service'))
JOURNAL = Path('/opt/configflow-agent/mihomo/linux-gateway.json')


class LabFailure(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise LabFailure(message)


def redact(value):
    if isinstance(value, dict):
        return {key: ('[REDACTED]' if key.lower() in ('command', 'token', 'password', 'secret', 'authorization') else redact(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r'(token=)[^\s&\"\']+', r'\1[REDACTED]', value)
        value = re.sub(r'(?i)(Bearer )[^\s\"\']+', r'\1[REDACTED]', value)
    return value


def private_json(path, value):
    with open(path, 'w', encoding='utf-8') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(redact(value), stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write('\n')


def metadata(paths):
    result = {}
    for path in paths:
        require(not path.is_symlink(), f'Unexpected symlink in lab-owned installation: {path}')
        if not path.exists():
            result[str(path)] = None
            continue
        info = path.stat()
        require(stat.S_ISREG(info.st_mode), f'Expected regular lab file: {path}')
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        result[str(path)] = {'sha256': digest.hexdigest(), 'size': info.st_size,
                             'uid': info.st_uid, 'gid': info.st_gid,
                             'mode': stat.S_IMODE(info.st_mode), 'mtime_ns': info.st_mtime_ns}
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise LabFailure('Refusing API redirect out of the fixed lab origin')


class Driver:
    def __init__(self, args):
        guard()
        require(socket.gethostname() == 'configflow-gateway-lab', 'Wrong VM hostname')
        require(os.stat('/proc/self/ns/net').st_ino == os.stat('/run/netns/cfgl-router').st_ino,
                'Run the driver in cfgl-router; installation subprocesses use cfgl-gateway')
        manifest_path = Path(args.manifest)
        require(not manifest_path.is_symlink(), 'Manifest cannot be a symlink')
        self.manifest = json.loads(manifest_path.read_text())
        parsed = urllib.parse.urlsplit(self.manifest['base_url'])
        require(parsed.scheme == 'http' and parsed.hostname == '192.0.2.1' and parsed.port == 8000
                and parsed.path in ('', '/') and not parsed.query and not parsed.fragment and not parsed.username,
                'Only the isolated http://192.0.2.1:8000 API is permitted')
        self.base = self.manifest['base_url'].rstrip('/')
        self.profile = self.manifest['profile_id']
        require(self.profile == 'gateway-lab', 'Only the dedicated gateway-lab profile is permitted')
        self.fixture = self.manifest['fixture']
        self.network = self.fixture['network']
        require(self.network == {'interface': 'lan0', 'address': '192.0.2.2', 'lan_cidr': '192.0.2.0/24', 'upstream_gateway': '192.0.2.1'},
                'Unexpected lab gateway topology')
        workspace = Path(self.manifest['workspace']).resolve()
        require(Path('/tmp') in workspace.parents and workspace.name.startswith('configflow-gateway-lab-'), 'Unsafe fixture workspace')
        token_path = Path(self.manifest['admin_token_file'])
        require(not token_path.is_symlink() and token_path.resolve().parent == workspace, 'Unsafe token path')
        self.token = token_path.read_text().strip()
        self.controller_token_file = str(Path(self.manifest['controller_token_file']).resolve())
        require(Path(self.controller_token_file).parent == workspace, 'Unsafe controller token path')
        self.artifact = Path(self.manifest['agent_artifact'])
        require(not self.artifact.is_symlink() and Path('/tmp') in self.artifact.resolve().parents
                and self.artifact.name == 'configflow-agent-linux-arm64', 'Only the test ARM64 artifact may be fault-injected')
        self.network_script = Path(args.network_script or Path(__file__).with_name('linux_gateway_lab_network.sh')).resolve()
        require(self.network_script.name == 'linux_gateway_lab_network.sh' and self.network_script.is_file(), 'Network harness missing')
        if args.output:
            self.output = Path(args.output)
            require(not self.output.exists(), 'Output directory must be new')
            require(Path('/tmp') in self.output.resolve().parents and self.output.name.startswith('configflow-gateway-lab-'), 'Unsafe output path')
            self.output.mkdir(mode=0o700)
        else:
            self.output = Path(tempfile.mkdtemp(prefix='configflow-gateway-lab-run-', dir='/tmp'))
        self.timeout = args.timeout
        self.run_id = self.output.name.rsplit('-', 1)[-1]
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.events = []
        self.sequence = 0
        self.report = {'success': False, 'started_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                       'output': str(self.output), 'api_origin': self.base, 'cases': {}, 'events': self.events,
                       'platform': {'uname': os.uname().release, 'machine': os.uname().machine},
                       'limitations': ['This run covers Debian ARM64/systemd, not OpenRC or amd64.']}
        self.save_report()

    def save_report(self):
        private_json(self.output / 'report.json', self.report)

    def event(self, label, **detail):
        item = {'at': dt.datetime.now(dt.timezone.utc).isoformat(), 'label': label, **detail}
        self.events.append(redact(item)); self.save_report()
        print(json.dumps(redact(item), ensure_ascii=False), flush=True)

    def request(self, method, path, body=None, *, text=False):
        require(path.startswith('/api/') and not path.startswith('//'), 'API path must remain on fixture origin')
        self.sequence += 1
        request = urllib.request.Request(self.base + path, method=method,
            headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json', 'X-ConfigFlow-Profile': self.profile},
            data=None if body is None else json.dumps(body).encode())
        try:
            with self.opener.open(request, timeout=180) as response:
                payload = response.read(8 * 1024 * 1024 + 1)
                require(len(payload) <= 8 * 1024 * 1024, 'Oversized fixture response')
                status_code = response.status
        except urllib.error.HTTPError as error:
            payload = error.read(1024 * 1024)
            private_json(self.output / f'http-{self.sequence:03d}-error.json',
                         {'method': method, 'path': path, 'status': error.code, 'body': payload.decode(errors='replace')})
            raise LabFailure(f'{method} {path}: HTTP {error.code}: {redact(payload.decode(errors="replace"))}') from error
        if text:
            return payload.decode()
        value = json.loads(payload)
        private_json(self.output / f'http-{self.sequence:03d}.json', {'method': method, 'path': path, 'status': status_code, 'response': value})
        require(not isinstance(value, dict) or value.get('success') is not False, f'API operation failed: {redact(value)}')
        return value

    def process(self, name, arguments, *, expect_failure=False, interactive=False):
        logfile = self.output / (name + '.log')
        start = time.monotonic()
        if interactive:
            master, slave = pty.openpty()
            def child_setup():
                os.setsid()
                fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
            child = subprocess.Popen(arguments, stdin=slave, stdout=slave, stderr=slave, preexec_fn=child_setup)
            os.close(slave)
            chunks, answered = [], False
            try:
                while True:
                    if time.monotonic() - start > self.timeout:
                        raise LabFailure(f'{name} exceeded process timeout')
                    readable, _, _ = select.select([master], [], [], 0.25)
                    if readable:
                        try:
                            chunk = os.read(master, 65536)
                        except OSError as error:
                            if error.errno == errno.EIO: break
                            raise
                        if not chunk: break
                        chunks.append(chunk)
                        tail = b''.join(chunks[-4:]).decode(errors='replace')
                        if not answered and ('请输入选项' in tail or 'Enter your choice' in tail):
                            os.write(master, b'1\n'); answered = True
                    if child.poll() is not None and not readable: break
                child.wait(timeout=10)
                output = b''.join(chunks).decode(errors='replace')
                require(answered, 'Ordinary installer did not present its expected opt-in menu')
            finally:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGTERM); child.wait(timeout=10)
                os.close(master)
            code = child.returncode
        else:
            result = subprocess.run(arguments, capture_output=True, text=True, timeout=self.timeout)
            output, code = result.stdout + '\n' + result.stderr, result.returncode
        logfile.write_text(redact(output)); logfile.chmod(0o600)
        self.event(name, returncode=code, elapsed_seconds=round(time.monotonic() - start, 2), log=str(logfile))
        require((code != 0) if expect_failure else (code == 0), f'{name} exited {code}; see {logfile}: {redact(output[-1800:])}')
        return output

    def snapshot(self, label):
        label = self.run_id + '-' + label
        output = self.process('snapshot-' + label, [*ROOT, 'sh', str(self.network_script), 'snapshot', label])
        paths = json.loads(output.strip())
        for scope in ('gateway', 'root'):
            source = Path(paths[scope])
            shutil.copy2(source, self.output / (label + '.' + scope + '.json'))
        return label, json.loads(Path(paths['gateway']).read_text())

    def compare(self, first, second, scope='both'):
        self.process('compare-' + second, [*ROOT, 'sh', str(self.network_script), 'compare', first, second, scope])

    def assert_unmanaged(self, snapshot, *, no_journal=True):
        tables = [entry['table']['name'] for entry in snapshot['nft']['nftables'] if 'table' in entry]
        require('configflow_gateway' not in tables, 'ConfigFlow forwarding appeared without explicit apply')
        if no_journal:
            require(not JOURNAL.exists(), 'Gateway ownership journal appeared without explicit apply')

    def probe(self, mode, name, *, baseline=None):
        arguments = [*ROOT, 'sh', str(self.network_script), 'probe', mode, '--controller-token-file', self.controller_token_file]
        if baseline:
            arguments.extend(['--baseline', baseline])
        output = self.process(name, arguments)
        value = json.loads(output.strip())
        require(value.get('success') is True, 'Real network probe did not confirm success')
        private_json(self.output / (name + '.json'), value)
        return value

    def prepare(self, name):
        path = f'/api/profiles/{self.profile}/gateway-onboarding'
        current = self.request('GET', path)
        body = {**self.fixture['onboarding_body'], 'expected_config_revision': current['config_revision']}
        preview = self.request('POST', path + '/preview', body)
        require(preview.get('ready') is True, f'Onboarding preview blocked: {preview}')
        saved = self.request('POST', path, body)
        revision = saved['config_revision']
        body = {'profile_id': self.profile, 'name': name, 'server_url': self.base, 'network': self.network, 'config_revision': revision}
        session = self.request('POST', '/api/agents/gateway-installations', body)
        again = self.request('POST', '/api/agents/gateway-installations', body)
        require(again['id'] == session['id'], 'Identical create retried as a different session')
        require(session['phase'] == 'waiting' and session.get('command'), 'New installation did not return a command')
        return session

    def wait_session(self, session_id, desired, timeout=600):
        end, last = time.monotonic() + timeout, None
        while time.monotonic() < end:
            current = self.request('GET', '/api/agents/gateway-installations/' + session_id)
            summary = (current['phase'], tuple((kind, value.get('status')) for kind, value in current.get('deployments', {}).items()))
            if summary != last:
                self.event('session-progress', session_id=session_id, phase=current['phase'], deployments=current.get('deployments', {})); last = summary
            require(not current.get('config_changed') and not current.get('target_changed'), f'Session identity/configuration changed: {current}')
            if current['phase'] == 'publish_failed':
                raise LabFailure('Real publication failed: ' + current.get('message', ''))
            tasks = current.get('deployments', {})
            if tasks.get('oxidns', {}).get('status') not in (None, 'preparing'):
                require(tasks.get('mihomo', {}).get('status') == 'succeeded', 'OxiDNS started before Mihomo succeeded')
            if current['phase'] == desired:
                if desired in ('enrolled', 'published'):
                    agents = current.get('agents', {})
                    if set(agents) != set(SERVICES) or any(agent.get('status') != 'online' for agent in agents.values()):
                        time.sleep(2); continue
                return current
            time.sleep(2)
        raise LabFailure(f'Timed out waiting for {desired}; session {session_id}')

    def install(self, session, label, **kwargs):
        command = session['command']
        require(self.base + '/api/agents/gateway-installations/' + session['id'] + '/script?' in command,
                'Refusing command not tied to this isolated session')
        return self.process(label, [*GATEWAY, 'sh', '-c', command], **kwargs)

    def identities(self):
        result = {}
        for service in SERVICES:
            path = Path('/opt/configflow-agent') / service / 'config.json'
            data = json.loads(path.read_text())
            require(data.get('agent_id'), 'Installer has not persisted an Agent identity')
            result[service] = data['agent_id']
        return result

    def host(self, session, operation, body=None):
        agent = session['agents']['mihomo']
        require(agent['host'] == '192.0.2.2' and agent['profile_id'] == self.profile and agent['service_type'] == 'mihomo', 'Unexpected target')
        path = '/api/agents/' + agent['id'] + '/linux-gateway/' + operation + '?profile_id=' + self.profile
        return self.request('GET' if operation == 'status' else 'POST', path, body)

    def publish(self, session):
        self.request('POST', '/api/agents/gateway-installations/' + session['id'] + '/publish', {})
        completed = self.wait_session(session['id'], 'published')
        require(all(completed['deployments'][service]['status'] == 'succeeded' for service in SERVICES), 'Both cores must be healthy')
        return completed

    def forwarding_case(self, session, label):
        before, before_data = self.snapshot(label + '-published-before-apply')
        self.assert_unmanaged(before_data)
        status = self.host(session, 'status')
        require(status.get('enabled_in_config') is True and status.get('supported') is not False, f'Gateway status not ready: {status}')
        preview = self.host(session, 'preview', self.network)
        require(preview.get('ready') is True, f'Real gateway preview blocked: {preview}')
        checked, _ = self.snapshot(label + '-preview-only'); self.compare(before, checked)
        applied = self.host(session, 'apply', self.network)
        require(applied.get('managed', {}).get('active') is True, f'Gateway apply failed: {applied}')
        self.snapshot(label + '-applied')
        probes = self.probe('active', label + '-active-probes')
        removed = self.host(session, 'remove', {})
        require(removed.get('managed', {}).get('active') is not True, f'Gateway remove did not finish: {removed}')
        after, after_data = self.snapshot(label + '-removed'); self.assert_unmanaged(after_data); self.compare(before, after)
        removed_probes = self.probe('removed', label + '-removed-probes', baseline=before)
        # Record whether Mihomo itself changed forwarding before explicit apply.
        return {'success': True, 'pre_apply_ip_forward': before_data['sysctls']['net/ipv4/ip_forward'],
                'remove_restored_exact_pre_apply_network': True, 'probes': probes, 'removed_probes': removed_probes}

    def run(self):
        self.event('start', report=str(self.output / 'report.json'))
        original_core = metadata(CORE_FILES)
        require(all(value is None for value in original_core.values()), 'Fresh-install case requires an empty disposable core installation')
        original, original_data = self.snapshot('before-install'); self.assert_unmanaged(original_data)
        state = self.request('GET', f'/api/profiles/{self.profile}/gateway-onboarding')
        require(not state.get('gateway_enabled'), 'Fixture must begin with ordinary/default-off configuration')
        new_name = 'gateway-lab-' + self.run_id
        session = self.prepare(new_name)

        # Real checksum rejection, not a fake API failure: the command already
        # contains the original immutable SHA256, but its served test asset is bad.
        artifact_backup = self.output / 'agent-artifact.original'
        shutil.copy2(self.artifact, artifact_backup)
        try:
            self.artifact.write_bytes(b'ConfigFlow disposable lab checksum failure\n')
            output = self.install(session, 'checksum-failure', expect_failure=True)
            require('SHA256' in output or 'checksum' in output.lower(), 'Installer failed for a different reason than checksum validation')
        finally:
            shutil.copy2(artifact_backup, self.artifact)
        failed_snapshot, failed_data = self.snapshot('after-checksum-failure')
        self.assert_unmanaged(failed_data); self.compare(original, failed_snapshot)
        require(metadata(CORE_FILES) == original_core, 'Rejected download changed core files')
        self.report['cases']['checksum_failure_retry'] = {'failure_observed': True, 'no_core_or_network_changes_on_failure': True}

        self.install(session, 'fresh-install')
        enrolled = self.wait_session(session['id'], 'enrolled', 180)
        installed, installed_data = self.snapshot('installed-not-published')
        self.assert_unmanaged(installed_data); self.compare(original, installed)
        import yaml
        bootstrap = yaml.safe_load(Path('/etc/mihomo/config.yaml').read_text())
        require(bootstrap.get('tun', {}).get('enable') is False, 'Fresh installer unexpectedly enabled TUN')
        ids = self.identities()
        require(ids == {kind: agent['id'] for kind, agent in enrolled['agents'].items()}, 'Server enrollment differs from persisted local identities')
        self.report['cases']['fresh_install'] = {'success': True, 'agent_ids': ids, 'tun_enabled': False, 'network_unchanged_before_publish': True}
        self.report['cases']['checksum_failure_retry']['same_session_retry_succeeded'] = True
        self.save_report()

        # Execute the ordinary AIO install path with its real terminal menu.
        before_ordinary_files = metadata(CORE_FILES)
        query = urllib.parse.urlencode({'name': new_name, 'type': 'aio', 'agent_ip': '192.0.2.2', 'server_url': self.base,
             'mihomo_agent_port': 8080, 'oxidns_agent_port': 8082,
             'mihomo_config_path': '/etc/mihomo/config.yaml', 'oxidns_config_path': '/etc/oxidns/config.yaml',
             'mihomo_service_unit': 'mihomo', 'oxidns_service_unit': 'oxidns',
             'mihomo_service_binary': '/usr/local/bin/mihomo', 'oxidns_service_binary': '/usr/local/bin/oxidns'})
        ordinary_script = self.request('GET', '/api/agents/install-script?' + query, text=True)
        ordinary_path = self.output / 'ordinary-install.sh'; ordinary_path.write_text(ordinary_script); ordinary_path.chmod(0o600)
        self.process('ordinary-aio-install', [*GATEWAY, 'sh', str(ordinary_path)], interactive=True)
        ordinary, ordinary_data = self.snapshot('ordinary-install-finished')
        self.assert_unmanaged(ordinary_data); self.compare(installed, ordinary)
        require(metadata(CORE_FILES) == before_ordinary_files, 'Ordinary installer altered a core config/binary/unit')
        require(self.identities() == ids, 'Ordinary reinstall changed Agent IDs')
        ordinary_probes = self.probe('ordinary', 'ordinary-does-not-forward')
        self.report['cases']['ordinary_mode'] = {'success': True, 'network_and_core_files_unchanged': True, 'probes': ordinary_probes}

        published = self.publish(enrolled)
        self.report['cases']['new_gateway_forwarding'] = self.forwarding_case(published, 'new')
        self.save_report()

        # Reclassify only these disposable, installer-owned cores as external
        # standard-layout cores. No core config/binary/unit is rewritten.
        for service in SERVICES:
            marker = Path(f'/etc/{service}/.configflow-gateway-core-v1')
            require(marker.is_file() and not marker.is_symlink() and marker.read_text().strip() == 'ConfigFlow gateway core v1', 'Unexpected core ownership marker')
            shutil.move(marker, self.output / (service + '-original-ownership-marker'))
        existing_files = metadata(CORE_FILES)
        existing_before, _ = self.snapshot('existing-core-before-install')
        existing = self.prepare('gateway-lab-existing-' + self.run_id)
        self.install(existing, 'existing-core-attach')
        existing_enrolled = self.wait_session(existing['id'], 'enrolled', 180)
        existing_after, _ = self.snapshot('existing-core-installed'); self.compare(existing_before, existing_after)
        preserved_files = metadata(CORE_FILES)
        private_json(self.output / 'existing-core-before.json', existing_files)
        private_json(self.output / 'existing-core-after.json', preserved_files)
        require(preserved_files == existing_files, 'Existing core bytes or metadata changed during installation')
        require(self.identities() == ids and {kind: item['id'] for kind, item in existing_enrolled['agents'].items()} == ids,
                'Existing-core attachment did not preserve Agent identities across rename')
        self.report['cases']['existing_standard_core_attach'] = {'success': True, 'renamed_installation': True,
            'agent_ids_preserved': True, 'core_hashes_and_metadata_preserved_before_publish': True}
        existing_published = self.publish(existing_enrolled)
        self.report['cases']['existing_gateway_forwarding'] = self.forwarding_case(existing_published, 'existing')
        final, _ = self.snapshot('finished'); self.compare(original, final, 'root')
        self.report.update(success=True, finished_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                           root_namespace_unchanged=True, gateway_network_removed=True)
        self.event('complete', report=str(self.output / 'report.json'))
        return self.report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--network-script')
    parser.add_argument('--output')
    parser.add_argument('--timeout', type=int, default=1800, help='Maximum seconds per installation process')
    args = parser.parse_args()
    driver = None
    try:
        driver = Driver(args)
        driver.run()
    except (RuntimeError, OSError, ValueError, KeyError, subprocess.SubprocessError, urllib.error.URLError) as error:
        if driver:
            driver.report['failure'] = str(redact(str(error)))
            driver.report['failed_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
            driver.save_report()
            print('LAB FAILED: ' + str(error) + '\nReport: ' + str(driver.output / 'report.json'), file=sys.stderr)
        else:
            print('LAB REFUSED: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
