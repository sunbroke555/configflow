#!/usr/bin/env python3
"""IPv6 rule-set routes against the real VM151 RouterOS, driven from the test server.

Runs this checkout's backend in-process on a private copy of the IPv6 preview
data: the preview service, its data and the Mihomo/Agent guests stay untouched
(no configuration is pushed to CT152). Only /ipv6/route rows commented
``cf-rule6:<router>`` are written, plus the lab's own 198.18.0.0/16 comment,
which is restored. Public Telegram prefixes are routed only inside the lab's
port-less bridges.

    rt -- ~/cf-test/.venv/bin/python scripts/tests/ipv6_ruleset_routes_lab.py
"""
import base64
import json
import os
import re
from pathlib import Path
import shutil
import sys
import threading
import time
import urllib.request

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ipv6_readiness_lab as lab  # noqa: E402

SOURCE = Path(__file__).resolve().parents[2]
PREVIEW = Path('/home/sunbroke/previews/configflow-ipv6-readiness')
WORK = Path('/tmp/configflow-ipv6-ruleset-lab')
ROUTER_ID = 'router_d10a2f2c9442'
COMMENT = 'cf-rule6:' + ROUTER_ID
GATEWAY = 'fe80::cf:15ff:fe00:52%ether2'
V6 = ['2001:b28:f23d::/48', '2001:b28:f23f::/48', '2001:67c:4e8::/48', '2a0a:f280::/32', '2001:b28:f242::/48']
NEW6 = ['2001:b28:f23d::/48', '2001:b28:f23c::/48', '2001:b28:f240::/48', '2001:b28:f244::/48', '2001:b28:f246::/48']
PROBE = '2001:b28:f23d::1'


def content(cidrs):
    return '\n'.join(['DOMAIN-SUFFIX,t.me'] + ['IP-CIDR6,' + cidr for cidr in cidrs]) + '\n'


def ros_client():
    credentials = json.loads((lab.ROOT / 'private.json').read_text())
    authorization = 'Basic ' + base64.b64encode(('admin:' + credentials['router_password']).encode()).decode()

    def ros(method, path, values=None):
        assert method == 'GET' or path.startswith(('/ipv6/route', '/ip/route')), path
        request = urllib.request.Request('http://127.0.0.1:19151/rest' + path, method=method,
                                         headers={'Authorization': authorization, 'Content-Type': 'application/json'},
                                         data=json.dumps(values).encode() if values is not None else None)
        with urllib.request.urlopen(request, timeout=15) as reply:
            return json.load(reply) if reply.status != 204 else None
    return ros


FIELDS = ('.id', 'dst-address', 'gateway', 'routing-table', 'distance', 'disabled', 'comment')


def fixed(rows, keys=FIELDS):
    return sorted(tuple(str(row.get(key, '')) for key in keys) for row in rows if row.get('dynamic') != 'true')


def baseline(ros):
    """Everything another agent could be changing on VM151, as comparable tuples."""
    rule = ('.id', 'chain', 'action', 'comment', 'disabled', 'protocol', 'in-interface', 'out-interface',
            'src-address', 'dst-address')
    return {'ipv6_routes': fixed(ros('GET', '/ipv6/route')), 'ip_routes': fixed(ros('GET', '/ip/route')),
            **{path: fixed(ros('GET', path), rule) for path in
               ('/ipv6/firewall/filter', '/ipv6/firewall/mangle', '/ipv6/firewall/nat', '/ipv6/firewall/raw',
                '/ip/firewall/filter', '/ip/firewall/nat', '/ip/firewall/mangle')},
            'ipv6_addresses': fixed(ros('GET', '/ipv6/address'), ('.id', 'address', 'interface', 'disabled')),
            'nd': fixed(ros('GET', '/ipv6/nd'), ('.id', 'interface', 'advertise-dns', 'disabled'))}


def probe_reaches_mihomo():
    """True when the client's IPv6 packets to a Telegram address arrive at CT152."""
    captured = {}

    def capture():
        result = lab.guest(152, 'timeout', '8', 'tcpdump', '-ni', 'eth1', '-c', '2', 'ip6 dst ' + PROBE,
                           check=False, timeout=30)
        captured['out'] = result.stdout.decode(errors='replace') + result.stderr.decode(errors='replace')
    thread = threading.Thread(target=capture)
    thread.start()
    time.sleep(2)
    lab.guest(153, 'ping', '-6', '-c', '3', '-i', '0.5', '-W', '1', PROBE, check=False, timeout=30)
    thread.join()
    match = re.search(r'(?:^|\n)(\d+) packets? captured', captured['out'])
    assert match, captured['out'][-400:]
    return int(match[1]) > 0


