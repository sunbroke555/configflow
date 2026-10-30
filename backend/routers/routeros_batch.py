"""Batched RouterOS static-route (and NAT rule) writes for thousands of CIDRs.

Written one at a time, every route costs a destination read, the write and a
read-back. Runs of additions instead go to the router as /execute scripts of
up to ROUTE_BATCH console adds, proven by the ID each add returns and one
destination-filtered read before and after. Runs of deletions are one
comma-separated remove, checked with one ID-filtered read. Rollback uses the
same batches. ``path`` selects /ip/route (the default) or /ipv6/route, whose
IPv6 rule-set routes use the same proofs. Measured on RouterOS 7.24.5 (x86 CHR, 3000 other routes): 100
console adds 0.17 s, a read of 200 destinations 0.08 s and of 200 IDs 0.8 s,
against 4-8 s for the table.

A batch records only operations it has fully proven. Whatever it cannot prove,
or the router rejects, goes through the single-route transaction, which reads,
writes and reports exactly as before.
"""
import re
import time
from typing import Any, Callable, Dict, List, Optional

from backend.routers.base import RouterError, RouterRollbackError
from backend.routers.routeros import NAT, ROUTE_PATHS, ROUTES, UncertainWrite, _comparable, _truthy

# 100 adds (21 kB) ran in 0.17 s and 300 (63 kB) in 0.39 s on the CHR: a wide
# margin under the 10 s request timeout on boards ten times slower.
ROUTE_BATCH = 100
# Smaller runs keep single writes: no fewer requests would be needed.
BATCH_MIN = 4
# The console caps one executed script at 64 kB.
SCRIPT_BYTES = 32 * 1024
QUERY_TERMS = 200
REMOVE_BATCH = 500
ROW_ID = re.compile(r'\*[0-9a-fA-F]+')
SKIPPED = 'skipped'
UNCERTAIN = 'RouterOS {path} 已提交但没有返回可信的新对象 ID；无法确认新增对象归属，已保留现场，请检查路由器'
# Fields a console add takes as they read back over REST. Others (blackhole is
# a bare flag on the console, a bare false a syntax error) use REST writes.
CONSOLE_FIELDS = frozenset({'dst-address', 'gateway', 'routing-table', 'distance', 'scope', 'target-scope',
                            'comment', 'disabled'})


def _query(driver, field: str, values: List[str], path: str = ROUTES) -> List[Dict[str, Any]]:
    """Rows whose ``field`` equals any value, via POST print with an OR query."""
    rows = []
    values = list(dict.fromkeys(values))
    for start in range(0, len(values), QUERY_TERMS):
        chunk = values[start:start + QUERY_TERMS]
        query = [f'{field}={value}' for value in chunk] + (['#' + '|' * (len(chunk) - 1)] if len(chunk) > 1 else [])
        result = driver._request('POST', path + '/print', {'.query': query})
        if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
            raise RouterError(f'RouterOS {path} 返回的列表格式无法识别')
        rows.extend(result)
    return rows


def _retry(read: Callable[[], Any], attempts: int = 3):
    for attempt in range(attempts):
        try:
            return read()
        except RouterError:
            if attempt + 1 == attempts:
                raise
            time.sleep(1)


def _rollback_failed(message: str) -> str:
    return 'RouterOS 回滚未完成：' + message


def _add_values(op: Dict[str, Any]) -> Dict[str, Any]:
    # The same payload as the single-route write in RouterOSDriver.apply.
    return {'dst-address': op['cidr'], 'gateway': op['gateway'], 'comment': op['comment'], 'disabled': 'false'}


def _add_line(index: int, values: Dict[str, Any], path: str = ROUTES) -> str:
    from backend.routers.ros_policy_write import _script_value
    params = ' '.join(f'{key}={_script_value(key, value)}' for key, value in values.items())
    # Stop at the first rejected add, so the routes written are always a prefix.
    return (f':if ($f=0) do={{:onerror e in={{:put ("cf-id:{index}:" . [{path}/add {params}])}}'
            f' do={{:set f 1; :put ("cf-err:{index}:" . $e)}}}}')


