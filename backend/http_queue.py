"""Report sustained HTTP queue waits without warning on ordinary bursts."""
import threading
import time

from waitress.task import ThreadedTaskDispatcher


QUEUE_WAIT_WARNING_SECONDS = 1.0
QUEUE_WARNING_INTERVAL_SECONDS = 30.0


class _QueuedTask:
    __slots__ = ('task', 'queued_at')

    def __init__(self, task, queued_at):
        self.task = task
        self.queued_at = queued_at

    def service(self):
        return self.task.service()

    def cancel(self):
        return self.task.cancel()


class QueueWaitDispatcher(ThreadedTaskDispatcher):
    """Keep Waitress workers; observe queued tasks even if every worker blocks."""

    def __init__(self, *, clock=time.monotonic):
        super().__init__()
        self._clock = clock
        self._last_warning = None
        self._stopped = threading.Event()
        self._wake = threading.Event()
        self._monitor = threading.Thread(
            target=self._watch_queue, name='configflow-queue-watch', daemon=True,
        )
        self._monitor.start()

    def add_task(self, task):
        with self.lock:
            if not self.queue:
                self._last_warning = None
            self.queue.append(_QueuedTask(task, self._clock()))
            self.queue_cv.notify()
            idle = len(self.threads) - self.stop_count - self.active_count
            depth = len(self.queue) - idle
        # Only the immediate depth message is downgraded. Worker failures and
        # shutdown warnings keep Waitress's original logging behavior.
        if depth > 0:
            self.queue_logger.debug('Task queue depth is %d', depth)
        self._wake.set()

    def _check_queue(self):
        with self.lock:
            if not self.queue:
                self._last_warning = None
                return None
            now = self._clock()
            waited = now - self.queue[0].queued_at
            delay = QUEUE_WAIT_WARNING_SECONDS - waited
            if self._last_warning is not None:
                delay = max(delay, QUEUE_WARNING_INTERVAL_SECONDS - (now - self._last_warning))
            if delay > 0:
                return delay
            depth = len(self.queue)
            self._last_warning = now
        self.queue_logger.warning(
            '请求排队已等待 %.1f 秒，队列中有 %d 个待处理任务', waited, depth,
        )
        return QUEUE_WARNING_INTERVAL_SECONDS

    def _watch_queue(self):
        # No periodic wakeups when the queue is empty. An independent observer
        # is necessary: logging only when service() starts misses stuck workers.
        while not self._stopped.is_set():
            delay = self._check_queue()
            self._wake.wait(delay)
            self._wake.clear()

    def shutdown(self, cancel_pending=True, timeout=5):
        self._stopped.set()
        self._wake.set()
        self._monitor.join(timeout=1)
        return super().shutdown(cancel_pending=cancel_pending, timeout=timeout)
