"""Guarded REST transactions for source-policy bundles.

Address-list members are written by /execute console batches and checked with
one filtered read per list, so thousands of members cost a few dozen requests
instead of one write and one read each. Other objects use individual resource
URLs. Compensation checks the exact last-written configuration before touching
a resource.
"""
import copy
import ipaddress
from itertools import groupby
import time
from urllib.parse import quote, urlencode

from backend.routers import ros_address_lists
from backend.routers.base import RouterError, RouterRollbackError
from backend.routers.ros_policy import (
    ADDRESSES, FILTER, MANGLE, NAT, ROUTES, RULES, TABLES, VRFS, bundle_health,
    check_policy_order, check_references, check_route_egress, config_row, enabled, owned_rows, snapshot, stable,
)
from backend.routers.routeros import ROUTE_FIELDS, MissingResource, UncertainWrite, _comparable, _matches
from backend.routers import routeros_batch
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


def _guard_in(live, item):
    """_guard against a snapshot just read instead of one GET per object."""
    row = next((row for row in live.get(item['path'], []) if row.get('.id') == item['id']), None)
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
    row_id, undo = _put(driver, path, values, journal, ids, restoring=restoring)
    return _confirm(driver, path, values, row_id, undo, _read(driver, path, row_id))


def _put(driver, path, values, journal, ids, *, restoring=False):
    """Write one object and journal it; _confirm checks the read-back."""
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
    return row_id, undo


def _confirm(driver, path, values, row_id, undo, actual):
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


# Measured on RouterOS 7.24 (x86 CHR): 200 members take ~0.1 s, leaving a wide
# margin under the 10 s request timeout on slower boards. The console caps one
# executed script at 64 kB.
ADDRESS_SCRIPT_BYTES = 16 * 1024
ADDRESS_BATCH_MEMBERS = 200
ADDRESS_REMOVE_BATCH = 500


def _script_string(value):
    text = str(value)
    for raw, escaped in (('\\', '\\\\'), ('"', '\\"'), ('$', '\\$'), ('\n', '\\n'), ('\r', '\\r'), ('\t', '\\t')):
        text = text.replace(raw, escaped)
    return '"' + text + '"'


def _script_value(key, value):
    # REST accepts true/false, but console commands reject them for yes/no flags.
    if key == 'disabled' and _comparable(key, value) in ('true', 'false'):
        return 'yes' if _comparable(key, value) == 'true' else 'no'
    return _script_string(value)


def _address_rows(driver, name, attempts=1):
    for attempt in range(attempts):
        try:
            return ros_address_lists.fetch(driver, name)
        except RouterError:
            if attempt + 1 == attempts:
                raise
            time.sleep(1)


def _run_address_batches(driver, shared, addresses):
    """Add members in console batches; return the failure count and first reason."""
    params = ' '.join(f'{key}={_script_value(key, value)}' for key, value in shared.items())
    head = ':local n 0\n:local m ""\n:foreach a in={'
    tail = ('} do={:onerror e in={/ip/firewall/address-list/add ' + params + ' address=$a}'
            ' do={:set n ($n+1); :if ($n=1) do={:set m ($a . " " . $e)}}}\n:put ("cf-done:" . $n . ":" . $m)')
    budget = ADDRESS_SCRIPT_BYTES - len(head.encode()) - len(tail.encode())
    batches, current, size = [], [], 0
    for address in addresses:
        item = _script_string(address)
        if current and (size + len(item) + 1 > budget or len(current) == ADDRESS_BATCH_MEMBERS):
            batches.append(current)
            current, size = [], 0
        current.append(item)
        size += len(item) + 1
    if current:
        batches.append(current)
    failed, first = 0, ''
    ros_address_lists.invalidate(driver)
    for batch in batches:
        result = driver._request('POST', '/execute', {'script': head + ';'.join(batch) + tail, 'as-string': ''})
        output = str(result.get('ret', '')) if isinstance(result, dict) else ''
        marker = output.rfind('cf-done:')
        count, _, message = output[marker + len('cf-done:'):].partition(':') if marker >= 0 else ('', '', '')
        if not count.strip().isdigit():
            # A syntax error or an interrupted script prints no completion marker.
            raise RouterError('批量写入地址列表未执行完成：' + (output.strip()[:200] or 'RouterOS 无输出'))
        failed += int(count)
        first = first or message.strip()
    return failed, first


