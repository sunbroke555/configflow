"""cf-pd prefix guard through preview, release, readback, rollback and withdrawal."""
import copy
from pathlib import Path

import pytest

from backend.routers import ipv6_pd_guard as pd
from backend.routers.plan import build_plan
from backend.routers.release import execute
from backend.routers.routeros import RouterOSDriver
from backend.routes import routers as api
from backend.test_router_dns_guard import RouterOS

PROD = (Path(__file__).parent / 'testdata' / 'cf-pd-watch-prod.rsc').read_text()
ROLLBACK_SOURCE = '/system script remove [find name="cf-pd-watch"]\n:log warning "cf-pd: rolled back"'
GATEWAY = '10.0.0.4'
ROUTER = {'id': 'ros', 'type': 'routeros', 'endpoint': 'https://10.0.0.1',
          'proxy_gateway_agent_id': 'gateway', 'ipv6_pd_guard': {'enabled': True}}
DESIRED = {'fake_ip_range': '198.18.0.0/16', 'proxy_gateway': GATEWAY, 'warnings': [],
           'proxy_cidrs': [{'cidr': '198.18.0.0/16', 'sources': ['fake-ip']}]}
CONFIG = {'mihomo': {'custom_config': '{}'}}
OLD = '2001:db8:b000::/64'


class PDRouter(RouterOS):
    def __init__(self):
        super().__init__(nats=[])
        self.rows[pd.CLIENTS] = [
            {'.id': '*C0', 'interface': 'pppoe-out1', 'pool-name': 'ipv6pool', 'pool-prefix-length': '60',
             'request': 'prefix', 'disabled': 'false', 'status': 'bound', 'prefix': '2001:db8:a000::/60, 1d'},
            {'.id': '*C1', 'interface': 'pppoe-out1', 'pool-name': 'v6pool', 'request': 'prefix',
             'disabled': 'true', 'comment': 'defconf'},
        ]
        self.rows[pd.ADDRESSES] = [
            {'.id': '*A4', 'address': '2001:db8:a000::1/64', 'from-pool': 'ipv6pool', 'interface': 'lan4',
             'advertise': 'true', 'disabled': 'false'},
            {'.id': '*A5', 'address': 'fdda::1/64', 'interface': 'lan4', 'advertise': 'true', 'disabled': 'true'},
        ]
        self.rows[pd.IPV4_ADDRESSES] = [{'.id': '*I1', 'address': '10.0.0.1/24', 'interface': 'lan4'}]
        self.rows[pd.SCRIPTS] = []
        self.rows[pd.SCHEDULER] = []
        self.rows[pd.LISTS] = [{'.id': '*L9', 'list': 'geo6', 'address': '2001:db8:9::/48', 'dynamic': 'false'}]
        self.rows[pd.PREFIXES] = [
            {'.id': '*P1', 'prefix': '2001:db8:a000::/64', 'interface': 'lan4', 'dynamic': 'true'},
            {'.id': '*P2', 'prefix': '2001:db8:cafe::/64', 'interface': 'lan4', 'comment': 'mine',
             'valid-lifetime': '1d', 'preferred-lifetime': '1d'},
        ]

    def request(self, method, url, json=None, **kwargs):
        result = super().request(method, url, json=json, **kwargs)
        # Scripts carry counters that change on every run, like a live device.
        for row in self.rows[pd.SCRIPTS] + self.rows[pd.SCHEDULER]:
            row['run-count'] = str(int(row.get('run-count', 0)) + 1)
        return result

    def manual(self, source=PROD, hook=pd.HOOK):
        """The production hand install: script, rollback helper, poll, recorded prefix, hook."""
        self.rows[pd.SCRIPTS] += [
            {'.id': '*S1', 'name': 'cf-pd-watch', 'source': source, 'policy': 'read,write,test',
             'comment': 'IPV6 前缀监控脚本', 'owner': 'admin', 'dont-require-permissions': 'false'},
            {'.id': '*S2', 'name': 'cf-pd-rollback', 'source': ROLLBACK_SOURCE, 'policy': 'read,write,test',
             'comment': 'IPV6 前缀监控移除脚本', 'owner': 'admin', 'dont-require-permissions': 'false'}]
        self.rows[pd.SCHEDULER].append(
            {'.id': '*T1', 'name': 'cf-pd-poll', 'start-time': 'startup', 'interval': '30s',
             'on-event': '/system script run cf-pd-watch', 'policy': 'read,write,test', 'comment': 'cf-pd',
             'disabled': 'false', 'owner': 'admin'})
        self.rows[pd.LISTS].append({'.id': '*L1', 'list': 'cf-pd-last', 'address': '2001:db8:a000::/64',
                                    'comment': 'cf-pd: current LAN prefix', 'dynamic': 'false'})
        self.rows[pd.CLIENTS][0]['script'] = hook

    def deprecated(self):
        self.rows[pd.PREFIXES].append({'.id': '*P3', 'prefix': OLD, 'interface': 'lan4', 'disabled': 'false',
                                       'preferred-lifetime': '0s', 'valid-lifetime': '10m',
                                       'comment': 'cf-pd: deprecated until 1767225600'})