def main():
    lab.guard()
    ros = ros_client()
    before = baseline(ros)
    assert not any(row.get('comment', '').startswith('cf-rule6:') for row in ros('GET', '/ipv6/route'))
    assert all(row[3] == 'ipv6-readiness-lab' for row in before['/ipv6/firewall/filter']), \
        'VM151 IPv6 firewall differs from the lab baseline; another test may be running'
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(mode=0o700, parents=True)
    shutil.copytree(PREVIEW / 'data', WORK / 'data')
    credentials = json.loads((PREVIEW / 'credentials.json').read_text())
    os.environ.update(DATA_DIR=str(WORK / 'data'), LOG_FILE=str(WORK / 'server.log'), LOG_LEVEL='WARNING',
                      ADMIN_USERNAME=credentials['username'], ADMIN_PASSWORD=credentials['password'],
                      JWT_SECRET_KEY=credentials['jwt_secret'], AGENT_REGISTRATION_KEY=credentials['registration_key'])
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        os.environ.pop(name, None)
    sys.path.insert(0, str(SOURCE))
    from backend.common import config as store
    from backend.common.config_repository import ProfileRepository
    store.set_repository(ProfileRepository(WORK / 'data', default_config_factory=store.get_default_config,
                                           initial_config_factory=store.get_default_config))
    from backend.app import app
    from backend.routes import routers as views
    from backend.routers import ipv6_routes, rule_sync
    from backend.utils.rule_utils import write_validated_rule_cache
    repository = store.get_repository()
    profile = repository.default_profile_id()

    def call(view, method, path, body=None):
        with app.test_request_context(path, method=method, json=body if body is not None else {}):
            reply = view.__wrapped__(ROUTER_ID)
        response, status = reply if isinstance(reply, tuple) else (reply, reply.status_code)
        return status, response.get_json()

    def report():
        status, data = call(views.router_report, 'GET', f'/api/routers/{ROUTER_ID}/report')
        assert status == 200, data
        return data['report']

    def plan():
        status, data = call(views.router_plan, 'POST', f'/api/routers/{ROUTER_ID}/plan')
        assert status == 200, data
        value = data['plan']
        assert not value['blockers'], value['blockers']
        kinds = {op['kind'] for stage in value['stages'] for op in stage['ops']}
        assert kinds <= {'route_comment', 'ipv6_routes_replace'}, kinds
        return value

    def apply(value):
        status, data = call(views.router_apply, 'POST', f'/api/routers/{ROUTER_ID}/apply',
                            {'fingerprint': value['fingerprint']})
        assert status == 200 and data['success'], data
        return data

    def owned():
        return [row for row in ros('GET', '/ipv6/route') if row.get('comment') == COMMENT]

    def set_source(cidrs):
        config = store.get_config(profile)
        source = next(rule for rule in config['rule_library'] if rule['id'] == 'lab-telegram6')
        write_validated_rule_cache(source, content(cidrs), config_data=config)
        store.reset_config_context()

    def mihomo(ipv6):
        def edit(config):
            document = yaml.safe_load(config['mihomo'].get('custom_config') or '') or {}
            document['ipv6'] = ipv6
            document.setdefault('tun', {})['enable'] = True
            config['mihomo']['custom_config'] = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
        repository.update_profile_transaction(profile, edit)
        store.reset_config_context()

    results = []
    adopted = None
    try:
        # A url source, so the automatic sync downloads it like a remote rule set.
        repository.update_shared_transaction(lambda shared: shared['rule_library'].append(
            {'id': 'lab-telegram6', 'name': 'LabTelegram6', 'source_type': 'url',
             'url': 'https://rules.invalid/telegram6.list', 'behavior': 'classical', 'enabled': True}))

        def seed(config):
            config['rule_configs'].append({'id': 'lab-tg6', 'itemType': 'ruleset', 'name': 'LabTelegram6',
                                           'library_rule_id': 'lab-telegram6', 'enabled': True, 'policy': 'PROXY'})
            router = next(item for item in config['routers']['items'] if item['id'] == ROUTER_ID)
            router.update(ip_rule_sets=['lab-tg6'], ros_policy_rule_sets=[], auto_rule_sync=True)
        repository.update_profile_transaction(profile, seed)
        mihomo(True)
        set_source(V6)

        assert not probe_reaches_mihomo(), 'Telegram IPv6 already reaches Mihomo before publishing'
        results.append('before-publish-client-ipv6-does-not-reach-mihomo')
        section = report()['ipv6_routes']
        assert section['status'] == 'missing' and section['gateway'] == GATEWAY, section
        assert not section['skip'], section
        results.append('report-lists-missing-routes-with-scoped-lla-next-hop')

        stale = plan()
        set_source(V6[:4])
        status, data = call(views.router_apply, 'POST', f'/api/routers/{ROUTER_ID}/apply',
                            {'fingerprint': stale['fingerprint']})
        assert status == 409 and not owned(), data
        results.append('changed-rule-set-rejects-stale-preview-without-writes')

        set_source(V6)
        value = plan()
        adopted = next((op for stage in value['stages'] for op in stage['ops'] if op['kind'] == 'route_comment'), None)
        stage = next(stage for stage in value['stages'] if stage['key'] == 'ipv6_routes')
        assert len(stage['ops'][0]['adds']) == 5
        apply(value)
        rows = owned()
        assert sorted(row['dst-address'] for row in rows) == sorted(V6), rows
        assert {row['gateway'] for row in rows} == {GATEWAY} and all(row['active'] == 'true' for row in rows)
        manifest = next(item for item in store.get_config(profile)['routers']['items']
                        if item['id'] == ROUTER_ID).get('ipv6_routes_state')
        assert manifest and len(manifest['resources']) == 5
        results.append('publish-batches-five-routes-to-lla-and-reads-back')
        assert probe_reaches_mihomo(), 'Published route did not bring client IPv6 to Mihomo'
        results.append('client-ipv6-to-telegram-prefix-reaches-mihomo')

        again = plan()
        assert again['empty'], again['stages']
        apply(again)
        assert report()['ipv6_routes']['status'] == 'synced'
        results.append('repeat-preview-is-empty-and-synced')

        # Automatic sync: four removed, four added, all in batches with readback.
        ids_before = {row['.id'] for row in owned()}
        receipt = rule_sync.sync(profile, ROUTER_ID, lambda source, config: content(NEW6))
        assert receipt['status'] == 'synced', receipt
        assert (receipt['added'], receipt['removed']) == (4, 4), receipt
        rows = owned()
        assert sorted(row['dst-address'] for row in rows) == sorted(NEW6), rows
        kept = next(row for row in rows if row['dst-address'] == V6[0])
        assert kept['.id'] in ids_before
        store.reset_config_context()
        assert report()['ipv6_routes']['status'] == 'synced'
        results.append('auto-sync-applies-ipv6-additions-and-removals-with-readback')

        # A failed read-back after the IPv6 writes restores everything on the real device.
        original_verify = ipv6_routes.verify

        def broken(driver, check):
            raise ipv6_routes.RouterError('lab: injected IPv6 read-back failure')
        ipv6_routes.verify = broken
        try:
            receipt = rule_sync.sync(profile, ROUTER_ID, lambda source, config: content(V6))
        finally:
            ipv6_routes.verify = original_verify
        assert receipt['status'] == 'paused', receipt
        rows = owned()
        assert sorted(row['dst-address'] for row in rows) == sorted(NEW6), rows
        store.reset_config_context()
        manifest = next(item for item in store.get_config(profile)['routers']['items']
                        if item['id'] == ROUTER_ID)['ipv6_routes_state']
        assert {entry['id'] for entry in manifest['resources']} == {row['.id'] for row in rows}
        results.append('auto-sync-failure-rolls-back-on-device-and-remaps-ownership')
        resumed = plan()  # the cache still holds NEW6: nothing to write; confirming resumes the sync
        assert resumed['empty'], resumed['stages']
        apply(resumed)
        results.append('manual-preview-after-pause-is-empty-and-resumes')

        original_host = views._ipv6_host
        views._ipv6_host = lambda config, agent: {'error': 'lab: Agent offline'}
        try:
            skipped = plan()
        finally:
            views._ipv6_host = original_host
        stage = next(stage for stage in skipped['stages'] if stage['key'] == 'ipv6_routes')
        assert stage['skipped'] and not stage['ops'] and not skipped['blockers']
        assert any('IPv6 规则集路由本次跳过' in warning for warning in skipped['warnings'])
        results.append('unknown-next-hop-skips-only-this-item')

        mihomo(False)
        value = plan()
        stage = next(stage for stage in value['stages'] if stage['key'] == 'ipv6_routes')
        assert len(stage['ops'][0]['deletes']) == 5
        apply(value)
        assert not owned()
        assert not next(item for item in store.get_config(profile)['routers']['items']
                        if item['id'] == ROUTER_ID).get('ipv6_routes_state')
        assert not probe_reaches_mihomo()
        results.append('mihomo-ipv6-off-withdraws-owned-routes')
    finally:
        # Never leave lab-owned routes or the adopted comment behind.
        for row in ros('GET', '/ipv6/route'):
            if row.get('comment') == COMMENT:
                ros('DELETE', '/ipv6/route/' + row['.id'])
        for old in ros('GET', '/ip/route'):
            original = next((row for row in before['ip_routes'] if row[0] == old['.id']), None)
            if original and old.get('comment') != original[6]:
                assert original[6] == 'ipv6-readiness-lab', original
                ros('PATCH', '/ip/route/' + old['.id'], {'comment': original[6]})
        after = baseline(ros)
        # Only routes are written here. Another session may test the IPv6
        # firewall on VM151 meanwhile: report such changes, do not undo them.
        for key in ('ipv6_routes', 'ip_routes'):
            assert after[key] == before[key], (key, before[key], after[key])
        others = sorted(key for key in before if before[key] != after[key])
        shutil.rmtree(WORK, ignore_errors=True)
    lab.save('ruleset-routes-results.json', {'success': True, 'cases': results, 'adopted_ipv4_comment': bool(adopted),
                                             'changed_by_others': others})
    print(json.dumps({'success': True, 'cases': results, 'routes_restored': True, 'changed_by_others': others},
                     ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
