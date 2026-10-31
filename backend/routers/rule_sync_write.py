"""Update only destinations already enrolled by a verified manual release."""
import copy
import ipaddress

from backend.routers.base import RouterError, RouterRollbackError
from backend.routers.ros_policy import (
    ADDRESSES, FILTER, MANGLE, NAT, RAW, ROUTES, config_row, owned_rows, snapshot, check_references,
)
from backend.routers.ros_policy_write import (
    _create, _create_addresses, _delete, _delete_addresses, _guard_in, _resource, restore, verify,
)


def static_resources(driver, desired):
    """Pin full stable server configurations, not just a cf comment or a prefix."""
    from backend.routers.base import is_managed
    wanted = {item['cidr'] for item in desired['proxy_cidrs']}
    result = []
    for row in driver._list(ROUTES):
        if (row.get('routing-table', 'main') == 'main' and row.get('dst-address') in wanted
                and row.get('gateway') == desired['proxy_gateway'] and is_managed(row.get('comment'))):
            from backend.routers.routeros_routes import _entry
            entry = _entry(row, set())
            if not entry['editable'] or not entry['enabled']:
                raise RouterError('受管静态路由含特殊字段或已停用，不能自动维护')
            result.append(_resource(ROUTES, row))
    if {item['config'].get('dst-address') for item in result} != wanted:
        raise RouterError('静态路由尚未完整发布或包含未受管的路由，请先手动预览发布')
    if len(result) != len(wanted):
        raise RouterError('受管静态路由存在重复，无法建立自动同步基准')
    return result


def restrict_plan(plan, baseline):
    if plan['blockers']:
        raise RouterError('；'.join(plan['blockers']))
    from backend.routers.plan import STAGE_TITLES
    from backend.routers.routeros_nat_list import member_update, migration
    merged, stale = migration(plan)
    if merged or stale:
        # Merging or removing old per-destination rules is always a release a person starts.
        detail = [f'{merged} 条待合并为 1 条列表规则'] if merged else []
        detail += [f'{stale} 条的目的网段已不再代理'] if stale else []
        raise RouterError(f'路由器上还有旧的逐条 NAT 免转换规则（{"，".join(detail)}），'
                          '自动同步不会迁移，请手动预览并发布一次完成迁移')
    nat = member_update(plan, baseline)
    for stage in plan['stages']:
        if stage['key'] == 'nat_bypass' and nat is not None:
            continue  # only destination members of the enrolled NAT list rule
        if stage['key'] == 'ipv6_routes':
            continue  # rule_sync kept only additions/removals for the enrolled gateway
        if stage['key'] != 'routes' and stage['ops']:
            raise RouterError(f'本次更新还需要修改「{stage["title"]}」，请手动预览发布')
    route_stage = next(stage for stage in plan['stages'] if stage['key'] == 'routes')
    old_ids = {item['id'] for item in baseline['static_resources']}
    operations = []
    for op in route_stage['ops']:
        if op['kind'] == 'nat_list_sync':
            # NAT list removals run ahead of the route deletions.
            if nat is None:
                raise RouterError(f'本次更新还需要修改「{STAGE_TITLES["nat_bypass"]}」，请手动预览发布')
        elif op['kind'] == 'ros_policy_replace':
            if op.get('migrations'):
                raise RouterError('更新需要迁移主表路由，请手动预览发布')
            previous = op.get('previous')
            if not previous or not op.get('spec'):
                raise RouterError('策略路由尚未建立或需要撤销，请手动预览发布')
            old = {key: value for key, value in previous['spec'].items() if key != 'destinations'}
            new = {key: value for key, value in op['spec'].items() if key != 'destinations'}
            if old != new:
                raise RouterError('来源、网关或 NAT 设置有变化，请手动预览发布')
        elif op['kind'] == 'route_delete':
            # A cf-prefixed object outside the enrolled receipt is not ours to delete.
            if op['id'] in old_ids:
                operations.append(op)
        elif op['kind'] == 'route_add':
            operations.append(op)
        else:
            raise RouterError('更新涉及接管或修改已有路由，请手动预览发布')
    return operations


def apply_delta(driver, desired, plan, baseline, ensure_current=lambda **_: None):
    """Write additions first, then removals; compensate with the existing journal.

    Source lists, Mangle, tables, NAT rules, DHCP and Agent settings are never
    written; of NAT only destination members of the enrolled list rule are.
    The caller persists the possibly remapped policy receipt even after rollback.
    """
    from backend.routers import ros_address_lists
    operations = restrict_plan(plan, baseline)
    # Lists NAT or Mangle select on are downloaded once for all reads below;
    # every address-list write drops what was read.
    with ros_address_lists.reuse(driver):
        return _apply_delta(driver, desired, plan, baseline, ensure_current, operations)


