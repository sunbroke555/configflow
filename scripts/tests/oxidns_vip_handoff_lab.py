#!/usr/bin/env python3
"""VIP handoff acceptance using only the snapshot-protected CT130/131/132 lab.

Run on 192.168.0.32, never on the Mac or PVE itself. The original HA-sync
fixture remains available with its unchanged defaults; this driver has its own
snapshot, private directory, services and HTTP tunnel. No production host is
part of GUESTS. Reports contain no Agent tokens or ConfigFlow credentials.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path
import time
import urllib.error
import urllib.request
import uuid

import oxidns_ha_sync_lab as lab

lab.ROOT = Path('/tmp/configflow-vip-handoff')
lab.GROOT = '/opt/cf-vip-handoff'
lab.SNAPSHOT = 'cf-vip-handoff-20261027'
lab.UNIT_PREFIX = 'cf-vip-handoff'
lab.SCRIPT = 'scripts/tests/oxidns_vip_handoff_lab.py'
lab.AGENT_BINARY = Path('/tmp/configflow-vip-handoff-agent')
lab.LOCAL_URL = 'http://127.0.0.1:19283'


def identities():
    private = json.loads((lab.ROOT / 'private.json').read_text())
    return {cid: private['members'][str(cid)]['id'] for cid in (130, 131)}


def agent_api(cid, method, path, body=None):
    """Reach the lab Agent from CT132; credentials travel on stdin, never argv."""
    if cid not in (130, 131) or not path.startswith('/api/'):
        raise ValueError('Not a laboratory Agent request')
    private = json.loads((lab.ROOT / 'private.json').read_text())
    token = private['members'][str(cid)]['token']
    script = '''import json,sys,urllib.error,urllib.request
v=json.load(sys.stdin)
r=urllib.request.Request(v['url'],method=v['method'],headers={'Content-Type':'application/json','Authorization':'Bearer '+v['token']},data=None if v['body'] is None else json.dumps(v['body']).encode())
try:
    with urllib.request.urlopen(r,timeout=30) as response: code,data=response.status,json.load(response)
except urllib.error.HTTPError as error: code,data=error.code,json.load(error)
print(json.dumps({'code':code,'data':data}))
'''
    value = {'url': 'http://' + lab.GUESTS[cid][1] + ':18081' + path,
             'token': token, 'method': method, 'body': body}
    return json.loads(lab.guest(132, 'python3', '-c', script, data=json.dumps(value).encode()))


def probe():
    """Keep the previous run's 100ms cadence / 300ms timeout for comparison."""
    destination = Path(lab.GROOT + '/dns-probe.jsonl')
    stop = Path(lab.GROOT + '/stop-probe')
    stop.unlink(missing_ok=True)
    with destination.open('w') as output, concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        while not stop.exists():
            started = time.time()
            values = list(pool.map(lab.dns_query, [lab.GUESTS[130][1], lab.GUESTS[131][1], lab.VIP]))
            output.write(json.dumps({'time': started, 'nodes': values}) + '\n')
            output.flush()
            time.sleep(max(0, .1 - (time.time() - started)))


def start_observation():
    for cid in (130, 131):
        lab.guest(cid, 'systemd-run', '--unit=' + lab.UNIT_PREFIX + '-addresses',
                  '/usr/sbin/ip', '-ts', 'monitor', 'address', 'dev', 'eth0.91')
    lab.guest(132, 'systemd-run', '--unit=' + lab.UNIT_PREFIX + '-probe',
              '/opt/cf-retest/venv/bin/python', lab.GROOT + '/source/' + lab.SCRIPT, 'probe')


def read_samples():
    raw = lab.guest(132, 'cat', lab.GROOT + '/dns-probe.jsonl')
    return raw, [json.loads(line) for line in raw.splitlines()]