def _settled_address_rows(driver, name):
    """After a lost reply the batch may still run; wait until the list stops growing."""
    rows = _address_rows(driver, name, attempts=3)
    for _ in range(10):
        time.sleep(1)
        latest = _address_rows(driver, name, attempts=3)
        if len(latest) == len(rows):
            return latest
        rows = latest
    raise RouterRollbackError('地址列表批量写入结果不明确，未删除任何不明归属的对象，请检查路由器：' + name)


def _requested(rows, shared, addresses, before):
    """Rows absent before a batch write that match one requested member."""
    wanted = {}
    for address in addresses:
        wanted.setdefault(_comparable('address', address), []).append({**shared, 'address': address})
    matched = []
    for row in rows:
        candidates = wanted.get(_comparable('address', row.get('address', '')))
        if row['.id'] not in before and candidates and _matches_values(ADDRESSES, row, candidates[0]):
            candidates.pop(0)
            matched.append(row)
    return matched


def _create_addresses(driver, entries, journal, ids, created=None):
    """Create address-list members in batches and journal every row they produced.

    One filtered read per list proves that each requested member exists exactly
    once with the submitted values. Rows nobody requested are never journaled.
    Until that read succeeds, a list-scoped sweep step stands in for the rows,
    so a failed read cannot leave a whole batch outside the rollback. Matched
    rows are also recorded in ``created`` (index -> resource) as they are found.
    """
    groups = {}
    for index, values in enumerate(entries):
        shared = tuple((key, value) for key, value in values.items() if key != 'address')
        groups.setdefault(shared, []).append((index, values))
    resources = [None] * len(entries)
    for shared, members in groups.items():
        shared = dict(shared)
        name = shared.get('list')
        if not name:
            raise RouterError('地址列表条目缺少列表名')
        before = {row['.id'] for row in ros_address_lists.read(driver, [name])[name]}
        sweep = {'action': 'sweep', 'path': ADDRESSES, 'list': name, 'shared': shared,
                 'addresses': [values['address'] for _, values in members], 'before': sorted(before)}
        journal.append(sweep)
        failure = None
        try:
            failed, reason = _run_address_batches(driver, shared, sweep['addresses'])
        except UncertainWrite as error:
            failure, rows = error, _settled_address_rows(driver, name)
        except RouterError as error:
            failure, rows = error, _address_rows(driver, name, attempts=3)
        else:
            rows = _address_rows(driver, name, attempts=3)
        journal.remove(sweep)
        wanted = {}
        for index, values in members:
            wanted.setdefault(_comparable('address', values['address']), []).append((index, values))
        unknown = []
        for row in rows:
            if row['.id'] in before or row['.id'] in ids[ADDRESSES]:
                continue
            matches = wanted.get(_comparable('address', row.get('address', '')))
            if not matches or not _matches_values(ADDRESSES, row, matches[0][1]):
                unknown.append(row)
                continue
            index, _ = matches.pop(0)
            ids[ADDRESSES].add(row['.id'])
            resources[index] = _resource(ADDRESSES, row)
            journal.append({'action': 'delete', **resources[index]})
            if created is not None:
                created[index] = resources[index]
        if failure is not None:
            raise failure
        if unknown:
            raise RouterError(f'批量写入期间地址列表 {name} 出现未提交的条目，保留这些对象，请检查路由器')
        missing = [values['address'] for items in wanted.values() for _, values in items]
        if not unknown and not missing and not failed:
            ros_address_lists.remember(driver, name, rows)
        if failed or missing:
            detail = f'；首个错误：{reason}' if reason else ''
            raise RouterError(f'地址列表 {name} 有 {max(failed, len(missing))} 条写入失败'
                              f'（{"、".join(missing[:5])}）{detail}')
    return resources


