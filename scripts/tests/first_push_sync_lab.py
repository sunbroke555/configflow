#!/usr/bin/env python3
"""First push of a newly added subscription on the isolated PVE lab (CT152 Mihomo Agent).

Run on the test server only, from a checkout synced by rt. Reuses the
subscription-hosts lab harness (backup / private ConfigFlow and tunnels /
publish / restore) with its own work directory, and never touches other guests:

  backup        tar of CT152 /etc/mihomo + /opt/configflow-agent (deployment journal, cache.db), file manifest, state
  fixture-up    CT154 lab DNS/SOCKS fixture (runtime only), as in the subscription-hosts lab
  source V REF  unpack WORK/src-V.tar (git archive of REF, copied by the caller) into WORK/src-V
  ct152-prepare put back the rule file the CT152 deployment journal references (from the journal's own backup)
  run V         fresh private data and server from source V: push with the plain subscription until 已同步, then add
                the airport subscription (top-level hosts, empty node cache) through the API, push once and record
                the content check, the deployed hosts / policy / fake-ip-filter and the provider file; push a second
                time only when the first push was not 已同步
  restore       stop everything, put CT152 back from the backup and compare (journal, cache.db, file times)

Typical run: backup; source before <upstream ref>; source after <fix ref>; fixture-up; ct152-prepare;
run before; run after; restore. Fixture subscriptions use .invalid names and documentation addresses only.
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import subscription_hosts_lab as base  # noqa: E402

base.WORK = Path('/tmp/cf-firstpush-lab')
WORK = base.WORK
AIRPORT = {'id': base.SUB_ID, 'name': '实验机场', 'url': f'http://127.0.0.1:{base.SUB}/airport.yaml', 'enabled': True,
           'interval': 86400}
POLICY = 'proxy-server-nameserver-policy'

PLAIN_ONLY = r'''
import json, sys
from pathlib import Path
from backend.common import config as store
from backend.common.config_repository import ProfileRepository
store.set_repository(ProfileRepository(Path(sys.argv[1]) / 'data', default_config_factory=store.get_default_config,
                                       initial_config_factory=store.get_default_config))
repo = store.get_repository()
def profile(p):
    for group in p['proxy_groups']:
        group['subscriptions'] = [item for item in group.get('subscriptions', []) if item != 'lab-airport']
repo.update_profile_transaction(repo.default_profile_id(), profile)
repo.update_shared_transaction(lambda s: s.update(
    subscriptions=[item for item in s['subscriptions'] if item['id'] != 'lab-airport']))
print(json.dumps({'subscriptions': [item['id'] for item in repo.get_shared()['subscriptions']]}))
'''


def cache_file(sub_id):
    return WORK / 'data/shared/subscribes' / f'{sub_id}.json'


def status(label):
    """The Agent card's content check right after the push (polls while it is 「正在核对」)."""
    started = time.monotonic()
    base.agent_status(label)
    sync = json.loads((WORK / f'evidence/{label}-agent-status.json').read_text())
    return {'content_sync': (sync or {}).get('status'), 'seconds_after_push': round(time.monotonic() - started, 1)}


def deployed(label):
    """What CT152 runs now: hosts-derived sections of config.yaml and the airport provider's first node."""
    document = yaml.safe_load(base.guest(152, 'cat', '/etc/mihomo/config.yaml').stdout)
    dns = document.get('dns') or {}
    provider = base.guest(152, 'cat', f"/etc/mihomo/providers/{AIRPORT['name']}.yaml", check=False)
    first = None
    if provider.returncode == 0:
        proxies = (yaml.safe_load(provider.stdout) or {}).get('proxies') or []
        first = list(proxies[0]) if proxies else None
    result = {'hosts': document.get('hosts'), 'policy': dns.get(POLICY), 'fake_ip_filter': dns.get('fake-ip-filter'),
              'airport_provider_first_node_keys': first}
    base.save(f'evidence/{label}-deployed.json', result)
    return result


def push(label):
    base.publish(label)
    return {**status(label), **deployed(label)}


def run(variant):
    base.stop()
    if (WORK / 'data').exists():
        shutil.rmtree(WORK / 'data')
    # base.seed writes the data with src-before; the stored format is the same for both trees.
    base.seed()
    done = subprocess.run(base.python(variant) + ['-c', PLAIN_ONLY, str(WORK)], cwd=WORK / f'src-{variant}',
                          env=base.environment(), capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-2000:]
    base.serve(variant)
    base.sub('v1', 'plain-v1')
    result = {'variant': variant, 'baseline': []}
    # A synced device with the plain subscription only.
    for attempt in range(3):
        step = push(f'{variant}-baseline-{attempt + 1}')
        result['baseline'].append(step)
        if step['content_sync'] == 'synced':
            break
    assert result['baseline'][-1]['content_sync'] == 'synced', result
    # 订阅来源 → 新增, then add it to the group, exactly what the pages send.
    status_code, body = base.api('POST', '/subscriptions', AIRPORT)
    assert status_code == 200 and body.get('success'), body
    status_code, body = base.api('PUT', '/proxy-groups/lab-group', {'subscriptions': [base.SUB_ID, base.PLAIN_ID]})
    assert status_code == 200 and body.get('success'), body
    result['airport_cache_before_first_push'] = cache_file(base.SUB_ID).exists()
    result['first_push'] = push(f'{variant}-first')
    result['airport_cache_after_first_push'] = cache_file(base.SUB_ID).exists()
    if result['first_push']['content_sync'] != 'synced':
        result['second_push'] = push(f'{variant}-second')
    base.stop()
    base.save(f'evidence/{variant}-summary.json', result)
    print(json.dumps(result, ensure_ascii=False, indent=1))


def restore():
    base.restore()
    # Writing the TUN's stable_secret back switches it to addr_gen_mode 2; put the recorded mode back and compare again.
    key = 'net.ipv6.conf.Meta.addr_gen_mode'
    recorded = json.loads((WORK / 'backup/ct152-state.json').read_text())['sysctl'].get(key)
    if recorded is not None:
        base.guest(152, 'sysctl', '-w', f'{key}={recorded}', check=False)
        base.verify()


if __name__ == '__main__':
    base.lab.guard(guests=False)
    action, args = sys.argv[1], sys.argv[2:]
    if action != 'source':
        base.lab.guard()
    {'backup': base.backup, 'fixture-up': base.fixture_up, 'source': base.source, 'ct152-prepare': base.ct152_prepare,
     'run': run, 'restore': restore, 'verify': base.verify}[action](*args)
