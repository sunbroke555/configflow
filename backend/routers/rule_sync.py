"""Opt-in remote-rule refresh. Only IP deltas from a manual receipt may publish."""
import copy
from datetime import datetime, timezone
from collections import OrderedDict
import threading
import time

from backend.routers.base import RouterError, RouterRollbackError
from backend.routers.store import find_router, routers_of, static_rule_sets
from backend.routers.rule_sync_rules import (
    MAX_BYTES, check_updates, digest, load_sources, ip_loader, selected_sources,
)
from backend.routers import rule_sync_store as store
from backend.routers.routeros_nat_list import nat_resources
from backend.routers.rule_sync_write import apply_delta, static_resources

_INTERVALS = (15, 30, 60, 180, 360, 720, 1440)
_started = False
_start_guard = threading.Lock()
_worker = threading.BoundedSemaphore(1)


class DownloadError(RouterError):
    """A transient fetch error can retry at the next configured interval."""


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def config_for(profile_id):
    from backend.common.config import get_repository
    return get_repository().get_compat_config(profile_id)


def controls(config, router):
    from backend.routes.routers import _agent
    from backend.routers.desired import fake_ip_network
    sources = selected_sources(config, router)
    fields = ['id', 'type', 'endpoint', 'username', 'password', 'verify_tls', 'proxy_gateway_agent_id',
              'dns_agent_id', 'manage_return_routing', 'ip_rule_sets', 'ros_policy_rule_sets']
    if (router.get('dns_guard') or {}).get('enabled') is True:
        # Added only while enabled, so existing baselines keep their digest. The
        # guard's manifest changes only through a manual release, not settings.
        fields.append('dns_guard')
    dns_agent = _agent(router.get('dns_agent_id'))
    value = {
        'router': {key: router.get(key) for key in fields},
        'agents': [{key: (agent or {}).get(key) for key in ('id', 'host', 'service_type', 'profile_id')}
                   for agent in (_agent(router.get('proxy_gateway_agent_id')), dns_agent)],
        'sources': {rule_id: {'item': {key: item.get(key) for key in
                    ('id', 'library_rule_id', 'policy', 'enabled', 'behavior')},
                    'library': {key: source.get(key) for key in
                    ('id', 'name', 'source_type', 'url', 'behavior', 'enabled')}}
                    for rule_id, (item, source) in sources.items()},
        'fake_ip': str(fake_ip_network(config)), 'oxidns': config.get('oxidns'),
        'proxy_groups': config.get('proxy_groups'),
    }
    from backend.routers import ipv6_fakeip
    if router.get('type') == 'routeros' and ipv6_fakeip.relevant(config, router):
        value['ipv6_fakeip'] = {'settings': ipv6_fakeip.settings(config), 'state': router.get('ipv6_fakeip_state')}
    ha = (dns_agent or {}).get('oxidns_ha') or {}
    if ha.get('enabled') is True:
        # Added only while DNS HA is enabled, like dns_guard above: the DHCP DNS
        # and hijack target become the pair's virtual IP.
        peer = _agent(ha.get('peer_id')) or {}
        value['dns_ha'] = {'group_id': ha.get('group_id'), 'vip': ha.get('vip'),
                           'peer': {'id': peer.get('id'), 'host': peer.get('host')}}
    return digest(value)


def _targets(config, router, records, gateway):
    from backend.routers.desired import compute_desired
    return compute_desired(config, gateway, static_rule_sets(router), content_loader=ip_loader(records))


def capture_manual(profile_id, config, router, driver, desired):
    """Called only after a successful explicit release, including verified no-ops.

    This receipt is not an extra backup: it stores ownership and source versions,
    never credentials or full downloaded content. Failure to enroll does not undo
    an otherwise successful user release.
    """
    if router['type'] != 'routeros':
        return
    timestamp = now()
    from backend.routers import ros_address_lists
    try:
        with ros_address_lists.reuse(driver):
            _capture(profile_id, config, router, driver, desired, timestamp)
    except (RouterError, OSError, ValueError, RuntimeError) as error:
        # Do not reuse an older receipt after an explicit publish changed objects.
        store.write(profile_id, router['id'], {'status': 'waiting',
                    'message': '本次发布未建立自动同步基准：' + (str(error) if isinstance(error, RouterError) else '同步记录无法保存，请重新预览发布'),
                    'checked_at': timestamp})


