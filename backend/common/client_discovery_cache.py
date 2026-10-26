"""Per-app router snapshots. Saved client names and policies are never cached."""
import copy
import hashlib
import json
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Optional

CACHE_TTL = 60
MAX_PROFILES = 128


@dataclass
class Snapshot:
    readings: list
    checked_at: str


@dataclass
class Entry:
    signature: str
    snapshot: Optional[Snapshot] = None
    attempted_at: float = 0
    error: str = ''
    pending: Optional[Future] = None


class DiscoveryCache:
    def __init__(self):
        self._lock = threading.Lock()
        self._entries = OrderedDict()

    def invalidate(self, profile_id: str):
        with self._lock:
            self._entries.pop(profile_id, None)

    def read(self, profile_id: str, routers: list, load, refresh: bool = False):
        # Include credentials and all router settings, but retain only their digest.
        signature = hashlib.sha256(json.dumps(routers, sort_keys=True).encode()).hexdigest()
        with self._lock:
            entry = self._entries.get(profile_id)
            if entry is None or entry.signature != signature:
                entry = Entry(signature)
                self._entries[profile_id] = entry
            self._entries.move_to_end(profile_id)
            while len(self._entries) > MAX_PROFILES:
                self._entries.popitem(last=False)
            if entry.snapshot is not None and not refresh:
                return self._result(entry, cached=True)
            owner = entry.pending is None
            if owner:
                entry.pending = Future()
            pending = entry.pending
        # A request performs the read; simultaneous opens/refreshes share it.
        if not owner:
            return copy.deepcopy(pending.result())
        try:
            snapshot = load()
            error = ''
            if not any(reading is not None for reading, _ in snapshot.readings):
                error = '；'.join(message for _, message in snapshot.readings if message) or '路由器读取失败'
            with self._lock:
                # An outage must not replace the last useful device list.
                if not error or entry.snapshot is None or not any(reading is not None for reading, _ in entry.snapshot.readings):
                    entry.snapshot = snapshot
                    error = ''
                entry.error = error
                entry.attempted_at = time.monotonic()
                result = self._result(entry, cached=bool(error))
                entry.pending = None
                pending.set_result(result)
            return copy.deepcopy(result)
        except Exception as failure:
            with self._lock:
                entry.pending = None
                pending.set_exception(failure)
            raise

    @staticmethod
    def _result(entry: Entry, cached: bool):
        return copy.deepcopy((entry.snapshot, {
            'cached': cached,
            'stale': time.monotonic() - entry.attempted_at >= CACHE_TTL,
            'refresh_error': entry.error,
        }))
