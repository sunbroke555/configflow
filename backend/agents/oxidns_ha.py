"""OxiDNS high availability: two OxiDNS Agents share one virtual IP (VIP) through keepalived.

The pair lives on both Agent records as ``oxidns_ha`` plus ``oxidns_ha_state``.
A record without them never enabled the feature, and disabling deletes both,
so nothing else changes for people who do not use it. Each Agent owns its
keepalived configuration; this module validates the pair, previews both
devices, applies the secondary before the primary and only then writes both
records in one transaction. A failed apply is undone on the devices, so no
record is ever left half enabled.

Routers do not get a switch of their own: a router whose DNS Agent is in an
enabled pair hands out the VIP instead (routers.dhcp_dns.dns_ha_settings).
"""
from contextlib import contextmanager
import fcntl
import ipaddress
import logging
import time
import uuid
from urllib.parse import urlsplit

import requests

from backend.agents.diagnostics import failure_text

PROTOCOL = 1
PRIORITY = {'primary': 150, 'secondary': 100}
REQUEST_FIELDS = {'peer_id', 'vip', 'vrid'}
# Measured in the lab: the holder is elected about 4 s after keepalived starts.
SETTLE_SECONDS = 10
_logger = logging.getLogger(__name__)


class HAError(ValueError):
    """User-facing failure; ``details`` are returned next to the message."""

    def __init__(self, message, **details):
        super().__init__(message)
        self.details = details


def enabled(agent):
    return ((agent or {}).get('oxidns_ha') or {}).get('enabled') is True


@contextmanager
def _lock(manager):
    # Serializes enable/disable across Gunicorn workers; process exit releases it.
    path = manager.repository.data_dir / '.oxidns-ha.lock'
    with open(path, 'a+b') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise HAError('DNS 高可用正在处理，请稍后重试') from None
        yield


def _name(agent):
    return (agent or {}).get('name') or (agent or {}).get('id') or 'Agent'


def _unicast_ipv4(value):
    try:
        if not isinstance(value, str):
            raise ValueError
        address = ipaddress.IPv4Address(value)
    except (ValueError, TypeError):
        return None
    if address.is_unspecified or address.is_loopback or address.is_multicast or int(address) == 0xffffffff:
        return None
    return address


def default_vrid(vip):
    """The VIP's last byte is easy to recognise in keepalived logs; 0 and 255 are not valid VRIDs."""
    last = int(ipaddress.IPv4Address(vip)) & 0xff
    return 53 if last in (0, 255) else last


def _paired(agent, peer):
    ha, peer_ha = agent.get('oxidns_ha') or {}, peer.get('oxidns_ha') or {}
    return (enabled(agent) and enabled(peer) and bool(ha.get('group_id')) and ha.get('group_id') == peer_ha.get('group_id')
            and ha.get('peer_id') == peer['id'] and peer_ha.get('peer_id') == agent['id'])


