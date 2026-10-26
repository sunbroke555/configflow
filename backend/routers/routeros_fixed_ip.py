"""Explicitly previewed DHCP reservations on RouterOS.

Only the router selected by discovery is opened by the caller. This module
never changes DHCP networks, DNS options, or another MAC's reservation.
"""
import ipaddress
import re
import uuid
from copy import deepcopy
from urllib.parse import quote

from backend.routers.base import RouterError, RouterRollbackError
from backend.routers.lan_clients import ipv4, normalize_mac
from backend.routers.routeros import (
    DHCP_LEASES, DHCP_NETWORKS, DHCP_SERVERS, MissingResource, UncertainWrite, _truthy,
)

PREFIX = 'cf-client:'
ROLLBACK_NOTE = '已移除本次静态预留，客户端续租恢复动态 DHCP，IP 可能变化'
# Runtime DHCP state must not invalidate a preview whenever the client renews.
LEASE_RUNTIME = {
    '.id', 'dynamic', 'status', 'last-seen', 'expires-after', 'host-name', 'age', 'class-id',
    'active-address', 'active-mac-address', 'active-client-id', 'active-server',
    'active-agent-circuit-id', 'active-agent-remote-id',
    'agent-circuit-id', 'agent-remote-id', 'radius', 'blocked',
}
ID = re.compile(r'\*[0-9A-Fa-f]+')


def _id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise RouterError('RouterOS DHCP 租约缺少有效对象 ID')
    return value


def _mac(value):
    mac = normalize_mac(value)
    if not re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}', mac) or int(mac[:2], 16) & 1 \
            or mac == '00:00:00:00:00:00':
        return ''
    return mac


def _fields(row):
    """Writable fields, preserving custom settings without replaying counters."""
    result = {key: value for key, value in row.items()
              if key not in LEASE_RUNTIME and not key.startswith('.')}
    result.setdefault('comment', '')
    result.setdefault('disabled', 'false')
    for key in ('disabled', 'always-broadcast', 'block-access', 'use-src-mac',
                'allow-dual-stack-queue', 'insert-queue-before'):
        if key in result and isinstance(result[key], bool):
            result[key] = str(result[key]).lower()
    if 'mac-address' in result:
        result['mac-address'] = normalize_mac(result['mac-address'])
    return result


def _identity(row):
    return {'ip': ipv4(row.get('address')), 'mac': _mac(row.get('mac-address')),
            'server': str(row.get('server') or ''), 'comment': str(row.get('comment') or '')}


def _row(driver, row_id):
    row_id = _id(row_id)
    try:
        row = driver._get(f'{DHCP_LEASES}/{quote(row_id, safe="*")}')
    except MissingResource:
        return None
    if not isinstance(row, dict) or row.get('.id') != row_id:
        raise RouterError('RouterOS DHCP 租约按 ID 回读格式无法识别')
    return row


def read(driver):
    """Read a fresh, complete reservation snapshot; no network mutation."""
    result = {'servers': driver._list(DHCP_SERVERS), 'networks': driver._list(DHCP_NETWORKS),
              'addresses': driver._list('/ip/address'), 'leases': driver._list(DHCP_LEASES)}
    seen = set()
    for row in result['leases']:
        row_id = _id(row.get('.id'))
        if row_id in seen:
            raise RouterError('RouterOS DHCP 租约 ID 不唯一')
        seen.add(row_id)
    return result


def _scopes(snapshot, ip):
    address = ipaddress.IPv4Address(ip)
    candidates = []
    for server in snapshot['servers']:
        if not server.get('name') or not server.get('interface') or _truthy(server.get('disabled')) \
                or _truthy(server.get('invalid')):
            continue
        # A remote relay cannot be mapped to the local interface subnet safely.
        if str(server.get('relay') or '0.0.0.0') != '0.0.0.0':
            continue
        interface = str(server['interface'])
        for own in snapshot['addresses']:
            if own.get('interface') != interface or _truthy(own.get('disabled')) or _truthy(own.get('invalid')):
                continue
            try:
                connected = ipaddress.IPv4Interface(str(own.get('address') or '')).network
            except ValueError:
                continue
            if address not in connected or address in (connected.network_address, connected.broadcast_address):
                continue
            for network in snapshot['networks']:
                if _truthy(network.get('disabled')) or _truthy(network.get('invalid')):
                    continue
                try:
                    subnet = ipaddress.IPv4Network(str(network.get('address') or ''), strict=False)
                except ValueError:
                    continue
                if address in subnet:
                    scope = {'server': str(server['name']), 'interface': interface, 'network': str(subnet),
                             'server_id': str(server.get('.id') or ''),
                             'network_id': str(network.get('.id') or ''),
                             'gateway': str(network.get('gateway') or ''), 'connected': str(connected)}
                    if scope not in candidates:
                        candidates.append(scope)
    # RouterOS uses the most specific DHCP network. Keep ambiguity between
    # servers/interfaces visible instead of selecting an arbitrary router LAN.
    if candidates:
        prefix = max(ipaddress.IPv4Network(row['network']).prefixlen for row in candidates)
        candidates = [row for row in candidates if ipaddress.IPv4Network(row['network']).prefixlen == prefix]
    return candidates