def journal_events(started):
    """Capture service lifecycle and VIP address events, not arbitrary logs."""
    result = lab.service_events(started)
    for cid in (130, 131):
        raw = lab.guest(cid, 'journalctl', '-u', 'keepalived', '-u', lab.UNIT_PREFIX + '-addresses',
                        '--since', '@' + str(started), '-o', 'json', '--no-pager')
        for line in raw.splitlines():
            value = json.loads(line)
            message = str(value.get('MESSAGE', ''))
            if not any(text in message for text in ('MASTER STATE', 'BACKUP STATE', 'FAULT STATE', lab.VIP)):
                continue
            result[str(cid)].append({'time': int(value['__REALTIME_TIMESTAMP']) / 1e6,
                                     'message': message, 'unit': value.get('_SYSTEMD_UNIT', '')})
        result[str(cid)].sort(key=lambda value: value['time'])
    return result


def source_hashes():
    paths = ('backend/agents/oxidns_ha_publish.py', 'backend/agents/oxidns_ha.py',
             'backend/agents/manager.py', 'backend/agents/oxidns_ha_handoff.py',
             'backend/agents/go-agent/routes/oxidns_ha.go', 'backend/agents/go-agent/routes/oxidns_ha_handoff.go',
             'backend/agents/go-agent/routes/oxidns_ha_check.go',
             'scripts/tests/oxidns_ha_sync_lab.py', 'scripts/tests/oxidns_vip_handoff_lab.py')
    return {path: hashlib.sha256((lab.REPO / path).read_bytes()).hexdigest() for path in paths}