def _remove_addresses(driver, ids):
    ros_address_lists.invalidate(driver)
    for start in range(0, len(ids), ADDRESS_REMOVE_BATCH):
        driver._request('POST', ADDRESSES + '/remove', {'.id': ','.join(ids[start:start + ADDRESS_REMOVE_BATCH])})


def _live_addresses(driver, items):
    lists = ros_address_lists.read(driver, {item['config'].get('list') for item in items})
    return {row['.id']: row for rows in lists.values() for row in rows}


def _forget(driver, items, removed):
    """After removing members, expect the remaining ones so the next read is a digest."""
    names = {item['config'].get('list') for item in items}
    known = ros_address_lists.known_lists(driver)
    for name in names & set(known):
        ros_address_lists.remember(driver, name, [row for row in known[name] if row['.id'] not in removed])
    hints = ros_address_lists.expected_lists(driver)
    for name in names & set(hints) - set(known):
        ros_address_lists.expect(driver, name, [row for row in hints[name] if row['.id'] not in removed])


def _delete_addresses(driver, items, journal):
    """Guard owned members with one read per list, journal them, then remove in batches."""
    ros_address_lists.invalidate(driver)  # destructive: never trust an earlier proof
    live = _live_addresses(driver, items)
    for item in items:
        row = live.get(item['id'])
        if row is None or config_row(row) != config_row(item['config']):
            raise RouterError('策略路由对象在操作期间发生变化，保留现场：' + item['path'] + '/' + item['id'])
    for item in items:
        journal.append({'action': 'restore', **item, 'values': _writable(ADDRESSES, live[item['id']])})
    _remove_addresses(driver, [item['id'] for item in items])
    _forget(driver, items, {item['id'] for item in items})
    if set(_live_addresses(driver, items)) & {item['id'] for item in items}:
        raise RouterError('策略路由回读校验失败：对象仍然存在')


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


def _delete_routes(driver, items, journal):
    """_delete for many routes: one ID read guards them all, one remove deletes them."""
    from backend.routers.routeros_batch import REMOVE_BATCH, _query
    rows = {row.get('.id'): row for row in _query(driver, '.id', [item['id'] for item in items])}
    failure, guarded = None, []
    for item in items:
        row = rows.get(item['id'])
        try:
            if row is None or config_row(row) != config_row(item['config']):
                raise RouterError('策略路由对象在操作期间发生变化，保留现场：' + item['path'] + '/' + item['id'])
            guarded.append({'action': 'restore', **item, 'values': _writable(ROUTES, row)})
        except RouterError as error:
            failure = error  # routes before it are deleted first, as one by one
            break
    journal.extend(guarded)
    removing = [step['id'] for step in guarded]
    try:
        for start in range(0, len(removing), REMOVE_BATCH):
            driver._request('POST', ROUTES + '/remove', {'.id': ','.join(removing[start:start + REMOVE_BATCH])})
    except RouterError:
        # A route gone meanwhile stops the remove there; a single DELETE
        # tolerates that, so delete what is left one by one, as before.
        for row in _query(driver, '.id', removing):
            driver._request('DELETE', ROUTES + '/' + quote(row['.id'], safe='*'))
    if removing and _query(driver, '.id', removing):
        raise RouterError('策略路由回读校验失败：对象仍然存在')
    if failure is not None:
        raise failure