def inspect(snapshot, ip, mac):
    result = {'eligible': False, 'reason': '', 'fixed': False, 'owned': False, 'dynamic': False,
              'scope': None, 'lease': None}

    def denied(message):
        return {**result, 'reason': message}

    ip, mac = ipv4(ip), _mac(mac)
    if not ip:
        return denied('固定 IP 仅支持单个 IPv4 地址')
    if not mac:
        return denied('未发现有效 MAC，无法固定 IP')
    leases = snapshot['leases']
    static = [row for row in leases if not _truthy(row.get('dynamic'))]
    same_mac = [row for row in static if _mac(row.get('mac-address')) == mac]
    same_ip = [row for row in static if ipv4(row.get('address')) == ip]
    if any(_mac(row.get('mac-address')) != mac for row in same_ip):
        return denied('这个 IP 已静态绑定给其他 MAC，不能覆盖')
    if same_mac:
        exact = [row for row in same_mac if ipv4(row.get('address')) == ip]
        result.update(fixed=bool(exact and not _truthy(exact[0].get('disabled'))),
                      lease=deepcopy(exact[0] if exact else same_mac[0]))
        result['owned'] = bool(exact and len(same_mac) == 1 and str(exact[0].get('comment') or '').startswith(PREFIX))
        if result['owned']:
            result['binding'] = _binding(exact[0])
        return denied('这个 MAC 已有静态租约，保留原设置，不覆盖')
    if any(ipv4(str(row.get('address') or '').split('/')[0]) == ip for row in snapshot['addresses']):
        return denied('这个 IP 是路由器接口地址，不能固定给客户端')
    dynamic = [row for row in leases if _truthy(row.get('dynamic'))
               and (ipv4(row.get('address')) == ip or _mac(row.get('mac-address')) == mac)]
    if any(ipv4(row.get('address')) != ip or _mac(row.get('mac-address')) != mac for row in dynamic):
        return denied('当前 DHCP 租约的 IP 与 MAC 已变化，请重新发现设备')
    if len(dynamic) > 1:
        return denied('存在多条匹配的动态租约，无法安全确定目标')
    lease = dynamic[0] if dynamic else None
    result.update(dynamic=bool(lease), lease=deepcopy(lease))
    scopes = _scopes(snapshot, ip)
    if lease:
        scopes = [scope for scope in scopes if scope['server'] == lease.get('server')]
    if not scopes:
        return denied('IP 不在该路由器任何已启用且可确认的 DHCP 网段内')
    if len(scopes) != 1:
        return denied('IP 对应多个 DHCP 服务，无法唯一确定固定 IP 的目标')
    if lease and (_truthy(lease.get('radius')) or _truthy(lease.get('disabled')) or _truthy(lease.get('blocked'))):
        return denied('该动态租约由 RADIUS 管理、被禁用或被阻止，不能自动固定')
    result.update(eligible=True, scope=scopes[0], lease=deepcopy(lease), dynamic=bool(lease))
    return result


def preview(driver, ip, mac, name):
    state = inspect(read(driver), ip, mac)
    if not state['eligible']:
        raise RouterError(state['reason'])
    ip, mac = ipv4(ip), _mac(mac)
    name = str(name or ip).strip()
    if any(ord(char) < 32 for char in name):
        raise RouterError('设备名称不能包含控制字符')
    comment = PREFIX + name
    lease = state['lease']
    return {'kind': 'make_static' if lease else 'add', 'ip': ip, 'mac': mac, 'name': name,
            'comment': comment, 'server': state['scope']['server'], 'scope': state['scope'],
            'id': lease['.id'] if lease else '', 'expected': _fields(lease) if lease else None,
            'summary': (f'RouterOS：把 {ip}（MAC {mac}）的动态租约转为静态。'
                        '若失败回滚，将移除预留；客户端续租恢复动态 DHCP，IP 可能变化') if lease else
                       f'RouterOS：新增静态租约 {state["scope"]["server"]} {ip} ↔ {mac}'}


def _binding(row):
    return {'vendor': 'routeros', 'id': _id(row.get('.id')), **_identity(row)}