def _capture(profile_id, config, router, driver, desired, timestamp):
    contents, records = load_sources(config, router)
    current = config_for(profile_id)
    live_router = find_router(current, router['id'])
    signature = controls(config, router)
    if live_router is None or controls(current, live_router) != signature:
        raise RouterError('发布期间设置有变化，请重新手动预览发布后建立基准')
    pinned = _targets(config, router, records, desired['proxy_gateway'])
    if pinned['proxy_cidrs'] != desired['proxy_cidrs']:
        raise RouterError('发布期间规则缓存已变化，请重新手动预览发布后建立基准')
    policy = getattr(driver, 'ros_policy_manifest', None)
    if router.get('ros_policy_rule_sets'):
        from backend.routers.desired import compute_desired
        from backend.routers.ros_policy_write import verify
        expected = compute_desired(config, desired['proxy_gateway'], router['ros_policy_rule_sets'], ip_loader(records))
        destinations = [item['cidr'] for item in expected['proxy_cidrs'] if item['sources'] != ['fake-ip']]
        if not policy or set(destinations) != set(policy['spec']['destinations']):
            raise RouterError('已发布策略与规则内容不一致，无法建立自动同步基准')
        verify(driver, {'spec': policy['spec'], 'previous': policy})
    baseline = {'control': signature, 'rules': records, 'desired': pinned,
                'static_resources': static_resources(driver, pinned), 'policy': copy.deepcopy(policy),
                'nat_list': nat_resources(driver)}
    store.write(profile_id, router['id'], {'status': 'synced', 'message': '已建立成功发布的同步基准',
                'synced_at': timestamp, 'updated_at': timestamp, 'baseline': baseline, 'rules': records})


class Downloads:
    """One bounded cache per round, shared by all profiles/routers in that round."""
    def __init__(self):
        self.items = OrderedDict()
        self.size = 0

    def __call__(self, source, config):
        from backend.utils.rule_fetch import request_rule
        from backend.utils.url_utils import safe_exception_details
        key = digest([source.get('url'), config.get('system_config')])
        if key in self.items:
            from backend.utils.ruleset_content import validate_remote_rule_content, RemoteRuleValidationError
            try:
                validate_remote_rule_content(self.items[key], source, config_data=config)
            except RemoteRuleValidationError as error:
                raise DownloadError(str(error)) from None
            return self.items[key]
        try:
            with request_rule(source.get('url') or '', config_data=config, stream=True,
                              timeout=(5, 10)) as response:
                response.raise_for_status()
                chunks, size, deadline = [], 0, time.monotonic() + 30
                for chunk in response.iter_content(64 * 1024):
                    size += len(chunk)
                    if size > MAX_BYTES or time.monotonic() > deadline:
                        raise RouterError('远程规则下载超过大小或时间限制')
                    chunks.append(chunk)
                content = b''.join(chunks).decode('utf-8-sig')
        except RouterError:
            raise
        except Exception as error:
            # Third-party responses and URL credentials are never shown in errors.
            raise DownloadError('远程规则下载失败：' + safe_exception_details(error)) from None
        from backend.utils.ruleset_content import validate_remote_rule_content, RemoteRuleValidationError
        try:
            validate_remote_rule_content(content, source, config_data=config)
        except RemoteRuleValidationError as error:
            raise DownloadError(str(error)) from None
        while self.items and self.size + size > 16 * 1024 * 1024:
            _, old = self.items.popitem(last=False)
            self.size -= len(old.encode())
        self.items[key] = content
        self.size += len(content.encode())
        return content


def _save_manifest(profile_id, router, driver):
    from backend.common.config import get_repository, reset_config_context
    manifest = getattr(driver, 'ros_policy_manifest', None)
    latest = find_router(config_for(profile_id), router['id'])
    if latest is not None and latest.get('ros_policy_state') == manifest:
        return
    get_repository().save_router_publication(profile_id, router, manifest=manifest)
    reset_config_context()