@pytest.fixture
def inspection(monkeypatch):
    session = PDRouter()
    driver = RouterOSDriver(ROUTER['endpoint'], 'admin', 'pw')
    driver.session = session
    agents = {'gateway': {'id': 'gateway', 'host': GATEWAY, 'service_type': 'mihomo'}}
    monkeypatch.setattr(api, '_agent', lambda agent_id: agents.get(agent_id))
    monkeypatch.setattr(api, 'compute_desired', lambda *_args, **_kwargs: copy.deepcopy(DESIRED))
    monkeypatch.setattr(api, '_return_cleanup', lambda *_: None)
    return session, driver


def preview(driver, router):
    _, desired, state, report = api._inspect_state(CONFIG, router, driver)
    return report, build_plan(router, desired, state, report, report['return_routing'], driver)


def stage(plan):
    return next(item for item in plan['stages'] if item['key'] == 'ipv6_pd_guard')


def release(driver, router):
    report, plan = preview(driver, router)
    result = execute(plan, driver, None)
    return report, plan, result, {**router, 'ipv6_pd_guard_state': driver.ipv6_pd_guard_manifest}


def test_fresh_install_is_owned_verified_and_withdrawn_with_its_runtime_state(inspection):
    session, driver = inspection
    report, plan = preview(driver, ROUTER)
    section = report['ipv6_pd_guard']
    assert (section['status'], section['lan'], section['pool'], section['client']) == \
        ('pending', 'lan4', 'ipv6pool', 'pppoe-out1')
    assert [item['status'] for item in section['items']] == ['missing', 'missing', 'missing']
    assert not plan['blockers'] and report['in_sync'] is False
    _, _, result, router = release(driver, ROUTER)
    assert result['ok'], result
    script = next(row for row in session.rows[pd.SCRIPTS] if row['name'] == 'cf-pd-watch')
    assert script['source'] == PROD and script['policy'] == 'read,write,test'
    assert session.rows[pd.CLIENTS][0]['script'] == pd.HOOK and 'script' not in session.rows[pd.CLIENTS][1]
    assert [entry['role'] for entry in router['ipv6_pd_guard_state']['resources']] == ['script', 'scheduler', 'hook']
    report, plan = preview(driver, router)
    assert report['ipv6_pd_guard']['status'] == 'synced' and not stage(plan)['ops']
    assert execute(plan, driver, None)['log'][-1]['summary'] == '回读确认 IPv6 前缀变化保护与模板一致'

    # The script ran and recorded state; then the prefix changed once.
    session.rows[pd.LISTS].append({'.id': '*L2', 'list': 'cf-pd-last', 'address': '2001:db8:a000::/64',
                                   'comment': 'cf-pd: current LAN prefix', 'dynamic': 'false'})
    session.deprecated()
    off = {**router, 'ipv6_pd_guard': {'enabled': False}}
    report, plan = preview(driver, off)
    assert report['ipv6_pd_guard']['status'] == 'pending'
    assert [item['role'] for item in report['ipv6_pd_guard']['items']] == ['hook', 'scheduler', 'script', 'state']
    _, _, result, off = release(driver, off)
    assert result['ok'], result
    assert off['ipv6_pd_guard_state'] is None
    assert session.rows[pd.SCRIPTS] == [] and session.rows[pd.SCHEDULER] == []
    assert 'script' not in session.rows[pd.CLIENTS][0] or session.rows[pd.CLIENTS][0]['script'] == ''
    assert [row['.id'] for row in session.rows[pd.LISTS]] == ['*L9']
    assert [row['.id'] for row in session.rows[pd.PREFIXES]] == ['*P1', '*P2']
    assert not api.ipv6_pd_guard.relevant(off)


