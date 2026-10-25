"""Manual iKuai IPv4 static routes, using the already verified web UI form.

The API has no compare-and-swap write. We re-read before each mutation and
compensate only while a complete fresh snapshot still proves what we changed.
"""
import copy
import ipaddress
import uuid

from backend.routers.base import RouterConflictError, RouterError, RouterRollbackError, is_managed
from backend.routers.ikuai import ROUTE_FIELDS, decode_value

# status is the computed running status, not part of the static_rt edit form.
RUNTIME_FIELDS = {'status'}
ENTRY_IDENTITY = ('id', 'cidr', 'gateway', 'enabled', 'comment', 'table', 'snapshot')


def _entry(raw):
    row = decode_value(copy.deepcopy(raw))
    snapshot = {key: value for key, value in row.items() if key not in RUNTIME_FIELDS}
    if isinstance(snapshot.get('netmask'), str):
        # The existing vendor form removes its display-only mask suffix too.
        snapshot['netmask'] = snapshot['netmask'].split(' ')[0]
    reasons = []
    missing = set(ROUTE_FIELDS) - snapshot.keys()
    unknown = snapshot.keys() - set(ROUTE_FIELDS)
    if missing or unknown:
        reasons.append('路由字段与已验证的 iKuai 4.x 格式不兼容，无法保留完整配置'
                       f'（缺少：{",".join(sorted(missing)) or "无"}；未知：{",".join(sorted(unknown)) or "无"}）')
    for key in ROUTE_FIELDS:
        if key not in snapshot:
            continue
        value = snapshot[key]
        if key in ('id', 'prio'):
            valid = type(value) is int and value >= (1 if key == 'id' else 0)
        else:
            valid = isinstance(value, str)
        if not valid:
            reasons.append(f'路由字段 {key} 类型无法识别')
    if snapshot.get('ip_type') != '4':
        reasons.append('仅支持普通 IPv4 静态路由')
    if snapshot.get('enabled') not in ('yes', 'no'):
        reasons.append('路由启用状态无法识别')
    if not isinstance(snapshot.get('interface'), str) or not snapshot['interface']:
        reasons.append('路由接口不完整')
    if not isinstance(snapshot.get('tagname'), str) or not 1 <= len(snapshot['tagname']) <= 15:
        reasons.append('路由名称无法按原表单保留')
    cidr = ''
    try:
        cidr = str(ipaddress.ip_network(f'{snapshot.get("dst_addr")}/{snapshot.get("netmask")}', strict=False))
        if not isinstance(ipaddress.ip_network(cidr), ipaddress.IPv4Network):
            reasons.append('仅支持普通 IPv4 静态路由')
    except ValueError:
        reasons.append('路由目的地址或掩码无法识别')
    gateway = snapshot.get('gateway')
    try:
        if not isinstance(gateway, str):
            raise ValueError
        ipaddress.IPv4Address(gateway)
    except (ValueError, TypeError):
        reasons.append('仅支持下一跳为 IPv4 地址的静态路由')
    comment = snapshot.get('comment') if isinstance(snapshot.get('comment'), str) else ''
    return {'id': str(snapshot.get('id', '')), 'cidr': cidr,
            'gateway': gateway if isinstance(gateway, str) else '',
            'enabled': snapshot.get('enabled') == 'yes', 'comment': comment, 'table': 'main',
            'editable': not reasons, 'reason': '；'.join(dict.fromkeys(reasons)),
            'managed': is_managed(comment), 'snapshot': snapshot}


def read_routes(driver):
    """Read every page, failing closed on truncation or inconsistent identities."""
    return [_entry(raw) for raw in driver._list_all('static_rt')]


def _values(values):
    if (not isinstance(values, dict) or set(values) != {'cidr', 'gateway', 'enabled', 'comment'}
            or not isinstance(values['cidr'], str) or not isinstance(values['gateway'], str)
            or type(values['enabled']) is not bool or not isinstance(values['comment'], str)):
        raise RouterError('静态路由参数格式无效')
    try:
        network = ipaddress.IPv4Network(values['cidr'], strict=False)
        gateway = str(ipaddress.IPv4Address(values['gateway']))
    except ValueError:
        raise RouterError('静态路由目的网段和下一跳必须是 IPv4') from None
    return {'cidr': str(network), 'gateway': gateway, 'enabled': values['enabled'],
            'comment': values['comment'].strip()}


def _find(entries, row_id):
    return next((entry for entry in entries if entry['id'] == row_id), None)


def _no_duplicate(entries, cidr, own_id=None):
    if any(entry['cidr'] == cidr and entry['id'] != own_id for entry in entries):
        raise RouterConflictError(f'已存在目的网段 {cidr} 的静态路由，请先处理重复配置')


def _snapshot_equal(entry, snapshot):
    return entry is not None and entry['editable'] and entry['snapshot'] == snapshot


def _new_entry(entries, before_ids, payload):
    tagged = [entry for entry in entries if entry['snapshot'].get('tagname') == payload['tagname']]
    if len(tagged) != 1 or tagged[0]['id'] in before_ids or not tagged[0]['editable']:
        return None
    actual = {key: value for key, value in tagged[0]['snapshot'].items() if key != 'id'}
    return tagged[0] if actual == payload else None


def _verify_record(driver, row_id, snapshot):
    entry = _find(read_routes(driver), row_id)
    if not _snapshot_equal(entry, snapshot):
        raise RouterError('静态路由回读与本次提交不一致')
    return entry


