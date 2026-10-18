"""Guarded REST transactions for source-policy bundles.

Readback uses individual resource URLs, so loading thousands of address-list
entries does not repeatedly download the entire list. Compensation checks the
exact last-written configuration before touching a resource.
"""
import copy
import ipaddress
import time
from urllib.parse import quote

from backend.routers.base import RouterError, RouterRollbackError
from backend.routers.ros_policy import (
    FILTER, MANGLE, NAT, ROUTES, RULES, TABLES, VRFS, bundle_health,
    check_policy_order, check_references, check_route_egress, config_row, enabled, owned_rows, snapshot, stable,
)
from backend.routers.routeros import ROUTE_FIELDS, MissingResource, UncertainWrite, _matches
from backend.routers.ros_values import comparable_values


def _read(driver, path, row_id):
    try:
        row = driver._get(path + '/' + quote(row_id, safe='*'))
        if isinstance(row, dict) and row.get('.id') == row_id:
            return row
    except MissingResource:
        return None
    raise RouterError('策略路由对象回读格式不完整：' + path)


def _guard(driver, item):
    row = _read(driver, item['path'], item['id'])
    if row is None or config_row(row) != config_row(item['config']):
        raise RouterError('策略路由对象在操作期间发生变化，保留现场：' + item['path'] + '/' + item['id'])
    return row


def _writable(path, row):
    if str(row.get('dynamic', 'false')).lower() in ('true', 'yes'):
        raise RouterError('动态策略对象不能修改')
    if path == ROUTES:
        return {key: value for key, value in row.items() if key in ROUTE_FIELDS}
    if path == TABLES:
        return {key: value for key, value in row.items() if key in {'name', 'fib', 'disabled'}}
    return {key: value for key, value in config_row(row).items() if key not in {'dynamic', 'invalid'}}


def _resource(path, row):
    return {'path': path, 'id': row['.id'], 'config': config_row(row)}


def _matches_values(path, row, values):
    return _matches(comparable_values(path, row), comparable_values(path, values))


def _create(driver, path, values, journal, ids, *, restoring=False):
    values = copy.deepcopy(values)
    if path == NAT and not restoring:
        rows = driver._list(NAT)
        if rows:
            values.setdefault('place-before', rows[0]['.id'])
    try:
        row = driver._request('PUT', path, values)
        if not isinstance(row, dict) or not row.get('.id'):
            raise UncertainWrite('策略对象已提交但未返回对象 ID')
    except UncertainWrite:
        candidates = [row for row in driver._list(path)
                      if row.get('.id') not in ids[path] and _matches_values(path, row, values)]
        if len(candidates) != 1:
            raise RouterRollbackError('策略对象写入结果不明确，未删除任何不明归属的对象，请检查路由器') from None
        journal.append({'action': 'delete', **_resource(path, candidates[0])})
        raise
    row_id = row['.id']
    ids[path].add(row_id)
    undo = {'action': 'delete', **_resource(path, row)}
    journal.append(undo)
    actual = _read(driver, path, row_id)
    if actual is None or not _matches_values(path, actual, values):
        fields = [key for key, value in values.items()
                  if not _matches_values(path, actual or {}, {key: value})]
        detail = '（对象不存在）' if actual is None else '（字段：' + '、'.join(fields) + '）'
        raise RouterError('策略路由回读与提交内容不一致：' + path + detail)
    # Full server defaults become the ownership baseline for future releases.
    undo['config'] = config_row(actual)
    if path == ROUTES and values.get('routing-table', 'main') != 'main' and str(actual.get('active', '')).lower() not in ('true', 'yes'):
        raise RouterError('独立路由表的下一跳未生效，不启用 Mangle')
    if path == NAT and values.get('place-before'):
        rows = driver._list(path)
        order = [row.get('.id') for row in rows]
        if values['place-before'] not in order or order.index(row_id) >= order.index(values['place-before']):
            raise RouterError('策略 NAT 免转换规则位置不正确')
    return _resource(path, actual)


def _delete(driver, item, journal):
    original = _guard(driver, item)
    values = _writable(item['path'], original)
    if item['path'] in (MANGLE, NAT):
        rows = driver._list(item['path'])
        ids = [row['.id'] for row in rows]
        index = ids.index(item['id'])
        if index + 1 < len(ids):
            values['place-before'] = ids[index + 1]
    journal.append({'action': 'restore', **item, 'values': values})
    driver._request('DELETE', item['path'] + '/' + quote(item['id'], safe='*'))
    if _read(driver, item['path'], item['id']) is not None:
        raise RouterError('策略路由回读校验失败：对象仍然存在')