def _cache_sources(config, router, contents):
    from backend.utils.rule_utils import write_validated_rule_cache
    seen = set()
    for rule_id, (_, source) in selected_sources(config, router).items():
        if source['id'] in seen or source.get('source_type', 'url') != 'url':
            continue
        seen.add(source['id'])
        write_validated_rule_cache(source, contents[rule_id], config_data=config)


RESUME_HINT = '请先「预览发布」并确认，重新建立同步基准'


def paused_message(receipt):
    """Why a paused sync is waiting, and how to resume it."""
    reason = (receipt.get('message') or '').rstrip('。.；;，, ')
    if not reason:
        return f'同步已暂停。{RESUME_HINT}'
    # Most pause reasons already say to preview and publish.
    return f'同步已暂停：{reason}' + ('' if '预览发布' in reason else f'。{RESUME_HINT}')


def needs_baseline(profile_id, router):
    """An automatic sync waits for a manual release to record its baseline."""
    if router.get('type') != 'routeros' or not router.get('auto_rule_sync'):
        return False
    receipt = store.read(profile_id, router['id'])
    return not receipt.get('baseline') or receipt.get('status') in ('paused', 'failed', 'waiting')


def sync(profile_id, router_id, download=None, trigger='schedule'):
    from backend.routers.route_editor import router_operation_lock
    from backend.routes.routers import _driver, _inspect
    from backend.routers.plan import build_plan
    connection = find_router(config_for(profile_id), router_id)
    with store.job_lock(profile_id, router_id, connection) as acquired:
        if not acquired:
            raise RouterError('此路由器正在发布或同步，请稍后再试')
        config = config_for(profile_id)
        router = find_router(config, router_id)
        if router is None or router.get('type') != 'routeros' or not router.get('auto_rule_sync'):
            raise RouterError('请先开启此路由器的远程规则自动同步')
        receipt = store.read(profile_id, router_id)
        baseline = receipt.get('baseline')
        if not baseline:
            raise RouterError('请先手动预览并成功发布一次，建立同步基准')
        if receipt.get('status') == 'running':
            receipt.update(status='paused', message='上次同步未正常结束，请手动预览发布，核对设备现场')
            store.write(profile_id, router_id, receipt)
        if receipt.get('status') == 'paused':
            raise RouterError('上次同步待人工确认，请先手动预览发布以重新建立基准')
        timestamp = now()
        receipt.update(status='running', message='正在检查远程规则', checked_at=timestamp)
        store.write(profile_id, router_id, receipt)
        driver, writes_started = None, False
        try:
            if controls(config, router) != baseline['control']:
                raise RouterError('来源、规则选择或网关设置尚未发布，请手动预览发布')
            if router.get('ros_policy_state') != baseline.get('policy'):
                raise RouterError('策略发布记录已变化，请重新手动预览发布')
            contents, records = load_sources(config, router, download or Downloads())
            receipt['rules'] = records
            if any(record['version'] != baseline['rules'][key]['version'] for key, record in records.items()):
                receipt['updated_at'] = timestamp
            check_updates(baseline['rules'], records)
            with router_operation_lock(router):
                from backend.common.config import get_repository
                last_revision, last_checked = None, 0
                def ensure_current(force=False):
                    nonlocal last_revision, last_checked
                    revision = get_repository().config_revision(profile_id)
                    if not force and revision == last_revision and time.monotonic() - last_checked < 1:
                        return
                    latest = config_for(profile_id)
                    item = find_router(latest, router_id)
                    if (item is None or not item.get('auto_rule_sync')
                            or controls(latest, item) != baseline['control']
                            or item.get('ros_policy_state') != router.get('ros_policy_state')):
                        raise RouterError('同步期间配置已变化，停止应用，请重新手动预览发布')
                    # Local edits must not be included by a remote refresh.
                    current_sources = selected_sources(latest, item)
                    for key, (_, source) in current_sources.items():
                        if source.get('source_type') == 'content':
                            import hashlib
                            if hashlib.sha256(source.get('content', '').encode()).hexdigest() != records[key]['version']:
                                raise RouterError('本地规则内容在同步期间被修改，停止应用')
                    last_revision, last_checked = revision, time.monotonic()
                ensure_current()
                driver = _driver(router)
                _, desired, state, report = _inspect(config, router, driver, content_loader=ip_loader(records))
                plan = build_plan(router, desired, state, report, report['return_routing'], driver)
                if {item['cidr'] for item in plan['checks']['routes']} != {item['cidr'] for item in desired['proxy_cidrs']}:
                    raise RouterError('目的网段存在冲突或无法完整核对，请手动预览发布')
                if (state.ipv6_fakeip_plan or {}).get('blockers') or (state.ipv6_fakeip_plan or {}).get('ops'):
                    raise RouterError('IPv6 Fake-IP 配置有差异或未能确认，请先手动预览发布')
                # An unclear ND setup is skipped as in manual releases; only a
                # pending change needs the manual preview to apply it.
                if (state.ipv6_dns_plan or {}).get('ops') and not (state.ipv6_dns_plan or {}).get('blockers'):
                    raise RouterError('IPv6 DNS 下发有待关闭的差异，请先手动预览发布')
                writes_started = True
                outcome = apply_delta(driver, desired, plan, baseline, ensure_current)
                _save_manifest(profile_id, router, driver)
                # Successful readback is the new baseline, even if subsequent cache I/O fails.
                baseline = {**baseline, 'rules': records, 'desired': {key: desired[key] for key in
                            ('fake_ip_range', 'proxy_gateway', 'proxy_cidrs', 'warnings')},
                            'static_resources': static_resources(driver, desired),
                            'policy': copy.deepcopy(getattr(driver, 'ros_policy_manifest', None)),
                            'nat_list': nat_resources(driver)}
                receipt.update(baseline=baseline, rules=records, status='running',
                               message='ROS 已核对，正在更新规则缓存', synced_at=now(), **outcome)
                store.write(profile_id, router_id, receipt)
                _cache_sources(config, router, contents)
                receipt.update(status='synced', message='远程规则已核对，ROS 已同步')
                store.write(profile_id, router_id, receipt)
        except Exception as error:
            rollback_failed = isinstance(error, RouterRollbackError)
            if driver is not None and writes_started:
                try:
                    _save_manifest(profile_id, router, driver)
                    # Restore may assign new IDs; keep them so the next withdrawal
                    # remains ownership-safe, rather than losing track of objects.
                    if receipt.get('status') != 'synced':
                        baseline = {**baseline,
                                    'static_resources': static_resources(driver, baseline['desired']),
                                    'policy': copy.deepcopy(getattr(driver, 'ros_policy_manifest', None))}
                        receipt['baseline'] = baseline
                except Exception:
                    rollback_failed = True
            from backend.utils.url_utils import safe_exception_details
            message = str(error) if isinstance(error, RouterError) else safe_exception_details(error)
            receipt.update(status='paused' if rollback_failed or (isinstance(error, RouterError) and not isinstance(error, DownloadError)) else 'failed',
                           message=message, checked_at=timestamp)
            store.write(profile_id, router_id, receipt)
        finally:
            if driver is not None:
                driver.close()
        _log_outcome(router, receipt)
        from backend.utils import task_status
        started = task_status.parse_time(timestamp)
        task_status.record(f'router_rule_sync:{profile_id}:{router_id}', ok=receipt.get('status') == 'synced',
                           started_at=timestamp, error=receipt.get('message', ''), trigger=trigger,
                           duration_ms=(datetime.now(timezone.utc) - started).total_seconds() * 1000 if started else 0)
        return store.public(receipt, True)


