"""iKuai 4.0.314 DHCP static allocations, captured on an isolated new VM.

See testdata/ikuai_fixed_ip_40314.json. The static_ show prefix is essential:
plain total,data returns the DHCP lease view and omits DNS and ownership.
Empty DNS is intentional: the client inherits the network's DNS on renewal.
"""
import ipaddress
import re
import uuid

from backend.routers.base import RouterConflictError, RouterError, RouterRollbackError
from backend.routers.ikuai import DHCP_STATIC_FIELDS, decode_value, _Rejected
from backend.routers.lan_clients import ipv4, normalize_mac

PREFIX = 'cf-client:'
FIELDS = set(DHCP_STATIC_FIELDS)
RUNTIME = {'termname', 'ip_addr_int', 'hostname'}


def _mac(value):
    mac = normalize_mac(value)
    if not re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}', mac) or int(mac[:2], 16) & 1 or mac == '00:00:00:00:00:00':
        return ''
    return mac


def _fields(row):
    if (set(DHCP_STATIC_FIELDS) - set(row) or set(row) - FIELDS - RUNTIME
            or type(row.get('id')) is not int or row['id'] < 1
            or any(not isinstance(row.get(key), str) for key in set(DHCP_STATIC_FIELDS) - {'id'})
            or row['enabled'] not in ('yes', 'no') or not ipv4(row['ip_addr']) or not _mac(row['mac'])):
        raise RouterError('iKuai 静态 DHCP 配置字段不完整或与已验证格式不同，无法安全修改。')
    result = {key: row[key] for key in FIELDS if key in row and key != 'id'}
    result['mac'] = _mac(result['mac'])
    return result


def _static(driver):
    rows = [decode_value(row) for row in driver._list_all('dhcp_static', 'static_')]
    for row in rows:
        _fields(row)
    return rows


def read(driver):
    return {'servers': [decode_value(row) for row in driver._list_all('dhcp_server')],
            'static': _static(driver), 'leases': driver._list_all('dhcp_lease', ids=False)}


def _scopes(snapshot, ip):
    address, scopes = ipaddress.IPv4Address(ip), []
    for row in snapshot['servers']:
        if row.get('enabled') != 'yes' or type(row.get('status')) is not int or row['status'] != 1:
            continue
        if not row.get('interface') or row.get('check_relay_only') != 0:
            continue
        try:
            start, end = [ipaddress.IPv4Address(value) for value in row['addr_pool'].split('-')]
            network = ipaddress.IPv4Network(f'{start}/{row["netmask"]}', strict=False)
            gateway = ipaddress.IPv4Address(row['gateway'])
            if start > end or end not in network or gateway not in network:
                continue
            if address not in network or address in (network.network_address, network.broadcast_address, gateway):
                continue
        except (ValueError, TypeError, KeyError):
            continue
        scopes.append({'id': row['id'], 'interface': row['interface'], 'gateway': str(gateway),
                       'network': str(network), 'addr_pool': row['addr_pool'], 'netmask': row['netmask']})
    return scopes


def _binding(row):
    return {'vendor': 'ikuai', 'id': str(row['id']), 'ip': row['ip_addr'], 'mac': _mac(row['mac']),
            'comment': row['comment'], 'interface': row['interface'], 'gateway': row['gateway']}