def _restore_routes(driver, steps, ids, remapped, failures):
    """Recreate deleted routes: one ID read, console adds in batches, REST for the rest.

    Each step fails or succeeds on its own, reported in step order as before.
    """
    from backend.routers.routeros_batch import BATCH_MIN, ROUTE_BATCH, UNCERTAIN, _console_add, _query, console_safe
    try:
        present = {row.get('.id'): row for row in _query(driver, '.id', [step['id'] for step in steps])}
    except RouterError:
        for step in steps:  # each step reads and reports on its own, as before
            try:
                _restore_step(driver, step, ids, remapped)
            except RouterError as error:
                failures.append(str(error))
        return
    errors, absent = {}, []
    for index, step in enumerate(steps):
        current = present.get(step['id'])
        if current is None:
            absent.append((index, copy.deepcopy(step['values'])))
        elif config_row(current) != config_row(step['config']):
            errors[index] = '策略路由对象在操作期间发生变化，保留现场：' + step['path'] + '/' + step['id']
    batched = [item for item in absent if console_safe(item[1])]
    single = [item for item in absent if not console_safe(item[1])]
    for start in range(0, len(batched), ROUTE_BATCH):
        chunk = batched[start:start + ROUTE_BATCH]
        try:
            added = _console_add(driver, [values for _, values in chunk]) if len(chunk) >= BATCH_MIN else None
        except RouterError as error:
            # Which of them were written is unknown: none is retried, the
            # other steps go on as one by one.
            for index, _ in chunk:
                errors[index] = str(error)
            continue
        count = added['count'] if added else 0
        for position, (index, values) in enumerate(chunk[:count]):
            if position not in added['rows']:
                failure = added['failure']
                errors[index] = str(failure[1]) if failure and failure[0] == position else UNCERTAIN
                continue
            row = added['rows'][position]
            try:
                if row is not None:
                    ids[ROUTES].add(row['.id'])
                undo = {'action': 'delete', **_resource(ROUTES, row)} if row else {}
                remapped[(ROUTES, steps[index]['id'])] = _confirm(
                    driver, ROUTES, values, (row or {}).get('.id'), undo, row)['id']
            except RouterError as error:
                errors[index] = str(error)
        if added and added['unexplained']:
            for index, _ in chunk[count:]:
                errors[index] = f'RouterOS {ROUTES} 批量写入未执行完成；无法确认新增对象归属，已保留现场，请检查路由器'
            continue
        single.extend(chunk[count:])  # the console rejected it, or too few for a batch
    created = []
    for index, values in sorted(single):
        try:
            created.append((index, values, *_put(driver, ROUTES, values, [], ids, restoring=True)))
        except RouterError as error:
            errors[index] = str(error)
    if created:
        try:
            rows = {row.get('.id'): row for row in _query(driver, '.id', [row_id for _, _, row_id, _ in created])}
        except RouterError as error:
            rows = None
            for index, *_ in created:
                errors[index] = str(error)
        for index, values, row_id, undo in created if rows is not None else []:
            try:
                remapped[(ROUTES, steps[index]['id'])] = _confirm(driver, ROUTES, values, row_id, undo, rows.get(row_id))['id']
            except RouterError as error:
                errors[index] = str(error)
    failures.extend(errors[index] for index in sorted(errors))


def _restore_step(driver, step, ids, remapped, delete_safe=True):
    current = _read(driver, step['path'], step['id'])
    if step['action'] == 'delete':
        if not delete_safe or current is None:
            return
        _guard(driver, step)
        # Do not drop an entire table if another writer has started
        # referencing it since the release. Leave it for inspection.
        if step['path'] == TABLES:
            check_references([step], snapshot(driver, reserve=False))
        driver._request('DELETE', step['path'] + '/' + quote(step['id'], safe='*'))
        if _read(driver, step['path'], step['id']) is not None:
            raise RouterError('回滚删除后对象仍存在')
        return
    if current is not None:
        _guard(driver, step)
        return
    values = copy.deepcopy(step['values'])
    before = values.get('place-before')
    if before:
        values['place-before'] = remapped.get((step['path'], before), before)
        if _read(driver, step['path'], values['place-before']) is None:
            raise RouterError('原规则位置已改变，不能安全恢复顺序')
    restored = _create(driver, step['path'], values, [], ids, restoring=True)
    remapped[(step['path'], step['id'])] = restored['id']


def _restore_delete_addresses(driver, steps):
    """Remove still-present members of a failed release; keep externally changed ones."""
    ros_address_lists.invalidate(driver)
    live = _live_addresses(driver, steps)
    present = [step for step in steps if step['id'] in live]
    changed = [step for step in present if config_row(live[step['id']]) != config_row(step['config'])]
    removing = [step['id'] for step in present if step not in changed]
    _remove_addresses(driver, removing)
    if set(_live_addresses(driver, steps)) & set(removing):
        raise RouterError('回滚删除后对象仍存在')
    if changed:
        raise RouterError('策略路由对象在操作期间发生变化，保留现场：' + ADDRESSES + '/' + changed[0]['id'])


