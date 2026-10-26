"""RouterOS NAT bypass by address lists must not change the paths that need no list.

These values were recorded before the list-based NAT bypass existed. Plans that
need no new exemption (not required, already covered by per-destination
rules, adopt-only, unknown) and source-policy plans keep producing the same
operations, fingerprints, checks and writes; iKuai never gets the list rule.
"""
import copy
import hashlib
import ipaddress
import json

import pytest

from backend.routers.base import RouterError
from backend.routers.plan import build_plan
from backend.routers.release import execute
from backend.routers.report import build_report
from backend.routers.routeros_nat import client_source_networks
from backend.routers.rule_sync_write import restrict_plan
from backend.test_ros_policy import FILTER, GATEWAY, NAT, ROUTER, TARGET, MemoryROS, desired_for, inspect

STATIC = {'fake_ip_range': '198.18.0.0/16', 'proxy_gateway': GATEWAY, 'warnings': [],
          'proxy_cidrs': [{'cidr': '198.18.0.0/16', 'sources': ['fake-ip']},
                          {'cidr': TARGET, 'sources': ['Telegram']}]}
MASQUERADE = {'.id': '*3', 'chain': 'srcnat', 'action': 'masquerade', 'disabled': 'false'}


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def legacy_rules():
    """What the per-destination nat_add wrote: every source fragment × destination."""
    rows, number = [], 0x60
    for cidr in ('198.18.0.0/16', TARGET):
        for source in client_source_networks(GATEWAY):
            number += 1
            rows.append({'.id': f'*{number:X}', 'chain': 'srcnat', 'action': 'accept', 'disabled': 'false',
                         'dst-address': cidr, 'comment': 'cf-mihomo', 'src-address': str(source)})
    return rows


SCENARIOS = {
    # Only a WAN-limited masquerade: no exemption is required.
    'not_required': lambda: [{'.id': '*3', 'chain': 'srcnat', 'action': 'masquerade',
                              'out-interface': 'pppoe-out1', 'disabled': 'false'}],
    'no_source_nat': lambda: [],
    'legacy_synced': lambda: legacy_rules() + [copy.deepcopy(MASQUERADE)],
    'adopt_only': lambda: [{'.id': '*A', 'chain': 'srcnat', 'action': 'accept', 'comment': 'tg',
                            'src-address': '192.168.0.0/24', 'disabled': 'false'}, copy.deepcopy(MASQUERADE)],
    'unknown': lambda: [{'.id': '*A', 'chain': 'srcnat', 'action': 'accept', 'comment': 'tg', 'protocol': 'tcp',
                         'src-address': '192.168.0.0/24', 'disabled': 'false'}, copy.deepcopy(MASQUERADE)],
}