def _log_outcome(router, receipt):
    from backend.utils.logger import get_logger, log_text
    logger = get_logger(__name__)
    label = log_text(router.get('name') or router.get('id'))
    if receipt.get('status') == 'synced':
        logger.info('主路由 %s 远程规则同步完成：新增 %s 条，删除 %s 条', label,
                    receipt.get('added', 0), receipt.get('removed', 0))
    elif receipt.get('status') == 'paused':
        logger.error('主路由 %s 远程规则同步已暂停，需要手动预览发布：%s', label, receipt.get('message', ''))
    else:
        logger.warning('主路由 %s 远程规则同步失败，将按间隔重试：%s', label, receipt.get('message', ''))


def run_round(download=None):
    from backend.common.config import get_repository, reset_config_context
    download = download or Downloads()
    for profile in get_repository().list_profiles():
        try:
            # Disabled routers do not hydrate shared rules or create worker jobs.
            config = get_repository().get_profile(profile['id'])
            for router in routers_of(config):
                if router.get('type') != 'routeros' or not router.get('auto_rule_sync'):
                    continue
                receipt = store.read(profile['id'], router['id'])
                if not receipt.get('baseline') or receipt.get('status') == 'paused':
                    continue
                checked = receipt.get('checked_at')
                try:
                    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(checked)).total_seconds() if checked else float('inf')
                except (ValueError, TypeError):
                    elapsed = float('inf')
                if receipt.get('status') not in ('running', 'queued') and elapsed < router.get('rule_sync_interval', 60) * 60:
                    continue
                try:
                    sync(profile['id'], router['id'], download)
                except RouterError as error:
                    from backend.utils.logger import get_logger, log_text
                    get_logger(__name__).info('主路由 %s 本轮远程规则同步已跳过：%s',
                                              log_text(router.get('name') or router['id']), error)
        finally:
            reset_config_context()