def normalize(request, agent, peer, agents):
    """Validate an enable request; returns {peer_id, vip, vrid, previous}."""
    if not isinstance(request, dict) or set(request) - REQUEST_FIELDS:
        raise HAError('DNS 高可用参数无效')
    if not agent or agent.get('service_type') != 'oxidns':
        raise HAError('只有 OxiDNS Agent 可以开启 DNS 高可用')
    if not isinstance(request.get('peer_id'), str) or not request['peer_id']:
        raise HAError('请选择另一台 OxiDNS 作为对端')
    if not peer or peer.get('service_type') != 'oxidns':
        raise HAError('对端 Agent 不存在或不是 OxiDNS')
    if peer['id'] == agent['id']:
        raise HAError('对端不能是本机')
    for item in (agent, peer):
        if not item.get('enabled', True):
            raise HAError(f'{_name(item)} 已停用，请先启用')
        if item.get('deployment_method') == 'docker':
            raise HAError(f'{_name(item)} 是 Docker 部署，不能管理宿主机的虚拟 IP；请改用 Shell 安装的 Agent')
    if agent.get('profile_id') != peer.get('profile_id'):
        raise HAError('两台 OxiDNS 必须使用同一个配置空间，才能给出相同的应答')
    hosts = [_unicast_ipv4(item.get('host')) for item in (agent, peer)]
    if not all(hosts):
        raise HAError('两台 OxiDNS 的主机地址都必须是局域网 IPv4 地址')
    if hosts[0] == hosts[1]:
        raise HAError('两台 OxiDNS 的主机地址不能相同')
    vip = _unicast_ipv4(request.get('vip'))
    if vip is None:
        raise HAError('虚拟 IP 必须是可用的单播 IPv4 地址，例如 192.168.1.53')
    if vip in hosts:
        raise HAError('虚拟 IP 不能与任何一台 OxiDNS 的主机地址相同')
    vrid = request.get('vrid')
    if vrid is None:
        vrid = default_vrid(vip)
    elif type(vrid) is not int or not 1 <= vrid <= 255:
        raise HAError('虚拟路由 ID（VRID）必须是 1 到 255 的整数')
    previous = None
    if enabled(agent) or enabled(peer):
        if not _paired(agent, peer):
            busy = agent if enabled(agent) else peer
            raise HAError(f'{_name(busy)} 已加入其他 DNS 高可用组，请先停用')
        previous = {item['id']: dict(item['oxidns_ha']) for item in (agent, peer)}
    ids = (agent['id'], peer['id'])
    for other in agents:
        if other['id'] in ids:
            continue
        if other.get('host') == str(vip):
            raise HAError(f'虚拟 IP {vip} 已是 Agent {_name(other)} 的主机地址')
        other_ha = other.get('oxidns_ha') or {}
        if enabled(other) and other_ha.get('peer_id') in ids:
            raise HAError(f'{_name(other)} 的 DNS 高可用仍指向这两台之一，请先在该 Agent 上停用')
        if enabled(other) and other_ha.get('vip') == str(vip):
            raise HAError(f'虚拟 IP {vip} 已被另一组 DNS 高可用使用')
    return {'peer_id': peer['id'], 'vip': str(vip), 'vrid': vrid, 'previous': previous}


def _spec(vip, vrid, role, member, other):
    return {'safety_version': PROTOCOL, 'vip': vip, 'vrid': vrid, 'priority': PRIORITY[role],
            'self_ip': member['host'], 'peer_ip': other['host']}


def _call(method, agent, *args):
    try:
        return method(agent, *args)
    except requests.RequestException:
        raise HAError(f'{_name(agent)} 无法连接，请检查网络后重试') from None
    except RuntimeError as error:
        raise HAError(f'{_name(agent)}：{failure_text(str(error), (agent.get("token"),))}') from None


def _require_protocol(manager, agent):
    # Same rule as reload_only_protocols: a protocol number, never a version string.
    try:
        capability = manager._deployment_request(agent, 'get', 'capabilities', timeout=5)
    except requests.RequestException:
        raise HAError(f'{_name(agent)} 无法连接，请检查网络后重试') from None
    protocols = capability.get('oxidns_ha_protocols')
    if not isinstance(protocols, list) or PROTOCOL not in protocols:
        raise HAError(f'{_name(agent)} 的 Agent 版本不支持 DNS 高可用，请先升级 Agent')


def _loopback(value):
    if not isinstance(value, str) or not value:
        return False
    try:
        host = urlsplit(value if '://' in value else 'udp://' + value).hostname
    except ValueError:
        return False
    if not host:
        return False
    if host.lower() == 'localhost' or host.lower().endswith('.localhost'):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def upstream_findings(manager, agent, peer):
    """Blockers and warnings from the shared OxiDNS draft (both members publish it byte for byte).

    Fake-IP answers are only valid on the Mihomo that issued them, and routers
    send the Fake-IP range to the single linked gateway. A loopback upstream
    makes each member ask its own local service, so answers would differ.
    """
    from backend.common.oxidns_editor import prepare_editor
    from backend.common.oxidns_native import load_native_yaml
    blockers, warnings = [], []
    for item in (agent, peer):
        if (item.get('dns_follow') or {}).get('connection') == 'auto':
            blockers.append(f'{_name(item)} 的 DNS 联动使用同机连接，会把上游改成本机回环地址。'
                            '两台 OxiDNS 必须转发到同一个 mihomo，请把联动改为网络地址连接')
    try:
        config = manager.repository.get_compat_config(agent.get('profile_id') or manager.repository.default_profile_id())
        settings = prepare_editor(config.get('oxidns', {}), config,
                                  (agent.get('applied_content') or {}).get('base_url', ''))
        document = load_native_yaml(settings['native_config'])
        plugins = [item for item in document.get('plugins') or [] if isinstance(item, dict)]
    except Exception as error:  # noqa: BLE001 - any failure must surface as a warning, never pass silently
        _logger.warning('DNS 高可用无法解析 OxiDNS 配置：%s', error)
        warnings.append('无法解析 OxiDNS 配置，未能确认两台 OxiDNS 转发到同一个 mihomo；'
                        '请自行确认代理 DNS 上游是 mihomo 的局域网地址，而不是本机回环地址')
        return blockers, warnings
    loopback = []
    cleanup = []
    for plugin in plugins:
        args = plugin.get('args') if isinstance(plugin.get('args'), dict) else {}
        if plugin.get('type') == 'forward':
            for entry in args.get('upstreams') or []:
                entry = entry if isinstance(entry, dict) else {'addr': entry}
                if _loopback(entry.get('addr')) or _loopback(entry.get('dial_addr')):
                    loopback.append(failure_text(str(entry.get('addr'))))
        elif plugin.get('type') in ('ros_address_list', 'ros_route') and args.get('cleanup_on_shutdown') is True:
            cleanup.append(plugin['type'])
    if loopback:
        blockers.append(f'OxiDNS 上游 {"、".join(dict.fromkeys(loopback))} 是本机回环地址。'
                        '两台 OxiDNS 必须转发到同一个 mihomo，请把代理 DNS 上游改为 mihomo 的局域网地址')
    if cleanup:
        warnings.append('两台 OxiDNS 会写入同一个 RouterOS 地址列表或路由，一台停止时的清理可能删掉另一台仍在使用的条目；'
                        '建议在 OxiDNS 设置里关闭「停止时清理」')
    return blockers, warnings


