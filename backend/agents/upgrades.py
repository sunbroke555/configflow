"""Durable, version-confirmed Agent updates; never confuse acceptance with success."""
from collections import OrderedDict
from datetime import datetime
import hashlib
import logging
import os
from pathlib import Path
from threading import Lock
import time
import uuid

import requests

from .version import get_latest_version
from .diagnostics import failure_text
from .operation_events import append_event, clean_events, event_time, merge_events

logger = logging.getLogger(__name__)
_RESULT_LEVELS = {'succeeded': (logging.INFO, '成功'), 'failed': (logging.WARNING, '失败'),
                  'rolled_back': (logging.WARNING, '失败，已回滚到原版本'),
                  'rollback_failed': (logging.ERROR, '失败，且回滚失败')}

TERMINAL = {'succeeded', 'failed', 'rolled_back', 'rollback_failed'}
ARCHES = ('linux-amd64', 'linux-arm64', 'linux-armv7')
STAGES = {'queued', 'downloading', 'verifying', 'backing_up', 'migrating',
          'replacing', 'restarting', 'checking', 'rolling_back'} | TERMINAL


def artifact_directory():
    configured = Path(os.getenv('AGENTS_STATIC_DIR', '/opt/configflow/static/agents'))
    return configured if configured.exists() else Path(__file__).resolve().parents[1] / 'static/agents'


_MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
_ARTIFACT_READ_BYTES = 256 * 1024
_ARTIFACT_CACHE_LIMIT = 12
_ARTIFACT_CACHE = OrderedDict()
_ARTIFACT_CACHE_LOCK = Lock()


class _ArtifactChanged(Exception):
    """The pathname and bytes read did not describe one stable file."""


def _artifact_signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_artifact(path, expected):
    """Hash through a fixed buffer; never retain the executable in memory."""
    digest = hashlib.sha256()
    size = 0
    magic = b''
    buffer = bytearray(_ARTIFACT_READ_BYTES)
    view = memoryview(buffer)
    with path.open('rb', buffering=0) as handle:
        if _artifact_signature(os.fstat(handle.fileno())) != expected:
            raise _ArtifactChanged
        while True:
            count = handle.readinto(buffer)
            if not count:
                break
            if len(magic) < 4:
                magic += bytes(view[:min(4 - len(magic), count)])
            size += count
            digest.update(view[:count])
        after_read = _artifact_signature(os.fstat(handle.fileno()))
    try:
        after_path = _artifact_signature(path.stat())
    except FileNotFoundError as error:
        raise _ArtifactChanged from error
    if expected != after_read or expected != after_path or size != expected[2]:
        raise _ArtifactChanged
    if magic != b'\x7fELF' or size > _MAX_ARTIFACT_BYTES:
        return None
    return digest.hexdigest(), size


def _artifact_metadata(path):
    # Absolute lexical paths isolate directories without resolving symlinks to
    # another cache slot. Target identity is still checked through stat/fstat.
    key = os.path.abspath(os.fspath(path))
    # On Windows st_ctime is creation time, so restoring mtime can conceal
    # an in-place edit. Keep streaming there, without trusting digest reuse.
    use_cache = os.name != 'nt'
    with _ARTIFACT_CACHE_LOCK:
        for attempt in range(2):
            if not path.is_file():
                _ARTIFACT_CACHE.pop(key, None)
                return None
            expected = _artifact_signature(path.stat())
            cached = _ARTIFACT_CACHE.get(key) if use_cache else None
            if cached is not None and cached[0] == expected:
                _ARTIFACT_CACHE.move_to_end(key)
                return cached[1]
            _ARTIFACT_CACHE.pop(key, None)
            try:
                metadata = _read_artifact(path, expected)
            except _ArtifactChanged:
                if attempt == 0:
                    continue
                raise OSError('Agent artifact changed while being read; please retry') from None
            if metadata is not None and use_cache:
                _ARTIFACT_CACHE[key] = (expected, metadata)
                while len(_ARTIFACT_CACHE) > _ARTIFACT_CACHE_LIMIT:
                    _ARTIFACT_CACHE.popitem(last=False)
            return metadata


def artifacts():
    result = {}
    for arch in ARCHES:
        path = artifact_directory() / ('configflow-agent-' + arch)
        metadata = _artifact_metadata(path)
        if metadata is not None:
            # Cache immutable tuples; callers may freely mutate either dict.
            result[arch] = {'sha256': metadata[0], 'size': metadata[1]}
    return result


def _request(agent, method, path, **kwargs):
    # Control-plane calls must not inherit the rule-download proxy.
    with requests.Session() as session:
        session.trust_env = False
        return session.request(method, f"http://{agent['host']}:{agent['port']}{path}",
                               headers={'Authorization': 'Bearer ' + agent['token']}, timeout=5, **kwargs)