def _restore_sweep(driver, step):
    """Remove members a batch wrote before its rows could be read and journaled."""
    rows = _address_rows(driver, step['list'], attempts=3)
    removing = [row['.id'] for row in _requested(rows, step['shared'], step['addresses'], set(step['before']))]
    _remove_addresses(driver, removing)
    if {row['.id'] for row in _address_rows(driver, step['list'], attempts=3)} & set(removing):
        raise RouterError('回滚删除后对象仍存在')


def _restore_addresses(driver, steps, ids, remapped, configs):
    """Recreate deleted members in batches; members still present must be unchanged."""
    ros_address_lists.invalidate(driver)
    live = _live_addresses(driver, steps)
    changed = [step for step in steps
               if step['id'] in live and config_row(live[step['id']]) != config_row(step['config'])]
    absent = [step for step in steps if step['id'] not in live]
    created = {}
    try:
        if absent:
            _create_addresses(driver, [step['values'] for step in absent], [], ids, created)
    finally:
        # Members recreated before a failure are still ours; keep their new IDs.
        for index, resource in created.items():
            remapped[(ADDRESSES, absent[index]['id'])] = resource['id']
            configs[(ADDRESSES, resource['id'])] = resource['config']
    if changed:
        raise RouterError('策略路由对象在操作期间发生变化，保留现场：' + ADDRESSES + '/' + changed[0]['id'])


def run_steps(driver, steps, ids, remapped, configs, failures, delete_safe, nat_batch=False):
    """Undo journal steps in order, batching members (and routes, and with
    ``nat_batch`` NAT rules that delete_rules removed); errors go to ``failures``."""
    from backend.routers import routeros_nat_list
    for (action, is_address), run in groupby(steps, key=lambda step: (step['action'], step.get('path') == ADDRESSES)):
        if is_address and action in ('delete', 'restore', 'sweep'):
            # Members are batched: one read per list instead of one per member.
            try:
                if action == 'restore':
                    _restore_addresses(driver, list(run), ids, remapped, configs)
                elif action == 'sweep' and delete_safe:
                    for step in run:
                        _restore_sweep(driver, step)
                elif delete_safe:
                    _restore_delete_addresses(driver, list(run))
            except RouterError as error:
                failures.append(str(error))
            continue
        run = list(run)
        if action == 'operation':
            # Keep the replacement protection if the old path could not be
            # restored; report the incomplete rollback for inspection. Route
            # undo records run in batches and stop at the first failure.
            if not failures:
                # NAT list undo puts old rules back before rules this rollback
                # may already have recreated under new IDs.
                moved = [[path, old, new] for (path, old), new in remapped.items()]
                undos = [{**step['undo'], 'remapped': moved} if step['undo'].get('kind') == 'nat_list_restore'
                         else step['undo'] for step in run]
                try:
                    failures.extend(error for error in driver.restore_many(undos, stop_on_failure=True)
                                    if error and error != routeros_batch.SKIPPED)
                except RouterError as error:  # the later steps and the manifest still run
                    failures.append(str(error))
            continue
        index = 0
        while index < len(run):
            if nat_batch and action == 'restore':
                end = index
                while end < len(run) and run[end]['path'] == NAT:
                    end += 1
                if end - index >= routeros_batch.BATCH_MIN:
                    try:
                        routeros_nat_list.restore_rules(driver, run[index:end], ids, remapped, failures)
                    except RouterError as error:  # the later steps still run
                        failures.append(str(error))
                    index = end
                    continue
            end = index
            while (action == 'restore' and end < len(run) and run[end]['path'] == ROUTES
                   and not run[end]['values'].get('place-before')):
                end += 1
            if end - index >= routeros_batch.BATCH_MIN:
                try:
                    _restore_routes(driver, run[index:end], ids, remapped, failures)
                except RouterError as error:  # the later steps and the manifest still run
                    failures.append(str(error))
                index = end
                continue
            step = run[index]
            index += 1
            try:
                _restore_step(driver, step, ids, remapped, delete_safe)
            except RouterError as error:
                failures.append(str(error))