def start_job(profile_id, router_id):
    if not _worker.acquire(blocking=False):
        raise RouterError('已有规则同步任务正在运行，请稍后重试')
    try:
        router = find_router(config_for(profile_id), router_id)
        with store.job_lock(profile_id, router_id, router) as acquired:
            if not acquired:
                raise RouterError('此路由器正在发布或同步，请稍后重试')
            receipt = store.read(profile_id, router_id)
            if not router or not router.get('auto_rule_sync') or not receipt.get('baseline'):
                raise RouterError('请开启自动同步，并先手动预览发布一次')
            if receipt.get('status') == 'paused':
                raise RouterError(paused_message(receipt))
            if receipt.get('status') in ('running', 'queued'):
                raise RouterError('上次同步尚未完成，请稍后重试')
            receipt.update(status='queued', message='已加入同步任务', checked_at=now())
            store.write(profile_id, router_id, receipt)
    except Exception:
        _worker.release()
        raise
    def run():
        from backend.common.config import reset_config_context
        try:
            sync(profile_id, router_id, trigger='manual')
        except RouterError as error:
            # A disable/cancel between enqueue and execution must not leave a
            # permanent loading indicator. Other workers cannot overlap this job.
            receipt = store.read(profile_id, router_id)
            if receipt.get('status') == 'queued':
                receipt.update(status='failed', message=str(error), checked_at=now())
                store.write(profile_id, router_id, receipt)
            from backend.utils.logger import get_logger, log_text
            get_logger(__name__).warning('主路由 %s 手动触发的远程规则同步未执行：%s',
                                         log_text((router or {}).get('name') or router_id), error)
        finally:
            reset_config_context()
            _worker.release()
    try:
        threading.Thread(target=run, name='router-rule-sync-now', daemon=True).start()
    except Exception:
        _worker.release()
        raise


def start_loop():
    global _started
    with _start_guard:
        if _started:
            return
        _started = True
    def run():
        import fcntl
        from backend.utils.logger import get_logger
        logger = get_logger(__name__)
        store.directory().mkdir(parents=True, exist_ok=True)
        with open(store.directory() / '.scheduler.lock', 'a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            while True:
                time.sleep(30)
                if not _worker.acquire(blocking=False):
                    continue
                try:
                    run_round()
                except Exception:
                    logger.exception('Router rule synchronization round failed')
                finally:
                    _worker.release()
    threading.Thread(target=run, name='router-rule-sync', daemon=True).start()
