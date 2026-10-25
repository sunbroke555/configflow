"""Explicit, previewed DHCP reservations. Names and policies remain keyed by IP."""
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json

from itsdangerous import BadSignature, URLSafeTimedSerializer

from backend.common.device_notes import normalize_name
from backend.common.oxidns_settings import OxidnsSettingsError
from backend.routers.base import RouterConflictError, RouterError, RouterRollbackError
from backend.routers.lan_clients import ipv4, normalize_mac
from backend.routers.route_editor import router_operation_lock
from backend.routers.store import find_router

KEY = 'client_fixed_ips'
BINDING_FIELDS = ('router_id', 'vendor', 'id', 'ip', 'mac', 'comment', 'server', 'interface', 'gateway')


def bindings(config):
    value = config.get(KEY, {})
    return value if isinstance(value, dict) else {}


def validate_bindings(value):
    if not isinstance(value, dict) or len(value) > 2048:
        raise OxidnsSettingsError('客户端固定 IP 记录必须为对象，最多 2048 条。')
    required = {'router_id', 'vendor', 'id', 'ip', 'mac', 'comment'}
    for ip, binding in value.items():
        if (not isinstance(binding, dict) or set(binding) - set(BINDING_FIELDS)
                or not required <= set(binding) or not ipv4(ip) or binding.get('ip') != ip
                or binding.get('vendor') not in ('routeros', 'ikuai')
                or not all(isinstance(v, str) and len(v) <= 512 and not any(ord(c) < 32 for c in v)
                           for v in binding.values())
                or not binding['router_id'] or not binding['id']
                or not normalize_mac(binding['mac']) or binding['mac'] != normalize_mac(binding['mac'])
                or not binding['comment'].startswith('cf-client:')):
            raise OxidnsSettingsError('客户端固定 IP 记录缺少有效 IP、MAC、路由器或归属标记。')


def adapter(vendor):
    if vendor == 'routeros':
        from backend.routers import routeros_fixed_ip
        return routeros_fixed_ip
    if vendor == 'ikuai':
        from backend.routers import ikuai_fixed_ip
        return ikuai_fixed_ip
    raise RouterError('此来源路由器不支持固定 IP。')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def _signer():
    from backend.common.auth import JWT_SECRET_KEY
    return URLSafeTimedSerializer(JWT_SECRET_KEY, salt='client-fixed-ip-v1')


def _guard_setup(profile_id):
    from backend.common.config import get_repository
    from backend.common.setup_flow import profile_sessions
    if any(not item.get('completed') and not item.get('abandoned')
           for item in profile_sessions(get_repository(), profile_id)):
        raise RouterConflictError('配置向导进行中，只允许命名；请完成向导后再固定或删除固定 IP。')


def normalize_request(body):
    if (not isinstance(body, dict) or set(body) != {'profile_id', 'operation', 'devices'}
            or body.get('operation') not in ('add', 'delete')
            or not isinstance(body.get('devices'), list) or not 1 <= len(body['devices']) <= 64):
        raise OxidnsSettingsError('请选择 1 至 64 台设备预览固定 IP 操作。')
    rows, seen, seen_macs = [], set(), set()
    for row in body['devices']:
        if not isinstance(row, dict) or set(row) != {'ip', 'mac', 'router_id', 'name'}:
            raise OxidnsSettingsError('固定 IP 请求需要 IP、MAC、来源路由器和设备名称。')
        ip, mac = ipv4(row.get('ip')), normalize_mac(row.get('mac'))
        router_id = row.get('router_id')
        if not ip or not mac or not isinstance(router_id, str) or not router_id or ip in seen:
            raise OxidnsSettingsError('固定 IP 只支持有 MAC 和明确来源路由器的单个 IPv4，且不能重复选择。')
        if (router_id, mac) in seen_macs:
            raise OxidnsSettingsError('同一路由器上的同一 MAC 只能选择一个固定 IP。')
        seen.add(ip)
        seen_macs.add((router_id, mac))
        rows.append({'ip': ip, 'mac': mac, 'router_id': router_id, 'name': normalize_name(row.get('name'))})
    return {'operation': body['operation'], 'devices': rows}