def _batches(ops: List[Dict[str, Any]], index: int) -> List[Dict[str, Any]]:
    """The next run of ``ops[index]``'s kind a single batch can carry."""
    kind = ops[index]['kind']
    batch, seen, size = [], set(), 0
    for op in ops[index:]:
        if op['kind'] != kind or len(batch) == ROUTE_BATCH or not op.get('cidr'):
            break
        key = op['cidr'] if kind == 'route_add' else op.get('id')
        if key in seen:
            break  # one destination (or ID) per batch keeps every proof per route
        if kind == 'route_add':
            size += len(_add_line(len(batch), _add_values(op)).encode()) + 1
            if batch and size > SCRIPT_BYTES:
                break
        seen.add(key)
        batch.append(op)
    return batch or [ops[index]]


def apply_routes(driver, ops: List[Dict[str, Any]], record: Callable[[Dict[str, Any], Dict[str, Any]], None]) -> None:
    """Apply ``ops`` in order like ``driver.apply``; ``record(op, undo)`` each one once it is proven."""
    index = 0
    while index < len(ops):
        batch = [ops[index]]
        if ops[index]['kind'] in ('route_add', 'route_delete'):
            batch = _batches(ops, index)
        left = batch
        if len(batch) >= BATCH_MIN:
            left = (_add if batch[0]['kind'] == 'route_add' else _delete)(driver, batch, record)
        for op in left:
            record(op, driver.apply(op))
        index += len(batch)


def _check(driver, row_id: str, values: Dict[str, Any], row: Optional[Dict[str, Any]]) -> None:
    """The single-route read-back check of a new route."""
    driver._check_written({'id': row_id, 'values': values}, row, [])


def console_safe(values: Dict[str, Any]) -> bool:
    return (set(values) <= CONSOLE_FIELDS and bool(values.get('dst-address'))
            and _comparable('disabled', values.get('disabled', 'false')) in ('true', 'false'))


def _refused(error: RouterError) -> bool:
    """Whether a failed /execute certainly ran none of its adds.

    RouterOS answers a refused script with HTTP 4xx, or with HTTP 500 "not
    enough permissions" (UncertainWrite for any other write). Every add in
    one script needs the same policy and catches its own errors, so a
    permission refusal cannot follow a successful add.
    """
    return not isinstance(error, UncertainWrite) or bool(
        re.search(r'\bnot enough permissions\b', str(error), re.IGNORECASE))


def _console_add(driver, values: List[Dict[str, Any]],
                 current: Optional[List[Dict[str, Any]]] = None, path: str = ROUTES) -> Optional[Dict[str, Any]]:
    """Add routes with one /execute and prove which rows are ours.

    ``current`` is a read of these destinations made just before, if any.
    None when nothing ran. Otherwise ``rows`` maps the index of each reported
    add to its row (None when it is gone); ``count`` adds were reported, in
    order; ``failure`` is the first reported add whose ID cannot be proven
    ours, as (index, error); ``unexplained`` means routes the script did not
    report may exist (it was interrupted).
    """
    destinations = [item['dst-address'] for item in values]
    try:
        if current is None:
            current = _query(driver, 'dst-address', destinations, path)
        before = {str(row.get('.id')) for row in current}
    except RouterError:
        return None  # nothing written
    script = ':local f 0\n' + '\n'.join(_add_line(index, item, path) for index, item in enumerate(values)) + '\n:put "cf-done"'
    try:
        result = driver._request('POST', '/execute', {'script': script, 'as-string': ''})
    except RouterError as error:
        if _refused(error):
            return None  # e.g. without the policy /execute needs: single writes follow
        raise RouterRollbackError(f'{error}；批量新增路由的结果未知，无法确认新增对象归属，已保留现场，请检查路由器') from error
    lines = (str(result.get('ret', '')) if isinstance(result, dict) else '').replace('\r', '').split('\n')
    ids, rejected = [], False
    for line in lines:
        match = re.fullmatch(r'cf-(id|err):(\d+):(.*)', line)
        if not match or int(match[2]) != len(ids) or rejected:
            continue
        if match[1] == 'err':
            rejected = True
        else:
            ids.append(match[3].strip())
    complete = 'cf-done' in lines and (rejected or len(ids) == len(values))
    try:
        after = _retry(lambda: _query(driver, 'dst-address', destinations, path))
    except RouterError as error:
        raise RouterRollbackError(f'{error}；批量新增的路由无法回读，未删除任何不明归属的对象，请检查路由器') from error
    by_id = {str(row.get('.id')): row for row in after}
    rows, failure = {}, None
    for index, row_id in enumerate(ids):
        row = by_id.get(row_id)
        if row is None and ROW_ID.fullmatch(row_id):
            try:
                row = driver._find(path, row_id)  # gone, or moved to another destination
            except RouterError as error:
                failure = failure or (index, RouterRollbackError(f'{error}；无法确认新增对象归属，已保留现场，请检查路由器'))
                continue
        if (not ROW_ID.fullmatch(row_id) or row_id in before or ids.index(row_id) != index
                or (row is not None and (_truthy(row.get('dynamic'))
                                         or not driver._in_scope(path, row, values[index])))):
            failure = failure or (index, RouterRollbackError(UNCERTAIN.format(path=path)))
            continue
        rows[index] = row
    unexplained = not complete and any(
        str(row.get('.id')) not in before and str(row.get('.id')) not in ids
        and any(driver._in_scope(path, row, item) for item in values[len(ids):]) for row in after)
    return {'rows': rows, 'count': len(ids), 'failure': failure, 'unexplained': unexplained}