def current_holder(ids, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, state = lab.api('GET', f'/api/agents/{ids[130]}/oxidns-ha')
        holders = [member['agent_id'] for member in state.get('members', [])
                   if (member.get('runtime') or {}).get('vip_present')]
        if len(holders) == 1:
            return next(cid for cid, ident in ids.items() if ident == holders[0])
        time.sleep(.2)
    raise AssertionError('No unique VIP holder')


def wait_holder(ids, expected, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if current_holder(ids) == expected:
            return
        time.sleep(.2)
    raise AssertionError('Expected VIP holder was not observed')


def collect_phase(name, started, **details):
    _, samples = read_samples()
    samples = [sample for sample in samples if started <= sample['time'] <= time.time()]
    events = journal_events(started)
    lab.assert_serial(events)
    return {'name': name, 'started_at': started, 'finished_at': time.time(),
            'service_events': events, 'dns_probe': {'samples': len(samples),
            'vip_failed_samples': sum(not sample['nodes'][2]['ok'] for sample in samples),
            'both_nodes_unavailable_samples': sum(not sample['nodes'][0]['ok'] and not sample['nodes'][1]['ok'] for sample in samples)},
            **details}


def prove_handoff(item, source, target, address):
    stops = [event['time'] for event in item['service_events'][str(source)] if event['message'].startswith('Stopping oxidns')]
    starts = [event['time'] for event in item['service_events'][str(target)] if event['message'].startswith('Started oxidns')]
    assert len(stops) == 1 and starts, 'Expected one old-holder restart and an updated standby start'
    _, samples = read_samples()
    ready = [sample['time'] for sample in samples if item['started_at'] <= sample['time'] < stops[0]
             and sample['nodes'][2].get('ok') and sample['nodes'][2].get('ip') == address]
    assert ready, 'VIP did not answer using the updated standby before old-holder stop'
    assert starts[0] < ready[0] < stops[0], 'Service / VIP DNS / old-holder restart order violated'
    item['handoff_proof'] = {'old_holder': source, 'updated_standby': target,
                            'standby_started_at': starts[0], 'updated_vip_dns_at': ready[0],
                            'old_holder_stopping_at': stops[0], 'vip_ready_lead_seconds': round(stops[0] - ready[0], 6)}


def test():
    lab.guard(); lab.start_tunnel()
    ids = identities()
    report = {'started_at': time.time(), 'passed': False, 'source_sha256': source_hashes(),
              'agent_binary_sha256': hashlib.sha256(lab.AGENT_BINARY.read_bytes()).hexdigest(),
              'scope': {'containers': list(lab.GUESTS), 'network': 'isolated cfgt VLAN91', 'vip': lab.VIP},
              'scenarios': [], 'probe_comparison': {'previous_report': 'oxidns-ha-sync-2026-10-27.json',
              'previous_normal_publication_failed_samples': 2, 'target_interval_ms': 100, 'timeout_ms': 300}}
    observing = False
    def save(item):
        report['scenarios'].append(item)
        lab.private(lab.ROOT / 'report.json', report)
    def publish_case(name, requester, address, workers):
        source = current_holder(ids); target = 131 if source == 130 else 130
        started = time.time()
        lab.set_draft(address, workers=workers, rule=f'full:{name}.invalid\n')
        code, _ = lab.publish(ids[requester])
        complete = lab.wait_ha(ids[requester])
        item = collect_phase(name, started, request_member=requester, http_status=code,
                             publication=complete['publication'], files=lab.matching_files(), answers=lab.dns_snapshot())
        prove_handoff(item, source, target, address)
        assert all(answer.get('ip') == address for answer in item['answers'].values())
        save(item)
    try:
        lab.set_draft('198.51.100.20', workers=2)
        lab.api('PUT', f'/api/agents/{ids[130]}/oxidns-ha', {'peer_id': ids[131], 'vip': lab.VIP})
        initial = lab.wait_ha(ids[130])
        assert initial['config']['enabled'] is True
        report['initial_publication'] = initial['publication']
        report['initial_files'] = lab.matching_files()
        for cid in (130, 131):
            assert 1 in (agent_api(cid, 'GET', '/api/capabilities')['data'].get('oxidns_ha_handoff_protocols') or [])
        start_observation(); observing = True
        time.sleep(1)
        publish_case('publish-from-primary', 130, '198.51.100.30', 3)
        publish_case('publish-from-secondary', 131, '198.51.100.40', 4)

        # The original primary fails; publish while the other node really holds VIP.
        original = current_holder(ids); replacement = 131 if original == 130 else 130
        lab.guest(original, 'systemctl', 'stop', 'oxidns')
        wait_holder(ids, replacement)
        assert lab.dns_snapshot()[lab.VIP].get('ok') is True
        publish_case('publish-after-real-holder-failure-with-roles-reversed', replacement, '198.51.100.45', 5)

        source = current_holder(ids); target = 131 if source == 130 else 130
        before = lab.snapshot(source); started = time.time()
        # Pause only the target's election process; its DNS and Agent stay alive.
        lab.guest(target, 'systemctl', 'kill', '--kill-whom=all', '--signal=STOP', 'keepalived')
        try:
            lab.set_draft('198.51.100.50', workers=6, rule='full:handoff-failure.invalid\n')
            lab.publish(ids[source]); failed = lab.wait_ha(ids[source], 'failed', timeout=100)
            assert lab.snapshot(source) == before, 'Failed handoff changed the old holder'
            item = collect_phase('failed-vip-migration-leaves-old-holder-untouched', started,
                                 old_holder=source, before=before, after=lab.snapshot(source), publication=failed['publication'])
            assert not any(event['message'].startswith('Stopping oxidns') for event in item['service_events'][str(source)])
            assert agent_api(source, 'GET', '/api/oxidns-ha/handoff')['data'].get('handoff') is None
            save(item)
        finally:
            lab.guest(target, 'systemctl', 'kill', '--kill-whom=all', '--signal=CONT', 'keepalived', check=False)
        publish_case('retry-after-handoff-failure', 131, '198.51.100.55', 7)

        source = current_holder(ids); target = 131 if source == 130 else 130
        before = {cid: lab.snapshot(cid) for cid in (130, 131)}; started = time.time()
        lab.set_draft('198.51.100.58', workers=9, rule='full:staged-handoff.invalid\n')
        lab.publish(ids[source], restart=False)
        ready = lab.wait_ha(ids[source], 'ready')
        assert {cid: lab.snapshot(cid) for cid in (130, 131)} == before, 'Staging changed live files or processes'
        assert current_holder(ids) == source, 'Staging moved VIP before activation'
        child = next(member for member in ready['publication']['members'] if member['agent_id'] == ids[target])
        lab.api('POST', f'/api/agents/{ids[target]}/deployments/{child["deployment_id"]}/activate', {})
        complete = lab.wait_ha(ids[target])
        item = collect_phase('staged-pair-activation-from-secondary-performs-handoff', started,
                             ready_publication=ready['publication'], publication=complete['publication'], files=lab.matching_files())
        prove_handoff(item, source, target, '198.51.100.58'); save(item)

        # Interrupt the coordinator and restart the giving-way Agent while the
        # persisted handoff record is active, then resume the same publication.
        source = current_holder(ids); target = 131 if source == 130 else 130
        started = time.time()
        lab.set_draft('198.51.100.60', workers=10, rule='full:restart-recovery.invalid\n')
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            accepted = pool.submit(lab.publish, ids[source], allow_error=True)
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                record = agent_api(source, 'GET', '/api/oxidns-ha/handoff')['data'].get('handoff')
                if record: break
                time.sleep(.1)
            else: raise AssertionError('Did not observe an in-flight handoff')
            lab.guest(132, 'systemctl', 'stop', lab.UNIT_PREFIX + '-backend')
            try: first_code = accepted.result()[0]
            except (OSError, urllib.error.URLError): first_code = 'connection-closed-during-stop'
        try:
            lab.guest(source, 'systemctl', 'restart', lab.UNIT_PREFIX + '-agent')
            time.sleep(2)
            recovered = agent_api(source, 'GET', '/api/oxidns-ha/handoff')['data'].get('handoff')
            assert recovered and recovered['operation_id'] == record['operation_id'], 'Agent restart lost the handoff identity'
            time.sleep(5)
            assert lab.dns_snapshot()[lab.VIP].get('ip') == '198.51.100.60'
        finally:
            lab.guest(132, 'systemctl', 'start', lab.UNIT_PREFIX + '-backend')
        time.sleep(2)
        complete = lab.wait_ha(ids[source])
        item = collect_phase('configflow-and-agent-restarts-resume-same-handoff', started,
                             initial_http_status=first_code, handoff_before=record, handoff_after_agent_restart=recovered,
                             publication=complete['publication'], files=lab.matching_files())
        prove_handoff(item, source, target, '198.51.100.60')
        for events in item['service_events'].values():
            assert sum(event['message'].startswith('Stopping oxidns') for event in events) <= 1
        save(item)
        source = current_holder(ids); target = 131 if source == 130 else 130
        started = time.time(); deployment_id = uuid.uuid4().hex
        lab.put(target, '/run/systemd/system/oxidns.service.d/vip-handoff-delay.conf', '[Service]\nExecStartPre=/bin/sleep 5\n')
        lab.guest(target, 'systemctl', 'daemon-reload')
        try:
            lab.set_draft('198.51.100.65', workers=11, rule='full:concurrent-handoff.invalid\n')
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                first = pool.submit(lab.publish, ids[source], deployment_id, allow_error=True)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    _, state = lab.api('GET', f'/api/agents/{ids[source]}/oxidns-ha')
                    job = state.get('publication') or {}
                    if job.get('owner_deployment_id') == deployment_id and job.get('status') not in lab.TERMINAL: break
                    time.sleep(.1)
                else: raise AssertionError('Concurrent publication did not become observable')
                duplicate = pool.submit(lab.publish, ids[source], deployment_id, allow_error=True)
                competing = pool.submit(lab.publish, ids[target], uuid.uuid4().hex, allow_error=True)
                codes = [result.result()[0] for result in (first, duplicate, competing)]
            assert codes[0] < 300 and codes[1] < 300 and codes[2] == 409, codes
            complete = lab.wait_ha(ids[source])
            item = collect_phase('duplicate-and-competing-requests-do-not-repeat-handoff', started,
                                 http_statuses=codes, publication=complete['publication'], files=lab.matching_files())
            prove_handoff(item, source, target, '198.51.100.65')
            for events in item['service_events'].values():
                assert sum(event['message'].startswith('Stopping oxidns') for event in events) <= 1
            save(item)
        finally:
            lab.guest(target, 'rm', '-f', '/run/systemd/system/oxidns.service.d/vip-handoff-delay.conf')
            lab.guest(target, 'systemctl', 'daemon-reload')

        source = current_holder(ids); target = 131 if source == 130 else 130
        started = time.time(); operation = 'lab-peer-failure-' + uuid.uuid4().hex
        request = {'operation_id': operation}
        try:
            assert agent_api(source, 'POST', '/api/oxidns-ha/handoff', request)['code'] < 300
            wait_holder(ids, target)
            before_failure = agent_api(source, 'GET', '/api/oxidns-ha/handoff')['data']['handoff']
            assert before_failure['yielding'] is True
            lab.guest(target, 'systemctl', 'stop', 'oxidns')
            wait_holder(ids, source)
            failed_peer = agent_api(source, 'GET', '/api/oxidns-ha/handoff')['data']['handoff']
            assert failed_peer['peer_healthy'] is False and failed_peer['yielding'] is False
            assert lab.dns_snapshot()[lab.VIP].get('ip') == '198.51.100.65'
            lab.guest(target, 'systemctl', 'start', 'oxidns')
            wait_holder(ids, target)
            recovered_peer = agent_api(source, 'GET', '/api/oxidns-ha/handoff')['data']['handoff']
            assert recovered_peer['operation_id'] == operation and recovered_peer['yielding'] is True
        finally:
            lab.guest(target, 'systemctl', 'start', 'oxidns', check=False)
            agent_api(source, 'DELETE', '/api/oxidns-ha/handoff', request)
        wait_holder(ids, source)
        save(collect_phase('peer-dns-failure-overrides-persistent-yield-and-recovers', started,
                           handoff_before=before_failure, handoff_during_peer_failure=failed_peer,
                           handoff_after_peer_recovery=recovered_peer, answers=lab.dns_snapshot()))
        report['passed'] = True
    except Exception as error:
        report['failure'] = str(error)
        raise
    finally:
        for cid in (130, 131):
            lab.guest(cid, 'systemctl', 'kill', '--kill-whom=all', '--signal=CONT', 'keepalived', check=False)
            lab.guest(cid, 'systemctl', 'start', 'oxidns', check=False)
        if observing:
            lab.guest(132, 'touch', lab.GROOT + '/stop-probe'); time.sleep(.5)
            raw, samples = read_samples()
            (lab.ROOT / 'dns-probe.jsonl').write_bytes(raw)
            report['dns_probe'] = {'sample_count': len(samples), 'sha256': hashlib.sha256(raw).hexdigest(),
                'both_nodes_unavailable_samples': sum(not sample['nodes'][0]['ok'] and not sample['nodes'][1]['ok'] for sample in samples)}
        report['finished_at'] = time.time()
        lab.private(lab.ROOT / 'report.json', report)
        print(json.dumps({'passed': report['passed'], 'scenarios': len(report['scenarios']), 'report': str(lab.ROOT / 'report.json')}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'update-source', 'server', 'probe', 'cleanup', 'reset', 'test'])
    command = parser.parse_args().command
    if command == 'prepare': lab.prepare()
    elif command == 'server': lab.server()
    elif command == 'probe': probe()
    elif command == 'test': test()
    elif command == 'cleanup': lab.cleanup()
    elif command == 'reset': lab.cleanup(reset=True)
    elif command == 'update-source':
        lab.guard(); lab.stage_source()
        lab.guest(132, 'systemctl', 'restart', lab.UNIT_PREFIX + '-backend')
        lab.start_tunnel()


if __name__ == '__main__':
    main()
