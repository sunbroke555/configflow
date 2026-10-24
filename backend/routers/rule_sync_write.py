"""Update only destinations already enrolled by a verified manual release."""
import copy
import ipaddress

from backend.routers.base import RouterError, RouterRollbackError
from backend.routers.ros_policy import ADDRESSES, MANGLE, ROUTES, config_row, owned_rows, snapshot, check_references
from backend.routers.ros_policy_write import (
    _create, _create_addresses, _delete, _delete_addresses, _guard, _resource, restore, verify,
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
    for stage in plan['stages']:
        if stage['key'] != 'routes' and stage['ops']:
            raise RouterError(f'本次更新还需要修改「{stage["title"]}」，请手动预览发布')
    route_stage = next(stage for stage in plan['stages'] if stage['key'] == 'routes')
    old_ids = {item['id'] for item in baseline['static_resources']}
    operations = []
    for op in route_stage['ops']:
        if op['kind'] == 'ros_policy_replace':
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

    Source lists, Mangle, tables, NAT, DHCP and Agent settings are never written.
    The caller persists the possibly remapped policy receipt even after rollback.
    """
    from backend.routers.release import _verify
    operations = restrict_plan(plan, baseline)
    previous = copy.deepcopy(getattr(driver, 'ros_policy_manifest', None))
    for item in baseline['static_resources']:
        _guard(driver, item)
    live = snapshot(driver, reserve=False)
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
    journal, log = [], []
    try:
        ensure_current(force=True)
        for path, values in additions:
            if path == ADDRESSES:
                continue
            ensure_current()
            if any(row.get('routing-table', 'main') == 'main'
                   and row.get('dst-address') == values['dst-address'] for row in driver._list(ROUTES)):
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
        driver.ros_policy_manifest = next_manifest
        # Exclude unrelated cf-prefixed static routes deliberately preserved above.
        checks = {**plan['checks'], 'removed_routes': [item['id'] for item in removals if item['path'] == ROUTES]}
        _verify('routes', checks, driver, None)
        _verify('nat_bypass', checks, driver, None)
        ensure_current(force=True)
    except (RouterError, RuntimeError) as error:
        try:
            restore(driver, {'steps': list(reversed(journal)), 'previous': previous,
                             'retained': (previous or {}).get('resources', [])})
        except RouterError as rollback_error:
            raise RouterRollbackError(f'{error}；{rollback_error}') from error
        raise
    return {'added': len(additions), 'removed': len(removals), 'log': log[-20:]}
