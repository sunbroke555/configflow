"""Opt-in per-device rule reconciliation, independent of browser requests.

The receipt of the last successful release is the source for every candidate.
Only enabled rule scopes are projected onto it; proxy and DNS drafts never
become an accidental automatic publication.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import logging
import os
import threading
import time
import uuid

import yaml

from . import rule_sync_store as store
from .rule_sync_snapshot import fetch_snapshot
from .version import compare_versions

MIN_AGENT_VERSION = '1.8.30-go'
POLL_SECONDS = 5
RETRY_SECONDS = 60
TERMINAL = ('succeeded', 'failed', 'rolled_back')
logger = logging.getLogger(__name__)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def settings_for(agent):
    settings = (agent.get('rule_sync') or {}).get('settings') or {}
    return {key: settings.get(key) is True for key in ('library', 'rules')}


def _update(manager, agent_id, *, state=None, settings=None):
    def update(agents):
        agent = next((item for item in agents if item['id'] == agent_id), None)
        if agent is None:
            return False
        sync = agent.setdefault('rule_sync', {})
        if settings is not None:
            sync['settings'] = deepcopy(settings)
        previous = {key: value for key, value in (sync.get('state') or {}).items() if key != 'updated_at'}
        if state is not None and previous != state:
            sync['state'] = {**deepcopy(state), 'updated_at': _now()}
        return True
    return manager._update_agents(update)


def status(manager, agent_id):
    agent = manager.get_agent_by_id(agent_id)
    if agent is None:
        raise KeyError('Agent not found')
    if agent.get('service_type') not in ('mihomo', 'oxidns'):
        raise ValueError('规则自动同步仅支持 Mihomo 和 OxiDNS')
    settings = settings_for(agent)
    state = (agent.get('rule_sync') or {}).get('state') or {}
    if not any(settings.values()):
        state = {'status': 'disabled', 'message': '自动同步已关闭'}
    elif not state:
        state = {'status': 'pending', 'message': '等待检查规则变化'}
    return {'success': True, 'settings': settings,
            'state': {key: state[key] for key in ('status', 'message', 'updated_at') if key in state},
            'baseline_ready': store.load(manager.repository, agent, with_bundle=False) is not None}


def configure(manager, agent_id, settings):
    status(manager, agent_id)  # validate target before any write
    if (not isinstance(settings, dict) or set(settings) != {'library', 'rules'}
            or any(type(value) is not bool for value in settings.values())):
        raise ValueError('library 和 rules 必须分别为布尔值')
    # Keep an accepted job's identity so process restart/disable/re-enable can
    # reconcile it; switching off prevents subsequent jobs, not an accepted API.
    _update(manager, agent_id, settings=settings)
    return status(manager, agent_id)


def _identity(agent):
    return (*tuple(agent.get(key) for key in ('id', 'profile_id', 'service_type', 'host', 'port', 'token', 'config_version')),
            (agent.get('applied_content') or {}).get('deployment_id'))


def _unchanged(manager, agent, settings, *, deployment_id=None):
    latest = manager.get_agent_by_id(agent['id'])
    return (latest and _identity(latest) == _identity(agent) and settings_for(latest) == settings
            and ((latest.get('latest_deployment') or {}).get('status') in (*TERMINAL, None)
                 or (latest.get('latest_deployment') or {}).get('deployment_id') ==
                    deployment_id))


def prepare_candidate(agent, source, baseline, settings, deployment_id):
    """Generate rule artifacts while pinning every non-rule delivery byte."""
    from .deployment_bundle import build_deployment_bundle, retarget_config_path, _pack
    from .rule_sync_projection import isolate_rule_bundle
    base_bundle = baseline['bundle']
    previous = store.bundle_files(base_bundle)
    service = agent['service_type']
    base_url = baseline.get('base_url', '')
    if service == 'mihomo':
        from backend.converters.mihomo import generate_mihomo_config
        content = generate_mihomo_config(source, base_url=base_url,
                    sync_lan_hosts=True, preflight_providers=False,
                    region_probe=bool(agent.get('domain_discovery_enabled')))
        downloads = []  # materializer reads the final rule-providers graph
    else:
        from backend.converters.oxidns import generate_oxidns_config, get_oxidns_ruleset_downloads
        content = generate_oxidns_config(source, base_url=base_url)
        downloads = get_oxidns_ruleset_downloads(source, base_url=base_url)
    pinned = [{'path': path, 'content': value, 'role': role} for path, (value, role) in previous.items()
              if role != 'config' and (role != 'rule' or not settings['library']
                    or (service == 'oxidns' and not path.startswith('rules/rule_')))]
    candidate = build_deployment_bundle(agent, source, content, ruleset_downloads=downloads,
                pinned_files=pinned, base_url=base_url, deployment_id=deployment_id)
    candidate = retarget_config_path(candidate, base_bundle.manifest['config_path'])
    comparison = next(item for item in candidate.manifest['files'] if item['role'] == 'config')
    candidate = isolate_rule_bundle(base_bundle, candidate, service)
    manifest = {**candidate.manifest, 'reload_only': True}
    return _pack(manifest, store.bundle_files(candidate), candidate.config_version, comparison_config=comparison)


class RuleSyncWorker:
    def __init__(self, manager, *, clock=time.time, prepare=prepare_candidate):
        self.manager = manager
        self.clock = clock
        self.prepare = prepare

    def _state(self, agent_id, status, message, **fields):
        state = {'status': status, 'message': message, **fields}
        agent = self.manager.get_agent_by_id(agent_id) or {}
        previous = {key: value for key, value in ((agent.get('rule_sync') or {}).get('state') or {}).items()
                    if key != 'updated_at'}
        if previous != state:
            _update(self.manager, agent_id, state=state)

    def run(self, agent_id):
        # Multiple web workers can start the loop; one device operation at a
        # time, across processes. Never hold the repository lock over I/O.
        root = store.root_for(self.manager.repository, agent_id)
        with (root / 'worker.lock').open('a+b') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            try:
                self._run(agent_id)
            except Exception as error:
                logger.warning('Agent rule sync failed (%s)', type(error).__name__)
                agent = self.manager.get_agent_by_id(agent_id) or {}
                state = (agent.get('rule_sync') or {}).get('state') or {}
                from .oxidns_rule_update import MESSAGES as ox_messages
                from .mihomo_panel import MESSAGES as mh_messages
                code = getattr(error, 'code', '')
                message = (ox_messages if agent.get('service_type') == 'oxidns' else mh_messages).get(code)
                blocked = code in ('RULE_UPDATE_NOT_CONFIGURED', 'RULE_UPDATE_AMBIGUOUS',
                    'API_NOT_CONFIGURED', 'CONFIG_UNSUPPORTED', 'TLS_UNSUPPORTED', 'CONTROLLER_UNAVAILABLE',
                    'UPGRADE_REQUIRED', 'CORE_UPDATE_UNSUPPORTED')
                self._state(agent_id, 'blocked' if blocked else 'failed',
                            message or '规则自动同步失败；将稍后重试，可查看设备发布状态',
                            retry_at=self.clock() + RETRY_SECONDS, force_refresh=True,
                            **{key: state[key] for key in ('native_task', 'key') if key in state})

    def _run(self, agent_id):
        from .rule_sync_projection import project_rule_changes, rule_change_kind
        agent = next((item for item in self.manager.get_all_agents() if item['id'] == agent_id), None)
        if agent is None or agent.get('service_type') not in ('mihomo', 'oxidns'):
            return
        settings = settings_for(agent)
        if not any(settings.values()):
            return
        old_state = (agent.get('rule_sync') or {}).get('state') or {}
        if old_state.get('retry_at', 0) > self.clock():
            return
        if not agent.get('enabled', True) or agent.get('status') != 'online':
            self._state(agent_id, 'pending', '等待设备上线后同步规则',
                        **{key: old_state[key] for key in ('native_task', 'key', 'force_refresh') if key in old_state},
                        **({'force_refresh': True} if old_state.get('status') == 'running' and 'force_refresh' not in old_state else {}))
            return
        try:
            supported = compare_versions(agent.get('version', '0'), MIN_AGENT_VERSION) >= 0
        except (TypeError, ValueError):
            supported = False
        if not supported:
            self._state(agent_id, 'blocked', f'请先将 Agent 升级到 {MIN_AGENT_VERSION} 或更新版本')
            return
        task = agent.get('latest_deployment') or {}
        if task and task.get('status') not in TERMINAL:
            task = self.manager.get_deployment(agent_id, task['deployment_id'])
            self._state(agent_id, 'running' if old_state.get('deployment_id') == task.get('deployment_id') else 'pending',
                        '等待配置发布完成，再检查规则变化', deployment_id=old_state.get('deployment_id'))
            return
        if old_state.get('deployment_id') == task.get('deployment_id') and task:
            if task.get('status') != 'succeeded':
                self._state(agent_id, 'failed', '规则热加载未完成，请查看设备发布状态；将稍后重试',
                            retry_at=self.clock() + RETRY_SECONDS)
                return
            self._state(agent_id, 'succeeded', '规则已通过原生接口热加载')
            agent = self.manager.get_agent_by_id(agent_id)
        baseline = store.load(self.manager.repository, agent, with_bundle=False)
        if baseline is None:
            self._state(agent_id, 'blocked', '先成功发布一次配置，建立自动同步基线')
            return
        current, _ = self.manager.repository.get_publish_snapshot(agent['profile_id'])
        try:
            kind = rule_change_kind(baseline['source'], current, agent['service_type'], settings)
            source = project_rule_changes(baseline['source'], current, settings, service_type=agent['service_type'])
        except ValueError:
            self._state(agent_id, 'blocked', '当前原生规则结构无法安全自动合并，请手动检查并发布配置')
            return
        if kind == 'none' and not (old_state.get('native_task') or settings['library'] and
                                  (old_state.get('force_refresh') or old_state.get('status') == 'running')):
            if old_state.get('status') != 'succeeded':
                self._state(agent_id, 'idle', '已引用的规则没有待同步变化')
            return
        if kind == 'none':
            kind = 'content'  # reconcile a reverted edit after an interrupted download
        key = hashlib.sha256(json.dumps([source, settings, (agent.get('applied_content') or {}).get('deployment_id')],
                    sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if old_state.get('native_task') and agent['service_type'] == 'oxidns':
            from .oxidns_rule_update import call
            result, _ = call(self.manager, agent_id, 'GET')
            native = result['task']
            if native['id'] == old_state['native_task'] and native['status'] == 'running':
                return
            baseline = store.load(self.manager.repository, agent)
            if baseline is None:
                raise ValueError('Rule sync receipt is no longer available')
            if not settings['library']:
                # Disabling a scope does not cancel an already accepted core
                # task, but it must prevent every subsequent download request.
                snapshot = fetch_snapshot(self.manager, agent, baseline)
                if _unchanged(self.manager, agent, settings):
                    store.advance_content(self.manager.repository, agent, baseline['source'], snapshot)
                    self._state(agent_id, 'idle', '此前受理的规则更新已结束，继续按当前开关检查')
                return
            if (native['id'] == old_state['native_task'] and native['status'] == 'succeeded'
                    and key == old_state.get('key') and _unchanged(self.manager, agent, settings)):
                snapshot = fetch_snapshot(self.manager, agent, baseline)
                if not _unchanged(self.manager, agent, settings):
                    return
                if not store.advance_content(self.manager.repository, agent, source, snapshot):
                    self._state(agent_id, 'pending', '设备配置已改变，重新检查规则变化')
                    return
                self._state(agent_id, 'succeeded', '已引用的规则集已下载并热重载')
                return
            if native['id'] == old_state['native_task'] and native['status'] == 'failed':
                self._state(agent_id, 'failed', native.get('message') or '原生规则更新失败，将稍后重试',
                            force_refresh=True, retry_at=self.clock() + RETRY_SECONDS)
                return
            self._state(agent_id, 'pending', '继续检查最新规则内容', key=key,
                        noticed_at=self.clock(), force_refresh=True)
            return
        if old_state.get('key') != key:
            self._state(agent_id, 'pending', '检测到规则变化，等待连续编辑完成', key=key, noticed_at=self.clock(),
                        force_refresh=bool(old_state.get('force_refresh') or old_state.get('status') == 'running'))
            return
        if self.clock() - old_state.get('noticed_at', 0) < POLL_SECONDS:
            return
        baseline = store.load(self.manager.repository, agent)
        if baseline is None:
            raise ValueError('Rule sync receipt is no longer available')
        if kind == 'content':
            self._refresh(agent, settings, baseline, source, key)
        else:
            self._publish(agent, settings, baseline, source, key)

    def _refresh(self, agent, settings, baseline, source, key):
        agent_id = agent['id']
        if not _unchanged(self.manager, agent, settings):
            return
        self._state(agent_id, 'running', '正在下载并热重载已引用的规则集', key=key)
        if agent['service_type'] == 'mihomo':
            from .mihomo_panel import call
            main = yaml.safe_load(store.bundle_files(baseline['bundle'])[baseline['bundle'].manifest['config_path']][0])
            providers = main.get('rule-providers') or {}
            if not providers or any(item.get('type') != 'http' for item in providers.values()):
                return self._publish(agent, settings, baseline, source, key)
            for name in providers:
                if not _unchanged(self.manager, agent, settings):
                    return
                call(self.manager, agent_id, 'providers/rule', 'PUT', {'name': name})
        else:
            from .oxidns_rule_update import call
            # An earlier response may have been lost, or a user may have
            # started a manual download. Let it finish before issuing our job.
            existing, _ = call(self.manager, agent_id, 'GET')
            if existing['task']['status'] == 'running':
                self._state(agent_id, 'pending', '等待已有规则更新完成后同步最新内容',
                            key=key, noticed_at=self.clock() - POLL_SECONDS)
                return
            result, _ = call(self.manager, agent_id, 'POST')
            task = result['task']
            task_id = task['id']
            if task['status'] == 'running':
                self._state(agent_id, 'running', '正在下载并热重载已引用的规则集', key=key,
                            native_task=task_id, noticed_at=self.clock() - POLL_SECONDS)
                return
            if task['status'] != 'succeeded':
                self._state(agent_id, 'failed', task.get('message') or '原生规则更新未确认成功，将稍后重试',
                            retry_at=self.clock() + RETRY_SECONDS)
                return
        if not _unchanged(self.manager, agent, settings):
            return
        # A source edited during the download must be checked again, not
        # silently acknowledged as part of the previous API operation.
        from .rule_sync_projection import project_rule_changes
        current, _ = self.manager.repository.get_publish_snapshot(agent['profile_id'])
        if project_rule_changes(baseline['source'], current, settings, service_type=agent['service_type']) != source:
            self._state(agent_id, 'pending', '更新期间规则又有变化，继续同步最新内容')
            return
        snapshot = fetch_snapshot(self.manager, agent, baseline)
        if not _unchanged(self.manager, agent, settings):
            return
        if not store.advance_content(self.manager.repository, agent, source, snapshot):
            self._state(agent_id, 'pending', '设备配置已改变，重新检查规则变化')
            return
        self._state(agent_id, 'succeeded', '已引用的规则集已下载并热重载')

    def _publish(self, agent, settings, baseline, source, key):
        agent_id, deployment_id = agent['id'], uuid.uuid4().hex
        reservation = self.manager.begin_deployment(agent_id, deployment_id)
        if not reservation.get('success'):
            self._state(agent_id, 'pending', '设备正在执行其他操作，稍后同步规则')
            return
        try:
            self._state(agent_id, 'running', '正在校验规则变化并准备热加载', deployment_id=deployment_id, key=key)
            baseline = {**baseline, 'bundle': fetch_snapshot(self.manager, agent, baseline)}
            bundle = self.prepare(agent, source, baseline, settings, deployment_id)
            if not _unchanged(self.manager, agent, settings, deployment_id=deployment_id):
                raise ValueError('Rule sync target or settings changed during preparation')
            result = self.manager.publish_deployment(agent_id, bundle, activate=True,
                base_url=baseline.get('base_url', ''), resource_config=source, source_snapshot=source)
            if result.get('status') == 'succeeded':
                self._state(agent_id, 'succeeded', '规则已通过原生接口热加载')
            elif result.get('status') in ('failed', 'rolled_back', 'rollback_failed'):
                self._state(agent_id, 'failed', '规则热加载未完成，请查看设备发布状态；将稍后重试',
                            retry_at=self.clock() + RETRY_SECONDS)
        except Exception:
            self.manager.record_deployment(agent_id, deployment_id, {
                'success': False, 'status': 'failed', 'message': '规则候选配置无法安全生成或设备设置已改变；未自动发布'})
            raise
        finally:
            self.manager.finish_deployment_preparation(agent_id, deployment_id)


_started_pid = None
_start_lock = threading.Lock()


def start_loop():
    global _started_pid
    with _start_lock:
        if _started_pid == os.getpid():
            return
        _started_pid = os.getpid()

    def loop():
        from backend.common.agent_manager import get_agent_manager
        from backend.common.config import reset_config_context
        pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='agent-rule-sync')
        pending = {}
        cursor = 0
        while True:
            try:
                manager = get_agent_manager()
                pending = {key: value for key, value in pending.items() if not value.done()}
                agents = manager.get_all_agents()
                if agents:
                    cursor %= len(agents)
                    agents = agents[cursor:] + agents[:cursor]
                for index, agent in enumerate(agents):
                    if len(pending) >= 2:
                        break
                    if (agent['id'] not in pending and agent.get('service_type') in ('mihomo', 'oxidns')
                            and any(settings_for(agent).values())):
                        pending[agent['id']] = pool.submit(RuleSyncWorker(manager).run, agent['id'])
                    cursor += 1
            except Exception as error:
                logger.warning('Agent rule sync scan failed (%s)', type(error).__name__)
            finally:
                reset_config_context()
            time.sleep(POLL_SECONDS)

    threading.Thread(target=loop, daemon=True, name='agent-rule-sync-loop').start()
