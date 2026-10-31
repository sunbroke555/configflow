#!/usr/bin/env python3
"""Real-RouterOS check of the cf-pd prefix guard in the owned PVE lab (VM161).

Runs the backend's own preview, plan, release and readback against the lab
home CHR through REST only. The manifest is kept in a local JSON file the way
the profile repository would keep it. Never point this at a production router.

    PD_LAB_REST=http://127.0.0.1:19161 PD_LAB_PASSWORD_FILE=~/.pdguard-rest.pw \
        python scripts/tests/ipv6_pd_guard_lab.py <scenario>...

Scenarios: reset, manual, rawcheck, borrowed, drift, update, withdraw, conflict, clearhook, stale, install, poll, status.
"""
import json
import os
import sys
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.routers import ipv6_pd_guard as pd  # noqa: E402
from backend.routers.plan import build_plan  # noqa: E402
from backend.routers.release import execute  # noqa: E402
from backend.routers.routeros import RouterOSDriver  # noqa: E402
from backend.routes import routers as api  # noqa: E402

REST = os.environ.get('PD_LAB_REST', 'http://127.0.0.1:19161')
assert REST.startswith('http://127.0.0.1:'), 'lab tunnel only'
PASSWORD = Path(os.path.expanduser(os.environ.get('PD_LAB_PASSWORD_FILE', '~/.pdguard-rest.pw'))).read_text().strip()
STATE = Path(os.path.expanduser('~/.pdguard-manifest.json'))
GATEWAY = '192.168.88.3'
ROLLBACK = ('/ipv6 dhcp-client set [find where interface="pppoe-out1" disabled=no script~"cf-pd-watch"] script=""\n'
            '/system scheduler remove [find name="cf-pd-poll"]\n/system script remove [find name="cf-pd-watch"]\n'
            '/ipv6 firewall address-list remove [find where list="cf-pd-last"]\n'
            '/ipv6 nd prefix remove [find where comment~"^cf-pd: deprecated"]\n'
            ':log warning "cf-pd: rolled back"\n/system script remove [find name="cf-pd-rollback"]\n')


def driver():
    value = RouterOSDriver(REST, 'admin', PASSWORD)
    assert value._get('/system/identity')['name'] == 'cf-pdg-home', 'not the lab router'
    return value


def router(enabled):
    state = json.loads(STATE.read_text()) if STATE.exists() else None
    return {'id': 'lab', 'type': 'routeros', 'endpoint': REST, 'proxy_gateway_agent_id': 'gw',
            'ipv6_pd_guard': {'enabled': enabled}, **({'ipv6_pd_guard_state': state} if state else {})}


def preview(enabled):
    agent = {'id': 'gw', 'host': GATEWAY, 'service_type': 'mihomo'}
    with mock.patch.object(api, '_agent', lambda agent_id: agent if agent_id else None), \
            mock.patch.object(api, 'compute_desired', lambda *_a, **_k: {
                'fake_ip_range': '198.18.0.0/16', 'proxy_gateway': GATEWAY, 'warnings': [], 'proxy_cidrs': []}), \
            mock.patch.object(api, '_return_cleanup', lambda *_: None):
        item = router(enabled)
        ros = driver()
        _, desired, state, report = api._inspect_state({'mihomo': {'custom_config': '{}'}}, item, ros)
        plan = build_plan(item, desired, state, report, report['return_routing'], ros)
    return ros, report, plan


def publish(enabled):
    ros, report, plan = preview(enabled)
    section = report.get('ipv6_pd_guard')
    stage = next((item for item in plan['stages'] if item['key'] == 'ipv6_pd_guard'), None)
    print(json.dumps({'report': section, 'plan_blockers': plan['blockers'], 'plan_warnings': plan['warnings'],
                      'stage_ops': [op['summary'] for op in (stage or {}).get('ops', [])],
                      'changes': [(c['method'], c['path'], c.get('id', 'new')) for op in (stage or {}).get('ops', [])
                                  for c in op['changes']]}, ensure_ascii=False, indent=1))
    result = execute(plan, ros, None)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    if hasattr(ros, 'ipv6_pd_guard_manifest'):
        if ros.ipv6_pd_guard_manifest:
            STATE.write_text(json.dumps(ros.ipv6_pd_guard_manifest))
        elif STATE.exists():
            STATE.unlink()
    return section, result


def rest(method, path, value=None):
    return driver()._request(method, path, value)


