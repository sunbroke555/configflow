"""Short-lived installer snapshots shared by all workers and server restarts."""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time


TTL_SECONDS = 24 * 60 * 60
MAX_SNAPSHOTS = 128
MAX_SCRIPT_BYTES = 512 * 1024
_TOKEN = re.compile(r"[A-Za-z0-9_-]{22}")


class InstallCommandStoreFull(Exception):
    """Keep existing unexpired links valid instead of silently evicting them."""


def _database_path():
    from backend.common.config import DATA_DIR
    return Path(DATA_DIR) / 'install-commands' / 'snapshots.sqlite3'


@contextmanager
def _database():
    path = _database_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    # Create privately before sqlite opens it, even with a permissive umask.
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    path.chmod(0o600)
    connection = sqlite3.connect(path, timeout=5)
    try:
        connection.execute('CREATE TABLE IF NOT EXISTS snapshots '
                           '(token TEXT PRIMARY KEY, script TEXT NOT NULL, expires_at INTEGER NOT NULL)')
        connection.execute('BEGIN IMMEDIATE')
        connection.execute('DELETE FROM snapshots WHERE expires_at <= ?', (int(time.time()),))
        yield connection
        connection.commit()
    finally:
        connection.close()


def save_install_script(script, *, expires_at=None):
    """Persist generated text atomically; only a random capability is public."""
    if not isinstance(script, str) or len(script.encode('utf-8')) > MAX_SCRIPT_BYTES:
        raise ValueError('安装脚本过大，请减少自定义参数后重试。')
    token = secrets.token_urlsafe(16)
    now = int(time.time())
    if expires_at is None:
        expires_at = now + TTL_SECONDS
    elif type(expires_at) is not int or not now < expires_at <= now + TTL_SECONDS:
        raise ValueError('安装链接有效期必须在未来 24 小时内。')
    with _database() as connection:
        if connection.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0] >= MAX_SNAPSHOTS:
            raise InstallCommandStoreFull()
        connection.execute('INSERT INTO snapshots VALUES (?, ?, ?)', (token, script, expires_at))
    return token, expires_at


def load_install_script(token):
    if not _TOKEN.fullmatch(token) or not _database_path().is_file():
        return None
    with _database() as connection:
        row = connection.execute('SELECT script FROM snapshots WHERE token = ?', (token,)).fetchone()
    return row[0] if row else None