def _save(manager, agent_id, update_id, patch, *, only_queued=False):
    finished = []

    def update(agents):
        agent = next(item for item in agents if item['id'] == agent_id)
        state = agent.get('latest_upgrade') or {}
        before = state.get('status')
        if state.get('update_id') != update_id:
            raise ValueError('Upgrade ownership changed')
        if state.get('status') in TERMINAL - {'rollback_failed'}:
            return dict(state)
        if only_queued and state.get('status') != 'queued':
            return dict(state)
        reported_at = event_time(patch.get('agent_updated_at')) if isinstance(patch.get('agent_updated_at'), str) else None
        if reported_at and state.get('agent_updated_at') and reported_at < state['agent_updated_at']:
            return dict(state)
        incoming = clean_events(patch.get('events'), (agent.get('token'),), STAGES | {'unknown'})
        events = merge_events(state.get('events'), incoming, secrets=(agent.get('token'),), statuses=STAGES | {'unknown'})
        if patch.get('status') and 'message' not in patch:
            state.pop('message', None)
        for key, value in patch.items():
            if key not in ('events', 'agent_updated_at'):
                state[key] = failure_text(value, (agent.get('token'),), limit=2048) if key in ('error', 'message') else value
        if reported_at:
            state['agent_updated_at'] = reported_at
        if (not incoming or incoming[-1]['status'] != state.get('status')
                or events and events[-1]['status'] != state.get('status')):
            events = append_event(events, state.get('status'), state.get('message') or state.get('error'),
                                  secrets=(agent.get('token'),))
        state['events'] = events
        state['updated_at'] = time.time()
        agent['latest_upgrade'] = state
        if state.get('status') != before and state.get('status') in _RESULT_LEVELS:
            finished.append((agent.get('name') or agent_id, dict(state)))
        return dict(state)
    result = manager._update_agents(update)
    for name, state in finished[-1:]:
        from backend.utils.logger import log_text
        detail = state.get('error') or state.get('message') or ''
        level, text = _RESULT_LEVELS[state['status']]
        logger.log(level, 'Agent %s 升级 %s → %s %s%s', log_text(name),
                   log_text(state.get('previous_version') or '?', 40), log_text(state.get('target_version') or '?', 40),
                   text, '（' + log_text(detail, 300) + '）' if detail and state['status'] != 'succeeded' else '')
    return result


def start(manager, agent_id):
    agent = manager.get_agent_by_id(agent_id)
    if not agent:
        raise ValueError('Agent not found')
    if agent.get('deployment_method') == 'docker':
        raise ValueError('Docker Agent 请更新镜像并重建容器；网页二进制更新不适用于 Docker')
    try:
        info_response = _request(agent, 'GET', '/api/upgrade-info')
        if info_response.status_code == 404:
            info = None
        else:
            info_response.raise_for_status()
            info = info_response.json()
    except (requests.RequestException, ValueError) as exc:
        raise ValueError('无法连接 Agent 并确认更新能力') from exc
    if info and (info.get('agent_id') != agent_id or info.get('deployment_method') != 'shell'):
        raise ValueError('Agent 身份或安装方式与网页记录不符')
    if not info and agent.get('deployment_method') != 'shell':
        raise ValueError('旧 Agent 未报告 Shell 安装方式，无法安全选择更新流程')
    available = artifacts()
    arch = info.get('architecture') if info else None
    if (arch and arch not in available) or (not info and set(available) != set(ARCHES)):
        raise ValueError('更新文件未准备就绪，请先更新 ConfigFlow 主服务镜像')
    state = {'update_id': uuid.uuid4().hex, 'target_version': get_latest_version(),
             'previous_version': agent.get('version', ''), 'status': 'queued',
             'agent_id': agent_id, 'started_at': time.time(), 'updated_at': time.time(),
             'protocol': 2 if info else 1, 'artifacts': available}
    state['message'] = f'正在提交 Agent {state["target_version"]} 更新请求' + (f'（{arch}）' if arch else '')
    state['events'] = append_event([], 'queued', state['message'])

    def reserve(agents):
        current = next(item for item in agents if item['id'] == agent_id)
        previous = current.get('latest_upgrade') or {}
        if previous.get('update_id') != (agent.get('latest_upgrade') or {}).get('update_id'):
            raise ValueError('当前更新任务已改变，请刷新后查看最新结果')
        if previous and (previous.get('status') not in TERMINAL or previous.get('status') == 'rollback_failed'):
            raise ValueError('已有更新正在执行或等待恢复，请先查询结果')
        deployment = current.get('latest_deployment') or {}
        if deployment and deployment.get('status') not in {'succeeded', 'failed', 'rolled_back'}:
            raise ValueError('存在未结束的配置发布，请先完成或恢复')
        current['latest_upgrade'] = dict(state)
    manager._update_agents(reserve)
    from backend.utils.logger import log_text
    logger.info('开始升级 Agent %s：%s → %s', log_text(agent.get('name') or agent_id),
                log_text(state['previous_version'] or '?', 40), log_text(state['target_version'], 40))
    payload = {'version': state['target_version'], 'update_id': state['update_id']}
    if info:
        payload.update(available[arch])
    try:
        response = _request(agent, 'POST', '/api/update', json=payload)
        if response.status_code not in (200, 202):
            try:
                message = response.json().get('message', 'Agent rejected upgrade')
            except ValueError:
                message = 'Agent rejected upgrade'
            return _save(manager, agent_id, state['update_id'], {'status': 'failed', 'error': message})
    except requests.RequestException:
        # The response may have been lost after acceptance. Keep the same ID
        # pending and reconcile with the Agent instead of resubmitting a new job.
        return _save(manager, agent_id, state['update_id'], {'status': 'checking', 'error': '请求结果待确认，正在查询 Agent'}, only_queued=True)
    # Acceptance alone does not prove that the independent worker is already
    # downloading. A simultaneous poll may also have observed a later stage.
    return _save(manager, agent_id, state['update_id'], {
        'status': 'queued', 'message': f'Agent {state["target_version"]} 更新请求已送达，等待设备处理'}, only_queued=True)