def status():
    clients = rest('GET', '/ipv6/dhcp-client')
    print(json.dumps({
        'scripts': [{k: v for k, v in row.items() if k != 'source'} for row in rest('GET', '/system/script')],
        'schedulers': rest('GET', '/system/scheduler'),
        'hooks': [(row['.id'], row.get('pool-name'), row.get('disabled'), row.get('script', '')) for row in clients],
        'last': rest('GET', '/ipv6/firewall/address-list?list=cf-pd-last'),
        'prefixes': [row for row in rest('GET', '/ipv6/nd/prefix') if row.get('dynamic') != 'true'],
        'manifest': json.loads(STATE.read_text()) if STATE.exists() else None}, ensure_ascii=False, indent=1))


def reset():
    for path in ('/system/scheduler', '/system/script'):
        for row in rest('GET', path):
            if row['name'].startswith('cf-pd'):
                rest('DELETE', f'{path}/{row[".id"]}')
    for row in rest('GET', '/ipv6/dhcp-client'):
        if row.get('script'):
            rest('PATCH', f'/ipv6/dhcp-client/{row[".id"]}', {'script': ''})
    for row in rest('GET', '/ipv6/firewall/address-list?list=cf-pd-last'):
        rest('DELETE', f'/ipv6/firewall/address-list/{row[".id"]}')
    for row in rest('GET', '/ipv6/nd/prefix'):
        if str(row.get('comment', '')).startswith('cf-pd'):
            rest('DELETE', f'/ipv6/nd/prefix/{row[".id"]}')
    if STATE.exists():
        STATE.unlink()


def manual(source=None, hook=pd.HOOK):
    """The production hand install, recreated object by object."""
    rest('PUT', '/system/script', {'name': 'cf-pd-watch', 'policy': 'read,write,test', 'comment': 'IPV6 前缀监控脚本',
                                   'source': source or pd.watch_source('lan4', 'ipv6pool')})
    rest('PUT', '/system/script', {'name': 'cf-pd-rollback', 'policy': 'read,write,test',
                                   'comment': 'IPV6 前缀监控移除脚本', 'source': ROLLBACK})
    rest('PUT', '/system/scheduler', {'name': 'cf-pd-poll', 'start-time': 'startup', 'interval': '30s',
                                      'on-event': '/system script run cf-pd-watch', 'policy': 'read,write,test',
                                      'comment': 'cf-pd'})
    client = next(row for row in rest('GET', '/ipv6/dhcp-client') if row.get('disabled') == 'false')
    rest('PATCH', f'/ipv6/dhcp-client/{client[".id"]}', {'script': hook})


def main(args):
    for name in args:
        print(f'### {name}')
        if name == 'reset':
            reset()
        elif name == 'manual':
            manual()
        elif name == 'drift':
            script = next(row for row in rest('GET', '/system/script') if row['name'] == 'cf-pd-watch')
            rest('PATCH', f'/system/script/{script[".id"]}', {'source': script['source'].replace('hold 3600', 'hold 1800')})
            client = next(row for row in rest('GET', '/ipv6/dhcp-client') if row.get('disabled') == 'false')
            rest('PATCH', f'/ipv6/dhcp-client/{client[".id"]}', {'script': '/system script run cf-pd-watch'})
        elif name == 'conflict':
            client = next(row for row in rest('GET', '/ipv6/dhcp-client') if row.get('disabled') == 'false')
            rest('PATCH', f'/ipv6/dhcp-client/{client[".id"]}', {'script': ':log info "my own hook"'})
        elif name == 'clearhook':
            client = next(row for row in rest('GET', '/ipv6/dhcp-client') if row.get('disabled') == 'false')
            rest('PATCH', f'/ipv6/dhcp-client/{client[".id"]}', {'script': ''})
        elif name in ('borrowed', 'update', 'install', 'enabled'):
            publish(True)
        elif name == 'stale':
            # Preview, then someone edits the hook before the release runs.
            ros, _, plan = preview(True)
            client = next(row for row in rest('GET', '/ipv6/dhcp-client') if row.get('disabled') == 'false')
            rest('PATCH', f'/ipv6/dhcp-client/{client[".id"]}', {'script': ':log info "edited meanwhile"'})
            print(json.dumps(execute(plan, ros, None), ensure_ascii=False, indent=1))
        elif name == 'withdraw':
            publish(False)
        elif name == 'rawcheck':
            # REST must store the multi-line source and the $"pd-valid" hook byte for byte.
            script = next(row for row in rest('GET', '/system/script') if row['name'] == 'cf-pd-watch')
            client = next(row for row in rest('GET', '/ipv6/dhcp-client') if row.get('disabled') == 'false')
            print(json.dumps({'source_exact': script['source'] == pd.watch_source('lan4', 'ipv6pool'),
                              'source_lines': script['source'].count('\n'),
                              'hook_exact': client.get('script') == pd.HOOK}))
        elif name == 'poll':
            time.sleep(35)
        elif name == 'status':
            status()
        else:
            raise SystemExit(f'unknown scenario {name}')


if __name__ == '__main__':
    main(sys.argv[1:])
