"""Complete WebDAV backups, with a coalesced background trigger for saved changes."""
import json
import logging
import os
import re
import tempfile
import threading
import time
from collections import namedtuple
from datetime import datetime, timedelta


logger = logging.getLogger(__name__)
# Settings saved before retention existed use these values.
DEFAULT_KEEP_LATEST = 30
DEFAULT_KEEP_DAILY = 30
RETENTION_LIMITS = {'keep_latest': 1000, 'keep_daily': 3650}
_BACKUP_NAME = re.compile(r'^config_backup_(\d{8})_(\d{6})(?:_(\d{6}))?\.json$')
BackupResult = namedtuple('BackupResult', 'filename removed')
_AGENT_RUNTIME_FIELDS = frozenset({
    'last_heartbeat', 'status', 'version', 'service_status', 'config_version',
    'system_metrics', 'cpu', 'memory', 'disk', 'network', 'updated_at',
    'deployments', 'latest_deployment', 'latest_upgrade', 'config_revision',
    'pushed_at', 'applied_content',
})


class WebDAVConfigurationError(ValueError):
    pass


def describe_webdav_error(error):
    """Keep status codes, including wrapped causes, without remote URLs or bodies."""
    detail = type(error).__name__
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        status = getattr(error, 'code', None)
        if isinstance(status, int) and 100 <= status <= 599:
            return f'{detail} HTTP {status}'
        error = error.__cause__
    return detail


BACKUP_TASK_KEY = 'webdav_backup'


def upload_backup(snapshot, settings, trigger='手动'):
    """Upload an export_all snapshot and record the run for 系统设置 → 定时任务."""
    import time
    from backend.utils import task_status
    started, clock = task_status.now_iso(), time.monotonic()
    kind = 'event' if trigger == '自动' else 'manual'
    try:
        result = _upload_backup(snapshot, settings, trigger)
    except Exception as error:
        task_status.record(BACKUP_TASK_KEY, ok=False, started_at=started, trigger=kind,
                           duration_ms=(time.monotonic() - clock) * 1000,
                           error=str(error) if isinstance(error, WebDAVConfigurationError) else describe_webdav_error(error))
        raise
    task_status.record(BACKUP_TASK_KEY, ok=True, started_at=started, trigger=kind,
                       duration_ms=(time.monotonic() - clock) * 1000)
    return result


def _upload_backup(snapshot, settings, trigger):
    """Upload an export_all snapshot; temporary files are removed on every outcome."""
    from webdav3.client import Client

    url = settings.get('webdav_url', '').rstrip('/')
    username = settings.get('webdav_username', '')
    password = settings.get('webdav_password', '')
    if not url or not username or not password:
        raise WebDAVConfigurationError('请填写完整的 WebDAV 配置')
    client = Client({'webdav_hostname': url, 'webdav_login': username,
                     'webdav_password': password, 'webdav_timeout': 30})
    directory = '/' + settings.get('webdav_path', '/config-flow-backup/').strip('/') + '/'
    if directory == '//':
        directory = '/'
    if not client.check(directory):
        current = '/'
        for part in directory.strip('/').split('/'):
            if not part:
                continue
            current += part + '/'
            if not client.check(current):
                try:
                    client.mkdir(current)
                except Exception as error:
                    raise WebDAVConfigurationError(
                        f'无法创建目录 {current}：{describe_webdav_error(error)}') from error

    filename = f'config_backup_{datetime.now().strftime("%Y%m%d_%H%M%S_%f")}.json'
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.json', delete=False) as handle:
            temporary = handle.name
            json.dump(snapshot, handle, ensure_ascii=False, indent=2)
        size = os.path.getsize(temporary)
        client.upload_sync(remote_path=directory + filename, local_path=temporary)
        logger.info('WebDAV %s备份完成：%s%s（%.1f KB）', trigger, directory, filename, size / 1024)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.remove(temporary)
    return BackupResult(filename, prune_backups(client, directory, settings))