def _add(driver, ops, record, path: str = ROUTES) -> List[Dict[str, Any]]:
    """Add one batch; return the operations still to be written one by one."""
    values = [_add_values(op) for op in ops]
    added = _console_add(driver, values, path=path)
    if added is None:
        return ops  # each route reads and writes on its own
    owned, failure = [], added['failure']
    for index, row in sorted(added['rows'].items()):
        if row is None:
            failure = failure if failure and failure[0] < index else (index, None)
            continue
        owned.append((index, row))
        if failure is None or index < failure[0]:
            try:
                _check(driver, row['.id'], values[index], row)
            except RouterError as error:
                failure = (index, error)
    undos = {index: {'kind': 'ros_restore', 'changes': [{
        'method': 'DELETE', 'path': path, 'id': row['.id'],
        'expected': driver._ownership_snapshot(path, {**values[index], **row})}]} for index, row in owned}
    first = failure[0] if failure else added['count']
    for index in range(first):
        record(ops[index], undos[index])
    if failure is not None:
        index, error = failure
        if error is None:
            error = RouterError('RouterOS 回读校验失败：没有找到已写入的对象')
        # Routes after the failed one never existed in a one-by-one release.
        extra = [undos[item]['changes'][0] for item, _ in owned if item >= index]
        problems = [problem for problem in restore_routes(driver, [{'kind': 'ros_restore', 'changes': [change]}
                                                                   for change in extra], path=path) if problem]
        if problems:
            raise RouterRollbackError(f'{error}；{problems[0]}') from error
        raise error
    if added['unexplained']:
        raise RouterRollbackError(f'RouterOS {path} 批量写入未执行完成；无法确认新增对象归属，已保留现场，请检查路由器')
    return ops[added['count']:]


def _delete(driver, ops, record, path: str = ROUTES) -> List[Dict[str, Any]]:
    """Delete one batch; return the operations still to be deleted one by one."""
    try:
        rows = _query(driver, 'dst-address', [op['cidr'] for op in ops], path)
    except RouterError:
        return ops
    by_id = {str(row.get('.id')): row for row in rows}
    prepared = []
    for op in ops:
        row = by_id.get(str(op['id']))
        # Anything unusual (gone, moved, dynamic) gets the single-route checks and errors.
        if row is None or _truthy(row.get('dynamic')) or row.get('dst-address') != op['cidr']:
            break
        known = [str(item.get('.id')) for item in rows if item.get('dst-address') == row.get('dst-address')]
        prepared.append((op, {'kind': 'ros_restore', 'changes': [{
            'method': 'RESTORE', 'path': path, 'id': str(op['id']), 'values': driver._payload(path, row),
            'expected': driver._ownership_snapshot(path, row), 'known_ids': known}]}))
    if len(prepared) < BATCH_MIN:
        return ops
    ids = [str(op['id']) for op, _ in prepared]
    failure = None
    for start in range(0, len(ids), REMOVE_BATCH):
        try:
            driver._request('POST', path + '/remove', {'.id': ','.join(ids[start:start + REMOVE_BATCH])})
        except RouterError as error:
            failure = error  # removal stops at the first failure; the read below shows how far it got
            break
    try:
        remaining = {str(row.get('.id')) for row in _retry(lambda: _query(driver, '.id', ids, path))}
    except RouterError:
        # Some routes may be gone. A present, unchanged route restores as a no-op.
        for op, undo in prepared:
            record(op, undo)
        raise
    left = []
    for op, undo in prepared:
        if str(op['id']) in remaining:
            left.append(op)
        else:
            record(op, undo)
    if failure is not None:
        # A route that vanished before the remove reached it cannot be told
        # apart from one it removed; restoring it on rollback is the safe side.
        # The release still fails there, as the one-by-one delete would have.
        raise failure
    return left + ops[len(prepared):]


