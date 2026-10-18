"""Agent artifact digests stay bounded, fresh and safe for concurrent callers."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
import time
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from backend.agents import upgrades


@pytest.fixture
def artifact_dir(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENTS_STATIC_DIR', str(tmp_path))
    with upgrades._ARTIFACT_CACHE_LOCK:
        upgrades._ARTIFACT_CACHE.clear()
    yield tmp_path
    with upgrades._ARTIFACT_CACHE_LOCK:
        upgrades._ARTIFACT_CACHE.clear()


def put(directory, content, arch=upgrades.ARCHES[0]):
    path = directory / ('configflow-agent-' + arch)
    path.write_bytes(content)
    return path


def info(content):
    return {'sha256': hashlib.sha256(content).hexdigest(), 'size': len(content)}


class ReadProxy:
    def __init__(self, handle, callback):
        self.handle, self.callback = handle, callback
        self.calls = 0

    def __enter__(self):
        self.handle.__enter__()
        return self

    def __exit__(self, *args):
        return self.handle.__exit__(*args)

    def fileno(self):
        return self.handle.fileno()

    def readinto(self, buffer):
        self.calls += 1
        count = self.handle.readinto(buffer)
        self.callback(self.calls, buffer, count)
        return count


def intercept_reads(monkeypatch, path, callback):
    original = Path.open
    opened = []

    def open_file(self, mode='r', *args, **kwargs):
        handle = original(self, mode, *args, **kwargs)
        if self == path and mode == 'rb':
            opened.append(self)
            return ReadProxy(handle, callback)
        return handle

    monkeypatch.setattr(Path, 'open', open_file)
    return opened


def test_streaming_matches_all_arches_and_preserves_order(artifact_dir, monkeypatch):
    contents = {arch: b'\x7fELF' + arch.encode() * 5000 for arch in upgrades.ARCHES}
    for arch, content in contents.items():
        put(artifact_dir, content, arch)
    monkeypatch.setattr(Path, 'read_bytes', lambda *_: pytest.fail('whole executable read'))
    monkeypatch.setattr(upgrades, '_ARTIFACT_READ_BYTES', 3)
    assert upgrades.artifacts() == {arch: info(content) for arch, content in contents.items()}
    assert list(upgrades.artifacts()) == list(upgrades.ARCHES)


@pytest.mark.parametrize('content,accepted', [
    (b'', False), (b'\x7fEL', False), (b'ELF!', False),
    (b'\x7fELF', True), (b'\x7fELF' + b'x' * 12, True),
    (b'\x7fELF' + b'x' * 13, False),
])
def test_elf_and_inclusive_size_limit(artifact_dir, monkeypatch, content, accepted):
    assert upgrades._MAX_ARTIFACT_BYTES == 128 * 1024 * 1024
    monkeypatch.setattr(upgrades, '_MAX_ARTIFACT_BYTES', 16)
    put(artifact_dir, content)
    expected = {upgrades.ARCHES[0]: info(content)} if accepted else {}
    assert upgrades.artifacts() == expected


def test_missing_and_nonregular_artifacts_are_skipped(artifact_dir):
    (artifact_dir / ('configflow-agent-' + upgrades.ARCHES[1])).mkdir()
    assert upgrades.artifacts() == {}


def test_cache_avoids_reread_and_returns_independent_dicts(artifact_dir, monkeypatch):
    content = b'\x7fELFcached'
    path = put(artifact_dir, content)
    opened = intercept_reads(monkeypatch, path, lambda *_: None)
    first = upgrades.artifacts()
    first[upgrades.ARCHES[0]]['sha256'] = 'corrupt'
    first['new'] = {}
    second = upgrades.artifacts()
    assert second == {upgrades.ARCHES[0]: info(content)}
    assert len(opened) == 1
    assert first is not second and first[upgrades.ARCHES[0]] is not second[upgrades.ARCHES[0]]


def test_in_place_edit_with_same_size_and_mtime_invalidates_cache(artifact_dir, monkeypatch):
    path = put(artifact_dir, b'\x7fELFold')
    original = path.stat()
    opened = intercept_reads(monkeypatch, path, lambda *_: None)
    assert upgrades.artifacts()[upgrades.ARCHES[0]] == info(b'\x7fELFold')
    path.write_bytes(b'\x7fELFnew')
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    # Older Linux filesystems can keep ctime unchanged within one clock tick.
    # This case exercises invalidation by ctime, so first establish that change.
    deadline = time.monotonic() + 2
    while path.stat().st_ctime_ns == original.st_ctime_ns and time.monotonic() < deadline:
        time.sleep(0.01)
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert path.stat().st_ctime_ns != original.st_ctime_ns
    assert path.stat().st_mtime_ns == original.st_mtime_ns
    assert upgrades.artifacts()[upgrades.ARCHES[0]] == info(b'\x7fELFnew')
    assert len(opened) == 2


def test_replacement_with_same_size_and_mtime_invalidates_cache(artifact_dir, monkeypatch):
    path = put(artifact_dir, b'\x7fELFold')
    original = path.stat()
    opened = intercept_reads(monkeypatch, path, lambda *_: None)
    upgrades.artifacts()
    replacement = artifact_dir / 'replacement'
    replacement.write_bytes(b'\x7fELFnew')
    os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
    replacement.replace(path)
    assert upgrades.artifacts()[upgrades.ARCHES[0]] == info(b'\x7fELFnew')
    assert len(opened) == 2


def test_deletion_discards_cached_summary_and_recreation_is_read(artifact_dir, monkeypatch):
    path = put(artifact_dir, b'\x7fELFold')
    opened = intercept_reads(monkeypatch, path, lambda *_: None)
    upgrades.artifacts()
    path.unlink()
    assert upgrades.artifacts() == {}
    assert not upgrades._ARTIFACT_CACHE
    path.write_bytes(b'\x7fELFnew')
    assert upgrades.artifacts()[upgrades.ARCHES[0]] == info(b'\x7fELFnew')
    assert len(opened) == 2


def test_directory_switch_does_not_reuse_same_name(artifact_dir, monkeypatch):
    put(artifact_dir, b'\x7fELFold')
    assert upgrades.artifacts()[upgrades.ARCHES[0]] == info(b'\x7fELFold')
    other = artifact_dir / 'other'
    other.mkdir()
    put(other, b'\x7fELFnew')
    monkeypatch.setenv('AGENTS_STATIC_DIR', str(other))
    assert upgrades.artifacts()[upgrades.ARCHES[0]] == info(b'\x7fELFnew')
    assert len(upgrades._ARTIFACT_CACHE) == 2
    monkeypatch.setenv('AGENTS_STATIC_DIR', str(artifact_dir))
    assert upgrades.artifacts()[upgrades.ARCHES[0]] == info(b'\x7fELFold')


def test_cache_is_bounded_across_directory_changes(artifact_dir, monkeypatch):
    monkeypatch.setattr(upgrades, '_ARTIFACT_CACHE_LIMIT', 2)
    for number in range(6):
        directory = artifact_dir / str(number)
        directory.mkdir()
        put(directory, b'\x7fELF' + bytes([number]))
        monkeypatch.setenv('AGENTS_STATIC_DIR', str(directory))
        assert upgrades.artifacts()
        assert len(upgrades._ARTIFACT_CACHE) <= 2


def test_replacement_while_reading_retries_before_publishing(artifact_dir, monkeypatch):
    path = put(artifact_dir, b'\x7fELFold-version')
    replacement = artifact_dir / 'replacement'
    new = b'\x7fELFnew-version'
    replacement.write_bytes(new)
    changed = []

    def replace_once(_calls, _buffer, count):
        if count and not changed:
            replacement.replace(path)
            changed.append(True)

    opened = intercept_reads(monkeypatch, path, replace_once)
    assert upgrades.artifacts()[upgrades.ARCHES[0]] == info(new)
    assert len(opened) == 2
    assert upgrades.artifacts()[upgrades.ARCHES[0]] == info(new)
    assert len(opened) == 2


def test_replacement_between_stat_and_open_retries(artifact_dir, monkeypatch):
    path = put(artifact_dir, b'\x7fELFold')
    replacement = artifact_dir / 'replacement'
    replacement.write_bytes(b'\x7fELFnew')
    original = Path.open
    opened = []

    def open_file(self, mode='r', *args, **kwargs):
        if self == path and mode == 'rb':
            if not opened:
                replacement.replace(path)
            opened.append(self)
        return original(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', open_file)
    assert upgrades.artifacts()[upgrades.ARCHES[0]] == info(b'\x7fELFnew')
    assert len(opened) == 2


def test_deletion_while_reading_is_not_published_or_cached(artifact_dir, monkeypatch):
    path = put(artifact_dir, b'\x7fELFremoved')
    removed = []

    def delete_once(_calls, _buffer, count):
        if count and not removed:
            path.unlink()
            removed.append(True)

    intercept_reads(monkeypatch, path, delete_once)
    assert upgrades.artifacts() == {}
    assert not upgrades._ARTIFACT_CACHE


def test_repeated_file_changes_fail_without_caching(artifact_dir, monkeypatch):
    path = put(artifact_dir, b'\x7fELFchanging')

    def alter_at_eof(_calls, _buffer, count):
        if not count:
            current = path.stat()
            os.utime(path, ns=(current.st_atime_ns, current.st_mtime_ns + 1))

    opened = intercept_reads(monkeypatch, path, alter_at_eof)
    with pytest.raises(OSError, match='changed while being read'):
        upgrades.artifacts()
    assert len(opened) == 2
    assert not upgrades._ARTIFACT_CACHE


@pytest.mark.parametrize('error', [PermissionError('unreadable'), OSError('read failure')])
def test_read_errors_propagate_and_are_not_cached(artifact_dir, monkeypatch, error):
    path = put(artifact_dir, b'\x7fELFbroken')

    def fail(*_):
        raise error

    intercept_reads(monkeypatch, path, fail)
    with pytest.raises(type(error), match=str(error)):
        upgrades.artifacts()
    assert not upgrades._ARTIFACT_CACHE


def test_invalid_oversize_file_does_not_hide_late_read_errors(artifact_dir, monkeypatch):
    path = put(artifact_dir, b'not-an-elf-file' * 10)
    monkeypatch.setattr(upgrades, '_MAX_ARTIFACT_BYTES', 8)
    monkeypatch.setattr(upgrades, '_ARTIFACT_READ_BYTES', 8)

    def fail_later(calls, _buffer, _count):
        if calls == 3:
            raise OSError('late read failure')

    intercept_reads(monkeypatch, path, fail_later)
    with pytest.raises(OSError, match='late read failure'):
        upgrades.artifacts()


def test_concurrent_requests_share_one_complete_digest(artifact_dir, monkeypatch):
    content = b'\x7fELFconcurrent'
    put(artifact_dir, content)
    original = upgrades._read_artifact
    entered, release = Event(), Event()
    reads = []

    def read(path, expected):
        reads.append(path)
        entered.set()
        assert release.wait(5)
        return original(path, expected)

    monkeypatch.setattr(upgrades, '_read_artifact', read)
    with ThreadPoolExecutor(max_workers=8) as pool:
        tasks = [pool.submit(upgrades.artifacts) for _ in range(8)]
        try:
            assert entered.wait(5)
        finally:
            release.set()
        results = [task.result(timeout=5) for task in tasks]
    assert len(reads) == 1
    assert all(result == {upgrades.ARCHES[0]: info(content)} for result in results)
    results[0][upgrades.ARCHES[0]]['size'] = -1
    assert results[1][upgrades.ARCHES[0]]['size'] == len(content)


def test_windows_streams_without_reusing_or_storing_digest_cache(artifact_dir, monkeypatch):
    path = put(artifact_dir, b'\x7fELFold')
    original = path.stat()
    opened = intercept_reads(monkeypatch, path, lambda *_: None)
    assert upgrades.artifacts()[upgrades.ARCHES[0]] == info(b'\x7fELFold')
    # Replace only this module's os facade: pathlib must keep the real host OS.
    monkeypatch.setattr(upgrades, 'os', SimpleNamespace(
        name='nt', path=os.path, fspath=os.fspath, fstat=os.fstat, getenv=os.getenv))
    assert upgrades.artifacts()[upgrades.ARCHES[0]] == info(b'\x7fELFold')
    assert len(opened) == 2
    assert not upgrades._ARTIFACT_CACHE
    path.write_bytes(b'\x7fELFnew')
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert upgrades.artifacts()[upgrades.ARCHES[0]] == info(b'\x7fELFnew')
    assert len(opened) == 3
    assert not upgrades._ARTIFACT_CACHE