def _preview(manager, members, specs):
    blockers = []
    networks = []
    for member in members:
        result = _call(manager.oxidns_ha_preview, member, specs[member['id']])
        items = [failure_text(str(item), (member.get('token'),)) for item in result.get('blockers') or []
                 if isinstance(item, str) and item]
        if result.get('ok') is not True and not items:
            items = ['预检未通过']
        blockers.extend(f'{_name(member)}：{item}' for item in items)
        try:
            networks.append(ipaddress.IPv4Network(result.get('cidr'), strict=False))
        except (TypeError, ValueError):
            pass
    vip = ipaddress.IPv4Address(specs[members[0]['id']]['vip'])
    if len(networks) == 2 and networks[0] != networks[1]:
        blockers.append(f'两台 OxiDNS 不在同一网段（{networks[0]} 与 {networks[1]}），虚拟 IP 无法在两者之间切换')
    elif networks and vip not in networks[0]:
        blockers.append(f'虚拟 IP {vip} 不在两台 OxiDNS 所在的网段 {networks[0]} 内')
    return blockers


def routers_using(manager, agent_ids):
    """Routers in any profile whose DHCP DNS uses one of these Agents; they need a manual release."""
    from backend.routers.store import routers_of
    result = []
    for profile in manager.repository.list_profiles():
        for item in routers_of(manager.repository.get_profile(profile['id'])):
            if item.get('dns_agent_id') in agent_ids:
                result.append({'id': item.get('id'), 'name': item.get('name') or item.get('id'),
                               'profile_id': profile['id']})
    return result


def _record_error(manager, ids, error, failed):
    """Note a failure on records that already carry DNS HA fields.

    Routers hand out the VIP only while the status is ``succeeded``. When the
    devices are known to be as they were (a refused disable, a fully undone
    reconfiguration) the status stays and only the error is shown; ``failed``
    marks a pair whose devices may no longer match the records.
    """
    def save(agents):
        now = int(time.time())
        for item in agents:
            state = item.get('oxidns_ha_state')
            if item['id'] not in ids or not (enabled(item) or isinstance(state, dict)):
                continue
            state = dict(state) if isinstance(state, dict) else {}
            if failed or not state.get('status'):
                state['status'] = 'failed'
            state.update(error=error, updated_at=now)
            item['oxidns_ha_state'] = state
    manager._update_agents(save)


def _record_leftover(manager, ids, error):
    """A first enable that could not be undone: keepalived may still run on these devices.

    Only ``oxidns_ha_state`` is written (no ``oxidns_ha``), so routers and the
    rest of ConfigFlow still treat the Agents as not enabled, while ``members``
    tells disable() which devices to clean up.
    """
    def save(agents):
        now = int(time.time())
        for item in agents:
            if item['id'] in ids and not enabled(item):
                item['oxidns_ha_state'] = {'status': 'failed', 'error': error, 'updated_at': now,
                                           'members': list(ids)}
    manager._update_agents(save)