def _restore_update(driver, before, desired):
    latest = read_routes(driver)
    current = _find(latest, before['id'])
    if _snapshot_equal(current, before['snapshot']):
        return
    if not _snapshot_equal(current, desired):
        raise RouterError('路由已被其他操作修改或删除，保留现状，未覆盖外部变更')
    _no_duplicate(latest, before['cidr'], before['id'])
    try:
        driver._write('static_rt', 'edit', driver._route_payload(before['snapshot']))
    except RouterError:
        # A lost edit response is reconciled by exact readback, not retried.
        _verify_record(driver, before['id'], before['snapshot'])
        return
    _verify_record(driver, before['id'], before['snapshot'])


def _restore_delete(driver, before):
    latest = read_routes(driver)
    current = _find(latest, before['id'])
    if _snapshot_equal(current, before['snapshot']):
        return
    if current is not None:
        raise RouterError('原路由 ID 已发生变化，保留现状，未覆盖外部变更')
    _no_duplicate(latest, before['cidr'])
    payload = driver._route_payload(before['snapshot'])
    payload.pop('id')
    if any(entry['snapshot'].get('tagname') == payload['tagname'] for entry in latest):
        raise RouterError('原路由名称已被其他配置占用，未自动重建')
    before_ids = {entry['id'] for entry in latest}
    try:
        driver._write('static_rt', 'add', payload)
    except RouterError:
        if _new_entry(read_routes(driver), before_ids, payload) is None:
            raise
        return
    if _new_entry(read_routes(driver), before_ids, payload) is None:
        raise RouterError('删除失败后的原路由重建无法精确确认，请检查路由列表')


def _restore_add(driver, previous, payload):
    latest = read_routes(driver)
    before_ids = {entry['id'] for entry in previous}
    entry = _new_entry(latest, before_ids, payload)
    if entry is None:
        old = {entry['id']: entry['snapshot'] for entry in previous}
        now = {entry['id']: entry['snapshot'] for entry in latest}
        if now == old:
            return
        raise RouterError('无法唯一确认本次新增路由，已保留所有配置，未按网段或网关猜测删除')
    try:
        driver._write('static_rt', 'del', {'id': entry['snapshot']['id']})
    except RouterError:
        if _find(read_routes(driver), entry['id']) is not None:
            raise
        return
    if _find(read_routes(driver), entry['id']) is not None:
        raise RouterError('本次新增路由撤销后仍然存在')


def mutate_route(driver, action, expected, values):
    """Mutate one ordinary static route, preserving every non-editor field."""
    if action not in ('add', 'update', 'delete'):
        raise RouterError('不支持的静态路由操作')
    if action != 'delete':
        values = _values(values)
    if action == 'add' and expected is not None:
        raise RouterError('新增路由不能指定旧记录')
    if action != 'add' and (not isinstance(expected, dict)
                            or not all(key in expected for key in ENTRY_IDENTITY)):
        raise RouterError('缺少完整旧路由快照，请刷新列表后重试')
    previous = read_routes(driver)
    before = None
    if action != 'add':
        before = _find(previous, expected['id'])
        if before is None or any(before[key] != expected[key] for key in ENTRY_IDENTITY):
            raise RouterConflictError('静态路由在读取后发生变化，请刷新列表后重试')
        if not before['editable']:
            raise RouterConflictError(before['reason'] or '这条路由暂不支持编辑')
    if action != 'delete':
        _no_duplicate(previous, values['cidr'], before['id'] if before else None)
    if action == 'update' and all(before[key] == values[key] for key in values):
        return {'entry': before, 'changed': False}
    if action == 'add':
        tagname = 'RT' + uuid.uuid4().hex[:13]
        if any(entry['snapshot'].get('tagname') == tagname for entry in previous):
            raise RouterConflictError('新路由名称已被占用，请重试')
        payload = {'enabled': 'yes', 'tagname': tagname, 'ip_type': '4', 'interface': 'auto',
                   'dst_addr': '', 'gateway': '', 'netmask': '', 'prio': 1, 'comment': ''}
    else:
        payload = driver._route_payload(before['snapshot'])
    if action != 'delete':
        network = ipaddress.IPv4Network(values['cidr'])
        payload.update(dst_addr=str(network.network_address), netmask=str(network.netmask),
                       gateway=values['gateway'], enabled='yes' if values['enabled'] else 'no',
                       comment=values['comment'])
    try:
        if action == 'delete':
            driver._write('static_rt', 'del', {'id': before['snapshot']['id']})
            if _find(read_routes(driver), before['id']) is not None:
                raise RouterError('路由删除后回读仍然存在')
            return {'entry': None, 'changed': True, 'id': before['id']}
        driver._write('static_rt', 'add' if action == 'add' else 'edit', payload)
        latest = read_routes(driver)
        entry = (_new_entry(latest, {item['id'] for item in previous}, payload) if action == 'add'
                 else _find(latest, before['id']))
        if entry is None or (action == 'update' and not _snapshot_equal(entry, payload)):
            raise RouterError('静态路由回读与本次提交不一致')
        _no_duplicate(latest, values['cidr'], entry['id'])
        return {'entry': entry, 'changed': True}
    except RouterError as error:
        try:
            if action == 'add':
                _restore_add(driver, previous, payload)
            elif action == 'update':
                _restore_update(driver, before, payload)
            else:
                _restore_delete(driver, before)
        except RouterError as rollback_error:
            raise RouterRollbackError(f'{error}；安全补偿未完成：{rollback_error}') from None
        raise RouterError(f'{error}；已确认未保留本次变更') from None
