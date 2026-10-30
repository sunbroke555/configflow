#!/usr/bin/env python3
"""Exercise managed Fake-IP v6 routes through the real upstream-router API.

Run on .32 against the owned VM151/CT152-154 preview only. Restore the original
Mihomo draft and borrowed routes. Never adopt or delete unrelated IPv6 routes.
"""
import argparse
import base64
import json
import ipaddress
import subprocess
import urllib.request
import uuid
from pathlib import Path

import yaml
import ipv6_readiness_lab as lab
import ipv6_verification_lab as runtime

ROUTER_ID = 'router_d10a2f2c9442'
RANGES = ('fdcf:150:1::/64', 'fdcf:150:2::/64')


def main(browser_script=None):
    lab.guard()
    assert json.loads((runtime.PREVIEW / 'connections.json').read_text())['owner'] == lab.OWNER
    token, _ = runtime.session()
    ident = runtime.source_id(token)
    credentials = json.loads((lab.ROOT / 'private.json').read_text())
    authorization = 'Basic ' + base64.b64encode(('admin:' + credentials['router_password']).encode()).decode()

    def ros(method, path, values=None):
        assert path.startswith(('/ipv6/route', '/ip/route'))
        req = urllib.request.Request('http://127.0.0.1:19151/rest' + path, method=method,
            headers={'Authorization': authorization, 'Content-Type': 'application/json'},
            data=json.dumps(values).encode() if values is not None else None)
        with urllib.request.urlopen(req, timeout=15) as reply:
            return json.load(reply) if reply.status != 204 else None

    def api(method, path, body=None):
        return runtime.api(method, path, body, token)

    root = '/api/routers/' + ROUTER_ID
    original = api('GET', '/api/custom-config/mihomo')
    last_draft = original['config']
    before6 = ros('GET', '/ipv6/route')
    before4 = ros('GET', '/ip/route')
    assert not any(row.get('dst-address') in RANGES for row in before6)
    assert not any(row.get('comment', '').startswith('cf-fakeip6:') for row in before6)
    lab.save('managed-route-original.json', original, private=True)
    results = []
    wrote_draft = False

    def save(enabled, cidr=RANGES[0], restore=False):
        nonlocal last_draft, wrote_draft
        assert api('GET', '/api/custom-config/mihomo')['config'] == last_draft, 'Draft changed outside this test; do not overwrite it'
        if restore:
            text = original['config']
        else:
            data = yaml.safe_load(original['config'])
            data['ipv6'] = True
            data['dns'].update(enable=True, ipv6=enabled, **{'enhanced-mode': 'fake-ip', 'fake-ip-range6': cidr})
            data['tun']['enable'] = True
            text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        api('POST', '/api/custom-config/mihomo', {'config': text, 'provider_update_interval': original['provider_update_interval']})
        last_draft = text
        wrote_draft = True

    def plan():
        value = api('POST', root + '/plan', {})['plan']
        assert not value['blockers'], value['blockers']
        for stage in value['stages']:
            for op in stage['ops']:
                assert op['kind'] in ('route_comment', 'ipv6_fakeip_replace'), 'Unexpected unrelated operation'
                if op['kind'] == 'route_comment':
                    old = next(row for row in before4 if row['.id'] == op['id'])
                    assert old.get('dst-address') == '198.18.0.0/16' and old.get('comment') == 'ipv6-readiness-lab'
        return value

    def apply(value):
        result = api('POST', root + '/apply', {'fingerprint': value['fingerprint']})
        assert result['success'] and result['result']['ok'], result.get('message')

    try:
        save(True)
        old = plan()
        save(True, RANGES[1])
        try:
            api('POST', root + '/apply', {'fingerprint': old['fingerprint']})
        except RuntimeError as error:
            assert 'HTTP 409' in str(error)
        else:
            raise AssertionError('Stale preview was accepted')
        assert not any(row.get('dst-address') in RANGES for row in ros('GET', '/ipv6/route'))
        results.append('changed-draft-rejects-stale-preview-without-route-write')
        apply(plan())
        rows = [row for row in ros('GET', '/ipv6/route') if row.get('dst-address') in RANGES]
        assert len(rows) == 1 and rows[0]['dst-address'] == RANGES[1]
        assert rows[0]['comment'] == 'cf-fakeip6:' + ROUTER_ID
        results.append('create-managed-route-and-read-back')
        gateway = rows[0]['gateway']
        address, separator, interface = gateway.partition('%')
        assert separator and interface and ipaddress.IPv6Address(address).is_link_local, gateway
        results.append('new-route-prefers-scoped-link-local-next-hop')
        repeated = plan()
        assert repeated['empty']
        apply(repeated)
        assert api('GET', root + '/report')['report']['ipv6_fakeip']['status'] == 'synced'
        results.append('repeat-publish-no-duplicate')
        save(True)
        apply(plan())
        rows = [row for row in ros('GET', '/ipv6/route') if row.get('dst-address') in RANGES]
        assert len(rows) == 1 and rows[0]['dst-address'] == RANGES[0]
        assert rows[0]['gateway'] == gateway, 'Changing the range changed the proven next hop'
        results.append('change-range-replaces-only-owned-route')
        if browser_script:
            subprocess.run(['node', str(browser_script)], check=True)
        save(False)
        apply(plan())
        assert not any(row.get('dst-address') in RANGES for row in ros('GET', '/ipv6/route'))
        results.append('disable-and-publish-withdraws-owned-route')
        # The previous readiness implementation left this independent route.
        # Re-enable its range and prove the new publisher only borrows it.
        assert any(row.get('dst-address') == 'fc00::/64' for row in before6)
        save(True, 'fc00::1/64')
        borrowed = api('GET', root + '/report')['report']['ipv6_fakeip']
        assert borrowed['status'] == 'borrowed', borrowed
        assert plan()['empty']
        apply(plan())
        results.append('borrow-existing-route-without-taking-ownership')
    finally:
        if wrote_draft:
            # Withdraw only this test's manifest-owned route before restoring the
            # previous draft. A changed/foreign object makes the plan refuse.
            save(False)
            apply(plan())
            save(False, restore=True)
        for old in before4:
            current = next((row for row in ros('GET', '/ip/route') if row['.id'] == old['.id']), None)
            if current and current.get('comment') != old.get('comment'):
                assert old.get('comment') == 'ipv6-readiness-lab' and current.get('comment') == 'cf-ipv6-readiness-lab'
                ros('PATCH', '/ip/route/' + old['.id'], {'comment': old['comment']})
        # Existing manual and old readiness routes must retain their IDs/fields.
        fields = ('.id', 'dst-address', 'gateway', 'routing-table', 'distance', 'disabled', 'comment')
        stable = lambda rows: sorted([tuple(str(row.get(key, '')) for key in fields) for row in rows])
        assert stable(ros('GET', '/ipv6/route')) == stable(before6), 'Existing IPv6 routes changed'
        assert api('GET', '/api/custom-config/mihomo')['config'] == original['config']
    # Re-publish the exact original draft so saved runtime evidence corresponds
    # to the current revision rather than this test's temporary YAML edits.
    deployment = uuid.uuid4().hex
    api('POST', f'/api/agents/{ident}/push-config', {'deployment_id': deployment, 'restart': True,
        'base_url': 'http://10.242.150.254:15820'})
    report = runtime.wait_report(token, ident, deployment)
    checks = {item['id']: item['status'] for item in report['checks']}
    for required in ('client_http_ipv4', 'client_mihomo_path', 'client_ipv6_transport', 'client_ros_path'):
        assert checks.get(required) == 'ok', {required: checks.get(required)}
    results.append('restore-original-IPv4-client-IPv6-node-path')
    lab.save('managed-route-results.json', {'success': True, 'cases': results, 'deployment_id': deployment})
    print(json.dumps({'success': True, 'cases': results, 'original_draft_restored': True}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--browser-script', type=Path)
    args = parser.parse_args()
    main(args.browser_script)