def inspect(snapshot, ip, mac):
    result = {'eligible': False, 'reason': '', 'fixed': False, 'owned': False, 'dynamic': False,
              'scope': None, 'lease': None}
    def denied(reason):
        return {**result, 'reason': reason}
    ip, mac = ipv4(ip), _mac(mac)
    if not ip:
        return denied('固定 IP 仅支持单个 IPv4 地址')
    if not mac:
        return denied('未发现有效 MAC，无法固定 IP')
    same_mac = [row for row in snapshot['static'] if _mac(row.get('mac')) == mac]
    same_ip = [row for row in snapshot['static'] if ipv4(row.get('ip_addr')) == ip]
    if any(_mac(row.get('mac')) != mac for row in same_ip):
        return denied('这个 IP 已静态绑定给其他 MAC，不能覆盖')
    if same_mac:
        exact = [row for row in same_mac if ipv4(row.get('ip_addr')) == ip]
        result['fixed'] = any(row.get('enabled') == 'yes' for row in exact)
        result['owned'] = bool(len(same_mac) == 1 and exact and exact[0]['comment'].startswith(PREFIX))
        if result['owned']:
            result['binding'] = _binding(exact[0])
        return denied('这个 MAC 已有静态分配，保留原设置，不覆盖')
    leases = [row for row in snapshot['leases'] if ipv4(row.get('ip_addr')) == ip or _mac(row.get('mac')) == mac]
    if any(ipv4(row.get('ip_addr')) != ip or _mac(row.get('mac')) != mac for row in leases):
        return denied('当前 DHCP 租约的 IP 与 MAC 已变化，请重新发现设备')
    result['dynamic'] = bool(leases)
    scopes = _scopes(snapshot, ip)
    if not scopes:
        return denied('IP 不在该路由器任何已启用且运行中的 DHCP 网段内，或是网关地址')
    if len(scopes) != 1:
        return denied('IP 对应多个 DHCP 服务，无法唯一确定固定 IP 的目标')
    if leases and any(row.get('interface') != scopes[0]['interface'] for row in leases):
        return denied('当前租约接口与 DHCP 网段不一致，请重新发现设备')
    return {**result, 'eligible': True, 'scope': scopes[0]}


def preview(driver, ip, mac, name):
    state = inspect(read(driver), ip, mac)
    if not state['eligible']:
        raise RouterConflictError(state['reason'])
    ip, mac = ipv4(ip), _mac(mac)
    name = str(name or ip).strip()
    if len(name) > 80 or any(ord(char) < 32 for char in name):
        raise RouterError('设备名称不能超过 80 字符或包含控制字符')
    return {'kind': 'add', 'ip': ip, 'mac': mac, 'name': name, 'scope': state['scope'],
            'comment': PREFIX + name,
            'summary': f'iKuai：新增静态分配 {state["scope"]["interface"]} {ip} ↔ {mac}（DNS 沿用网段）'}


def _row(driver, row_id):
    # Do not use IKuaiDriver._row: its default total,data is the wrong view.
    return next((row for row in _static(driver) if str(row['id']) == str(row_id)), None)


def _matches(row, binding):
    return row is not None and all(_binding(row).get(key) == binding.get(key)
                                  for key in ('vendor', 'id', 'ip', 'mac', 'comment', 'interface', 'gateway'))


def _remove(driver, binding, expected):
    current = _row(driver, binding['id'])
    if current is None:
        return
    if not binding.get('comment', '').startswith(PREFIX) or not _matches(current, binding) or _fields(current) != expected:
        raise RouterConflictError('iKuai 固定 IP 已被其他操作修改，保留现状，不会删除。')
    try:
        driver._write('dhcp_static', 'del', {'id': str(binding['id'])})
    except _Rejected:
        raise
    except RouterError:
        if _row(driver, binding['id']) is None:
            return
        raise
    if _row(driver, binding['id']) is not None:
        raise RouterError('iKuai 静态分配删除后仍存在，回读校验失败。')


def _expected(payload):
    # The firmware derives hostname from DHCP traffic even when add sends ''.
    return {key: value for key, value in payload.items() if key not in RUNTIME and key != 'id'}


def _new_row(driver, known_ids, payload):
    matches = [row for row in _static(driver) if row['tagname'] == payload['tagname']]
    if len(matches) != 1 or matches[0]['id'] in known_ids or _fields(matches[0]) != _expected(payload):
        return None
    return matches[0]


