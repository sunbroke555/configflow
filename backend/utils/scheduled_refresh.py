"""Server-side scheduled refresh of subscriptions and remote rule sources.

Devices, OxiDNS and publications read the validated caches. This loop
downloads each source when its interval is due, keeps the last valid cache
when a download or validation fails, and records every run for
系统设置 → 定时任务. A source without a valid cache yet is downloaded on first
use instead of waiting for the next round.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
import threading
import time

from backend.utils import task_status
from backend.utils.logger import get_logger, log_text

logger = get_logger(__name__)

DEFAULT_RULE_INTERVAL = 86400
# A failed source retries sooner than its interval: 5, 10, 20 … minutes,
# never later than the interval itself.
RETRY_SECONDS = 300
TICK_SECONDS = 30
FIRST_DELAY_SECONDS = 30
WORKERS = 3
RULE_TIMEOUT = 30

_running = set()
_running_guard = threading.Lock()
_started = False
_start_guard = threading.Lock()


class TaskBusy(RuntimeError):
    pass


class StaleRuleCache(RuntimeError):
    """A valid cache served while the source's last refresh failed."""


def subscription_key(sub_id):
    return f'subscription:{sub_id}'


def rule_source_key(rule_id):
    return f'rule_source:{rule_id}'


def is_running(key):
    with _running_guard:
        return key in _running


def _claim(key):
    with _running_guard:
        if key in _running:
            return False
        _running.add(key)
        return True


def _release(key):
    with _running_guard:
        _running.discard(key)


def describe_failure(error):
    """A readable reason that never renders a URL, token or response body."""
    import requests
    from backend.utils.ruleset_content import RemoteRuleValidationError
    from backend.utils.sub_store_client import SubscriptionFetchError
    from backend.utils.url_utils import safe_exception_details
    if isinstance(error, SubscriptionFetchError):
        error = error.direct_error or error.sub_store_error or error
    if isinstance(error, (RemoteRuleValidationError, StaleRuleCache)):
        return str(error)
    status = getattr(getattr(error, 'response', None), 'status_code', None)
    if isinstance(status, int):
        return f'上游返回 HTTP {status}'
    if isinstance(error, (requests.exceptions.MissingSchema, requests.exceptions.InvalidSchema,
                          requests.exceptions.InvalidURL)):
        return '地址无效，请检查是否为完整的 http(s) 地址'
    if isinstance(error, requests.exceptions.SSLError):
        return 'TLS 证书校验失败'
    if isinstance(error, requests.Timeout):
        return '连接上游超时'
    if isinstance(error, requests.ConnectionError):
        return '无法连接上游'
    if type(error) is ValueError or type(error).__name__ == 'StrategyReferenceError':
        return str(error)
    return f'更新失败（{safe_exception_details(error).removeprefix("exception_type=")}）'


def rule_interval(rule):
    value = rule.get('update_interval', DEFAULT_RULE_INTERVAL)
    return value if type(value) is int else DEFAULT_RULE_INTERVAL


def subscription_interval(sub):
    from backend.utils.subscription_refresh import DEFAULT_SOURCE_INTERVAL
    value = sub.get('interval', DEFAULT_SOURCE_INTERVAL)
    return value if type(value) is int else DEFAULT_SOURCE_INTERVAL


def next_run_at(entry, interval, baseline=None, now=None):
    """When a source is due next; None when its automatic updates are off.

    A source that never ran and has no cache is due now.
    """
    if not interval:
        return None
    last = task_status.parse_time(entry.get('last_run_at')) or task_status.parse_time(baseline)
    if last is None:
        return now or datetime.now(timezone.utc)
    delay = interval
    if entry.get('last_status') == 'failed':
        failures = max(1, int(entry.get('consecutive_failures') or 1))
        delay = min(interval, RETRY_SECONDS * 2 ** min(failures - 1, 10))
    return last + timedelta(seconds=delay)


def subscription_cache_time(sub):
    """When the stored nodes of the current URL were fetched, if known."""
    from backend.utils.subscription_cache import load_subscription_cache
    cache = load_subscription_cache(sub['id']) or {}
    if cache.get('nodes') and (cache.get('metadata') or {}).get('url') == sub.get('url'):
        return cache.get('updated_at')
    return None


