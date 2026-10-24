"""SQLite storage engine for the configuration document.

The repository keeps one JSON-shaped document in memory. Here it is stored
as one row per (scope, owner, field): ``root`` keys besides the sections,
``system`` and ``shared`` fields, and each profile's fields. Order is data
(export_all must round-trip byte for byte), so section and field positions
and the top-level key order are stored too.

This module only depends on the standard library and never imports the
repository, which maps its errors to user-facing messages.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
import uuid
from collections import namedtuple
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

FILENAME = 'configflow.sqlite3'
STORAGE_VERSION = 1
APPLICATION_ID = 0x43464C57  # "CFLW"
SECTION_KEYS = ('system', 'shared')
PROFILES_KEY = 'profiles'
REQUIRED_META = ('storage_version', 'store_id', 'generation', 'root_order')
# SQLITE_CORRUPT and SQLITE_NOTADB (primary result codes).
_CORRUPT_CODES = {11, 26}

SCHEMA = (
    'CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID',
    """CREATE TABLE sections (
        scope TEXT NOT NULL CHECK (scope IN ('root', 'system', 'shared', 'profile')),
        owner TEXT NOT NULL,
        position INTEGER NOT NULL,
        PRIMARY KEY (scope, owner),
        CHECK ((scope = 'profile') = (owner <> ''))
    ) WITHOUT ROWID""",
    """CREATE TABLE fields (
        scope TEXT NOT NULL,
        owner TEXT NOT NULL,
        field TEXT NOT NULL,
        position INTEGER NOT NULL,
        value TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision >= 1),
        PRIMARY KEY (scope, owner, field),
        FOREIGN KEY (scope, owner) REFERENCES sections (scope, owner) ON DELETE CASCADE
    ) WITHOUT ROWID""",
)


class StoreError(Exception):
    """Base error of the storage engine."""


class StoreCorrupt(StoreError):
    """The database file is physically damaged or is not a configuration store."""


class StoreUnsupported(StoreError):
    """The database was written by a newer storage version."""


class StoreConflict(StoreError):
    """The database changed under a writer that holds the repository lock."""


class ConfigStorageError(OSError):
    """A runtime SQLite failure; keeps the 500 semantics of disk errors."""


# commit_id is random per committed generation: a restored older database
# that reaches the same generation number again never matches a cached head.
Head = namedtuple('Head', 'store_id generation commit_id')
Entry = namedtuple('Entry', 'head document size')
Changes = namedtuple('Changes', 'delete_sections upsert_sections delete_fields upsert_fields root_order')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def dumps(value: Any) -> str:
    # No sort_keys: key order is part of the document.
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _is_corrupt(error: BaseException) -> bool:
    code = getattr(error, 'sqlite_errorcode', None)
    if code is not None:
        return (code & 0xff) in _CORRUPT_CODES
    message = str(error).lower()
    return 'malformed' in message or 'not a database' in message


def _section_rows(section, scope, owner, fields):
    if not isinstance(section, dict):
        raise TypeError(f'配置节 {scope}/{owner} 必须是对象')
    for position, (field, value) in enumerate(section.items()):
        if not isinstance(field, str):
            raise TypeError(f'配置节 {scope}/{owner} 的字段名必须是字符串')
        fields[(scope, owner, field)] = (position, dumps(value))


def document_rows(document: Dict[str, Any]):
    """Split a document into (root_order, sections, fields) rows."""
    if not isinstance(document, dict):
        raise TypeError('配置文档必须是对象')
    root_order: List[str] = []
    sections: Dict[Tuple[str, str], int] = {('root', ''): 0}
    fields: Dict[Tuple[str, str, str], Tuple[int, str]] = {}
    root_position = 0
    for key, value in document.items():
        if not isinstance(key, str):
            raise TypeError('配置文档的顶层键必须是字符串')
        root_order.append(key)
        if key in SECTION_KEYS:
            sections[(key, '')] = 0
            _section_rows(value, key, '', fields)
        elif key == PROFILES_KEY:
            if not isinstance(value, dict):
                raise TypeError('profiles 必须是对象')
            for position, (owner, profile) in enumerate(value.items()):
                if not isinstance(owner, str) or not owner:
                    raise TypeError('配置空间 ID 必须是非空字符串')
                sections[('profile', owner)] = position
                _section_rows(profile, 'profile', owner, fields)
        else:
            fields[('root', '', key)] = (root_position, dumps(value))
            root_position += 1
    return root_order, sections, fields


def build_document(root_order, sections, fields_parsed) -> Dict[str, Any]:
    """Inverse of document_rows; fields_parsed maps keys to (position, value)."""
    grouped: Dict[Tuple[str, str], list] = {}
    for (scope, owner, field), (position, value) in fields_parsed.items():
        if (scope, owner) not in sections:
            raise StoreCorrupt(f'字段 {scope}/{owner}/{field} 没有所属配置节')
        grouped.setdefault((scope, owner), []).append((position, field, value))

    def section(key):
        return {field: value for _, field, value in sorted(grouped.get(key, ()), key=lambda item: item[0])}

    root = section(('root', ''))
    expected_root = [key for key in root_order if key not in SECTION_KEYS and key != PROFILES_KEY]
    if len(set(root_order)) != len(root_order) or set(expected_root) != set(root):
        raise StoreCorrupt('顶层键顺序与数据库内容不一致')
    for scope in SECTION_KEYS:
        if (scope in root_order) != ((scope, '') in sections):
            raise StoreCorrupt(f'配置节 {scope} 与顶层键顺序不一致')
    profiles = sorted((position, owner) for (scope, owner), position in sections.items() if scope == 'profile')
    if profiles and PROFILES_KEY not in root_order:
        raise StoreCorrupt('配置空间与顶层键顺序不一致')
    document: Dict[str, Any] = {}
    for key in root_order:
        if key in SECTION_KEYS:
            document[key] = section((key, ''))
        elif key == PROFILES_KEY:
            document[key] = {owner: section(('profile', owner)) for _, owner in profiles}
        else:
            document[key] = root[key]
    return document


def _lookup(document, scope, owner, field):
    """The value of one row in a loaded document, or a sentinel."""
    try:
        if scope == 'root':
            return document[field]
        if scope == 'profile':
            return document[PROFILES_KEY][owner][field]
        return document[scope][field]
    except (KeyError, TypeError):
        return _ABSENT


_ABSENT = object()


class ConfigStore:
    """One SQLite file; a new connection per call, never shared across threads."""

    def __init__(self, path: Path, timeout: Callable[[], float]):
        self.path = Path(path)
        self._timeout = timeout

    # -- connections -----------------------------------------------------

    def connect(self, path: Optional[Path] = None, *, readonly: bool = False) -> sqlite3.Connection:
        target = Path(path or self.path)
        # mode=rw never creates a missing database as an empty file.
        uri = target.absolute().as_uri() + ('?mode=ro' if readonly else '?mode=rw')
        connection = sqlite3.connect(uri, uri=True, timeout=self._timeout(), isolation_level=None)
        try:
            connection.execute('PRAGMA foreign_keys=ON')
            # EXTRA = FULL plus a directory fsync after the DELETE-mode journal
            # is unlinked, which is the commit point.
            connection.execute('PRAGMA synchronous=EXTRA')
        except BaseException:
            connection.close()
            raise
        return connection

    @staticmethod
    @contextmanager
    def _translate() -> Iterator[None]:
        try:
            yield
        except sqlite3.Error as error:
            if _is_corrupt(error):
                raise ConfigStorageError(f'配置数据库已损坏：{error}') from error
            raise ConfigStorageError(f'配置数据库访问失败：{error}') from error

    @staticmethod
    def _rollback(connection):
        if connection.in_transaction:
            try:
                connection.execute('ROLLBACK')
            except sqlite3.Error:
                pass

    @contextmanager
    def reading(self) -> Iterator[sqlite3.Connection]:
        """One deferred read transaction: everything read inside is consistent."""
        with self._translate():
            connection = self.connect()
            try:
                connection.execute('BEGIN')
                yield connection
                connection.execute('COMMIT')
            finally:
                self._rollback(connection)
                connection.close()

    # -- reads -------------------------------------------------------------

    @staticmethod
    def _meta_rows(connection, keys=None) -> Dict[str, Any]:
        if keys is None:
            rows = connection.execute('SELECT key, value FROM meta').fetchall()
        else:
            placeholders = ','.join('?' for _ in keys)
            rows = connection.execute(f'SELECT key, value FROM meta WHERE key IN ({placeholders})', tuple(keys)).fetchall()
        try:
            return {key: json.loads(value) for key, value in rows}
        except ValueError as error:
            raise StoreCorrupt(f'数据库元信息无法解析：{error}') from error

    def head(self, connection) -> Head:
        meta = self._meta_rows(connection, ('store_id', 'generation', 'commit_id'))
        store_id, generation, commit_id = meta.get('store_id'), meta.get('generation'), meta.get('commit_id')
        if not isinstance(store_id, str) or not isinstance(generation, int) or isinstance(generation, bool):
            raise StoreCorrupt('数据库元信息缺少 store_id/generation')
        if commit_id is not None and not isinstance(commit_id, str):
            raise StoreCorrupt('数据库元信息 commit_id 无效')
        return Head(store_id, generation, commit_id)

    def meta(self, key: str, connection=None):
        if connection is None:
            with self.reading() as connection:
                return self.meta(key, connection)
        return self._meta_rows(connection, (key,)).get(key)

    @staticmethod
    def _stored_rows(connection):
        sections = {(scope, owner): position for scope, owner, position in
                    connection.execute('SELECT scope, owner, position FROM sections')}
        fields = {(scope, owner, field): (position, value) for scope, owner, field, position, value in
                  connection.execute('SELECT scope, owner, field, position, value FROM fields')}
        return sections, fields

    def load(self, connection=None) -> Entry:
        """A fresh read of the whole document, never cached."""
        if connection is None:
            with self.reading() as connection:
                return self.load(connection)
        head = self.head(connection)
        root_order = self.meta('root_order', connection)
        if not isinstance(root_order, list) or any(not isinstance(key, str) for key in root_order):
            raise StoreCorrupt('数据库元信息 root_order 无效')
        sections, fields = self._stored_rows(connection)
        size = 0
        parsed = {}
        for key, (position, value) in fields.items():
            size += len(value)
            try:
                parsed[key] = (position, json.loads(value))
            except ValueError as error:
                raise StoreCorrupt(f'字段 {"/".join(key)} 不是有效 JSON：{error}') from error
        return Entry(head, build_document(root_order, sections, parsed), size)

    def fingerprint(self) -> tuple:
        """(store_id, generation, digest of every row); tests compare it for 'unchanged'."""
        digest = hashlib.sha256()
        with self.reading() as connection:
            head = self.head(connection)
            for table, order in (('meta', 'key'), ('sections', 'scope, owner'), ('fields', 'scope, owner, field')):
                digest.update(table.encode())
                for row in connection.execute(f'SELECT * FROM {table} ORDER BY {order}'):
                    digest.update(json.dumps(row, ensure_ascii=False).encode('utf-8'))
        return head.store_id, head.generation, digest.hexdigest()

    # -- writes ------------------------------------------------------------

    @staticmethod
    def _diff(stored_root_order, stored_sections, stored_fields, root_order, sections, fields) -> Changes:
        delete_sections = [key for key in stored_sections if key not in sections]
        upsert_sections = [(key, position) for key, position in sections.items()
                           if stored_sections.get(key) != position]
        delete_fields = [key for key in stored_fields
                         if key not in fields and (key[0], key[1]) in sections]
        upsert_fields = [(key, row) for key, row in fields.items() if stored_fields.get(key) != row]
        return Changes(delete_sections, upsert_sections, delete_fields, upsert_fields,
                       root_order if root_order != stored_root_order else None)

    def commit(self, base: Entry, document: Dict[str, Any], *, meta: Optional[Dict[str, Any]] = None) -> Optional[Entry]:
        """Write the rows that differ from the database in one transaction.

        Returns the committed entry, or None when neither the document nor
        meta changed (nothing is written and the generation stays).
        """
        # Serialize first: a non-JSON value must fail before anything is written.
        root_order, sections, fields = document_rows(document)
        meta = dict(meta or {})
        for value in meta.values():
            dumps(value)
        with self._translate():
            connection = self.connect()
            try:
                connection.execute('BEGIN IMMEDIATE')
                head = self.head(connection)
                if head != base.head:
                    raise StoreConflict('配置数据库在事务外被修改')
                stored_root_order = self.meta('root_order', connection)
                stored_sections, stored_fields = self._stored_rows(connection)
                changes = self._diff(stored_root_order, stored_sections, stored_fields, root_order, sections, fields)
                changed = any((changes.delete_sections, changes.upsert_sections, changes.delete_fields,
                               changes.upsert_fields, changes.root_order is not None))
                if not changed and not meta:
                    connection.execute('ROLLBACK')
                    return None
                generation = head.generation + (1 if changed else 0)
                commit_id = uuid.uuid4().hex if changed else head.commit_id
                if changed:
                    meta['commit_id'] = commit_id
                self._apply(connection, changes, generation, meta)
                self._commit(connection)
            except BaseException:
                self._rollback(connection)
                raise
            finally:
                connection.close()
        # The new cache entry: unchanged rows reuse the previous (immutable)
        # values, changed rows are parsed from the text already written.
        parsed = {}
        size = 0
        for key, (position, text) in fields.items():
            size += len(text)
            value = _ABSENT
            if stored_fields.get(key, (None, None))[1] == text:
                value = _lookup(base.document, *key)
            parsed[key] = (position, json.loads(text) if value is _ABSENT else value)
        return Entry(Head(head.store_id, generation, commit_id), build_document(root_order, sections, parsed), size)

    def _apply(self, connection, changes: Changes, generation: int, meta: Dict[str, Any]) -> None:
        """Every statement of one commit (seam for failure injection before COMMIT)."""
        connection.executemany('DELETE FROM sections WHERE scope = ? AND owner = ?', changes.delete_sections)
        connection.executemany(
            'INSERT INTO sections (scope, owner, position) VALUES (?, ?, ?) '
            'ON CONFLICT (scope, owner) DO UPDATE SET position = excluded.position',
            [(scope, owner, position) for (scope, owner), position in changes.upsert_sections])
        connection.executemany('DELETE FROM fields WHERE scope = ? AND owner = ? AND field = ?', changes.delete_fields)
        connection.executemany(
            'INSERT INTO fields (scope, owner, field, position, value, revision) VALUES (?, ?, ?, ?, ?, ?) '
            'ON CONFLICT (scope, owner, field) DO UPDATE SET position = excluded.position, '
            'value = excluded.value, revision = excluded.revision',
            [(scope, owner, field, position, value, generation)
             for (scope, owner, field), (position, value) in changes.upsert_fields])
        updates = dict(meta)
        if changes.root_order is not None:
            updates['root_order'] = changes.root_order
        updates['generation'] = generation
        connection.executemany('INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)',
                               [(key, dumps(value)) for key, value in updates.items()])

    def _commit(self, connection) -> None:
        """COMMIT itself (seam for 'committed or not' failure injection)."""
        connection.execute('COMMIT')

    # -- building and checking the file ---------------------------------------

    def _fsync_dir(self) -> None:
        if os.name == 'nt':
            return
        descriptor = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _fsync_file(path: Path) -> None:
        with open(path, 'rb') as handle:
            os.fsync(handle.fileno())

    def build_temporary(self, document: Dict[str, Any], meta: Dict[str, Any]) -> Tuple[Path, Entry]:
        """Write a complete, checked database next to the real one (0600)."""
        root_order, sections, fields = document_rows(document)
        descriptor, name = tempfile.mkstemp(prefix=f'.{self.path.name}.', suffix='.tmp', dir=self.path.parent)
        os.close(descriptor)
        temporary = Path(name)
        try:
            connection = self.connect(temporary)
            try:
                mode = connection.execute('PRAGMA journal_mode=DELETE').fetchone()[0]
                if str(mode).lower() != 'delete':
                    raise StoreError(f'无法设置数据库日志模式：{mode}')
                connection.execute(f'PRAGMA application_id={APPLICATION_ID}')
                connection.execute('BEGIN IMMEDIATE')
                for statement in SCHEMA:
                    connection.execute(statement)
                values = {'storage_version': STORAGE_VERSION, 'store_id': uuid.uuid4().hex, 'generation': 1,
                          'commit_id': uuid.uuid4().hex, 'root_order': root_order, 'created_at': _now(), **meta}
                connection.executemany('INSERT INTO meta (key, value) VALUES (?, ?)',
                                       [(key, dumps(value)) for key, value in values.items()])
                connection.executemany('INSERT INTO sections (scope, owner, position) VALUES (?, ?, ?)',
                                       [(scope, owner, position) for (scope, owner), position in sections.items()])
                connection.executemany(
                    'INSERT INTO fields (scope, owner, field, position, value, revision) VALUES (?, ?, ?, ?, ?, 1)',
                    [(scope, owner, field, position, value)
                     for (scope, owner, field), (position, value) in fields.items()])
                connection.execute('COMMIT')
                result = connection.execute('PRAGMA quick_check').fetchall()
                if result != [('ok',)]:
                    raise StoreError(f'新建数据库未通过完整性检查：{result}')
            finally:
                self._rollback(connection)
                connection.close()
            entry = ConfigStore(temporary, self._timeout).load()
            if dumps(entry.document) != dumps(document):
                raise StoreError('新建数据库的内容与迁移文档不一致')
            self._fsync_file(temporary)
            return temporary, entry
        except BaseException:
            self.discard_temporary(temporary)
            raise

    @staticmethod
    def discard_temporary(temporary: Path) -> None:
        temporary.unlink(missing_ok=True)
        Path(str(temporary) + '-journal').unlink(missing_ok=True)

    def install(self, temporary: Path) -> None:
        os.replace(temporary, self.path)
        self._fsync_dir()

    def create(self, document: Dict[str, Any], meta: Dict[str, Any]) -> Entry:
        """Build the database in a temporary file and atomically move it into place."""
        temporary, entry = self.build_temporary(document, meta)
        try:
            self.install(temporary)
        finally:
            if temporary.exists():
                self.discard_temporary(temporary)
        return entry

    def check(self) -> None:
        """quick_check, schema, meta and storage version of an existing file."""
        try:
            connection = self.connect()
            try:
                result = connection.execute('PRAGMA quick_check').fetchall()
                if result != [('ok',)]:
                    raise StoreCorrupt('完整性检查失败：' + '; '.join(str(row[0]) for row in result[:5]))
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                missing = {'meta', 'sections', 'fields'} - tables
                if missing:
                    raise StoreCorrupt('缺少数据表：' + '、'.join(sorted(missing)))
                meta = self._meta_rows(connection)
                missing = [key for key in REQUIRED_META if key not in meta]
                if missing:
                    raise StoreCorrupt('缺少数据库元信息：' + '、'.join(missing))
                version = meta['storage_version']
                if not isinstance(version, int) or isinstance(version, bool) or version < 1:
                    raise StoreCorrupt(f'数据库存储版本无效：{version!r}')
                if version > STORAGE_VERSION:
                    raise StoreUnsupported(f'数据库存储版本 {version} 高于当前支持的 {STORAGE_VERSION}')
                mode = str(connection.execute('PRAGMA journal_mode').fetchone()[0]).lower()
                if mode != 'delete':
                    mode = str(connection.execute('PRAGMA journal_mode=DELETE').fetchone()[0]).lower()
                    if mode != 'delete':
                        raise StoreError(f'无法把数据库日志模式恢复为 DELETE：{mode}')
            finally:
                connection.close()
        except sqlite3.Error as error:
            if _is_corrupt(error):
                raise StoreCorrupt(str(error)) from error
            raise

    def salvage_meta(self) -> Optional[Dict[str, Any]]:
        """Best-effort read-only meta of a damaged file; None on any error."""
        try:
            connection = self.connect(readonly=True)
            try:
                return self._meta_rows(connection)
            finally:
                connection.close()
        except Exception:
            return None

    def corrupt_target(self) -> Path:
        return self.path.with_name(f'{self.path.name}.corrupt-{time.time_ns()}')

    def preserve_corrupt(self, target: Optional[Path] = None) -> Path:
        """Keep the damaged file (and its hot journal) under another name.

        The file itself stays in place until install() atomically replaces it:
        a crash in between leaves the damaged database, never a missing one.
        """
        target = target or self.corrupt_target()
        try:
            os.link(self.path, target)
        except OSError:  # filesystems without hard links (some NAS)
            shutil.copy2(self.path, target)
            self._fsync_file(target)
        journal = Path(str(self.path) + '-journal')
        if journal.exists():
            # Must go before install(): it would be applied to the new file.
            os.replace(journal, Path(str(target) + '-journal'))
        self._fsync_dir()
        return target

    def backup_to(self, destination: Path) -> None:
        """Consistent online copy through the SQLite backup API (0600)."""
        destination = Path(destination)
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        with self._translate():
            source = self.connect()
            try:
                target = sqlite3.connect(destination.absolute().as_uri() + '?mode=rw', uri=True)
                try:
                    source.backup(target)
                finally:
                    target.close()
            finally:
                source.close()
        self._fsync_file(destination)

    def discard_stale_temporaries(self) -> None:
        """Remove builds interrupted by a crash; callers hold the repository lock."""
        for pattern in (f'.{self.path.name}.*.tmp', f'.{self.path.name}.*.tmp-journal'):
            for path in self.path.parent.glob(pattern):
                if path.is_file() and not path.is_symlink():
                    path.unlink(missing_ok=True)
