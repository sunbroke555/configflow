"""Bounded, profile-scoped OxiDNS client samples; never store device history."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import ipaddress
import json
from threading import Lock
from time import monotonic

from backend.agents import oxidns_monitor
from backend.agents.oxidns_monitor import MonitorError
from backend.common.device_groups import infrastructure_snapshot


MAX_AGENTS = 16
MAX_CLIENTS = 20000
MAX_WORKERS = 4
CACHE_SECONDS = 10
_CACHE = OrderedDict()
_LOCK = Lock()


def _profile(config):
    default = (config.get('system_config') or {}).get('default_profile_id') or 'default'
    return config.get('profile_id') or default, default


def _binding(agent, default):
    if not isinstance(agent, dict):
        return None
    values = {key: agent.get(key) for key in ('id', 'host', 'port', 'service_type', 'enabled', 'version')}
    values['profile_id'] = agent.get('profile_id') or default
    values['token_hash'] = hashlib.sha256(str(agent.get('token') or '').encode()).hexdigest()
    return json.dumps(values, sort_keys=True, separators=(',', ':'))


class SnapshotManager:
    """Pin the endpoint and token, so a concurrent rebind cannot change a GET."""
    def __init__(self, agent):
        self.agent = deepcopy(agent)

    def get_agent_by_id(self, identity):
        return deepcopy(self.agent) if identity == self.agent.get('id') else None


def clear_activity_cache():
    with _LOCK:
        _CACHE.clear()


def _query(manager, agent, default):
    row = {'id': agent['id'], 'name': agent.get('name') or '', 'state': 'unavailable'}
    if agent.get('status') == 'offline':
        return {**row, 'state': 'offline', 'message': 'Agent 离线，暂无查询样本。'}, None
    if _binding(manager.get_agent_by_id(agent['id']), default) != _binding(agent, default):
        return {**row, 'message': '采集期间 Agent 绑定已变化，请刷新后重试。'}, None
    try:
        result = oxidns_monitor.clients(SnapshotManager(agent), agent['id'], {})
    except MonitorError as error:
        states = {'UPGRADE_REQUIRED': ('upgrade_required', '客户端查询样本需要 Agent 1.7.1-go 或更新版本。'),
                  'RECORDER_NOT_CONFIGURED': ('recorder_disabled', '查询记录未启用，暂无查询样本。'),
                  'AGENT_UNREACHABLE': ('offline', '暂时无法连接 Agent，暂无查询样本。')}
        state, message = states.get(error.code, ('unavailable', '暂时无法读取查询样本，请检查 Agent 和 OxiDNS 管理接口。'))
        return {**row, 'state': state, 'message': message}, None
    except Exception:
        return {**row, 'message': '暂时无法读取查询样本，请稍后重试。'}, None
    if _binding(manager.get_agent_by_id(agent['id']), default) != _binding(agent, default):
        return {**row, 'message': '采集期间 Agent 绑定已变化，已丢弃此次样本。'}, None
    return {**row, 'state': 'available', 'recorder': result['recorder'],
            'sample_size': result['sample_size'], 'sample_limited': result['sample_limited']}, result


def activity(config, manager, *, use_cache=True, infrastructure=None):
    """Sum bounded samples, not unique DNS requests or last-seen timestamps."""
    profile_id, default = _profile(config)
    # The resolved snapshot decides membership; the live manager supplies status
    # and is checked again after collection, including cached collection.
    allowed = [agent for agent in config.get('agents') or []
               if isinstance(agent, dict) and agent.get('service_type') == 'oxidns'
               and agent.get('enabled', True) and (agent.get('profile_id') or default) == profile_id]
    live = {agent['id']: agent for agent in manager.get_all_agents()}
    candidates = []
    for agent in allowed:
        current = live.get(agent['id'])
        candidates.append(deepcopy(current) if _binding(current, default) == _binding(agent, default) else deepcopy(agent))
    infrastructure = infrastructure if infrastructure is not None else infrastructure_snapshot(config)
    addresses = infrastructure.get('addresses') or []
    key = (id(manager), profile_id, tuple((_binding(a, default), a.get('status')) for a in candidates),
           json.dumps(addresses, sort_keys=True, ensure_ascii=False))
    now = monotonic()
    with _LOCK:
        while _CACHE and next(iter(_CACHE.values()))[0] <= now:
            _CACHE.popitem(last=False)
        cached = _CACHE.get(key)
    if use_cache and cached and all(
            _binding(manager.get_agent_by_id(agent['id']), default) == _binding(agent, default)
            for agent in candidates):
        return deepcopy(cached[1])

    selected = candidates[:MAX_AGENTS]
    if selected:
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(selected)), thread_name_prefix='device-group-sample') as executor:
            collected = list(executor.map(lambda a: _query(manager, a, default), selected))
    else:
        collected = []
    rows = [row for row, _ in collected]
    rows.extend({'id': a['id'], 'name': a.get('name') or '', 'state': 'unavailable',
                 'message': f'单次最多汇总 {MAX_AGENTS} 台 OxiDNS Agent，请缩小范围后重试。'}
                for a in candidates[MAX_AGENTS:])
    clients, sample_size, limited = {}, 0, len(candidates) > MAX_AGENTS
    for row, result in collected:
        if result is None:
            continue
        # A later worker may finish after another target was rebound.
        agent = next(a for a in selected if a['id'] == row['id'])
        if _binding(manager.get_agent_by_id(agent['id']), default) != _binding(agent, default):
            row.update(state='unavailable', message='采集期间 Agent 绑定已变化，已丢弃此次样本。')
            row.pop('sample_size', None)
            continue
        sample_size += result['sample_size']
        limited |= result['sample_limited']
        for client in result['clients']:
            address = ipaddress.ip_address(client['ip'])
            if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
                address = address.ipv4_mapped
            ip = str(address)
            merged = clients.setdefault(ip, {'ip': ip, 'count': 0, 'agent_ids': []})
            merged['count'] += client['count']
            if row['id'] not in merged['agent_ids']:
                merged['agent_ids'].append(row['id'])
    values = sorted(clients.values(), key=lambda row: (-row['count'], row['ip']))
    if len(values) > MAX_CLIENTS:
        values, limited = values[:MAX_CLIENTS], True
    infrastructure_samples = []
    seen = set()
    if sample_size:
        for entry in addresses:
            ip = str(ipaddress.ip_address(entry['ip']))
            if (ip, entry['kind']) in seen or ip not in clients:
                continue
            seen.add((ip, entry['kind']))
            count = clients[ip]['count']
            infrastructure_samples.append({'ip': ip, 'kind': entry['kind'], 'count': count,
                                           'share': count / sample_size})
    response = {'success': True, 'partial': any(row['state'] != 'available' for row in rows),
                'sample_size': sample_size, 'sample_limited': limited, 'clients': values,
                'agents': rows, 'infrastructure_samples': infrastructure_samples}
    with _LOCK:
        _CACHE[key] = (monotonic() + CACHE_SECONDS, deepcopy(response))
        _CACHE.move_to_end(key)
        while len(_CACHE) > 8:
            _CACHE.popitem(last=False)
    return response