def _leftover_members(agent, by_id):
    """Agents named by a leftover marker that are not in a working pair now."""
    state = agent.get('oxidns_ha_state')
    ids = state.get('members') if isinstance(state, dict) and isinstance(state.get('members'), list) else []
    members = [agent]
    for item_id in ids:
        item = by_id.get(item_id) if isinstance(item_id, str) else None
        if item and item['id'] != agent['id'] and not enabled(item):
            members.append(item)
    return members


def _undo(manager, attempted, previous, by_id):
    """Put attempted members back: re-apply the previous pairing, or remove ours."""
    errors = []
    for member in reversed(attempted):
        try:
            if previous:
                old = previous[member['id']]
                other = by_id[old['peer_id']]
                _call(manager.oxidns_ha_apply, member, _spec(old['vip'], old['vrid'], old['role'], member, other))
            else:
                _call(manager.oxidns_ha_remove, member)
        except HAError as error:
            errors.append(str(error))
    return errors


def _fail(manager, attempted, previous, by_id, ids, cause):
    """Undo the attempted devices, record what is left and raise the combined error."""
    undo = _undo(manager, attempted, previous, by_id)
    message = cause + (f'；回滚未完成：{"；".join(undo)}' if undo else '')
    try:
        if previous:
            _record_error(manager, ids, message, failed=bool(undo))
        elif undo:
            _record_leftover(manager, [member['id'] for member in attempted], message)
    except Exception:  # noqa: BLE001 - the device error matters more than a failed note about it
        _logger.exception('DNS 高可用无法记录失败状态')
    raise HAError(message)


def configure(manager, agent_id, request, *, base_url=''):
    """Capture one configuration and sync both cores before enabling the VIP."""
    from . import oxidns_ha_publish as publications
    with _lock(manager):
        agents = manager.get_all_agents()
        by_id = {item['id']: item for item in agents}
        agent = by_id.get(agent_id)
        peer_id = request.get('peer_id') if isinstance(request, dict) else None
        peer = by_id.get(peer_id) if isinstance(peer_id, str) else None
        settings = normalize(request, agent, peer, agents)
        blockers, warnings = upstream_findings(manager, agent, peer)
        if blockers:
            raise HAError('无法开启 DNS 高可用：' + '；'.join(blockers), blockers=blockers)
        for member in (agent, peer):
            _require_protocol(manager, member)
        specs = {agent['id']: _spec(settings['vip'], settings['vrid'], 'primary', agent, peer),
                 peer['id']: _spec(settings['vip'], settings['vrid'], 'secondary', peer, agent)}
        blockers = _preview(manager, [agent, peer], specs)
        if blockers:
            raise HAError('无法开启 DNS 高可用：' + '；'.join(blockers), blockers=blockers)
        intent = {'peer_id': peer['id'], 'vip': settings['vip'], 'vrid': settings['vrid']}
        publication = publications.start_enable(manager, agent, peer, intent,
            base_url=base_url or (agent.get('applied_content') or {}).get('base_url', ''),
            warnings=warnings, routers=routers_using(manager, {agent['id'], peer['id']}))
    return {'success': True, 'config': agent.get('oxidns_ha'), 'publication': publication,
            'warnings': warnings, 'routers': routers_using(manager, {agent['id'], peer['id']})}


