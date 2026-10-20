"""Complete WebDAV backups, with a coalesced background trigger for saved changes."""
import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime


logger = logging.getLogger(__name__)
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


def upload_backup(snapshot, settings):
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
        client.upload_sync(remote_path=directory + filename, local_path=temporary)
        return filename
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.remove(temporary)


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
                    upload_backup(snapshot, settings)
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