def batch_restorable(undo: Dict[str, Any]) -> bool:
    changes = undo.get('changes') if isinstance(undo, dict) and undo.get('kind') == 'ros_restore' else None
    return (isinstance(changes, list) and len(changes) == 1 and changes[0].get('path') in ROUTE_PATHS
            and changes[0].get('method') in ('DELETE', 'RESTORE'))


def restore_routes(driver, undos: List[Dict[str, Any]], stop_on_failure: bool = False,
                   path: Optional[str] = None) -> List[Optional[str]]:
    """Run undo records like ``driver.apply``; return each one's error text, or None.

    With ``stop_on_failure`` the records after a failed one return SKIPPED.
    Runs batch only records of one route path (``path`` limits them to one).
    """
    errors: List[Optional[str]] = []
    index = 0
    while index < len(undos):
        if stop_on_failure and any(error for error in errors):
            errors.extend([SKIPPED] * (len(undos) - index))
            break
        method = (undos[index]['changes'][0]['method'] if batch_restorable(undos[index])
                  and undos[index]['changes'][0]['path'] == (path or undos[index]['changes'][0]['path']) else None)
        run_path = undos[index]['changes'][0]['path'] if method else None
        end = index + 1
        while (method and end < len(undos) and batch_restorable(undos[end])
               and undos[end]['changes'][0]['method'] == method and undos[end]['changes'][0]['path'] == run_path):
            end += 1
        group = undos[index:end]
        # A console batch cannot stop at a failed add (later ones are already
        # written), so stopping restores go one by one.
        if method and len(group) >= BATCH_MIN and not (stop_on_failure and method == 'RESTORE'):
            changes = [undo['changes'][0] for undo in group]
            errors.extend(_restore_deleted(driver, changes, stop_on_failure, run_path) if method == 'DELETE'
                          else _restore_removed(driver, changes, run_path))
        else:
            for undo in group:
                if stop_on_failure and any(error for error in errors):
                    errors.append(SKIPPED)
                    continue
                errors.append(_single(lambda: driver.apply(undo)))
        index = end
    return errors


def _single(action) -> Optional[str]:
    try:
        action()
    except RouterError as error:
        return str(error)
    return None


def _restore_deleted(driver, changes, stop_on_failure, path: str = ROUTES) -> List[Optional[str]]:
    """Remove routes this release added, unless they changed since."""
    errors: List[Optional[str]] = [None] * len(changes)

    def fail(index, error):
        # With stop_on_failure nothing after the first failure is touched.
        errors[index] = SKIPPED if stop_on_failure and any(errors[:index]) else error()

    def single(index):
        fail(index, lambda: _single(lambda: driver._restore([changes[index]])))

    try:
        rows = _query(driver, 'dst-address', [str(change['expected'].get('dst-address', '')) for change in changes], path)
    except RouterError:
        rows = None
    by_id = {str(row.get('.id')): row for row in rows or []}
    pending: List[int] = []

    def flush():
        if not pending:
            return
        ids = [str(changes[index]['id']) for index in pending]
        for start in range(0, len(ids), REMOVE_BATCH):
            try:
                driver._request('POST', path + '/remove', {'.id': ','.join(ids[start:start + REMOVE_BATCH])})
            except RouterError:
                break  # the remove stops at the first failure: the rest are still there
        try:
            remaining = {str(row.get('.id')) for row in _retry(lambda: _query(driver, '.id', ids, path))}
        except RouterError:
            remaining = set(ids)  # the single path re-reads each one
        for index in pending:
            if str(changes[index]['id']) in remaining:
                single(index)
        pending.clear()

    for index, change in enumerate(changes):
        if stop_on_failure and any(errors[:index]):
            errors[index:] = [SKIPPED] * (len(changes) - index)
            break
        row = by_id.get(str(change['id']))
        if rows is not None and row is not None and 'expected' in change and not _truthy(row.get('dynamic')) \
                and driver._ownership_snapshot(path, row) == change.get('expected'):
            pending.append(index)
            continue
        flush()  # keep the order: earlier routes go first
        if rows is None or row is None or 'expected' not in change:
            single(index)  # absent or moved: the single path reads it by ID
        elif _truthy(row.get('dynamic')):
            fail(index, lambda: _rollback_failed('RouterOS 动态对象不能接管、修改或删除'))
        else:
            fail(index, lambda: _rollback_failed('对象在操作期间发生外部变化，保留现有配置'))
    flush()
    return errors