# Recorded on claude/ros-release-batch (d998f08a), before list-based NAT bypass.
EXPECTED = {'policy': {'bundle': 'd34900c79f390f0cb7195ac5f59b61bb46a322e8a486106488e2e8f8b25d59f1',
                       'checks': '2940f8036634166f0075a1c9de295ab3827a58a0fae8ad4fd572b0335f6b2972',
                       'fingerprint': '074288f26a91a9e1',
                       'spec': '07c8ffd07742f7e7dbe1a2975ee59928b8a2d5e3ed85b56ec5d24d20d1e71adc',
                       'stages': '5bf64df491ae7990d4e013149157153900133852d5db205dc06fe571b4d1e10f',
                       'summary': '发布来源策略：1 个来源范围、1 个目的网段 → 192.168.0.3',
                       'writes': 'cbaf7684319c0c97c617a9a75e75d0ba4a65ddd3104bbf93e39c16e857d632fd'},
            'policy_literal_migration': {'bundle': '8a930a2b516aa9702485a78e5cd5f0fb2d1b7b8a7246188dc9247d4a889e63fc',
                                         'migrations': [{'config': {'action': 'accept',
                                                                    'chain': 'srcnat',
                                                                    'comment': 'cf-old',
                                                                    'disabled': 'false',
                                                                    'dst-address': '91.108.4.0/23',
                                                                    'protocol': 'tcp',
                                                                    'src-address': '192.168.0.0/24'},
                                                         'id': '*AB',
                                                         'path': '/ip/firewall/nat'}],
                                         'replacements': [{'path': '/ip/firewall/nat',
                                                           'values': {'action': 'accept',
                                                                      'chain': 'srcnat',
                                                                      'comment': 'cf-old',
                                                                      'disabled': 'false',
                                                                      'dst-address': '91.108.5.0/24',
                                                                      'place-before': '*AB',
                                                                      'protocol': 'tcp',
                                                                      'src-address': '192.168.0.0/24'}}],
                                         'summary': '发布来源策略：1 个来源范围、1 个目的网段 → 192.168.0.3；仅为这些来源和目的网段添加 LAN 出口 NAT '
                                                    '免转换；迁移 0 条受管主表路由，收窄 1 条旧 NAT 免转换'},
            'restrict': {'nat_add': '本次更新还需要修改「主路由 NAT 免转换」，请手动预览发布', 'nat_update': '本次更新还需要修改「主路由 NAT 免转换」，请手动预览发布'},
            'static:adopt_only': {'blockers': [],
                                  'checks': '07f2b7bb67fb14a5e2b4e4ed000e51f60d7518b0d35ee4adfaffa28abac860ed',
                                  'fingerprint': '0d9479d5b804acf3',
                                  'log': [('routes', '新增路由 91.108.4.0/22 → 192.168.0.3（备注 cf-Telegram）', 'ok'),
                                          ('routes', '回读校验通过', 'verified'),
                                          ('nat_bypass', '接管 NAT 免转换规则「tg」（备注 → cf-tg）', 'ok'),
                                          ('nat_bypass', '回读校验通过', 'verified')],
                                  'nat_ops': '76252a6b0f9efd7f348728ff59f6eabf93c545740c246bbf70087de41502aeea',
                                  'report': 'aef838be797ba5fb048dcc5d8052430a8a93c576db7a94d443e10bb12dbb9f2e',
                                  'stages': 'b8aa55826a0c7ce04ad66a3dfff65482ee1499ce9c2189230e803f1668db4a0e',
                                  'state': '513ef8b6437f2966ca0a32a2f68b4e0663f451c805b1f291bfedf001be84efe3',
                                  'warnings': [],
                                  'writes': '7922868da63e9550c33e24b594a84a45b324f81dbbaf223be40b8c4841aba79f'},
            'static:legacy_synced': {'blockers': [],
                                     'checks': '07f2b7bb67fb14a5e2b4e4ed000e51f60d7518b0d35ee4adfaffa28abac860ed',
                                     'fingerprint': 'f565b26931426fc2',
                                     'log': [('routes', '新增路由 91.108.4.0/22 → 192.168.0.3（备注 cf-Telegram）', 'ok'),
                                             ('routes', '回读校验通过', 'verified')],
                                     'nat_ops': '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945',
                                     'report': '96538ca7570e6608e8ee81b8df2cc523cc5bc7a17686dc56e33706e538740d4f',
                                     'stages': 'bad18b96323f5837e5f2769726a6bb15b0d37b8e5253597e6281bad0c7be30c3',
                                     'state': 'eed61f70d8212a04615d5d7f6759f11a2b6a7f801246543468bd58630bcf56d1',
                                     'warnings': [],
                                     'writes': '06407e136b598823f33ed8cb32ef4efa0a7e69f7268e974e9122f34612637994'},
            'static:no_source_nat': {'blockers': [],
                                     'checks': 'ad12c2e80707257a90e0bb2068efa8ec0323f22d197465255e3074f34f9fd026',
                                     'fingerprint': 'e7a78c539cdb12d7',
                                     'log': [('routes', '新增路由 91.108.4.0/22 → 192.168.0.3（备注 cf-Telegram）', 'ok'),
                                             ('routes', '回读校验通过', 'verified'),
                                             ('nat_bypass', '回读确认无需额外 NAT 免转换', 'verified')],
                                     'nat_ops': '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945',
                                     'report': '6b69dbe0ef8f8778fcbfd76ac46faa3cd563ebd0e092826bf9343acc92d667da',
                                     'stages': 'bad18b96323f5837e5f2769726a6bb15b0d37b8e5253597e6281bad0c7be30c3',
                                     'state': 'c0c9aedd7c679bc13290c2599b4d1846e3eb75e53d144002be1ae97bae98bc84',
                                     'warnings': [],
                                     'writes': '06407e136b598823f33ed8cb32ef4efa0a7e69f7268e974e9122f34612637994'},
            'static:not_required': {'blockers': [],
                                    'checks': 'ad12c2e80707257a90e0bb2068efa8ec0323f22d197465255e3074f34f9fd026',
                                    'fingerprint': '048339497115c7ca',
                                    'log': [('routes', '新增路由 91.108.4.0/22 → 192.168.0.3（备注 cf-Telegram）', 'ok'),
                                            ('routes', '回读校验通过', 'verified'),
                                            ('nat_bypass', '回读确认无需额外 NAT 免转换', 'verified')],
                                    'nat_ops': '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945',
                                    'report': '341d6399cde8abb713258970eec2888be27f600abeb20930840be665776ddc89',
                                    'stages': 'bad18b96323f5837e5f2769726a6bb15b0d37b8e5253597e6281bad0c7be30c3',
                                    'state': 'c15a8448c9835360b810f851ad86f6c6f8065a058dd73e856b6fbdfed87f8c99',
                                    'warnings': [],
                                    'writes': '06407e136b598823f33ed8cb32ef4efa0a7e69f7268e974e9122f34612637994'},
            'static:unknown': {'blockers': ['198.18.0.0/16：无法确认是否需要 NAT '
                                            '免转换。已有免转换规则的顺序或来源、地址列表、接口、协议等条件不能保证客户端流量均免转换；请先检查现有规则',
                                            '91.108.4.0/22：无法确认是否需要 NAT '
                                            '免转换。已有免转换规则的顺序或来源、地址列表、接口、协议等条件不能保证客户端流量均免转换；请先检查现有规则'],
                               'checks': '7fa568836d3673f20e6f667dd9ccc2e6f92d136bd08fcb246a395e111be612f5',
                               'fingerprint': 'f9c3f87d40bd31a0',
                               'nat_ops': '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945',
                               'report': '9cd9f10203b76dbce71005a81a435ce16f533d59f755121e73a4e7e708469b25',
                               'stages': 'bad18b96323f5837e5f2769726a6bb15b0d37b8e5253597e6281bad0c7be30c3',
                               'state': '39fd13c5f67f19108b80eac44311cfd3d253a55794bd7e3851365b172f87d317',
                               'warnings': []}}


