"""Queue pressure tests use blocked workers, not just synthetic log messages."""
import logging
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.http_queue import QueueWaitDispatcher
from backend.http_server import run_http_server


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate()


def task(service):
    return SimpleNamespace(service=service, cancel=Mock())


def warnings(caplog):
    return [record for record in caplog.records
            if record.name == 'waitress.queue' and record.levelno >= logging.WARNING]


def test_short_burst_keeps_fifo_without_warning(caplog):
    caplog.set_level(logging.DEBUG, logger='waitress.queue')
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    order = []
    dispatcher = QueueWaitDispatcher()
    dispatcher.set_thread_count(1)
    try:
        dispatcher.add_task(task(lambda: (entered.set(), release.wait(2))))
        assert entered.wait(1)
        dispatcher.add_task(task(lambda: order.append('first')))
        dispatcher.add_task(task(lambda: (order.append('second'), done.set())))
        release.set()
        assert done.wait(1)
        assert order == ['first', 'second']
        assert not warnings(caplog)
        assert any(record.levelno == logging.DEBUG and 'Task queue depth' in record.getMessage()
                   for record in caplog.records)
    finally:
        release.set()
        dispatcher.shutdown(timeout=2)
    assert not dispatcher._monitor.is_alive()


def test_stuck_worker_warns_without_new_requests_or_task_completion(caplog):
    caplog.set_level(logging.WARNING, logger='waitress.queue')
    entered, release, served = threading.Event(), threading.Event(), threading.Event()
    dispatcher = QueueWaitDispatcher()
    dispatcher.set_thread_count(1)
    try:
        dispatcher.add_task(task(lambda: (entered.set(), release.wait(5))))
        assert entered.wait(1)
        dispatcher.add_task(task(served.set))
        started = time.monotonic()
        wait_for(lambda: len(warnings(caplog)) == 1, timeout=2.5)
        assert time.monotonic() - started >= 0.9
        assert not served.is_set()
        assert '1 个待处理任务' in warnings(caplog)[0].getMessage()
        release.set()
        assert served.wait(1)
    finally:
        release.set()
        dispatcher.shutdown(timeout=2)


def test_continuous_queue_is_rate_limited_and_new_episode_can_warn(caplog):
    caplog.set_level(logging.WARNING, logger='waitress.queue')
    now = [0.0]
    entered, release, drained = threading.Event(), threading.Event(), threading.Event()
    dispatcher = QueueWaitDispatcher(clock=lambda: now[0])
    dispatcher.set_thread_count(1)
    try:
        dispatcher.add_task(task(lambda: (entered.set(), release.wait(5))))
        assert entered.wait(1)
        dispatcher.add_task(task(lambda: None))
        now[0] = 1.1
        dispatcher._wake.set()
        wait_for(lambda: len(warnings(caplog)) == 1)
        now[0] = 30.9
        dispatcher.add_task(task(drained.set))
        time.sleep(0.03)
        assert len(warnings(caplog)) == 1
        now[0] = 31.1
        dispatcher._wake.set()
        wait_for(lambda: len(warnings(caplog)) == 2)
        assert '2 个待处理任务' in warnings(caplog)[1].getMessage()
        release.set()
        assert drained.wait(1)

        # Recovery ends the previous episode; a later real wait need not wait
        # for the old episode's 30-second reporting interval.
        entered.clear()
        release.clear()
        dispatcher.add_task(task(lambda: (entered.set(), release.wait(5))))
        assert entered.wait(1)
        dispatcher.add_task(task(lambda: None))
        now[0] = 32.2
        dispatcher._wake.set()
        wait_for(lambda: len(warnings(caplog)) == 3)
    finally:
        release.set()
        dispatcher.shutdown(timeout=2)


def test_task_failures_are_still_logged_and_next_task_runs(caplog):
    caplog.set_level(logging.ERROR, logger='waitress')
    done = threading.Event()
    dispatcher = QueueWaitDispatcher()
    dispatcher.set_thread_count(1)

    def fail():
        raise RuntimeError('synthetic task failure')

    try:
        dispatcher.add_task(task(fail))
        dispatcher.add_task(task(done.set))
        assert done.wait(1)
        assert any(record.levelno == logging.ERROR and 'Exception when servicing' in record.getMessage()
                   for record in caplog.records)
    finally:
        dispatcher.shutdown(timeout=2)


def test_shutdown_cancels_pending_tasks_and_stops_idle_monitor():
    dispatcher = QueueWaitDispatcher()
    pending = task(Mock())
    dispatcher.add_task(pending)
    dispatcher.shutdown(timeout=1)
    pending.cancel.assert_called_once()
    pending.service.assert_not_called()
    assert not dispatcher._monitor.is_alive()


def test_startup_failure_releases_workers_and_monitor(monkeypatch):
    captured = []

    def fail(app, **options):
        captured.append(options['_dispatcher'])
        raise OSError('synthetic bind failure')

    monkeypatch.setattr('waitress.serve', fail)
    with pytest.raises(OSError, match='synthetic bind failure'):
        run_http_server(Mock())
    assert not captured[0]._monitor.is_alive()
    assert not captured[0].threads