def _seen(driver, vendor, ip, mac):
    """Never trust a client-supplied router id or a MAC borrowed from another router."""
    if vendor == 'routeros':
        rows = driver._list('/ip/dhcp-server/lease') + driver._list('/ip/arp')
        return any(ipv4(row.get('active-address') or row.get('address')) == ip
                   and normalize_mac(row.get('active-mac-address') or row.get('mac-address')) == mac for row in rows)
    rows = driver._list_all('dhcp_lease', ids=False) + driver._list_all('monitor_lanip', ids=False)
    return any(ipv4(row.get('ip_addr')) == ip and normalize_mac(row.get('mac')) == mac for row in rows)


def _router(config, router_id):
    router = find_router(config, router_id)
    if not router or router.get('type') not in ('routeros', 'ikuai'):
        raise RouterConflictError('发现这台设备的路由器已不存在或不支持写入，请刷新设备列表。')
    return router


def _plan(config, intent, drivers):
    operations = []
    for item in intent['devices']:
        router = _router(config, item['router_id'])
        driver = drivers[item['router_id']]
        if not getattr(driver, 'supports_write', False):
            raise RouterError('此来源路由器不支持写入 DHCP 静态租约。')
        module = adapter(router['type'])
        if intent['operation'] == 'add':
            if not _seen(driver, router['type'], item['ip'], item['mac']):
                raise RouterConflictError(f"{item['ip']} 的 MAC 或来源路由器已变化，请重新发现设备。")
            op = module.preview(driver, item['ip'], item['mac'], item['name'])
        else:
            binding = bindings(config).get(item['ip'])
            if not binding:
                state = module.inspect(module.read(driver), item['ip'], item['mac'])
                if state.get('owned') and state.get('binding'):
                    binding = {**state['binding'], 'router_id': item['router_id']}
            if (not binding or binding.get('router_id') != item['router_id']
                    or binding.get('mac') != item['mac']):
                raise RouterConflictError('没有这台设备由 ConfigFlow 创建的固定 IP 记录，不会删除其他静态租约。')
            op = module.preview_delete(driver, binding)
        operations.append({'router_id': item['router_id'], 'connection': _digest(router), 'op': op})
    return operations


def _open(config, intent, stack):
    from backend.common.client_discovery import open_driver
    drivers = {}
    for router_id in sorted({item['router_id'] for item in intent['devices']}):
        driver = open_driver(_router(config, router_id))
        stack.callback(driver.close)
        drivers[router_id] = driver
    return drivers


def preview(config, profile_id, intent):
    _guard_setup(profile_id)
    with ExitStack() as stack:
        operations = _plan(config, intent, _open(config, intent, stack))
    actions = []
    for item, operation in zip(intent['devices'], operations):
        router = _router(config, item['router_id'])
        actions.append({'ip': item['ip'], 'router_id': item['router_id'],
                        'router_name': router.get('name') or item['router_id'], 'router_type': router['type'],
                        'description': operation['op']['summary']})
    token = _signer().dumps({'profile_id': profile_id, 'intent': intent, 'operations': operations})
    return {'token': token, 'actions': actions}


def _public_binding(binding, router_id):
    return {key: value for key, value in {**binding, 'router_id': router_id}.items() if key in BINDING_FIELDS}


