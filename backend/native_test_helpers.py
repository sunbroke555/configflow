"""Loopback-only readiness helpers for optional native kernel tests."""
import socket
import time

import pytest


def wait_for_mihomo_routing(process, proxy_port, log_path, *,
                            expected_log='match Match using FALLBACK', timeout=5):
    """Wait for the data path independently of the assertions under test.

    Provider counts become visible before executor finishes GC and switches
    the tunnel from Inner to Running. During that gap external HTTP requests
    close without a route/log. Only this separate loopback probe is retried;
    callers still issue every actual matching assertion exactly once.
    """
    target = '127.0.0.1:30999'
    request = (f'GET http://{target}/ HTTP/1.1\r\nHost: {target}\r\n'
               'Connection: close\r\n\r\n').encode()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(('127.0.0.1', proxy_port), timeout=.2) as connection:
                connection.settimeout(.2)
                connection.sendall(request)
                connection.recv(1024)
        except OSError:
            pass
        logs = log_path.read_text()
        if any(f' --> {target} ' in line and expected_log in line for line in logs.splitlines()):
            return
        assert process.poll() is None, logs
        time.sleep(.05)
    pytest.fail('Mihomo routing did not become ready: ' + log_path.read_text())