def _restore_removed(driver, changes, path: str = ROUTES) -> List[Optional[str]]:
    """Recreate routes this release deleted: one presence read, then console
    adds in batches, each checked against a fresh read of its destinations
    (the rest take the single-route path)."""
    errors: List[Optional[str]] = [None] * len(changes)

    def single(index):
        errors[index] = _single(lambda: driver._restore([changes[index]]))

    try:
        present = {str(row.get('.id')): row for row in _query(driver, '.id', [str(change['id']) for change in changes], path)}
    except RouterError:
        for index in range(len(changes)):
            single(index)
        return errors
    pending: List[int] = []

    def flush():
        chunk = pending[:]
        pending.clear()
        try:
            # As one by one: the same-destination check reads just before the
            # write, so it sees what this rollback (or anyone) wrote, or may have.
            rows = _query(driver, 'dst-address', [changes[index]['values']['dst-address'] for index in chunk], path) \
                if len(chunk) >= BATCH_MIN else None
        except RouterError:
            rows = None
        if rows is None:
            for index in chunk:
                single(index)
            return
        adding = []
        for index in chunk:
            values = changes[index]['values']
            if any(str(row.get('.id')) not in changes[index].get('known_ids', [])
                   and row.get('dst-address') == values.get('dst-address')
                   and row.get('routing-table', 'main') == values.get('routing-table', 'main') for row in rows):
                errors[index] = _rollback_failed('回滚期间已有其他程序新增同网段路由，保留现有配置')
            else:
                adding.append(index)
        try:
            added = _console_add(driver, [changes[index]['values'] for index in adding], rows, path) \
                if len(adding) >= BATCH_MIN else None
        except RouterError as error:
            # Which of them were written is unknown: none is retried.
            for index in adding:
                errors[index] = _rollback_failed(str(error))
            return
        if added is None:
            for index in adding:
                single(index)
            return
        failure = added['failure']
        for position, index in enumerate(adding[:added['count']]):
            if position not in added['rows']:
                errors[index] = _rollback_failed(str(failure[1]) if failure and failure[0] == position
                                                 else UNCERTAIN.format(path=path))
                continue
            row = added['rows'][position]
            try:
                _check(driver, (row or {}).get('.id', ''), changes[index]['values'], row)
            except RouterError as error:
                errors[index] = _rollback_failed(str(error))
        for index in adding[added['count']:]:
            if added['unexplained']:
                errors[index] = _rollback_failed(f'RouterOS {path} 批量写入未执行完成；无法确认新增对象归属，已保留现场，请检查路由器')
            else:
                single(index)  # the console rejected it: the single path reports why

    for index, change in enumerate(changes):
        values = change['values']
        current = present.get(str(change['id']))
        if current is not None:
            try:
                if driver._ownership_snapshot(path, current) != change.get('expected'):
                    errors[index] = _rollback_failed('回滚对象已发生外部变化，保留现有配置')
            except RouterError as error:
                errors[index] = _rollback_failed(str(error))
            continue
        if any(_comparable('dst-address', changes[item]['values'].get('dst-address', ''))
               == _comparable('dst-address', values.get('dst-address', '')) for item in pending):
            flush()  # its outcome decides the same-destination check of this one
        if not console_safe(values):
            single(index)
            continue
        pending.append(index)
        if len(pending) == ROUTE_BATCH:
            flush()
    flush()
    return errors


# Console add rejects true/false for yes/no flags; a bare false is even a
# syntax error. Every other value is quoted, and a rule the console rejects
# makes the whole batch fall back to single REST writes.
NAT_FLAGS = ('disabled', 'log')
NAT_SETTLE_READS = 8


def _nat_value(key: str, value: Any) -> str:
    from backend.routers.ros_policy_write import _script_string
    if key in NAT_FLAGS and _comparable(key, value) in ('true', 'false'):
        return 'yes' if _comparable(key, value) == 'true' else 'no'
    return _script_string(value)


