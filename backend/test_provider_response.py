"""Provider downloads answer within Mihomo's 20 s timeout using the last good content."""
from concurrent.futures import Future
import threading
import time

from flask import Flask, Response, jsonify, make_response

from backend.common import config as config_module
from backend.common.config_repository import ProfileRepository
from backend.utils import provider_response


def _setup(tmp_path, monkeypatch, seconds=0.2):
    repository = ProfileRepository(tmp_path)
    repository.create_profile({"id": "alpha", "name": "Alpha"})
    repository.create_profile({"id": "beta", "name": "Beta"})
    config_module.set_repository(repository)
    monkeypatch.setattr(provider_response, "RESPONSE_SECONDS", seconds)
    return repository, Flask(__name__)


def _serve(app, render, profile="alpha", query="", config=None):
    with app.test_request_context("/provider" + query):
        response = provider_response.serve_provider("aggregation", "agg-1", profile, config or {"v": 1},
                                                    lambda: make_response(render()))
        return response.status_code, response.get_data(as_text=True), response.headers.get("X-ConfigFlow-Provider")


def _slow(content, gate, calls=None):
    def render():
        if calls is not None:
            calls.append(1)
        gate.wait(5)
        return Response(content, mimetype="text/yaml")
    return render


def _wait_for(predicate):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert predicate()


def test_immediate_render_clears_flight_without_reentering_locked_callback(tmp_path, monkeypatch):
    _, app = _setup(tmp_path, monkeypatch)
    lock = threading.Lock()
    flights = {}

    class CompletedFuture(Future):
        def add_done_callback(self, callback):
            # Fail immediately with the old implementation instead of hanging
            # the test process when Future invokes the callback synchronously.
            assert not lock.locked(), "completed callback registered while holding flight lock"
            super().add_done_callback(callback)

    class ImmediatePool:
        def submit(self, run):
            future = CompletedFuture()
            future.set_result(run())
            return future

    monkeypatch.setattr(provider_response, "_pool", ImmediatePool())
    monkeypatch.setattr(provider_response, "_flights_lock", lock)
    monkeypatch.setattr(provider_response, "_flights", flights)
    for content in ("proxies: [first]", "proxies: [second]"):
        assert _serve(app, lambda: Response(content, mimetype="text/yaml")) == (200, content, None)
        assert flights == {}


def test_slow_render_returns_last_good_and_refreshes_it_in_the_background(tmp_path, monkeypatch):
    repository, app = _setup(tmp_path, monkeypatch)
    assert _serve(app, lambda: Response("proxies: [old]", mimetype="text/yaml"))[1] == "proxies: [old]"

    gate = threading.Event()
    started = time.monotonic()
    status, body, marker = _serve(app, _slow("proxies: [new]", gate))
    assert (status, body, marker) == (200, "proxies: [old]", "last-good")
    assert time.monotonic() - started < 2

    gate.set()
    path = repository.profile_path("alpha", "providers/served/aggregation-agg-1.yaml")
    _wait_for(lambda: path.read_text(encoding="utf-8") == "proxies: [new]")
    assert _serve(app, lambda: Response("proxies: [new]", mimetype="text/yaml"))[1] == "proxies: [new]"


def test_slow_render_without_last_good_still_waits(tmp_path, monkeypatch):
    _, app = _setup(tmp_path, monkeypatch)
    gate = threading.Event()
    threading.Timer(0.5, gate.set).start()
    assert _serve(app, _slow("proxies: [first]", gate)) == (200, "proxies: [first]", None)


def test_concurrent_requests_share_one_render(tmp_path, monkeypatch):
    _, app = _setup(tmp_path, monkeypatch, seconds=5)
    gate, calls, results = threading.Event(), [], []
    render = _slow("proxies: [shared]", gate, calls)
    threads = [threading.Thread(target=lambda: results.append(_serve(app, render))) for _ in range(3)]
    for thread in threads:
        thread.start()
    _wait_for(lambda: calls)
    time.sleep(0.2)
    gate.set()
    for thread in threads:
        thread.join(5)
    assert calls == [1] and [body for _, body, _ in results] == ["proxies: [shared]"] * 3


def test_other_formats_never_fall_back(tmp_path, monkeypatch):
    _, app = _setup(tmp_path, monkeypatch)
    _serve(app, lambda: Response("proxies: [old]", mimetype="text/yaml"))
    gate = threading.Event()
    threading.Timer(0.5, gate.set).start()
    status, body, marker = _serve(app, _slow("surge-text", gate), query="?format=surge")
    assert (status, body, marker) == (200, "surge-text", None)


def test_last_good_is_scoped_to_the_profile(tmp_path, monkeypatch):
    _, app = _setup(tmp_path, monkeypatch)
    _serve(app, lambda: Response("proxies: [beta]", mimetype="text/yaml"), profile="beta")
    gate = threading.Event()
    threading.Timer(0.5, gate.set).start()
    # Alpha has never been served: it must not receive beta's content.
    assert _serve(app, _slow("proxies: [alpha]", gate), profile="alpha")[1] == "proxies: [alpha]"


def test_aggregation_falls_back_to_committed_artifact_before_first_serve(tmp_path, monkeypatch):
    repository, app = _setup(tmp_path, monkeypatch)
    repository.write_profile_text("alpha", "providers/agg-1.yaml", "proxies: [committed]")
    gate = threading.Event()
    status, body, marker = _serve(app, _slow("proxies: [new]", gate))
    gate.set()
    assert (status, body, marker) == (200, "proxies: [committed]", "last-good")


def test_failed_render_is_not_stored_as_last_good(tmp_path, monkeypatch):
    repository, app = _setup(tmp_path, monkeypatch)
    status, _, _ = _serve(app, lambda: (jsonify({"success": False}), 400))
    assert status == 400
    assert not repository.profile_path("alpha", "providers/served/aggregation-agg-1.yaml").exists()


def test_aggregation_endpoint_falls_back_when_generation_is_slow(tmp_path, monkeypatch):
    repository, _ = _setup(tmp_path, monkeypatch)
    from backend.routes import aggregations, register_blueprints
    app = Flask(__name__)
    register_blueprints(app)
    client = app.test_client()
    repository.save_shared({"subscription_aggregations": [{"id": "agg-1", "name": "Agg", "subscriptions": [], "nodes": []}]})
    repository.save_profile("alpha", {"proxy_groups": [{"id": "g", "name": "G", "type": "select", "aggregations": ["agg-1"]}]})
    monkeypatch.setattr(aggregations, "validate_token_or_jwt", lambda request: {"valid": True})

    monkeypatch.setattr(aggregations, "generate_aggregation_provider", lambda *a, **k: {"content": "proxies: [one]"})
    first = client.get("/api/profiles/alpha/aggregations/agg-1/provider")
    assert first.status_code == 200 and first.get_data(as_text=True) == "proxies: [one]"

    gate = threading.Event()

    def slow(*args, **kwargs):
        gate.wait(5)
        return {"content": "proxies: [two]"}

    monkeypatch.setattr(aggregations, "generate_aggregation_provider", slow)
    second = client.get("/api/profiles/alpha/aggregations/agg-1/provider")
    gate.set()
    assert second.status_code == 200 and second.get_data(as_text=True) == "proxies: [one]"
    assert second.headers["X-ConfigFlow-Provider"] == "last-good"