# User decision (2026-10-25): per-destination rules of earlier releases are
# merged into the list rule as soon as a preview sees them, CIDRs changed or
# not. That is the only recorded value allowed to differ: the per-destination
# fixture now plans that merge. Its report, state read, blockers, warnings and
# routes stage stay as recorded above.
MERGED = {'legacy_synced': {
    'checks': '0e2451fd3484780e26740357fe329d2cbd451ca47eaa351112d77569c093998a',
    'fingerprint': 'd4df26a580b360b8',
    'log': [('routes', '新增路由 91.108.4.0/22 → 192.168.0.3（备注 cf-Telegram）', 'ok'),
            ('routes', '回读校验通过', 'verified'),
            ('nat_bypass', '将 28 条 NAT 免转换规则合并为 1 条「cf-mihomo-nat」：源地址列表 cf-mihomo-nat-src'
                           '（192.168.0.3 所在 /24 除 192.168.0.3 外，14 段）→ 目的地址列表 cf-mihomo-nat-dst（2 个网段），'
                           '放在所有源 NAT 转换规则之前；逐条规则过多会拖慢每个新连接的源 NAT 匹配，自动同步也无法更新逐条规则；'
                           '先建立并回读校验新规则，再删除旧规则（备注 cf-mihomo）', 'ok'),
            ('nat_bypass', '回读校验通过', 'verified')],
    'nat_ops': 'd23f4c1a97a89d33ac2c8ce1c6657a40b2c80b55f7d5e87d8cfd160f5f70b7f9',
    'stages': '01261f69d67d4968f749a6ce64ca14f3188ba97de98f96b76321297c4f779761',
    'writes': '82fc3eea4d099fbd9898db1a30b56b62832936d500f2e680e5bca9b41fda5a38',
}}


def static_device(scenario):
    driver = MemoryROS()
    driver.rows[NAT] = SCENARIOS[scenario]()
    return driver


def static_preview(driver):
    state = driver.read_state()
    desired = copy.deepcopy(STATIC)
    report = build_report(desired, state)
    plan = build_plan(ROUTER, desired, state, report, {'status': 'disabled'}, driver)
    return state, report, plan


def observe_static(scenario):
    driver = static_device(scenario)
    state, report, plan = static_preview(driver)
    nat = next(stage for stage in plan['stages'] if stage['key'] == 'nat_bypass')
    result = {
        'report': _hash(report['nat_bypass']), 'stages': _hash(plan['stages']), 'nat_ops': _hash(nat['ops']),
        'checks': _hash(plan['checks']), 'fingerprint': plan['fingerprint'],
        'blockers': plan['blockers'], 'warnings': plan['warnings'],
        'state': _hash({'context': state.nat_context, 'address_sets': state.address_sets,
                        'nat_bypass': [vars(rule) for rule in state.nat_bypass]}),
    }
    if not plan['blockers']:
        outcome = execute(plan, driver, None)
        assert outcome['ok'], outcome
        result['writes'] = _hash(driver.writes)
        result['log'] = [(entry['stage'], entry['summary'], entry['status']) for entry in outcome['log']]
    return result