def retention_settings(settings):
    """Saved values; missing or invalid ones (e.g. from an imported file) use the defaults."""
    values = []
    for key, default in (('keep_latest', DEFAULT_KEEP_LATEST), ('keep_daily', DEFAULT_KEEP_DAILY)):
        value = settings.get(key, default)
        values.append(value if type(value) is int and 0 <= value <= RETENTION_LIMITS[key] else default)
    return tuple(values)


def expired_backups(names, keep_latest, keep_daily, today):
    """Backups this program named that are neither among the newest keep_latest
    nor the newest of one of the last keep_daily days; keep_latest 0 keeps all."""
    if keep_latest <= 0:
        return []
    backups = sorted(((m.group(1), m.group(2), m.group(3) or '', name)
                      for name in names if (m := _BACKUP_NAME.match(name))), reverse=True)
    keep = {name for *_, name in backups[:keep_latest]}
    if keep_daily > 0:
        first_day = (today - timedelta(days=keep_daily - 1)).strftime('%Y%m%d')
        days = set()
        for day, _, _, name in backups:
            if day >= first_day and day not in days:
                days.add(day)
                keep.add(name)
    return [name for *_, name in backups if name not in keep]


def prune_backups(client, directory, settings):
    """Delete expired backups after an upload; failures are only logged."""
    keep_latest, keep_daily = retention_settings(settings)
    if keep_latest <= 0:
        return 0
    try:
        names = client.list(directory)
    except Exception as error:
        logger.warning('WebDAV 旧备份清理失败：无法列出备份目录（%s）', describe_webdav_error(error))
        return 0
    removed = 0
    for name in expired_backups(names, keep_latest, keep_daily, datetime.now().date()):
        try:
            client.clean(directory + name)
            removed += 1
        except Exception as error:
            logger.warning('WebDAV 旧备份 %s 删除失败（%s）', name, describe_webdav_error(error))
    if removed:
        logger.info('WebDAV 已清理 %d 个旧备份（保留最近 %d 份及最近 %d 天每天最后一份）',
                    removed, keep_latest, keep_daily)
    return removed


def _configuration_changed(previous, current):
    if previous['profiles'] != current['profiles'] or previous['shared'] != current['shared']:
        return True
    old, new = previous['system'], current['system']
    if any(old.get(key) != new.get(key) for key in ('system_config', 'backup')):
        return True

    def agent_settings(system):
        return {agent['id']: {key: value for key, value in agent.items() if key not in _AGENT_RUNTIME_FIELDS}
                for agent in system.get('agents', [])}

    return agent_settings(old) != agent_settings(new)


class _AutoBackupWorker:
    """One uploader and one pending reference, not one snapshot/thread per save."""
    def __init__(self, delay=1.0):
        self.delay = delay
        self._condition = threading.Condition()
        self._pending = None
        self._deadline = 0.0
        self._thread = None

    def schedule(self, repository):
        with self._condition:
            self._pending = repository
            self._deadline = time.monotonic() + self.delay
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name='webdav-auto-backup', daemon=True)
                try:
                    self._thread.start()
                except Exception:
                    self._thread = None
                    self._pending = None
                    raise
            self._condition.notify()

    def _run(self):
        while True:
            with self._condition:
                if self._pending is None:
                    self._thread = None
                    return
                remaining = self._deadline - time.monotonic()
                if remaining > 0:
                    self._condition.wait(remaining)
                    continue
                repository, self._pending = self._pending, None
            try:
                # Read after the debounce period, outside the committing thread.
                snapshot = repository.export_all()
                settings = snapshot['system'].get('backup', {})
                if settings.get('auto_backup'):
                    upload_backup(snapshot, settings, trigger='自动')
            except Exception as error:
                logger.warning('WebDAV 自动备份失败（%s）；已保存的配置不受影响',
                               describe_webdav_error(error))
            # No retry: a later configuration change may schedule another backup.


_worker = _AutoBackupWorker()


def configuration_committed(repository, previous, current):
    """Called only after a successful disk commit and after its locks are released."""
    try:
        if current['system'].get('backup', {}).get('auto_backup') and _configuration_changed(previous, current):
            _worker.schedule(repository)
    except Exception as error:
        logger.warning('无法安排 WebDAV 自动备份（%s）；已保存的配置不受影响', type(error).__name__)