def _configure_keepalived(manager, agent_id, request, *, rule_sync_settings=None):
    """Enable (or change) the pair with this Agent as primary."""
    with _lock(manager):
        agents = manager.get_all_agents()
        by_id = {item['id']: item for item in agents}
        agent = by_id.get(agent_id)
        peer_id = request.get('peer_id') if isinstance(request, dict) else None
        peer = by_id.get(peer_id) if isinstance(peer_id, str) else None
        settings = normalize(request, agent, peer, agents)
        blockers, warnings = upstream_findings(manager, agent, peer)
        if blockers:
            raise HAError('无法开启 DNS 高可用：' + '；'.join(blockers), blockers=blockers)
        for member in (agent, peer):
            _require_protocol(manager, member)
        roles = {agent['id']: 'primary', peer['id']: 'secondary'}
        specs = {agent['id']: _spec(settings['vip'], settings['vrid'], 'primary', agent, peer),
                 peer['id']: _spec(settings['vip'], settings['vrid'], 'secondary', peer, agent)}
        blockers = _preview(manager, [agent, peer], specs)
        if blockers:
            raise HAError('无法开启 DNS 高可用：' + '；'.join(blockers), blockers=blockers)
        previous = settings['previous']
        # The secondary first: the VIP appears only once the primary starts.
        attempted = []
        try:
            for member in (peer, agent):
                attempted.append(member)
                _call(manager.oxidns_ha_apply, member, specs[member['id']])
        except HAError as error:
            _fail(manager, attempted, previous, by_id, set(roles), str(error))
        group_id = (previous or {}).get(agent['id'], {}).get('group_id') or uuid.uuid4().hex

        def save(items):
            current = {item['id']: item for item in items}
            for member in (agent, peer):
                stored = current.get(member['id'])
                if not stored or any(stored.get(key) != member.get(key)
                                     for key in ('host', 'port', 'service_type', 'profile_id', 'enabled')):
                    raise HAError('Agent 信息在开启过程中发生变化，请刷新后重试')
                if enabled(stored) and (stored['oxidns_ha'].get('group_id') != group_id or previous is None):
                    raise HAError(f'{_name(stored)} 已加入其他 DNS 高可用组，请刷新后重试')
            now = int(time.time())
            for member, other in ((agent, peer), (peer, agent)):
                stored = current[member['id']]
                stored['oxidns_ha'] = {'enabled': True, 'group_id': group_id, 'peer_id': other['id'],
                                       'vip': settings['vip'], 'vrid': settings['vrid'], 'role': roles[member['id']]}
                stored['oxidns_ha_state'] = {'status': 'succeeded', 'error': '', 'updated_at': now}
                if rule_sync_settings is not None:
                    stored.setdefault('rule_sync', {})['settings'] = dict(rule_sync_settings)
        try:
            manager._update_agents(save)
        except Exception as error:  # noqa: BLE001 - both devices are applied; any failure must undo them
            if not isinstance(error, HAError):
                _logger.exception('DNS 高可用保存记录失败')
                error = '保存 DNS 高可用记录失败，请重试'
            _fail(manager, [peer, agent], previous, by_id, set(roles), str(error))
    result = _settled_status(manager, agent_id)
    result.update(warnings=warnings, routers=routers_using(manager, {agent['id'], peer['id']}))
    return result


def _settled_status(manager, agent_id):
    """keepalived elects the VIP holder a few seconds after starting; report that, not the election."""
    deadline = time.monotonic() + SETTLE_SECONDS
    while True:
        result = status(manager, agent_id)
        if result['health'] not in ('split', 'down') or time.monotonic() >= deadline:
            return result
        time.sleep(1)


def disable(manager, agent_id, force=False):
    from . import oxidns_ha_publish as publications
    agent = manager.get_agent_by_id(agent_id) or {}
    children = (agent.get(publications.FIELD) or {}).get('children') or {agent_id: ''}
    peer_id = (agent.get('oxidns_ha') or {}).get('peer_id')
    if peer_id:
        children = {agent_id: '', peer_id: ''}
    with publications._lock(manager, {'children': children}) as acquired:
        if not acquired:
            raise HAError('主备配置正在处理，请等待完成后再停用高可用')
        return _disable(manager, agent_id, force)


def _disable(manager, agent_id, force=False):
    """Remove keepalived from both members, then delete the fields from both records.

    An unreachable member blocks unless ``force``: its keepalived would keep the
    VIP, so the person must clean it up on the device.
    """
    with _lock(manager):
        agents = manager.get_all_agents()
        by_id = {item['id']: item for item in agents}
        agent = by_id.get(agent_id)
        from .oxidns_ha_publish import active
        if active(agent):
            raise HAError('主备配置发布尚未结束，请确认两台结果后再停用高可用')
        if not agent:
            raise HAError('Agent 不存在')
        ha = agent.get('oxidns_ha')
        if ha is None and 'oxidns_ha_state' not in agent:
            return {'success': True, 'config': None, 'routers': [], 'warnings': []}
        ha = ha if isinstance(ha, dict) else {}
        if enabled(agent):
            members = [agent]
            peer = by_id.get(ha.get('peer_id'))
            if peer and peer['id'] != agent['id'] and (
                    (peer.get('oxidns_ha') or {}).get('group_id') == ha.get('group_id')
                    or (peer.get('oxidns_ha') or {}).get('peer_id') == agent['id']):
                members.append(peer)
        else:
            # A first enable that failed half way: clean the devices it touched.
            members = _leftover_members(agent, by_id)
        # Remove the secondary first, so the VIP never moves on its way out.
        members.sort(key=lambda item: ((item.get('oxidns_ha') or {}).get('role') != 'secondary'))
        errors = []
        for member in members:
            try:
                _call(manager.oxidns_ha_remove, member)
            except HAError as error:
                errors.append(str(error))
        ids = {member['id'] for member in members}
        if errors and not force:
            message = '停用未完成：' + '；'.join(errors)
            # A refused removal leaves keepalived as it was, so the pair keeps serving the VIP.
            _record_error(manager, ids, message, failed=False)
            raise HAError(message + '。设备离线时可以强制清除记录，之后请在设备上手动删除 keepalived 中的 ConfigFlow 配置',
                          can_force=True)

        cleaning = enabled(agent)

        def save(items):
            for item in items:
                # A leftover marker never clears a member that has since joined a working pair.
                if item['id'] in ids and (cleaning or not enabled(item)):
                    item.pop('oxidns_ha', None)
                    item.pop('oxidns_ha_state', None)
                    item.pop('oxidns_ha_publication', None)
        manager._update_agents(save)
    return {'success': True, 'config': None, 'routers': routers_using(manager, ids), 'warnings': errors}


