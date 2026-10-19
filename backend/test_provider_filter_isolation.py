"""Provider flights must isolate each group's filters."""
import threading

import pytest
from flask import Flask, Response, request

from backend.utils import provider_response


@pytest.mark.parametrize('format', ['surge', 'loon'])
@pytest.mark.parametrize('field', ['regex', 'exclude_regex'])
def test_concurrent_group_filters_use_independent_provider_flights(monkeypatch, format, field):
    monkeypatch.setattr(provider_response, '_flights', {})
    monkeypatch.setattr(provider_response, '_flights_lock', threading.Lock())
    app = Flask(__name__)
    started = threading.Event()
    release = threading.Event()
    count_lock = threading.Lock()
    patterns, results, errors = [], {}, []

    def render():
        pattern = request.args[field]
        with count_lock:
            patterns.append(pattern)
            if len(patterns) == 2:
                started.set()
        release.wait(3)
        return Response(pattern, mimetype='text/plain')

    def serve(pattern):
        try:
            with app.test_request_context('/provider', query_string={'format': format, field: pattern}):
                response = provider_response.serve_provider('subscription', 'feed', 'default', {}, render)
                results[pattern] = response.get_data(as_text=True)
        except Exception as exc:
            errors.append(exc)

    workers = [threading.Thread(target=serve, args=(pattern,), daemon=True) for pattern in ('HK', 'JP')]
    for worker in workers:
        worker.start()
    try:
        assert started.wait(2), 'different filters incorrectly shared one provider render'
    finally:
        release.set()
        for worker in workers:
            worker.join(2)
    assert all(not worker.is_alive() for worker in workers)
    assert errors == []
    assert results == {'HK': 'HK', 'JP': 'JP'}