def restore(driver, op):
    failures = []
    remapped = {}
    live = snapshot(driver)
    ids = {path: {row['.id'] for row in rows} for path, rows in live.items()}
    deleting = [step for step in op['steps'] if step['action'] == 'delete']
    # Preserve the whole new bundle if another writer started using its lists
    # or private chain. Checking only the table would leave foreign rules broken.
    try:
        retained = op.get('retained') or []
        if retained:
            # Destination-only refreshes keep their own Mangle/table resources.
            # Verify these exact owners before excluding their references.
            present = owned_rows({'resources': retained}, live)
            missing_allowed = {(step['path'], step['id']) for step in op['steps'] if step['action'] == 'restore'}
            present_keys = {(item['path'], item['id']) for item in present}
            if any((item['path'], item['id']) not in present_keys | missing_allowed for item in retained):
                raise RouterError('保留的策略对象缺失，无法安全回滚')
        check_references(deleting + retained, live)
        delete_safe = True
    except RouterError as error:
        failures.append(str(error))
        delete_safe = False
    for step in op['steps']:
        try:
            if step['action'] == 'operation':
                # Keep the replacement protection if the old path could not
                # be restored; report the incomplete rollback for inspection.
                if failures:
                    continue
                driver.apply(step['undo'])
                continue
            current = _read(driver, step['path'], step['id'])
            if step['action'] == 'delete':
                if not delete_safe:
                    continue
                if current is None:
                    continue
                _guard(driver, step)
                # Do not drop an entire table if another writer has started
                # referencing it since the release. Leave it for inspection.
                if step['path'] == TABLES:
                    check_references([step], snapshot(driver))
                driver._request('DELETE', step['path'] + '/' + quote(step['id'], safe='*'))
                if _read(driver, step['path'], step['id']) is not None:
                    raise RouterError('回滚删除后对象仍存在')
            else:
                if current is not None:
                    _guard(driver, step)
                    continue
                values = copy.deepcopy(step['values'])
                before = values.get('place-before')
                if before:
                    values['place-before'] = remapped.get((step['path'], before), before)
                    if _read(driver, step['path'], values['place-before']) is None:
                        raise RouterError('原规则位置已改变，不能安全恢复顺序')
                restored = _create(driver, step['path'], values, [], ids, restoring=True)
                remapped[(step['path'], step['id'])] = restored['id']
        except RouterError as error:
            failures.append(str(error))
    previous = copy.deepcopy(op.get('previous'))
    if previous:
        for item in previous['resources']:
            new_id = remapped.get((item['path'], item['id']))
            if new_id:
                item['id'] = new_id
                row = _read(driver, item['path'], new_id)
                if row:
                    item['config'] = config_row(row)
    driver.ros_policy_manifest = previous
    if failures:
        raise RouterRollbackError('策略路由回滚未完成：' + '；'.join(failures))
    return {}


def _verify_replacement_nat(driver, op, resources):
    previous = (op.get('previous') or {}).get('spec') or {}
    if not previous.get('sources') or not op.get('static_checks'):
        return
    from backend.routers.routeros_nat import _egress, preserves_sources, RouteEgressIndex
    state = driver.read_state()
    live = snapshot(driver)
    removing = {(item['path'], item['id']) for item in op['owned'] + op['migrations']}
    policy = removing | {(item['path'], item['id']) for item in resources}
    context = state.nat_context or {}
    context['mangle'] = [row for row in live[MANGLE] if (MANGLE, row['.id']) not in policy]
    route_index = RouteEgressIndex(context)
    rows = [row for row in live[NAT] if (NAT, row['.id']) not in removing]
    sources = [ipaddress.IPv4Network(value) for value in previous['sources']]
    for check in op['static_checks']:
        target = ipaddress.IPv4Network(check['cidr'])
        destinations = []
        for value in previous.get('destinations', []):
            old = ipaddress.IPv4Network(value)
            if old.overlaps(target):
                destinations.append(old if old.subnet_of(target) else target)
        if not destinations:
            continue
        interface, reason = _egress(check['cidr'], check['gateway'], context, route_index=route_index)
        if interface is None or not preserves_sources(rows, sources, destinations, interface, context):
            raise RouterError('替代静态 NAT 无法证明覆盖原策略的来源、目的及出口，保留原来源策略：'
                              + check['cidr'] + (('；' + reason) if reason else ''))