def _matches(row, binding):
    return row is not None and row.get('.id') == binding.get('id') and not _truthy(row.get('dynamic')) \
        and all(_identity(row)[key] == binding.get(key) for key in ('ip', 'mac', 'server', 'comment'))


def _check(row, binding, disabled=False):
    if not _matches(row, binding) or _truthy(row.get('disabled')) != disabled:
        raise RouterError('RouterOS 固定 IP 回读校验失败：ID、IP、MAC、服务、启用状态或归属标记不一致')


def _remove(driver, binding, expected=None):
    row = _row(driver, binding['id'])
    if row is None:
        return
    if not _matches(row, binding) or expected is not None and _fields(row) != expected:
        raise RouterError('RouterOS 租约已发生外部变化，保留现有配置')
    failure = None
    try:
        driver._request('DELETE', f'{DHCP_LEASES}/{quote(binding["id"], safe="*")}')
    except UncertainWrite as error:
        failure = error
    readback_error = None
    try:
        remaining = _row(driver, binding['id'])
    except RouterError as error:
        # DELETE may already be committed. Resolve a transient read failure
        # before returning, so the caller can safely remove persisted metadata.
        readback_error = error
        try:
            remaining = _row(driver, binding['id'])
        except RouterError as retry_error:
            raise RouterRollbackError(f'RouterOS 删除结果未知，无法确认租约是否仍存在；请检查路由器：{retry_error}') from error
    if remaining is not None:
        raise failure or readback_error or RouterError('RouterOS 删除回读校验失败：静态租约仍存在')


def apply(driver, op):
    """Recheck a preview, write one lease, and return a transient undo receipt."""
    latest = preview(driver, op.get('ip'), op.get('mac'), op.get('name'))
    if latest != op:
        raise RouterError('RouterOS DHCP 配置或租约在预览后发生变化，请重新预览')
    expected_binding = {'vendor': 'routeros', 'id': op['id'], 'ip': op['ip'], 'mac': op['mac'],
                        'comment': op['comment'], 'server': op['server']}
    # A unique operation suffix makes lost PUT acknowledgements recoverable
    # without mistaking a concurrent writer's identical reservation for ours.
    expected_binding['comment'] += ':' + uuid.uuid4().hex
    original = deepcopy(op['expected'])
    acquired = None
    acquired_fields = None
    conversion_pending = False
    try:
        if op['kind'] == 'make_static':
            try:
                driver._request('POST', DHCP_LEASES + '/make-static', {'.id': op['id']})
                conversion_pending = True
            except UncertainWrite:
                conversion_pending = True
                # A command reply can be lost; the existing ID is trustworthy.
                row = _row(driver, op['id'])
                if row is None or _truthy(row.get('dynamic')):
                    raise
                acquired = _binding(row)
                acquired_fields = deepcopy(original)
                raise
            row = _row(driver, op['id'])
            if row is None or _truthy(row.get('dynamic')):
                raise RouterError('RouterOS 未将动态租约转为静态')
            acquired = _binding(row)
            acquired_fields = deepcopy(original)
            if _fields(row) != original:
                raise RouterRollbackError('RouterOS 转换期间租约配置已变化，已保留现场，请检查路由器')
            # Before adding ownership, the conversion must still refer to the
            # exact address, MAC, and server the human approved.
            if any(acquired[key] != expected_binding[key] for key in ('ip', 'mac', 'server')):
                acquired = None
                raise RouterRollbackError('RouterOS 转换后的租约与预览目标不一致，已保留现场，请检查路由器')
            driver._request('PATCH', f'{DHCP_LEASES}/{quote(op["id"], safe="*")}', {'comment': expected_binding['comment']})
        else:
            known_ids = {row['.id'] for row in driver._list(DHCP_LEASES)}
            values = {'mac-address': op['mac'], 'address': op['ip'], 'server': op['server'],
                      'comment': expected_binding['comment'], 'disabled': 'false'}
            try:
                response = driver._request('PUT', DHCP_LEASES, values)
                new_id = response.get('.id') if isinstance(response, dict) else None
                if not isinstance(new_id, str) or not ID.fullmatch(new_id) or new_id in known_ids:
                    raise UncertainWrite('RouterOS 新建租约未返回可信的新对象 ID')
            except UncertainWrite as error:
                try:
                    candidates = [row for row in driver._list(DHCP_LEASES)
                                  if row.get('.id') not in known_ids
                                  and _matches(row, {**expected_binding, 'id': row.get('.id')})]
                    if len(candidates) != 1:
                        raise RouterError('无法唯一确认本次新增租约')
                    recovered = _row(driver, _id(candidates[0].get('.id')))
                    if recovered is None or _fields(recovered) != _fields(candidates[0]):
                        raise RouterError('新增租约在恢复回读时发生变化')
                    acquired = _binding(recovered)
                    acquired_fields = _fields(recovered)
                    expected_binding['id'] = acquired['id']
                except RouterError as recovery_error:
                    raise RouterRollbackError(f'{error}；{recovery_error}，保留现场，请检查路由器') from error
                raise error
            expected_binding['id'] = new_id
            acquired = expected_binding
            acquired_fields = _fields({**values, **response})
        row = _row(driver, expected_binding['id'])
        _check(row, expected_binding)
        return {**expected_binding, 'undo': {'kind': op['kind'], 'expected': _fields(row), 'original': original}}
    except RouterError as error:
        compensated = False
        if acquired or conversion_pending:
            try:
                current = _row(driver, (acquired or expected_binding)['id'])
                if current is not None and not _truthy(current.get('dynamic')):
                    expected = deepcopy(acquired_fields if acquired_fields is not None else original)
                    # Only the original or our submitted comment can be ours;
                    # custom lease fields must also still match our last read.
                    allowed_comments = {str(expected.get('comment') or ''), expected_binding['comment']}
                    expected['comment'] = str(current.get('comment') or '')
                    if any(_identity(current)[key] != expected_binding[key] for key in ('ip', 'mac', 'server')) \
                            or str(current.get('comment') or '') not in allowed_comments \
                            or _fields(current) != expected:
                        raise RouterError('回滚目标已发生外部变化，保留现有配置')
                    _remove(driver, _binding(current), expected)
                    compensated = True
            except RouterError as rollback_error:
                raise RouterRollbackError(f'{error}；RouterOS 固定 IP 回滚未完成：{rollback_error}') from error
        if compensated and op['kind'] == 'make_static':
            raise RouterError(f'{error}；{ROLLBACK_NOTE}') from error
        raise


