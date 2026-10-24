"""Durable, ordered device-group batches over the existing deployment protocol.

The worker never fabricates a deployment outcome. Unknown/ready/failed tasks
pause the batch, and retries reconcile the original ID before sending anything.
"""
from copy import deepcopy
from contextlib import contextmanager
import re
from threading import Lock, Thread
import time
import uuid

from backend.common.config_repository import ProfileInUse, ProfileRepositoryError, ProfileValidationError
from backend.common import device_group_workspace as workspace

_RUNNING = {}
_THREAD_LOCK = Lock()
_TERMINAL = {'succeeded', 'failed', 'rolled_back', 'rollback_failed'}


def _confirmed_success(task):
    return (task.get('status') == 'succeeded' and not task.get('pending')
            and not task.get('query_error') and not task.get('http_status'))


def _ambiguous(task):
    return bool(task.get('pending') or task.get('query_error') or task.get('http_status'))


def _missing_reservation(task):
    # This is a local journal miss, not an Agent's missing/lost POST receipt.
    return (task.get('http_status') == 404 and task.get('message') == 'Deployment not found'
            and not task.get('status') and not task.get('pending') and not task.get('query_error'))


def _path(batch_id):
    if not isinstance(batch_id, str) or not re.fullmatch(r'[a-fA-F0-9-]{8,64}', batch_id):
        raise ProfileValidationError('发布批次 ID 无效')
    return f'device-group-publications/{batch_id}.json'


def _read(repository, profile_id, batch_id):
    try:
        return repository.read_profile_json(profile_id, _path(batch_id))
    except (FileNotFoundError, ProfileRepositoryError) as error:
        if isinstance(error, FileNotFoundError) or isinstance(error.__cause__, FileNotFoundError):
            raise ProfileValidationError('未找到这个设备组发布批次，请刷新后重试') from None
        raise


def _save(repository, batch):
    repository.write_profile_json(batch['profile_id'], _path(batch['batch_id']), batch)