def apply(driver, op):
    live = snapshot(driver)
    # Ordinary static routes can be written earlier in the same stage. Their
    # IDs are guarded separately; every other policy dependency must be stable.
    expected = {key: value for key, value in op['evidence'].items() if key != ROUTES}
    actual = {key: value for key, value in stable(live).items() if key != ROUTES}
    if actual != expected:
        raise RouterError('策略路由配置在预览后发生变化，请重新预览')
    owned = owned_rows(op.get('previous'), live)
    check_references(owned, live)
    for migration in op['migrations']:
        _guard(driver, migration)
    journal, resources = [], []
    ids = {path: {row['.id'] for row in rows} for path, rows in live.items()}
    try:
        for preparation in op.get('prepare_static', []):
            undo = driver.apply(preparation)
            journal.append({'action': 'operation', 'undo': undo})
        if op.get('static_checks'):
            from backend.routers.base import is_managed, StaticRouteIndex
            state = driver.read_state()
            route_index = StaticRouteIndex(state.routes)
            for check in op['static_checks']:
                candidates = route_index.select(check['cidr'], check['gateway']).candidates
                if not candidates or not all(is_managed(route.comment) for route in candidates):
                    raise RouterError('替代静态路由尚未生效，保留原来源策略：' + check['cidr'])
        if op.get('static_nat_checks'):
            from backend.routers.release import verify_nat
            verify_nat(op['static_nat_checks'], driver.read_state())
            for path in (NAT, ROUTES):
                ids[path].update(row['.id'] for row in driver._list(path))
        for entry in op['bundle']:
            resource = _create(driver, entry['path'], entry['values'], journal, ids)
            resources.append(resource)
            if entry['path'] == ROUTES:
                check_route_egress(_read(driver, ROUTES, resource['id']), op['spec']['gateway'], op['spec']['egress_interface'])
        for replacement in op.get('replacements', []):
            _create(driver, replacement['path'], replacement['values'], journal, ids)
        _verify_replacement_nat(driver, op, resources)
        for migration in op['migrations']:
            _delete(driver, migration, journal)
        # Reverse creation order: withdraw the old entry rule first, then its
        # private chain/NAT/route/table and finally its list members.
        check_references(owned, snapshot(driver))
        for entry in reversed(owned):
            _delete(driver, entry, journal)
    except RouterError as error:
        undo = {'steps': list(reversed(journal)), 'previous': op.get('previous')}
        try:
            restore(driver, undo)
        except RouterError as rollback_error:
            raise RouterRollbackError(f'{error}；{rollback_error}') from error
        raise
    driver.ros_policy_manifest = {'spec': op['spec'], 'resources': resources} if resources else None
    return {'kind': 'ros_policy_restore', 'steps': list(reversed(journal)), 'previous': op.get('previous')}


def verify(driver, check):
    manifest = getattr(driver, 'ros_policy_manifest', check.get('previous'))
    live = snapshot(driver)
    owned = owned_rows(manifest, live)
    if check['spec'] is None:
        if owned or owned_rows(check.get('previous'), live):
            raise RouterError('策略路由撤销后仍有受管对象')
        return
    # ROS may briefly report invalid=true while installing a new firewall chain.
    # Wait only for this runtime transition; config changes still fail every read.
    keys = {(entry['path'], entry['id']) for entry in owned}
    for _ in range(8):
        if not any((path, row.get('.id')) in keys
                   and str(row.get('invalid', '')).lower() in ('true', 'yes', '1')
                   for path, rows in live.items() for row in rows):
            break
        time.sleep(0.25)
        live = snapshot(driver)
        owned = owned_rows(manifest, live)
    if not manifest or manifest.get('spec') != check['spec'] or len(owned) != len(manifest['resources']):
        raise RouterError('策略路由回读校验失败：来源、目的或转发资源不完整')
    check_references(owned, live)
    check_policy_order(live)
    bundle_health(manifest, live)
    owned_keys = {(entry['path'], entry['id']) for entry in owned}
    if any(enabled(row) and row.get('action') == 'fasttrack-connection' for row in live[FILTER]):
        raise RouterError('发布期间出现 FastTrack，无法确认来源策略继续生效')
    from backend.routers.routeros_nat import assess_nat, _egress, RouteEgressIndex
    state = driver.read_state()
    state.nat_context.update(
        mangle=[row for row in live[MANGLE] if (MANGLE, row['.id']) not in owned_keys],
        routing_rules=live[RULES], vrfs=live[VRFS], routes=live[ROUTES])
    if any(enabled(row) and row.get('chain') == 'prerouting'
           and row.get('action') in {'mark-routing', 'route', 'jump', 'accept', 'return'}
           for row in state.nat_context['mangle']):
        raise RouterError('发布期间出现其他 prerouting 分流或终止规则，请重新预览')
    route_index = RouteEgressIndex(state.nat_context)
    interface, reason = _egress(check['spec']['destinations'][0], check['spec']['gateway'], state.nat_context,
                               policy_routing=True, route_index=route_index)
    if interface is None:
        raise RouterError('回读无法确认策略下一跳出口：' + reason)
    if check['spec'].get('egress_interface', interface) != interface:
        raise RouterError('策略下一跳出口在发布期间发生变化')
    if check['spec']['nat_interface']:
        if interface != check['spec']['nat_interface']:
            raise RouterError('策略下一跳出口在发布期间发生变化')
    else:
        for cidr in check['spec']['destinations']:
            assessment = assess_nat(cidr, check['spec']['gateway'], state, policy_routing=True,
                                    route_index=route_index)
            if assessment['status'] != 'not_required':
                raise RouterError('策略流量不再能确认无需 NAT 免转换：' + assessment['detail'])
    for entry in owned:
        if entry['path'] == ROUTES:
            row = _read(driver, ROUTES, entry['id'])
            check_route_egress(row, check['spec']['gateway'], interface)
    for migration in check.get('migrations', []):
        if any(row.get('.id') == migration['id'] for row in live[migration['path']]):
            raise RouterError('应迁移的受管路由或 NAT 免转换仍然存在')
    for replacement in check.get('replacements', []):
        expected = {key: value for key, value in replacement['values'].items() if key != 'place-before'}
        if not any(_matches(row, expected) for row in live[replacement['path']]):
            raise RouterError('迁移后应保留的 NAT 目的范围不完整')