def observe_ikuai(variant):
    from backend.test_router_release import DESIRED, HOME_NATS, HOME_ROUTES, StatefulIKuai, _driver, _plan
    nats = copy.deepcopy(HOME_NATS)
    if variant == 'managed':
        nats[0]['comment'] = 'cf-mihomo'
    elif variant == 'empty':
        nats = []
    driver = _driver(StatefulIKuai(HOME_ROUTES, nats))
    plan = _plan(driver)
    nat = next(stage for stage in plan['stages'] if stage['key'] == 'nat_bypass')
    return {'nat_ops': nat['ops'], 'stages': _hash(plan['stages']), 'checks': _hash(plan['checks']),
            'fingerprint': plan['fingerprint'], 'blockers': plan['blockers'], 'desired': _hash(DESIRED)}


def observe_policy():
    driver = MemoryROS()
    _, plan = inspect(driver)
    op = next(op for stage in plan['stages'] for op in stage['ops'] if op['kind'] == 'ros_policy_replace')
    result = {'bundle': _hash(op['bundle']), 'spec': _hash(op['spec']), 'summary': op['summary'],
              'fingerprint': plan['fingerprint'], 'stages': _hash(plan['stages']), 'checks': _hash(plan['checks'])}
    outcome = execute(plan, driver, None)
    assert outcome['ok'], outcome
    result['writes'] = _hash(driver.writes)
    return result


def observe_policy_literal_migration():
    driver = MemoryROS()
    driver.rows[NAT][0].pop('out-interface')
    driver.rows[NAT].insert(0, {'.id': '*AB', 'chain': 'srcnat', 'action': 'accept',
                                'dst-address': '91.108.4.0/23', 'src-address': '192.168.0.0/24',
                                'protocol': 'tcp', 'comment': 'cf-old', 'disabled': 'false'})
    _, plan = inspect(driver, desired_for(driver, cidrs=['91.108.4.0/24']))
    op = next(op for stage in plan['stages'] for op in stage['ops'] if op['kind'] == 'ros_policy_replace')
    return {'migrations': op['migrations'], 'replacements': op['replacements'], 'summary': op['summary'],
            'bundle': _hash(op['bundle'])}


def restrict_message(kind):
    plan = {'blockers': [], 'stages': [
        {'key': 'routes', 'title': '主路由代理路由', 'ops': []},
        {'key': 'nat_bypass', 'title': '主路由 NAT 免转换', 'ops': [{'kind': kind}]}]}
    with pytest.raises(RouterError) as error:
        restrict_plan(plan, {'static_resources': []})
    return str(error.value)


def observe_all():
    result = {f'static:{name}': observe_static(name) for name in SCENARIOS}
    result['policy'] = observe_policy()
    result['policy_literal_migration'] = observe_policy_literal_migration()
    result['restrict'] = {kind: restrict_message(kind) for kind in ('nat_add', 'nat_update')}
    return result


@pytest.mark.parametrize('scenario', list(SCENARIOS))
def test_static_ros_paths_without_a_list_need_are_unchanged(scenario):
    assert observe_static(scenario) == {**EXPECTED[f'static:{scenario}'], **MERGED.get(scenario, {})}


def test_merging_old_rules_leaves_the_routes_stage_as_recorded():
    driver = static_device('legacy_synced')
    _, _, plan = static_preview(driver)
    merged = next(stage for stage in plan['stages'] if stage['key'] == 'nat_bypass')
    [op] = merged['ops']
    assert op['kind'] == 'nat_list_sync' and len(op['legacy']) == 28 and not op['stale']
    reference = static_preview(static_device('no_source_nat'))[2]
    assert ([stage for stage in plan['stages'] if stage['key'] != 'nat_bypass']
            == [stage for stage in reference['stages'] if stage['key'] != 'nat_bypass'])


@pytest.mark.parametrize('variant', ['home', 'managed', 'empty'])
def test_ikuai_never_gets_the_routeros_list_rule(variant):
    # iKuai NAT now references ConfigFlow IP groups (test_ikuai_groups.py);
    # the RouterOS address-list rule must still never reach it.
    nat_ops = observe_ikuai(variant)['nat_ops']
    assert nat_ops and all(op['kind'] != 'nat_list_sync' for op in nat_ops)


def test_source_policy_bundle_and_its_nat_are_unchanged():
    assert observe_policy() == EXPECTED['policy']