def poll(manager, agent_id):
    agent = manager.get_agent_by_id(agent_id)
    if not agent or not agent.get('latest_upgrade'):
        raise ValueError('No upgrade recorded')
    state = dict(agent['latest_upgrade'])
    if state['status'] in TERMINAL - {'rollback_failed'}:
        return state
    if state.get('recovery_reported_at') and agent.get('version') == state['previous_version']:
        try:
            confirmed = datetime.fromisoformat(agent.get('last_heartbeat', '')).timestamp()
            if confirmed > state['recovery_reported_at']:
                return _save(manager, agent_id, state['update_id'], {'status': 'rolled_back'})
        except ValueError:
            pass
    try:
        worker_patch = None
        response = _request(agent, 'GET', '/api/update') if state['protocol'] == 2 else None
        if response is not None and response.status_code == 200:
            result = response.json()
            if result.get('update_id') == state['update_id'] and result.get('agent_id') == agent_id and result.get('status') in STAGES:
                worker_patch = {'status': result['status'], 'error': str(result.get('error', ''))[:2048],
                                'events': result.get('events'), 'agent_updated_at': result.get('updated_at')}
                if isinstance(result.get('message'), str):
                    worker_patch['message'] = result['message']
                if result['status'] != 'succeeded':
                    return _save(manager, agent_id, state['update_id'], worker_patch)
        info_response = _request(agent, 'GET', '/api/upgrade-info')
        if info_response.status_code == 200:
            info = info_response.json()
            if info.get('agent_id') == agent_id and info.get('version') == state['target_version'] and info.get('migration_ready') is True:
                # Protocol 2 also needs the independent worker's final result.
                if state['protocol'] == 1 or response.status_code == 200 and result.get('update_id') == state['update_id'] and result.get('status') == 'succeeded':
                    return _save(manager, agent_id, state['update_id'], {**(worker_patch or {}), 'status': 'succeeded', 'error': ''})
        if worker_patch is not None and worker_patch['status'] == 'succeeded':
            worker_patch.update(status='checking', message='Agent 已报告更新完成，正在确认版本、身份与迁移结果')
            worker_patch['events'] = [entry for entry in clean_events(worker_patch['events'], statuses=STAGES)
                                      if entry['status'] not in TERMINAL]
            return _save(manager, agent_id, state['update_id'], worker_patch)
    except (requests.RequestException, ValueError):
        pass
    deadline = state['started_at'] + 600
    if time.time() > deadline:
        # The Agent did not report this job in progress. A heartbeat sent after
        # the deadline that still carries the old version proves the update
        # never took effect, so release the lock and allow a retry.
        if agent.get('version') == state['previous_version'] and _heartbeat_after(agent, deadline):
            return _save(manager, agent_id, state['update_id'], {
                'status': 'failed', 'error': f"更新未生效：Agent 仍在运行 {state['previous_version']}，可重新更新；详情请查看 Agent 日志"})
        # Do not claim rollback when the old executable cannot be reached.
        if state['status'] != 'unknown':
            return _save(manager, agent_id, state['update_id'], {'status': 'unknown', 'error': '升级结果未能确认，正在继续查询；请查看 Agent 日志'})
    return state


def _heartbeat_after(agent, moment):
    try:
        return datetime.fromisoformat(agent.get('last_heartbeat', '')).timestamp() > moment
    except ValueError:
        return False


def report_legacy(manager, agent_id, payload):
    agent = manager.get_agent_by_id(agent_id)
    state = agent.get('latest_upgrade') or {}
    if state.get('protocol') != 1 or payload.get('update_id') != state.get('update_id') or payload.get('status') != 'rolling_back':
        raise ValueError('Invalid legacy upgrade report')
    return _save(manager, agent_id, state['update_id'], {'status': 'rolling_back', 'recovery_reported_at': time.time(), 'error': str(payload.get('error', 'Migration failed'))[:2048]})