def apply(profile_id, token):
    from backend.common.client_devices import list_devices
    from backend.common.config import get_config, reset_config_context, update_config_transaction
    from backend.routers.rule_sync_store import job_lock
    if not isinstance(token, str) or not token or len(token) > 1_000_000:
        raise RouterConflictError('缺少固定 IP 预览确认，请重新预览。')
    try:
        approved = _signer().loads(token, max_age=600)
    except BadSignature:
        raise RouterConflictError('固定 IP 预览无效或已过期，请重新预览确认。') from None
    if approved.get('profile_id') != profile_id:
        raise RouterConflictError('配置空间已切换，请重新预览。')
    _guard_setup(profile_id)
    intent, completed = approved['intent'], []
    with ExitStack() as stack:
        initial = get_config(profile_id)
        connections = set()
        for router_id in sorted({item['router_id'] for item in intent['devices']}):
            router = _router(initial, router_id)
            key = (router['type'], router['endpoint'].rstrip('/'))
            if key in connections:
                continue
            connections.add(key)
            if not stack.enter_context(job_lock(profile_id, router_id, router)):
                raise RouterConflictError('此路由器正在发布或同步，请稍后重新预览。')
            stack.enter_context(router_operation_lock(router))
        reset_config_context()
        config = get_config(profile_id)
        _guard_setup(profile_id)
        for item in intent['devices']:
            if _router(initial, item['router_id']) != _router(config, item['router_id']):
                raise RouterConflictError('路由器连接在等待期间发生变化，请重新预览。')
        drivers = _open(config, intent, stack)
        operations = _plan(config, intent, drivers)
        if operations != approved['operations']:
            raise RouterConflictError('路由器或 DHCP 绑定在预览后发生变化，请重新预览确认。')
        before_rows = {row['ip']: row for row in list_devices(config)['devices']}
        for item in intent['devices']:
            if item['ip'] not in before_rows:
                raise RouterConflictError('请先保存设备名称或 DNS 策略，再固定 IP。')
        before_bindings = deepcopy(bindings(config))
        try:
            results = {}
            for item, operation in zip(intent['devices'], operations):
                driver = drivers[item['router_id']]
                module = adapter(_router(config, item['router_id'])['type'])
                receipt = (module.apply(driver, operation['op']) if intent['operation'] == 'add'
                           else module.delete(driver, operation['op']))
                completed.append((item, module, driver, receipt))
                if intent['operation'] == 'add':
                    results[item['ip']] = _public_binding(receipt, item['router_id'])

            def save(current):
                rows = {row['ip']: row for row in list_devices(current)['devices']}
                for item in intent['devices']:
                    ip = item['ip']
                    if (rows.get(ip) != before_rows.get(ip)
                            or bindings(current).get(ip) != before_bindings.get(ip)
                            or _router(current, item['router_id']) != _router(config, item['router_id'])):
                        raise RouterConflictError('设备或路由器设置已被其他页面修改，已撤销固定 IP 操作，请刷新。')
                updated = deepcopy(bindings(current))
                for item in intent['devices']:
                    if intent['operation'] == 'add':
                        updated[item['ip']] = results[item['ip']]
                    else:
                        updated.pop(item['ip'], None)
                current[KEY] = updated
            update_config_transaction(save, profile_id)
        except Exception as error:
            failures, restored = [], {}
            for item, module, driver, receipt in reversed(completed):
                try:
                    if intent['operation'] == 'add':
                        module.rollback(driver, receipt)
                    else:
                        binding = module.restore_deleted(driver, receipt)
                        if binding:
                            restored[item['ip']] = _public_binding(binding, item['router_id'])
                except Exception as undo_error:
                    failures.append(f"{item['ip']}：{undo_error}")
            if restored:
                def save_restored(current):
                    updated = deepcopy(bindings(current))
                    present = {row['ip'] for row in list_devices(current)['devices']}
                    for ip, binding in restored.items():
                        if (ip in present and updated.get(ip) == before_bindings.get(ip)
                                and find_router(current, binding['router_id']) == find_router(config, binding['router_id'])):
                            updated[ip] = binding
                    current[KEY] = updated
                try:
                    update_config_transaction(save_restored, profile_id)
                except Exception as restore_error:
                    failures.append(f'恢复绑定标识保存失败：{restore_error}')
            if failures:
                raise RouterRollbackError(f'{error}；回滚未完成，请检查路由器：' + '；'.join(failures)) from None
            dynamic_undo = any(receipt.get('undo', {}).get('kind') == 'make_static'
                               for _, _, _, receipt in completed)
            note = ('；RouterOS 已移除本次静态预留，客户端续租恢复动态 DHCP，IP 可能变化。'
                    if dynamic_undo else '')
            if isinstance(error, (RouterError, OxidnsSettingsError)):
                raise type(error)(str(error) + note) from error
            raise RouterError('固定 IP 记录保存失败，已撤销本次路由器变更，请重试。' + note) from error


def enrich(driver, reading, router_id):
    """An extra read failure disables only reservation controls, never discovery."""
    try:
        if not getattr(driver, 'supports_write', False):
            raise RouterError('此来源路由器不支持写入。')
        module = adapter(driver.vendor)
        snapshot = module.read(driver)
        for ip, sighting in reading.sightings.items():
            state = module.inspect(snapshot, ip, sighting.mac)
            sighting.fixed_ip = {'eligible': bool(state['eligible']), 'reason': state.get('reason', ''),
                                 'status': 'static' if state.get('fixed') else ('dynamic' if state.get('dynamic') else 'none'),
                                 'owned': bool(state.get('owned')), 'router_id': router_id}
            if state.get('owned') and state.get('binding'):
                sighting.fixed_ip['binding'] = _public_binding(state['binding'], router_id)
    except RouterError as error:
        for sighting in reading.sightings.values():
            sighting.fixed_ip = {'eligible': False, 'reason': str(error), 'status': 'none',
                                 'owned': False, 'router_id': router_id}