def test_matching_manual_install_is_borrowed_and_never_removed(inspection):
    session, driver = inspection
    session.manual()
    before = copy.deepcopy(session.rows)
    report, plan = preview(driver, ROUTER)
    section = report['ipv6_pd_guard']
    assert section['status'] == 'borrowed' and '沿用' in section['message'] and section['rollback_script']
    assert [item['status'] for item in section['items']] == ['borrowed'] * 3
    assert report['in_sync'] is True and not stage(plan)['ops']
    _, _, result, router = release(driver, ROUTER)
    assert result['ok'] and router['ipv6_pd_guard_state'] is None
    assert session.writes == []
    # Turning the switch off has nothing ConfigFlow owns to withdraw.
    off = {**router, 'ipv6_pd_guard': {'enabled': False}}
    assert not api.ipv6_pd_guard.relevant(off)
    assert {path: [{k: v for k, v in row.items() if k != 'run-count'} for row in rows]
            for path, rows in session.rows.items()} == \
        {path: [{k: v for k, v in row.items() if k != 'run-count'} for row in rows] for path, rows in before.items()}


def test_differing_manual_install_is_updated_in_place_then_owned(inspection):
    session, driver = inspection
    session.manual(source=PROD.replace('"lan4"', '"lan3"'), hook='/system script run cf-pd-watch')
    report, plan = preview(driver, ROUTER)
    items = {item['role']: item for item in report['ipv6_pd_guard']['items']}
    assert items['script']['status'] == 'update' and 'lan3' in items['script']['detail']
    assert items['hook']['status'] == 'update' and items['scheduler']['status'] == 'adopt'
    changes = stage(plan)['ops'][0]['changes']
    assert [(change['method'], change['path']) for change in changes] == [
        ('PATCH', pd.SCRIPTS), ('DELETE', pd.LISTS), ('PATCH', pd.CLIENTS)]
    _, _, result, router = release(driver, ROUTER)
    assert result['ok'], result
    assert next(row for row in session.rows[pd.SCRIPTS] if row['.id'] == '*S1')['source'] == PROD
    assert next(row for row in session.rows[pd.SCRIPTS] if row['.id'] == '*S1')['comment'] == 'IPV6 前缀监控脚本'
    # Once a confirmed release rewrote part of it, ConfigFlow manages the whole set.
    assert {entry['role']: entry['id'] for entry in router['ipv6_pd_guard_state']['resources']} == \
        {'script': '*S1', 'scheduler': '*T1', 'hook': '*C0'}
    report, _ = preview(driver, router)
    assert report['ipv6_pd_guard']['status'] == 'synced'
    assert [item['status'] for item in report['ipv6_pd_guard']['items']] == ['synced'] * 3
    session.deprecated()
    off = {**router, 'ipv6_pd_guard': {'enabled': False}}
    _, _, result, off = release(driver, off)
    assert result['ok'], result
    # The user's own rollback helper and unrelated prefixes stay.
    assert [row['name'] for row in session.rows[pd.SCRIPTS]] == ['cf-pd-rollback']
    assert session.rows[pd.SCHEDULER] == []
    assert [row['.id'] for row in session.rows[pd.PREFIXES]] == ['*P1', '*P2']
    assert [row['.id'] for row in session.rows[pd.LISTS]] == ['*L9']


def test_withdraw_readback_tolerates_equal_ids_in_different_tables(inspection):
    session, driver = inspection
    _, _, result, router = release(driver, ROUTER)
    assert result['ok']
    script = next(row for row in session.rows[pd.SCRIPTS] if row['name'] == 'cf-pd-watch')
    # A dynamic RA prefix happens to share the script's ID.
    session.rows[pd.PREFIXES][0]['.id'] = script['.id']
    _, _, result, off = release(driver, {**router, 'ipv6_pd_guard': {'enabled': False}})
    assert result['ok'], result