@contextmanager
def _lock(repository, profile_id):
    path = repository.profile_path(profile_id, 'device-group-publications/.batch.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with repository._lock(path):
        yield


def _latest_raw(repository, profile_id):
    try:
        value = repository.read_profile_json(profile_id, 'device-group-publications/latest.json')
    except (FileNotFoundError, ProfileRepositoryError) as error:
        if isinstance(error, FileNotFoundError) or isinstance(error.__cause__, FileNotFoundError):
            return None
        raise
    return _read(repository, profile_id, value['batch_id'])


def _public(batch):
    from backend.routes.agents import _public_deployment
    from .deployment_messages import deployment_message
    fields = ('batch_id', 'profile_id', 'config_revision', 'revision', 'state', 'phase', 'message', 'targets')
    result = {key: deepcopy(batch[key]) for key in fields}
    result['message'] = deployment_message(result['message'])
    for target in result['targets']:
        target.pop('target_fingerprint', None)
        target.pop('attempt', None)
        target.pop('reservation_rejected', None)
        if isinstance(target.get('deployment'), dict):
            target['deployment'] = _public_deployment(target['deployment'])
        if 'message' in target:
            target['message'] = deployment_message(target['message'])
    paused = batch['state'] == 'paused'
    stale = bool(batch.get('requires_new_preview'))
    result['actions'] = {'can_retry': paused and not stale,
                         'can_reconcile': paused,
                         'activate_agent_ids': [target['agent_id'] for target in batch['targets']
                             if (target.get('deployment') or {}).get('status') == 'ready' and
                             not _ambiguous(target['deployment']) and not stale],
                         'requires_new_preview': stale}
    return result


def latest(repository, profile_id):
    value = _latest_raw(repository, profile_id)
    return _public(value) if value else None


def get(repository, profile_id, batch_id):
    batch = _read(repository, profile_id, batch_id)
    # Restart-safe recovery resumes the same durable IDs, never a fresh batch.
    if batch['state'] in ('preparing', 'running'):
        from backend.common.agent_manager import get_agent_manager
        _start(repository, get_agent_manager(), profile_id, batch_id)
    return _public(batch)


def _guard(repository, batch):
    config, revision = repository.get_publish_snapshot(batch['profile_id'])
    if revision != batch['config_revision'] or workspace.revision(config, revision) != batch['revision']:
        raise ProfileInUse('配置或 Agent 连接已改变，请重新预览；旧批次不会改用新草稿')
    available = {row['id']: row for row in workspace.targets(config)}
    for target in batch['targets']:
        current = available.get(target['agent_id'])
        if current is None or current['target_fingerprint'] != target['target_fingerprint']:
            raise ProfileInUse('发布目标或绑定已改变，请重新预览')
    return config, available


def blocks_follow(manager, target, source=None):
    """DNS follow observes the same batch barrier, including cross-space links."""
    identities = {target.get('id')}
    if source:
        identities.add(source.get('id'))
    default = manager.repository.default_profile_id()
    profiles = {item.get('profile_id') or default for item in (target, source) if item}
    for profile_id in profiles:
        batch = _latest_raw(manager.repository, profile_id)
        if batch and batch['state'] in ('preparing', 'running', 'paused') and \
                identities.intersection(row['agent_id'] for row in batch['targets']):
            return True
    return False


def require_coordinated_publish(config, service_type):
    if config.get('device_groups') is not None and service_type in ('mihomo', 'oxidns'):
        raise ProfileInUse('此配置空间已启用设备组，请到设备组页按顺序发布 Mihomo 和 OxiDNS')


def _existing_create(repository, profile_id, batch_id, payload, base_url):
    try:
        existing = _read(repository, profile_id, batch_id)
    except ProfileValidationError:
        return None
    if existing.get('preview_token') != payload.get('preview_token') or existing.get('base_url', '') != base_url or \
            sorted(row['agent_id'] for row in existing['targets']) != sorted(payload['target_ids']):
        raise ProfileInUse('这个批次 ID 已用于其他内容，请查询原批次或重新预览')
    return _public(existing)


def _require_available_batch(previous):
    if previous and previous['state'] in ('preparing', 'running', 'paused'):
        active = any((row.get('deployment') or {}).get('status') not in _TERMINAL or
                     _ambiguous(row.get('deployment') or {})
                     for row in previous['targets'] if row.get('deployment_id') and not row.get('reservation_rejected'))
        if active or previous['state'] != 'paused':
            raise ProfileInUse('已有设备组批次尚未完成，请先查询或处理原批次')


def create(repository, manager, profile_id, payload):
    if not isinstance(payload, dict):
        raise ProfileValidationError('发布请求必须是对象')
    batch_id = payload.get('batch_id')
    _path(batch_id)
    base_url = payload.get('base_url', '')
    if not isinstance(base_url, str) or len(base_url) > 4096 or any(ord(c) < 32 for c in base_url):
        raise ProfileValidationError('发布地址必须是有效文本')
    identities = payload.get('target_ids')
    if not isinstance(identities, list) or any(not isinstance(value, str) for value in identities):
        raise ProfileValidationError('发布目标必须是 Agent ID 列表')
    # The worker holds the profile lock while contacting Agents. Atomic reads
    # keep duplicate clicks and running-batch requests responsive; the lock
    # below still rechecks both facts before creating a new durable batch.
    existing = _existing_create(repository, profile_id, batch_id, payload, base_url)
    if existing:
        return existing
    _require_available_batch(_latest_raw(repository, profile_id))
    with _lock(repository, profile_id):
        existing = _existing_create(repository, profile_id, batch_id, payload, base_url)
        if existing:
            return existing
        config, revision = repository.get_publish_snapshot(profile_id)
        if config.get('device_groups') is None:
            raise ProfileValidationError('请先保存设备组草稿后发布')
        expected = workspace.preview_token(config, revision, config, base_url=base_url)
        if payload.get('preview_token') != expected:
            raise ProfileInUse('发布预览已过期，请重新预览已保存的草稿')
        target_rows = workspace.targets(config)
        identities = payload.get('target_ids')
        # Every bound target must move together. Silently omitting an offline
        # Mihomo would invalidate the promised barrier before the DNS phase.
        if not isinstance(identities, list) or any(not isinstance(value, str) for value in identities) or \
                len(identities) != len(set(identities)) or \
                set(identities) != {row['id'] for row in target_rows} or not target_rows:
            raise ProfileValidationError('请包含此配置空间全部受影响的 Mihomo 和 OxiDNS Agent')
        previous = _latest_raw(repository, profile_id)
        _require_available_batch(previous)
        if previous and previous['state'] in ('preparing', 'running', 'paused'):
            previous.update(state='failed', message='已由新的确认预览替代', requires_new_preview=True)
            _save(repository, previous)
        for agent in manager.get_all_agents():
            follow = agent.get('dns_follow') or {}
            if (agent['id'] in identities or follow.get('source_agent_id') in identities) and \
                    (agent.get('dns_follow_state') or {}).get('status') in ('preparing', 'publishing'):
                raise ProfileInUse('关联 DNS 联动正在发布，请待其完成后再启动设备组批次')
            sync = (agent.get('rule_sync') or {}).get('state') or {}
            if agent['id'] in identities and (sync.get('status') == 'running' or sync.get('native_task')):
                raise ProfileInUse('关联规则自动同步正在执行，请待其完成后再启动设备组批次')
        batch = {'batch_id': batch_id, 'profile_id': profile_id, 'config_revision': revision,
                 'revision': workspace.revision(config, revision), 'preview_token': expected,
                 'base_url': payload.get('base_url', ''), 'state': 'preparing', 'phase': 'mihomo',
                 'message': '正在检查所有发布目标', 'requires_new_preview': False,
                 'targets': [{'agent_id': row['id'], 'name': row['name'], 'service_type': row['service_type'],
                              'target_fingerprint': row['target_fingerprint'], 'state': 'queued', 'attempt': 0}
                             for row in target_rows]}
        _save(repository, batch)
        repository.write_profile_json(profile_id, 'device-group-publications/latest.json', {'batch_id': batch_id})
    _start(repository, manager, profile_id, batch_id)
    return _public(batch)


def _version(manager, agent_id, profile_id=None):
    from backend.agents.device_group_versions import target_mihomo_capability
    from backend.common.device_group_capabilities import DeviceGroupMihomoVersionError
    try:
        return target_mihomo_capability(manager, agent_id, profile_id=profile_id, strict=True,
                                        default_profile_id=manager.repository.default_profile_id())
    except DeviceGroupMihomoVersionError as error:
        if error.capability.get('reason_code') == 'target_changed':
            raise ProfileInUse(str(error)) from None
        raise


def _publish(manager, agent, config, batch, target):
    from backend.routes.agents import _prepare_transactional_content
    from backend.utils.provider_delivery import commit_provider_bundle
    deployment_id = target['deployment_id']
    reservation = manager.begin_deployment(agent['id'], deployment_id)
    if not reservation.get('success') and not reservation.get('existing'):
        # begin_deployment is a local reservation: this explicit rejection
        # proves this candidate never reached preparation or an Agent POST.
        target['reservation_rejected'] = True
        return reservation
    if target.pop('reservation_rejected', None):
        # Clear the proof durably before any preparation/network operation.
        # A subsequent crash or missing Agent receipt must stay ambiguous.
        _save(manager.repository, batch)
    if reservation.get('existing'):
        return reservation
    try:
        prepared = _prepare_transactional_content(agent, config, base_url=batch['base_url'], deployment_id=deployment_id)
        _guard(manager.repository, batch)
        if prepared['prepared_providers']:
            commit_provider_bundle(batch['profile_id'], prepared['prepared_providers'])
        return manager.publish_deployment(agent['id'], prepared['bundle'], activate=True,
            config_revision=batch['config_revision'], base_url=batch['base_url'],
            resource_config=config, source_snapshot=config,
            expected_target_fingerprint=target['target_fingerprint'])
    except Exception as error:
        manager.record_deployment(agent['id'], deployment_id, {'status': 'failed', 'success': False,
            'message': str(error) if isinstance(error, ValueError) else '准备发布失败，请检查设备连接后重试'}, only_unsubmitted=True)
        raise
    finally:
        manager.finish_deployment_preparation(agent['id'], deployment_id)


def _pause(repository, batch, target, message, *, stale=False):
    batch.update(state='paused', message=message, requires_new_preview=stale)
    if target:
        target.update(state='failed' if (target.get('deployment') or {}).get('status') in _TERMINAL else 'paused', message=message)
    _save(repository, batch)


def advance(repository, manager, profile_id, batch_id):
    """One worker owns a profile batch across preparation and status polling."""
    with _lock(repository, profile_id):
        batch = _read(repository, profile_id, batch_id)
        if batch['state'] not in ('preparing', 'running'):
            return
        current_target = None
        try:
            config, available = _guard(repository, batch)
            from backend.common.device_groups import compile_groups
            compiled = compile_groups(config)
            requires_group_syntax = any(row['id'] != 'default' and row['mode'] != 'follow' and
                                        compiled['effective'].get(row['id']) for row in compiled['groups'])
            # Check every target before touching the first one. A disabled or
            # offline target still permits saving, but cannot begin a release.
            for row in batch['targets']:
                live = available[row['agent_id']]
                if live['status'] != 'online' or not live['enabled']:
                    raise ValueError(f'「{row["name"]}」离线或已停用，发布已暂停；草稿已保留')
                if row['service_type'] == 'mihomo' and requires_group_syntax:
                    _version(manager, row['agent_id'], batch['profile_id'])
            batch.update(state='running', message='正在按顺序发布设备组')
            _save(repository, batch)
            for target in batch['targets']:
                current_target = target
                if target['state'] == 'succeeded':
                    continue
                config, _ = _guard(repository, batch)
                if target['service_type'] == 'oxidns':
                    # A manual rollback or an ambiguous status since the last
                    # worker run must not open the DNS phase's barrier.
                    for peer in batch['targets']:
                        if peer['service_type'] != 'mihomo':
                            continue
                        task = manager.get_deployment(peer['agent_id'], peer['deployment_id'],
                            expected_target_fingerprint=peer['target_fingerprint'])
                        peer['deployment'] = task
                        if not _confirmed_success(task):
                            _pause(repository, batch, peer, task.get('message') or 'Mihomo 状态尚未确认，OxiDNS 发布已暂停')
                            return
                batch['phase'] = target['service_type']
                target['state'] = 'running'
                if target.get('deployment_id'):
                    task = manager.get_deployment(target['agent_id'], target['deployment_id'],
                        expected_target_fingerprint=target['target_fingerprint'])
                    if target.get('reservation_rejected') and _missing_reservation(task):
                        agent = manager.get_agent_by_id(target['agent_id'])
                        task = _publish(manager, agent, config, batch, target)
                    elif target.get('reservation_rejected') and task.get('status'):
                        # A journal now exists for this ID. Its status, including
                        # unknown/submitted, owns recovery from this point on.
                        target.pop('reservation_rejected', None)
                        _save(repository, batch)
                else:
                    target['attempt'] += 1
                    target['deployment_id'] = uuid.uuid4().hex
                    _save(repository, batch)  # Durable before any Agent request.
                    agent = manager.get_agent_by_id(target['agent_id'])
                    task = _publish(manager, agent, config, batch, target)
                deadline = time.monotonic() + 300
                while True:
                    target['deployment'] = task
                    _save(repository, batch)
                    status = task.get('status')
                    if _confirmed_success(task):
                        target['state'] = 'succeeded'
                        _save(repository, batch)
                        break
                    if status in _TERMINAL or status in ('unknown', 'ready') or task.get('http_status') or time.monotonic() >= deadline:
                        _pause(repository, batch, target, task.get('message') or '发布结果待确认，后续设备已暂停')
                        return
                    time.sleep(1)
                    _guard(repository, batch)
                    task = manager.get_deployment(target['agent_id'], target['deployment_id'],
                        expected_target_fingerprint=target['target_fingerprint'])
            batch.update(state='succeeded', phase='completed', message='Mihomo 与 OxiDNS 已按顺序完成发布')
            _save(repository, batch)
        except Exception as error:
            message = str(error) if isinstance(error, ValueError) else '发布检查失败，请确认设备连接后重试'
            _pause(repository, batch, current_target, message, stale=isinstance(error, ProfileInUse))


def _start(repository, manager, profile_id, batch_id):
    key = (str(repository.data_dir), profile_id, batch_id)
    with _THREAD_LOCK:
        if key in _RUNNING:
            return
        def run():
            try:
                advance(repository, manager, profile_id, batch_id)
            finally:
                with _THREAD_LOCK:
                    _RUNNING.pop(key, None)
        thread = Thread(target=run, daemon=True, name='device-group-publish')
        _RUNNING[key] = thread
        thread.start()


def retry(repository, manager, profile_id, batch_id, payload):
    if not isinstance(payload, dict):
        raise ProfileValidationError('发布恢复请求必须是对象')
    action = payload.get('action')
    if action not in ('reconcile', 'retry', 'activate'):
        raise ProfileValidationError('发布恢复操作无效')
    current = _read(repository, profile_id, batch_id)
    if current['state'] == 'succeeded':
        return _public(current)
    if current['state'] != 'paused':
        raise ProfileInUse('发布批次仍在执行，请先查询当前进度')
    with _lock(repository, profile_id):
        batch = _read(repository, profile_id, batch_id)
        if batch['state'] == 'succeeded':
            return _public(batch)
        if batch['state'] != 'paused':
            raise ProfileInUse('发布批次仍在执行，请先查询当前进度')
        target = next((row for row in batch['targets'] if row['state'] != 'succeeded'), None)
        try:
            _guard(repository, batch)
        except ProfileInUse as error:
            _pause(repository, batch, None, str(error), stale=True)
            if action != 'reconcile':
                raise
            # A new draft cannot resume an old batch, but its original target
            # may still be queried safely. Never follow a changed endpoint.
            if target and target.get('deployment_id'):
                agent = manager.get_agent_by_id(target['agent_id'])
                if agent and workspace.target_fingerprint(agent, repository.default_profile_id()) == target['target_fingerprint']:
                    task = manager.get_deployment(target['agent_id'], target['deployment_id'],
                        expected_target_fingerprint=target['target_fingerprint'])
                    target['deployment'] = task
                    if _confirmed_success(task):
                        target['state'] = 'succeeded'
                    _save(repository, batch)
            return _public(batch)
        if target and target.get('deployment_id'):
            task = manager.get_deployment(target['agent_id'], target['deployment_id'],
                expected_target_fingerprint=target['target_fingerprint'])
            target['deployment'] = task
            if action == 'activate':
                if payload.get('agent_id') != target['agent_id'] or task.get('status') != 'ready' or _ambiguous(task):
                    raise ProfileInUse('只能激活当前阶段已校验的发布任务')
                target['deployment'] = manager.activate_deployment(target['agent_id'], target['deployment_id'],
                    expected_target_fingerprint=target['target_fingerprint'])
            elif action == 'retry' and task.get('status') in ('failed', 'rolled_back') and \
                    not _ambiguous(task):
                # A definitely failed attempt may receive a new ID. Unknown,
                # lost, or absent records must never be resubmitted blindly.
                target.pop('deployment_id', None)
                target.pop('deployment', None)
        batch.update(state='running', message='正在核对原发布任务', requires_new_preview=False)
        _save(repository, batch)
    _start(repository, manager, profile_id, batch_id)
    return _public(batch)
