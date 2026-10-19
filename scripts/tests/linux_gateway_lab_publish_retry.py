#!/usr/bin/env python3
"""Real failed-publication/rollback/retry case for the marked disposable VM.

Run only after the main driver leaves both cores healthy and forwarding removed:
  ip netns exec cfgl-router /opt/configflow-lab/venv/bin/python \
    scripts/tests/linux_gateway_lab_publish_retry.py \
    --manifest /tmp/configflow-gateway-lab-server/manifest.json

Uses the real installer, systemd, Agent deployment journals and server APIs. A
temporary OxiDNS ExecStartPre exits once, then permits the Agent's actual rollback
to restart the old core. No network configuration is applied or removed. The
failure drop-in is removed in finally, including when an assertion fails.
"""
from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request

from linux_gateway_lab_driver import Driver, LabFailure, CORE_FILES, ROOT, metadata, private_json, redact, require
from linux_gateway_lab_support import dns_a


def write_private(path, text, mode=0o600):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write(text)


class PublishRetryDriver(Driver):
    def systemctl(self, *arguments):
        return subprocess.run([*ROOT, 'systemctl', *arguments], check=True,
                              capture_output=True, text=True, timeout=45).stdout.strip()

    def healthy(self, label):
        processes = {}
        for service in ('mihomo', 'oxidns'):
            require(self.systemctl('is-active', service) == 'active', service + ' is not active')
            processes[service] = int(self.systemctl('show', '--property=MainPID', '--value', service))
            require(processes[service] > 0, service + ' has no live core process')
        answers = dns_a(self.fixture['targets']['proxy_domain'], '192.0.2.2')
        require(answers and all(ipaddress.ip_address(item) in ipaddress.ip_network('198.18.0.0/16') for item in answers),
                'OxiDNS did not resolve the proxy fixture through healthy Mihomo fake DNS')
        self.event(label, processes=processes, dns_answers=answers)
        return processes

    def wait_failed(self, session_id):
        deadline, last = time.monotonic() + 240, None
        while time.monotonic() < deadline:
            session = self.request('GET', '/api/agents/gateway-installations/' + session_id)
            require(not session.get('config_changed') and not session.get('target_changed'), 'Session changed during fault injection')
            summary = (session['phase'], tuple((key, item.get('status')) for key, item in session['deployments'].items()))
            if summary != last:
                self.event('fault-publication-progress', session_id=session_id, phase=session['phase'], deployments=session['deployments'])
                last = summary
            if session['phase'] == 'publish_failed':
                return session
            require(session['phase'] != 'published', 'Fault injection did not make the real publication fail')
            time.sleep(1)
        raise LabFailure('Timed out waiting for the real OxiDNS start failure and rollback')

    def expect_retry_confirmation(self, session_id):
        path = '/api/agents/gateway-installations/' + session_id + '/publish'
        request = urllib.request.Request(self.base + path, method='POST', data=b'{}',
            headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=20) as response:
                raise LabFailure(f'Failed session resumed without retry_failed=true: HTTP {response.status}')
        except urllib.error.HTTPError as error:
            require(error.code == 409, 'Failed retry needs an explicit 409 confirmation boundary')
            private_json(self.output / 'retry-confirmation.json', {'status': error.code, 'response': json.load(error)})

    def managed_oxidns_files(self):
        root = Path('/etc/oxidns')
        active = root / '.configflow-deployments/active-manifest.json'
        require(active.is_file() and not active.is_symlink(), 'A prior healthy OxiDNS deployment is required')
        document = json.loads(active.read_text())
        paths = [active, root / 'config.yaml']
        for item in document['files']:
            path = root / item['path']
            require(root in path.resolve().parents, 'Unexpected managed OxiDNS path')
            paths.append(path)
        return tuple(dict.fromkeys(paths))

    def run(self):
        self.event('publish-retry-start', report=str(self.output / 'report.json'))
        require(all(item is not None for item in metadata(CORE_FILES).values()), 'Both standard-layout cores must already exist')
        before_network, before_network_data = self.snapshot('retry-before')
        self.assert_unmanaged(before_network_data)
        self.healthy('baseline-cores-healthy')
        identities = self.identities()

        # A legitimate runtime setting change requires a restart even when the
        # installed OxiDNS supports reload; the candidate remains fully valid.
        import yaml
        live_config = yaml.safe_load(Path('/etc/oxidns/config.yaml').read_text())
        settings = self.request('GET', '/api/oxidns/settings')
        require(settings.get('config_mode') == 'template', 'This fixture expects template-mode OxiDNS')
        settings['log']['level'] = 'debug' if live_config.get('log', {}).get('level') != 'debug' else 'info'
        self.request('POST', '/api/oxidns/settings', settings)
        session = self.prepare('gateway-lab-retry-' + self.run_id)
        before_install = metadata(CORE_FILES)
        self.install(session, 'retry-existing-core-attach')
        enrolled = self.wait_session(session['id'], 'enrolled', 180)
        require(self.identities() == identities, 'Reattachment changed existing Agent identities')
        require(metadata(CORE_FILES) == before_install, 'Existing-core attachment changed core files')

        rollback_paths = self.managed_oxidns_files()
        original_files = metadata(rollback_paths)
        # Use a private, uniquely named run-time drop-in. Never rewrite a core
        # executable, unit, config, or another harness's namespace drop-in.
        require(re.fullmatch(r'[A-Za-z0-9_-]+', self.run_id), 'Unsafe run identifier')
        require(re.fullmatch(r'/tmp/configflow-gateway-lab-[A-Za-z0-9_-]+', str(self.output)),
                'The fault-injection output directory must have a simple absolute lab path')
        dropin_directory = Path('/run/systemd/system/oxidns.service.d')
        require(not dropin_directory.is_symlink(), 'Unexpected OxiDNS drop-in symlink')
        dropin_directory.mkdir(parents=True, exist_ok=True)
        dropin = dropin_directory / ('95-configflow-lab-retry-' + self.run_id + '.conf')
        hook = self.output / 'fail-next-oxidns-start.sh'
        sentinel = self.output / 'fail-next-oxidns-start'
        consumed = self.output / 'failure-was-injected'
        write_private(sentinel, 'Only the next OxiDNS activation should fail.\n')
        write_private(hook, '#!/bin/sh\nset -eu\nif [ -f ' + shlex.quote(str(sentinel)) + ' ]; then\n'
                      '  rm -- ' + shlex.quote(str(sentinel)) + '\n'
                      '  printf "%s\\n" "actual systemd ExecStartPre failure" > ' + shlex.quote(str(consumed)) + '\n'
                      '  exit 42\nfi\n', 0o700)
        write_private(dropin, '[Service]\nRestart=no\nExecStartPre=/bin/sh ' + str(hook) + '\n')
        try:
            self.systemctl('daemon-reload')
            require(self.systemctl('show', '--property=Restart', '--value', 'oxidns') == 'no', 'Fault isolation drop-in was not loaded')
            self.request('POST', '/api/agents/gateway-installations/' + session['id'] + '/publish', {})
            failed = self.wait_failed(session['id'])
            require(consumed.is_file() and not sentinel.exists(), 'The real one-shot systemd failure did not execute')
            require(failed['deployments']['mihomo']['status'] == 'succeeded', 'The first core did not finish successfully')
            require(failed['deployments']['oxidns']['status'] == 'rolled_back', 'OxiDNS did not automatically restore its healthy predecessor')
            failed_id = failed['deployments']['oxidns']['deployment_id']
            require(re.fullmatch(r'[a-f0-9]{32}', failed_id), 'Unexpected deployment journal ID')
            journal_path = Path('/etc/oxidns/.configflow-deployments') / failed_id / 'state.json'
            journal = json.loads(journal_path.read_text())
            private_json(self.output / 'oxidns-failed-journal.json', journal)
            require(journal['status'] == 'rolled_back' and journal.get('failed_stage') == 'starting'
                    and journal.get('backup_complete') and journal.get('was_running') and not journal.get('rollback_error'),
                    'Agent journal did not prove an actual start failure followed by complete rollback')
            restored_files = metadata(rollback_paths)
            for path, original in original_files.items():
                restored = restored_files[path]
                require(restored is not None and all(restored[key] == original[key] for key in ('sha256', 'size', 'uid', 'gid', 'mode')),
                        'Rollback failed to restore original managed bytes/permissions: ' + path)
            self.healthy('automatic-rollback-healthy')
            self.expect_retry_confirmation(session['id'])
            unchanged = self.request('GET', '/api/agents/gateway-installations/' + session['id'])
            require(unchanged['deployments'] == failed['deployments'], 'Rejected unconfirmed retry changed task IDs')
        finally:
            # This runs even if publication, assertions or transport fail. The
            # one-shot condition cannot survive the test to surprise later work.
            dropin.unlink(missing_ok=True)
            sentinel.unlink(missing_ok=True)
            self.systemctl('daemon-reload')
            self.systemctl('reset-failed', 'oxidns')
            # start is a no-op for an already healthy rollback, not a restart.
            self.systemctl('start', 'oxidns')

        processes = self.healthy('failure-condition-removed')
        mihomo_paths = (Path('/etc/mihomo/config.yaml'), Path('/etc/mihomo/.configflow-deployments/active-manifest.json'))
        stable_mihomo = metadata(mihomo_paths)
        first_id = failed['deployments']['mihomo']['deployment_id']
        agent_before = self.request('GET', '/api/agents/' + enrolled['agents']['mihomo']['id'])
        self.request('POST', '/api/agents/gateway-installations/' + session['id'] + '/publish', {'retry_failed': True})
        completed = self.wait_session(session['id'], 'published', 240)
        require(completed['deployments']['mihomo']['deployment_id'] == first_id, 'Retry created a new Mihomo deployment')
        require(completed['deployments']['oxidns']['deployment_id'] != failed_id, 'Failed OxiDNS task did not receive a new retry ID')
        require(metadata(mihomo_paths) == stable_mihomo, 'Retry rewrote the already successful Mihomo files')
        final_processes = self.healthy('same-session-retry-healthy')
        require(final_processes['mihomo'] == processes['mihomo'], 'Retry restarted the already successful Mihomo process')
        agent_after = self.request('GET', '/api/agents/' + enrolled['agents']['mihomo']['id'])
        require(agent_after['deployments'] == agent_before['deployments'], 'Retry added or changed a Mihomo task')
        after_network, after_network_data = self.snapshot('retry-after')
        self.assert_unmanaged(after_network_data)
        self.compare(before_network, after_network, 'root')
        self.report['cases']['publication_failure_and_retry'] = {
            'success': True, 'session_id': session['id'], 'real_systemd_failure': True,
            'old_oxidns_files_and_health_restored': True, 'requires_explicit_retry': True,
            'same_session_retry': True, 'mihomo_task_and_process_and_files_preserved': True,
            'failed_oxidns_deployment_id': failed_id,
            'retry_oxidns_deployment_id': completed['deployments']['oxidns']['deployment_id'],
            'mihomo_deployment_id': first_id, 'forwarding_remains_explicit': True}
        self.report.update(success=True, finished_at=dt.datetime.now(dt.timezone.utc).isoformat())
        self.event('publish-retry-complete', report=str(self.output / 'report.json'))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--network-script')
    parser.add_argument('--output')
    parser.add_argument('--timeout', type=int, default=1800)
    args = parser.parse_args()
    driver = None
    try:
        driver = PublishRetryDriver(args)
        driver.run()
    except (LabFailure, OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError, urllib.error.URLError) as error:
        if driver:
            driver.report.update(failure=str(redact(str(error))), failed_at=dt.datetime.now(dt.timezone.utc).isoformat())
            driver.save_report()
            print('LAB RETRY FAILED: ' + str(error) + '\nReport: ' + str(driver.output / 'report.json'), file=sys.stderr)
        else:
            print('LAB RETRY REFUSED: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