def _apply_delta(driver, desired, plan, baseline, ensure_current, operations):
    from backend.routers import ros_address_lists
    from backend.routers.release import _verify
    previous = copy.deepcopy(getattr(driver, 'ros_policy_manifest', None))
    live = snapshot(driver, reserve=False)
    # The snapshot holds each enrolled route as a by-ID read would return it.
    for item in baseline['static_resources']:
        _guard_in(live, item)
    from backend.routers import routeros_nat_list
    nat = routeros_nat_list.member_update(plan, baseline)
    retained_nat = []
    if nat is not None:
        # The list rule and its sources stay exactly as the manual release left them.
        enrolled = baseline['nat_list']
        for item in [enrolled['rule']] + enrolled['src']:
            _guard_in(live, item)
        # As strict as the manual release: no other firewall rule may select
        # the lists, or a member change would silently change that rule too.
        foreign = [(path, row_id, name) for path in (NAT, MANGLE, FILTER, RAW)
                   for row_id, name in routeros_nat_list.references(live[path])
                   if not (path == NAT and row_id == enrolled['rule']['id'])]
        if foreign:
            path, row_id, name = foreign[0]
            raise RouterError(f'其他防火墙规则 {path}/{row_id} 正在引用 {name}，自动同步不会修改，请手动预览发布')
        sources = {row['.id'] for row in live[ADDRESSES] if row.get('list') == routeros_nat_list.SRC_LIST
                   and row.get('comment') == routeros_nat_list.COMMENT}
        if sources != {item['id'] for item in enrolled['src']}:
            raise RouterError('NAT 免转换源列表与同步基准不一致，请手动预览发布')
        # Rollback may remove members it wrote while the rule and the
        # list's other members, which all name the list, stay.
        retained_nat = [enrolled['rule']] + [_resource(ADDRESSES, row) for row in live[ADDRESSES]
                                             if row.get('list') == routeros_nat_list.DST_LIST]
    wanted = {item['cidr'] for item in desired['proxy_cidrs']}
    known_static = {item['id'] for item in baseline['static_resources']}
    if any(row.get('routing-table', 'main') == 'main' and row.get('dst-address') in wanted
           and row['.id'] not in known_static for row in live[ROUTES]):
        raise RouterError('目的网段已有未登记的静态路由，自动同步不会接管，请手动预览发布')
    if previous:
        owned = owned_rows(previous, live)
        if len(owned) != len(previous['resources']):
            raise RouterError('已发布的策略对象缺失，请手动预览发布')
        check_references(owned, live)
        verify(driver, {'spec': previous['spec'], 'previous': previous})
    old_static = {item['id']: item for item in baseline['static_resources']}
    additions, removals = [], []
    for op in operations:
        if op['kind'] == 'route_add':
            additions.append((ROUTES, {'dst-address': op['cidr'], 'gateway': op['gateway'],
                                      'comment': op['comment'], 'disabled': 'false'}))
        else:
            removals.append(old_static[op['id']])
    next_manifest = copy.deepcopy(previous)
    check = plan['checks'].get('ros_policy')
    if check and previous:
        spec = check['spec']
        entry = next((item for item in previous['resources'] if item['path'] == MANGLE
                      and item['config'].get('chain') == 'prerouting'
                      and item['config'].get('action') == 'jump'), None)
        if not entry:
            raise RouterError('策略入口的目的地址列表无法识别')
        name = entry['config']['dst-address-list']
        addresses = [item for item in previous['resources'] if item['path'] == ADDRESSES
                     and item['config'].get('list') == name]
        old = {str(ipaddress.IPv4Network(item['config']['address'], strict=False)): item for item in addresses}
        new = set(spec['destinations'])
        for cidr in sorted(new - old.keys()):
            additions.append((ADDRESSES, {'list': name, 'address': cidr,
                                         'comment': entry['config']['comment'], 'disabled': 'false'}))
        removals.extend(old[cidr] for cidr in sorted(old.keys() - new))
        next_manifest['spec'] = spec
    removed_ids = {(item['path'], item['id']) for item in removals}
    retained = [item for item in (previous or {}).get('resources', [])
                if (item['path'], item['id']) not in removed_ids]
    if next_manifest:
        next_manifest['resources'] = copy.deepcopy(retained)
    ids = {path: {row['.id'] for row in rows} for path, rows in live.items()}
    journal, log, undo6 = [], [], None
    try:
        ensure_current(force=True)
        for path, values in additions:
            if path == ADDRESSES:
                continue
            ensure_current()
            if any(row.get('routing-table', 'main') == 'main'
                   and row.get('dst-address') == values['dst-address'] for row in driver._scoped(ROUTES, values)):
                raise RouterError('写入前发现同网段路由，保留手工对象，请重新预览')
            _create(driver, path, values, journal, ids)
            log.append({'status': 'ok', 'summary': f'新增 {values["dst-address"]}'})
        members = [values for path, values in additions if path == ADDRESSES]
        if members:
            ensure_current()
            for values, resource in zip(members, _create_addresses(driver, members, journal, ids)):
                if next_manifest:
                    # Preserve dependency order: withdrawal reverses this sequence
                    # and must deactivate Mangle before deleting list members.
                    position = next((i for i, item in enumerate(next_manifest['resources'])
                                     if item['path'] != ADDRESSES), len(next_manifest['resources']))
                    next_manifest['resources'].insert(position, resource)
                log.append({'status': 'ok', 'summary': f'新增 {values["address"]}'})
        # NAT members follow the routes they protect: added after them,
        # removed before them, so no CIDR is exempted on its way out the WAN.
        if nat is not None and nat['dst_add']:
            ensure_current()
            _create_addresses(driver, routeros_nat_list.entries(routeros_nat_list.DST_LIST, nat['dst_add']),
                              journal, ids)
            log.extend({'status': 'ok', 'summary': f'NAT 免转换新增 {cidr}'} for cidr in nat['dst_add'])
        if nat is not None and nat['dst_remove']:
            ensure_current()
            _delete_addresses(driver, nat['dst_remove'], journal)
            log.extend({'status': 'ok', 'summary': f'NAT 免转换移除 {item["config"].get("address")}'}
                       for item in nat['dst_remove'])
        for item in removals:
            if item['path'] == ADDRESSES:
                continue
            ensure_current()
            _delete(driver, item, journal)
            log.append({'status': 'ok', 'summary': f'删除 {item["config"]["dst-address"]}'})
        members = [item for item in removals if item['path'] == ADDRESSES]
        if members:
            ensure_current()
            _delete_addresses(driver, members, journal)
            log.extend({'status': 'ok', 'summary': f'删除 {item["config"].get("address")}'} for item in members)
        routes6 = next((op for stage in plan['stages'] if stage['key'] == 'ipv6_routes' for op in stage['ops']), None)
        if routes6 is not None:
            # IPv6 destinations go after the IPv4 ones: their own batches and
            # proofs; a later failure undoes them before the IPv4 journal.
            ensure_current()
            from backend.routers import ipv6_routes
            undo6 = ipv6_routes.apply(driver, routes6)
            log.append({'status': 'ok', 'summary': routes6['summary']})
        driver.ros_policy_manifest = next_manifest
        # Exclude unrelated cf-prefixed static routes deliberately preserved above.
        checks = {**plan['checks'], 'removed_routes': [item['id'] for item in removals if item['path'] == ROUTES]}
        # The final read-back proves the lists afresh: others may have changed
        # them while the routes were written.
        ros_address_lists.invalidate(driver)
        state = _verify('routes', checks, driver, None)
        _verify('nat_bypass', checks, driver, None, state)  # nothing written in between
        if checks.get('ipv6_fakeip') is not None:
            # IPv6 is never written by remote-rule sync, but outside edits during
            # this IPv4 delta must fail readback and compensate our own writes.
            _verify('ipv6_fakeip', checks, driver, None)
        if checks.get('ipv6_dns') is not None:
            _verify('ipv6_dns', checks, driver, None)
        if checks.get('ipv6_routes') is not None:
            _verify('ipv6_routes', checks, driver, None)
        ensure_current(force=True)
    except (RouterError, RuntimeError) as error:
        problems = []
        if undo6 is not None:
            from backend.routers import ipv6_routes
            try:
                ipv6_routes.restore(driver, undo6)
            except RouterError as rollback_error:
                problems.append(str(rollback_error))
        try:
            restore(driver, {'steps': list(reversed(journal)), 'previous': previous,
                             'retained': (previous or {}).get('resources', []) + retained_nat})
        except RouterError as rollback_error:
            problems.append(str(rollback_error))
        if problems:
            raise RouterRollbackError(f'{error}；{"；".join(problems)}') from error
        raise
    members = (len(nat['dst_add']), len(nat['dst_remove'])) if nat is not None else (0, 0)
    routes6 = next((op for stage in plan['stages'] if stage['key'] == 'ipv6_routes' for op in stage['ops']), None)
    members6 = (len(routes6['adds']), len(routes6['deletes'])) if routes6 else (0, 0)
    return {'added': len(additions) + members[0] + members6[0],
            'removed': len(removals) + members[1] + members6[1], 'log': log[-20:]}