def test_source_policy_literal_nat_migration_is_unchanged():
    assert observe_policy_literal_migration() == EXPECTED['policy_literal_migration']


@pytest.mark.parametrize('kind', ['nat_add', 'nat_update'])
def test_rule_sync_still_refuses_per_rule_nat_writes(kind):
    assert restrict_message(kind) == EXPECTED['restrict'][kind]


def test_legacy_fixture_matches_the_per_destination_writer():
    """The legacy rows above are what nat_add produced for this gateway."""
    driver = MemoryROS()
    driver.rows[NAT] = []
    from backend.routers.plan import _lan_except
    driver.apply({'kind': 'nat_add', 'src': _lan_except(GATEWAY), 'dst': ['198.18.0.0/16', TARGET],
                  'comment': 'cf-mihomo'})
    written = sorted((row['dst-address'], str(ipaddress.ip_network(row['src-address']))) for row in driver.rows[NAT])
    expected = sorted((row['dst-address'], row['src-address']) for row in legacy_rules())
    assert written == expected


# --- reads ---------------------------------------------------------------------

# Every request (method, path, body) of a preview and of the release that
# follows, recorded on claude/ros-release-batch (d998f08a). Paths that need no
# NAT list must not read the two lists or raw. The return-routing advisory adds
# one read-only filter snapshot; every pre-existing request stays identical.
REQUESTS = {
    'adopt_only': ('e15a0b51fe155ba58dd6d02141e45202f534f886e027a40bc5057a449f3be462', 3,
                   'a67474d6856298988e9f28ebdf17cba59dc8e4e5be5c3608b87092d7b46dfd69', 13),
    'hairpin': ('b80a25c35b3823aaa6c33206be380aa0ceb297eb45567a12d8bf393044be77e7', 5,
                '26f5dab85ddcc3eedc7c6418208db9f710ec6ee25b4d24c59f08cc89bc4c83c6', 8),
    'no_source_nat': ('e15a0b51fe155ba58dd6d02141e45202f534f886e027a40bc5057a449f3be462', 3,
                      '19886ff50f66f29cc5ab7206de6601d86402c74db0fce3aa1f85d2ba37385665', 6),
    'not_required': ('b80a25c35b3823aaa6c33206be380aa0ceb297eb45567a12d8bf393044be77e7', 5,
                     '26f5dab85ddcc3eedc7c6418208db9f710ec6ee25b4d24c59f08cc89bc4c83c6', 8),
    'unknown': ('e15a0b51fe155ba58dd6d02141e45202f534f886e027a40bc5057a449f3be462', 3,
                '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945', 0),
    'policy': ('35dac5389cdb083e460be41272f7571d5774b35cc230bdb9e0f389c6c17d0b08', 15,
               '9265fce070493e6879c92eb87b89565d28a32de6cb15fa279868fd130d83e338', 60),
}
READ_SCENARIOS = {
    **{name: SCENARIOS[name] for name in ('not_required', 'no_source_nat', 'adopt_only', 'unknown')},
    # A DNS guard style LAN hairpin translates, but needs no exemption either.
    'hairpin': lambda: [{'.id': '*3', 'chain': 'srcnat', 'action': 'masquerade',
                         'out-interface': 'pppoe-out1', 'disabled': 'false'},
                        {'.id': '*4', 'chain': 'srcnat', 'action': 'masquerade', 'protocol': 'udp',
                         'dst-port': '53', 'dst-address': '192.168.0.53', 'src-address': '192.168.0.0/24',
                         'out-interface': 'bridge', 'disabled': 'false'}],
}


def _recorded(driver):
    sent = []
    send = driver._request

    def request(method, path, values=None, **kwargs):
        sent.append((method, path, values))
        return send(method, path, values, **kwargs)
    driver._request = request
    return sent


def observe_requests(scenario):
    driver = MemoryROS()
    if scenario == 'policy':
        sent = _recorded(driver)
        _, plan = inspect(driver)
    else:
        driver.rows[NAT] = READ_SCENARIOS[scenario]()
        sent = _recorded(driver)
        plan = static_preview(driver)[2]
    preview = list(sent)
    if scenario != 'policy':
        assert preview.count(('GET', FILTER, None)) == 1
        preview.remove(('GET', FILTER, None))
    sent.clear()
    if not plan['blockers']:
        assert execute(plan, driver, None)['ok']
    return _hash(preview), len(preview), _hash(sent), len(sent)


@pytest.mark.parametrize('scenario', list(REQUESTS))
def test_paths_without_a_list_need_send_the_recorded_requests(scenario):
    assert observe_requests(scenario) == REQUESTS[scenario]