def rule_cache_time(rule):
    from backend.utils.rule_utils import rule_cache_read_path
    try:
        path = rule_cache_read_path(rule)
        if not isinstance(path, str) or not os.path.isfile(path):
            return None
        stamp = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc)
    except (OSError, ValueError):
        return None
    return stamp.isoformat(timespec='seconds').replace('+00:00', 'Z')


def is_library_source(rule):
    """Whether this declaration is a stored library source the task refreshes."""
    from backend.common.config import get_repository
    if rule.get('source_type', 'url') != 'url' or not rule.get('url') or rule.get('id') is None:
        return False
    library = get_repository().get_shared().get('rule_library', [])
    stored = next((item for item in library if item.get('id') == rule.get('id')), None)
    fields = ('url', 'behavior', 'format', 'source_type')
    return stored is not None and all(stored.get(key) == rule.get(key) for key in fields)


def refresh_subscription(sub, trigger='schedule'):
    """Download and store one subscription; returns (nodes, result, cache)."""
    from backend.utils import subscription_health
    from backend.utils.subscription_cache import load_subscription_cache
    from backend.utils.subscription_filter import filter_subscription_items
    from backend.utils.subscription_refresh import download_subscription_nodes
    key = subscription_key(sub['id'])
    started, clock = task_status.now_iso(), time.monotonic()
    try:
        nodes, result, cache = download_subscription_nodes(sub)
    except Exception as error:
        previous = load_subscription_cache(sub['id']) or {}
        retained = bool(previous.get('nodes'))
        subscription_health.record_fetch(sub['id'], ok=False, from_cache=retained,
                                         count=previous.get('count') or 0)
        reason = describe_failure(error)
        task_status.record(key, ok=False, started_at=started, trigger=trigger, error=reason,
                           duration_ms=(time.monotonic() - clock) * 1000, cache_retained=retained,
                           cache_updated_at=previous.get('updated_at') if retained else None)
        logger.warning('订阅「%s」更新失败%s：%s', log_text(sub.get('name') or sub['id']),
                       '，继续使用原有缓存' if retained else '', reason)
        raise
    subscription_health.record_fetch(sub['id'], ok=True, count=len(filter_subscription_items(sub, nodes)),
                                     traffic=getattr(result, 'traffic', None))
    task_status.record(key, ok=True, started_at=started, trigger=trigger,
                       duration_ms=(time.monotonic() - clock) * 1000, cache_updated_at=cache.get('updated_at'))
    return nodes, result, cache


def refresh_rule_source(rule, trigger='schedule', *, config_data=None, fetch=None,
                        timeout=RULE_TIMEOUT, allow_stale=False):
    """Download and validate one library source; returns (bytes, refresh_error)."""
    from backend.utils.rule_utils import read_validated_rule_cache, refresh_remote_rule
    key = rule_source_key(rule['id'])
    started, clock = task_status.now_iso(), time.monotonic()
    try:
        content, error = refresh_remote_rule(rule, config_data, fetch=fetch, timeout=timeout,
                                             allow_stale=allow_stale)
    except Exception as failure:
        retained = read_validated_rule_cache(rule, config_data) is not None
        task_status.record(key, ok=False, started_at=started, trigger=trigger, error=describe_failure(failure),
                           duration_ms=(time.monotonic() - clock) * 1000, cache_retained=retained,
                           cache_updated_at=rule_cache_time(rule) if retained else None)
        if trigger == 'schedule':
            logger.warning('规则集「%s」更新失败%s：%s', log_text(rule.get('name') or rule['id']),
                           '，继续使用上一次有效缓存' if retained else '', describe_failure(failure))
        raise
    duration = (time.monotonic() - clock) * 1000
    if error is not None:
        # allow_stale served the previous bytes; refresh_remote_rule logged it.
        task_status.record(key, ok=False, started_at=started, trigger=trigger, error=describe_failure(error),
                           duration_ms=duration, cache_retained=True, cache_updated_at=rule_cache_time(rule))
    else:
        task_status.record(key, ok=True, started_at=started, trigger=trigger, duration_ms=duration,
                           cache_updated_at=task_status.now_iso())
    return content, error