def rollback(driver, binding):
    """Undo only our verified reservation, never a foreign or changed lease.

    RouterOS exposes make-static but no inverse command. Removing the new
    reservation returns the client to dynamic DHCP at its next renewal.
    """
    if not str(binding.get('comment') or '').startswith(PREFIX) or not isinstance(binding.get('undo'), dict):
        raise RouterError('RouterOS 固定 IP 回滚缺少本次写入的归属证据')
    _remove(driver, binding, binding['undo'].get('expected'))


def preview_delete(driver, binding):
    if binding.get('vendor') != 'routeros' or not str(binding.get('comment') or '').startswith(PREFIX):
        raise RouterError('只能删除 ConfigFlow 创建的静态租约')
    row = _row(driver, binding.get('id'))
    if not _matches(row, binding):
        raise RouterError('静态租约已不存在或归属、IP、MAC 发生变化，不能删除')
    return {**{key: binding[key] for key in ('vendor', 'id', 'ip', 'mac', 'server', 'comment')},
            'kind': 'delete', 'expected': _fields(row),
            'summary': f'RouterOS：删除 ConfigFlow 创建的静态租约 {binding["ip"]} ↔ {binding["mac"]}'}


def delete(driver, binding):
    op = preview_delete(driver, binding)
    if 'expected' in binding and op != binding:
        raise RouterError('RouterOS 静态租约在预览后发生变化，请重新预览')
    _remove(driver, op, op['expected'])
    return op


def restore_deleted(driver, receipt):
    """Restore a batch deletion only if no other reservation claimed IP/MAC."""
    if receipt.get('kind') != 'delete' or not str(receipt.get('comment') or '').startswith(PREFIX):
        raise RouterError('RouterOS 删除回滚缺少归属证据')
    current = _row(driver, receipt['id'])
    if current is not None:
        if _matches(current, receipt) and _fields(current) == receipt['expected']:
            return _binding(current)
        raise RouterError('RouterOS 删除回滚的对象 ID 已被占用，保留现有配置')
    for row in driver._list(DHCP_LEASES):
        if ipv4(row.get('address')) == receipt['ip'] or _mac(row.get('mac-address')) == receipt['mac']:
            raise RouterError('RouterOS 删除回滚时 IP 或 MAC 已出现其他租约，保留现有配置')
    response = driver._request('PUT', DHCP_LEASES, receipt['expected'])
    row_id = _id(response.get('.id') if isinstance(response, dict) else None)
    restored = {**receipt, 'id': row_id}
    row = _row(driver, row_id)
    _check(row, restored, disabled=_truthy(receipt['expected'].get('disabled')))
    if _fields(row) != receipt['expected']:
        raise RouterRollbackError('RouterOS 删除回滚回读失败，原租约字段未完整恢复')
    return _binding(row)