def put_nat(driver, changes: List[Dict[str, Any]], undo: List[Dict[str, Any]]) -> int:
    """Write NAT rules of one transaction in /execute batches; return how many were written.

    Each rule is proven like a single PUT (an ID absent from the table before,
    values and order read back), with one table read per batch: the read after
    a batch is the next batch's "before". Rules from the first batch that cannot
    be proven on are left to single REST writes, with nothing of it left behind.
    """
    try:
        rows = driver._list(NAT)
    except RouterError:
        return 0
    handled, batch, size = 0, [], 0
    for change in changes + [None]:
        line = None if change is None else _nat_line(len(batch), change['values'])
        if batch and (change is None or len(batch) == ROUTE_BATCH or size + len(line.encode()) > SCRIPT_BYTES):
            rows = _nat_batch(driver, [item['values'] for item in batch], rows, undo)
            if rows is None:
                return handled
            handled += len(batch)
            batch, size = [], 0
        if change is not None:
            line = _nat_line(len(batch), change['values'])  # renumbered after a flush
            batch.append(change)
            size += len(line.encode()) + 1
    return handled


def _nat_line(index: int, values: Dict[str, Any]) -> str:
    params = ' '.join(f'{key}={_nat_value(key, value)}' for key, value in values.items())
    return (f':if ($f=0) do={{:onerror e in={{:put ("cf-id:{index}:" . [/ip/firewall/nat/add {params}])}}'
            f' do={{:set f 1; :put ("cf-err:{index}:" . $e)}}}}')


def _nat_batch(driver, values, rows, undo):
    """One batch; the table read after it, or None when the caller must write them one by one."""
    before = {str(row.get('.id')) for row in rows}
    script = ':local f 0\n' + '\n'.join(_nat_line(index, item) for index, item in enumerate(values)) + '\n:put "cf-done"'
    try:
        result = driver._request('POST', '/execute', {'script': script, 'as-string': ''})
    except RouterError as error:
        if _refused(error):
            return None
        raise RouterRollbackError(f'{error}；批量新增 NAT 规则的结果未知，无法确认新增对象归属，已保留现场，请检查路由器') from error
    output = (str(result.get('ret', '')) if isinstance(result, dict) else '').replace('\r', '').split('\n')
    ids = []
    for line in output:
        match = re.fullmatch(r'cf-id:(\d+):(.*)', line)
        if match and int(match[1]) == len(ids):
            ids.append(match[2].strip())
    try:
        rows = _retry(lambda: driver._list(NAT))
        # RouterOS reports freshly installed rules invalid=true for about half
        # a second (319 of 320 just after a batch on the CHR, none 0.6 s later),
        # and the NAT read-back does not count invalid rules. Wait for that
        # runtime transition only, as the policy read-back does.
        for _ in range(NAT_SETTLE_READS):
            if not any(_truthy(row.get('invalid')) and str(row.get('.id')) in ids for row in rows):
                break
            time.sleep(0.25)
            rows = _retry(lambda: driver._list(NAT))
    except RouterError as error:
        raise RouterRollbackError(f'{error}；批量新增的 NAT 规则无法回读，未删除任何不明归属的对象，请检查路由器') from error
    by_id = {str(row.get('.id')): row for row in rows}
    owned, valid = [], 'cf-done' in output and len(ids) == len(values)
    # An interrupted script may have added rules it did not report.
    unproven = 'cf-done' not in output and any(str(row.get('.id')) not in before | set(ids) for row in rows)
    for index, row_id in enumerate(ids):
        row = by_id.get(row_id)
        if not ROW_ID.fullmatch(row_id) or row_id in before or ids.index(row_id) != index:
            unproven, valid = True, False  # never delete what this batch cannot prove it created
            continue
        if row is None:
            valid = False
            continue
        expected = None
        try:
            expected = driver._ownership_snapshot(NAT, {**values[index], **row})
            if valid:
                driver._check_written({'id': row_id, 'values': values[index]}, row, rows)
        except RouterError:
            valid = False
        owned.append((row_id, expected))
    if valid:
        undo.extend({'method': 'DELETE', 'path': NAT, 'id': row_id, 'expected': expected} for row_id, expected in owned)
        return rows
    failures = []
    for row_id, expected in owned:
        if expected is None:
            unproven = True
            continue
        try:
            driver._restore([{'method': 'DELETE', 'path': NAT, 'id': row_id, 'expected': expected}])
        except RouterError as error:
            failures.append(str(error))
    if unproven or failures:
        raise RouterRollbackError('；'.join(failures + ([f'RouterOS {NAT} 批量写入结果无法确认，未删除任何不明归属的对象，请检查路由器']
                                                         if unproven else [])))
    return None