def apply(driver, op):
    if preview(driver, op.get('ip'), op.get('mac'), op.get('name')) != op:
        raise RouterConflictError('iKuai DHCP 设置在预览后发生变化，请重新预览。')
    known_ids = {row['id'] for row in _static(driver)}
    payload = {'interface': op['scope']['interface'], 'gateway': op['scope']['gateway'],
               'ip_addr': op['ip'], 'mac': op['mac'], 'dns1': '', 'dns2': '', 'enabled': 'yes',
               'hostname': '', 'comment': op['comment'], 'tagname': 'cfip_' + uuid.uuid4().hex[:10]}
    acquired = None
    try:
        driver._write('dhcp_static', 'add', payload)
        # add returns top-level rowid, not results. Read the full static table
        # and match this operation's unique <=15-character tag before taking ownership.
        acquired = _new_row(driver, known_ids, payload)
        if acquired is None:
            raise RouterError('iKuai 新增静态分配回读不一致，无法核对 DNS、IP 或归属。')
        actual = _row(driver, acquired['id'])
        if actual is None or _fields(actual) != _expected(payload):
            raise RouterError('iKuai 静态分配按 ID 回读校验失败。')
        return {**_binding(actual), 'undo': {'expected': _expected(payload)}}
    except _Rejected:
        raise
    except RouterError as error:
        try:
            acquired = acquired or _new_row(driver, known_ids, payload)
            if acquired is not None:
                _remove(driver, _binding(acquired), _expected(payload))
            else:
                rows = _static(driver)
                if any(row['tagname'] == payload['tagname'] for row in rows):
                    raise RouterError('新绑定已发生外部变化，无法安全撤销，保留现场')
        except RouterError as rollback_error:
            raise RouterRollbackError(f'{error}；iKuai 固定 IP 回滚未完成：{rollback_error}') from error
        raise


def rollback(driver, binding):
    if not isinstance(binding.get('undo'), dict):
        raise RouterError('iKuai 固定 IP 回滚缺少本次写入证据。')
    _remove(driver, binding, binding['undo']['expected'])


def preview_delete(driver, binding):
    if binding.get('vendor') != 'ikuai' or not binding.get('comment', '').startswith(PREFIX):
        raise RouterConflictError('只能删除 ConfigFlow 创建的静态分配。')
    current = _row(driver, binding.get('id'))
    if not _matches(current, binding):
        raise RouterConflictError('iKuai 静态分配已不存在或 IP、MAC、归属发生变化，不能删除。')
    return {**_binding(current), 'kind': 'delete', 'expected': _fields(current),
            'summary': f'iKuai：删除 ConfigFlow 创建的静态分配 {binding["interface"]} {binding["ip"]} ↔ {binding["mac"]}'}


def delete(driver, binding):
    op = preview_delete(driver, binding)
    if 'expected' in binding and op != binding:
        raise RouterConflictError('iKuai 静态分配在预览后发生变化，请重新预览。')
    try:
        _remove(driver, op, op['expected'])
    except _Rejected:
        raise
    except RouterError as error:
        try:
            # A transient readback error after a successful deletion is not
            # evidence to recreate the reservation under a new, unrecorded ID.
            if _row(driver, op['id']) is None:
                return op
        except RouterError as read_error:
            raise RouterRollbackError(f'{error}；iKuai 删除结果无法确认，请检查路由器：{read_error}') from error
        raise
    return op


def restore_deleted(driver, receipt):
    if receipt.get('kind') != 'delete' or not receipt.get('comment', '').startswith(PREFIX):
        raise RouterError('iKuai 删除回滚缺少归属证据。')
    rows = _static(driver)
    current = next((row for row in rows if str(row['id']) == receipt['id']), None)
    if current:
        if _matches(current, receipt) and _fields(current) == receipt['expected']:
            return _binding(current)
        raise RouterError('iKuai 原静态分配 ID 已被占用，保留现状。')
    if any(ipv4(row['ip_addr']) == receipt['ip'] or _mac(row['mac']) == receipt['mac']
           or row['tagname'] == receipt['expected']['tagname'] for row in rows):
        raise RouterError('iKuai 删除回滚时 IP、MAC 或名称已被占用，保留现状。')
    known_ids = {row['id'] for row in rows}
    try:
        driver._write('dhcp_static', 'add', {**receipt['expected'], 'hostname': ''})
    except _Rejected:
        raise
    except RouterError:
        restored = _new_row(driver, known_ids, receipt['expected'])
        if restored is not None:
            return _binding(restored)
        raise
    restored = _new_row(driver, known_ids, receipt['expected'])
    if restored is None:
        raise RouterRollbackError('iKuai 删除回滚后无法核对原静态分配。')
    return _binding(restored)