def test_runtime_state_written_during_withdrawal_is_swept(inspection, monkeypatch):
    session, driver = inspection
    _, _, result, router = release(driver, ROUTER)
    assert result['ok']
    original = session.request

    def racing(method, url, json=None, **kwargs):
        response = original(method, url, json=json, **kwargs)
        if method == 'DELETE' and '/rest/system/script/' in url:
            # The hook loop still running records the prefix again.
            session.rows[pd.LISTS].append({'.id': '*LR', 'list': 'cf-pd-last', 'address': '2001:db8:a000::/64',
                                           'dynamic': 'false'})
        return response
    monkeypatch.setattr(session, 'request', racing)
    _, _, result, off = release(driver, {**router, 'ipv6_pd_guard': {'enabled': False}})
    assert result['ok'], result
    assert [row['.id'] for row in session.rows[pd.LISTS]] == ['*L9']


def test_hook_conflict_skips_only_this_stage(inspection):
    session, driver = inspection
    session.rows[pd.CLIENTS][0]['script'] = ':log info "my own hook"'
    report, plan = preview(driver, ROUTER)
    section = report['ipv6_pd_guard']
    assert section['status'] == 'blocked' and '不能覆盖' in section['blockers'][0]
    assert not plan['blockers'] and stage(plan)['skipped'] and stage(plan)['ops'] == []
    assert any(item.startswith('IPv6 前缀变化保护本次跳过：') for item in plan['warnings'])
    result = execute(plan, driver, None)
    assert result['ok'] and result['log'][-1]['status'] == 'warning'
    assert session.rows[pd.CLIENTS][0]['script'] == ':log info "my own hook"'
    assert session.rows[pd.SCRIPTS] == []


def test_no_prefix_client_is_explained_and_skipped(inspection):
    session, driver = inspection
    session.rows[pd.CLIENTS][0]['disabled'] = 'true'
    report, plan = preview(driver, ROUTER)
    assert '没有启用并请求前缀' in report['ipv6_pd_guard']['message']
    assert not plan['blockers'] and stage(plan)['skipped']


def test_a_failed_hook_write_rolls_back_created_objects(inspection):
    session, driver = inspection
    session.fail.add(('PATCH', pd.CLIENTS, 1))
    _, plan = preview(driver, ROUTER)
    result = execute(plan, driver, None)
    assert not result['ok'] and result['failed_stage'] == 'ipv6_pd_guard'
    assert session.rows[pd.SCRIPTS] == [] and session.rows[pd.SCHEDULER] == []
    assert driver.ipv6_pd_guard_manifest is None


def test_a_change_after_preview_stops_the_write(inspection):
    session, driver = inspection
    _, plan = preview(driver, ROUTER)
    session.rows[pd.CLIENTS][0]['script'] = ':log info "added meanwhile"'
    result = execute(plan, driver, None)
    assert not result['ok'] and '预览后发生变化' in result['log'][-1]['summary']
    assert session.rows[pd.SCRIPTS] == []


def test_preview_fingerprint_ignores_run_counters_and_lease_timers(inspection):
    session, driver = inspection
    _, first = preview(driver, ROUTER)
    session.rows[pd.CLIENTS][0]['prefix'] = '2001:db8:a000::/60, 23h59m'
    _, second = preview(driver, ROUTER)
    assert first['fingerprint'] == second['fingerprint']


def test_owned_object_rewritten_by_hand_blocks_withdrawal(inspection):
    session, driver = inspection
    _, _, result, router = release(driver, ROUTER)
    assert result['ok']
    next(row for row in session.rows[pd.SCRIPTS] if row['name'] == 'cf-pd-watch')['source'] = ':log info mine'
    report, plan = preview(driver, {**router, 'ipv6_pd_guard': {'enabled': False}})
    assert report['ipv6_pd_guard']['status'] == 'blocked' and not plan['blockers']
    assert stage(plan)['ops'] == []