def _member(manager, agent, role, problem=''):
    entry = {'agent_id': agent['id'], 'name': _name(agent), 'host': agent.get('host', ''),
             'online': agent.get('status') == 'online', 'role_config': role, 'runtime': None, 'error': problem}
    if problem:
        return entry
    if not entry['online']:
        entry['error'] = 'Agent 离线'
        return entry
    try:
        result = _call(manager.oxidns_ha_status, agent)
    except HAError as error:
        entry['error'] = str(error)
        return entry
    runtime = result.get('runtime')
    entry['runtime'] = runtime if isinstance(runtime, dict) else None
    managed = result.get('managed')
    if result.get('error'):
        entry['error'] = failure_text(str(result['error']), (agent.get('token'),))
    elif not isinstance(managed, dict):
        entry['error'] = '设备上没有 DNS 高可用配置，请重新开启'
    elif managed.get('active') is not True or managed.get('pending') is True:
        entry['error'] = '设备上的 DNS 高可用配置尚未生效'
    return entry


def _health(members, state):
    runtimes = [member['runtime'] for member in members if member['runtime']]
    holders = sum(1 for runtime in runtimes if runtime.get('vip_present') is True)
    running = [runtime for runtime in runtimes if runtime.get('keepalived_running') is True]
    if holders >= 2 or (len(running) == 2 and holders == 0):
        return 'split'
    if holders == 0:
        # With one member unreachable the VIP may still be on it.
        return 'degraded' if len(runtimes) == 1 else 'down'
    if (len(running) == 2 and all(runtime.get('healthy') is True for runtime in runtimes)
            and not any(member['error'] for member in members) and (state or {}).get('status') == 'succeeded'):
        return 'ok'
    return 'degraded'


def status(manager, agent_id):
    """Pair settings, publication progress and live roles."""
    from . import oxidns_ha_publish as publications
    publications.start_loop(manager)
    publications.advance(manager, agent_id)
    agents = manager.get_all_agents()
    by_id = {item['id']: item for item in agents}
    agent = by_id.get(agent_id)
    if not agent:
        raise HAError('Agent 不存在')
    publication = publications.summary(agent.get(publications.FIELD) or {}) or None
    if not enabled(agent):
        if publication is None:
            return {'success': True, 'config': None}
        return {'success': True, 'config': None, 'publication': publication,
                'warnings': (publication or {}).get('warnings', []), 'routers': (publication or {}).get('routers', [])}
    ha = agent['oxidns_ha']
    peer = by_id.get(ha.get('peer_id'))
    members = [_member(manager, agent, ha.get('role'))]
    if not peer:
        members.append({'agent_id': ha.get('peer_id'), 'name': '', 'host': '', 'online': False, 'role_config': None,
                        'runtime': None, 'error': '对端 Agent 不存在，请停用后重新设置'})
    elif not _paired(agent, peer):
        members.append(_member(manager, peer, None, '对端的 DNS 高可用记录与本机不一致，请停用后重新设置'))
    else:
        members.append(_member(manager, peer, peer['oxidns_ha'].get('role')))
    state = agent.get('oxidns_ha_state')
    return {'success': True, 'config': ha, 'state': state, 'members': members, 'health': _health(members, state),
            'publication': publication, 'warnings': (publication or {}).get('warnings', []),
            'routers': (publication or {}).get('routers', [])}