def restore(driver, op):
    failures = []
    remapped = {}
    live = snapshot(driver, reserve=False)
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
    configs = {}
    run_steps(driver, op['steps'], ids, remapped, configs, failures, delete_safe)
    previous = copy.deepcopy(op.get('previous'))
    if previous:
        for item in previous['resources']:
            new_id = remapped.get((item['path'], item['id']))
            if new_id:
                item['id'] = new_id
                config = configs.get((item['path'], new_id))
                row = None if config else _read(driver, item['path'], new_id)
                if config or row:
                    item['config'] = config or config_row(row)
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
    live = snapshot(driver, reserve=False)
    removing = {(item['path'], item['id']) for item in op['owned'] + op['migrations']}
    policy = removing | {(item['path'], item['id']) for item in resources}
    context = state.nat_context or {}
    context['mangle'] = [row for row in live[MANGLE] if (MANGLE, row['.id']) not in policy]
    # List selectors (ConfigFlow's NAT bypass lists among them) prove coverage
    # with the members this read found, as the NAT assessment does.
    context['address_sets'] = state.address_sets
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
        _guard_in(live, migration)
    journal, resources = [], []
    ids = {path: {row['.id'] for row in rows} for path, rows in live.items()}
    try:
        # Thousands of replacement routes are written in batches.
        driver.apply_many(op.get('prepare_static', []),
                          lambda _, undo: journal.append({'action': 'operation', 'undo': undo}))
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
        for is_address, run in groupby(op['bundle'], key=lambda entry: entry['path'] == ADDRESSES):
            run = list(run)
            if is_address:
                resources.extend(_create_addresses(driver, [entry['values'] for entry in run], journal, ids))
                continue
            for entry in run:
                resource = _create(driver, entry['path'], entry['values'], journal, ids)
                resources.append(resource)
                if entry['path'] == ROUTES:
                    check_route_egress(_read(driver, ROUTES, resource['id']), op['spec']['gateway'], op['spec']['egress_interface'])
        for replacement in op.get('replacements', []):
            _create(driver, replacement['path'], replacement['values'], journal, ids)
        _verify_replacement_nat(driver, op, resources)
        # NAT list members first: until the static route goes, traffic to them
        # is only translated, never exempted on its way out of the WAN.
        members = [item for item in op['migrations'] if item['path'] == ADDRESSES]
        if members:
            _delete_addresses(driver, members, journal)
        migrations, index = [item for item in op['migrations'] if item['path'] != ADDRESSES], 0
        while index < len(migrations):
            end = index
            while end < len(migrations) and migrations[end]['path'] == ROUTES:
                end += 1
            if end - index >= routeros_batch.BATCH_MIN:
                _delete_routes(driver, migrations[index:end], journal)
                index = end
                continue
            _delete(driver, migrations[index], journal)
            index += 1
        # Reverse creation order: withdraw the old entry rule first, then its
        # private chain/NAT/route/table and finally its list members.
        check_references(owned, snapshot(driver, reserve=False))
        for is_address, run in groupby(reversed(owned), key=lambda entry: entry['path'] == ADDRESSES):
            if is_address:
                _delete_addresses(driver, list(run), journal)
                continue
            for entry in run:
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
    live = snapshot(driver, reserve=False)
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
        live = snapshot(driver, reserve=False)
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
    present = {(path, row.get('.id')) for path, rows in live.items() for row in rows}
    for migration in check.get('migrations', []):
        if (migration['path'], migration['id']) in present:
            raise RouterError('应迁移的受管路由或 NAT 免转换仍然存在')
    for replacement in check.get('replacements', []):
        expected = {key: value for key, value in replacement['values'].items() if key != 'place-before'}
        if not any(_matches(row, expected) for row in live[replacement['path']]):
            raise RouterError('迁移后应保留的 NAT 目的范围不完整')