def read_rule_source(rule, config_data=None, *, fetch=None, timeout=10):
    """A consumer's view of a remote source: (bytes, refresh_error).

    Library sources come from the cache the scheduled task maintains; only a
    source without a valid cache yet is downloaded now. Declarations outside
    the library have no task and still refresh on each read.
    """
    from backend.utils.rule_utils import read_validated_rule_cache, refresh_remote_rule
    if not is_library_source(rule):
        return refresh_remote_rule(rule, config_data, fetch=fetch, timeout=timeout, allow_stale=True)
    cached = read_validated_rule_cache(rule, config_data)
    if cached is not None:
        entry = task_status.get(rule_source_key(rule['id']))
        stale = StaleRuleCache(entry.get('last_error') or '规则集更新失败') if entry.get('last_status') == 'failed' else None
        return cached, stale
    return refresh_rule_source(rule, 'pull', config_data=config_data, fetch=fetch, timeout=timeout,
                               allow_stale=True)


def _due(now=None):
    from backend.common.config import get_repository
    now = now or datetime.now(timezone.utc)
    shared = get_repository().get_shared()
    statuses = task_status.load()
    jobs = []
    for sub in shared.get('subscriptions', []):
        if not sub.get('enabled', True) or not sub.get('url'):
            continue
        key = subscription_key(sub['id'])
        due = next_run_at(statuses.get(key) or {}, subscription_interval(sub), subscription_cache_time(sub), now)
        if due is not None and due <= now:
            jobs.append((key, 'subscription', sub))
    for rule in shared.get('rule_library', []):
        if rule.get('source_type', 'url') != 'url' or not rule.get('url') or not rule.get('enabled', True):
            continue
        key = rule_source_key(rule['id'])
        due = next_run_at(statuses.get(key) or {}, rule_interval(rule), rule_cache_time(rule), now)
        if due is not None and due <= now:
            jobs.append((key, 'rule_source', rule))
    return jobs


def _run(kind, resource, trigger):
    from backend.common.config import reset_config_context
    try:
        if kind == 'subscription':
            refresh_subscription(resource, trigger)
        else:
            refresh_rule_source(resource, trigger)
    except Exception:
        pass  # recorded in task_status and logged by the refresh itself
    finally:
        reset_config_context()


def run_round(now=None):
    jobs = [(key, kind, resource) for key, kind, resource in _due(now) if _claim(key)]
    if not jobs:
        return 0

    def work(job):
        key, kind, resource = job
        try:
            _run(kind, resource, 'schedule')
        finally:
            _release(key)

    with ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix='scheduled-refresh') as pool:
        list(pool.map(work, jobs))
    return len(jobs)


def find_resource(kind, resource_id):
    from backend.common.config import get_repository
    shared = get_repository().get_shared()
    items = shared.get('subscriptions' if kind == 'subscription' else 'rule_library', [])
    resource = next((item for item in items if item.get('id') == resource_id), None)
    if resource is None:
        raise LookupError('任务对应的来源已不存在，请刷新后重试')
    if not resource.get('url') or resource.get('source_type', 'url') != 'url':
        raise ValueError('这个来源没有订阅地址，不需要更新')
    return resource


def run_now(kind, resource_id):
    """Start one refresh in the background; the result lands in task_status."""
    resource = find_resource(kind, resource_id)
    key = subscription_key(resource_id) if kind == 'subscription' else rule_source_key(resource_id)
    if not _claim(key):
        raise TaskBusy('这个任务正在运行，请稍后查看结果')

    def work():
        try:
            _run(kind, resource, 'manual')
        finally:
            _release(key)

    try:
        threading.Thread(target=work, name=f'task-{key[:40]}', daemon=True).start()
    except Exception:
        _release(key)
        raise
    return key


def start_loop():
    """One scheduler across processes, guarded by a file lock."""
    global _started
    with _start_guard:
        if _started:
            return
        _started = True

    def loop():
        import fcntl
        from backend.common.config import get_repository, reset_config_context
        lock_path = os.path.join(str(get_repository().data_dir), '.task-scheduler.lock')
        handle = open(lock_path, 'a')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return
        time.sleep(FIRST_DELAY_SECONDS)
        while True:
            try:
                run_round()
            except Exception:
                logger.exception('Scheduled refresh round failed')
            finally:
                reset_config_context()
            time.sleep(TICK_SECONDS)

    threading.Thread(target=loop, name='scheduled-refresh', daemon=True).start()
