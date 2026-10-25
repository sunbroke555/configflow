"""Keep backend tests independent of local data and other pytest workers."""
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    # config.py resolves these paths during import, before fixtures can run.
    # A fresh directory also separates concurrent serial pytest invocations.
    worker = getattr(config, 'workerinput', {}).get('workerid', 'main')
    directory = TemporaryDirectory(prefix=f'configflow-pytest-{worker}-')
    environment = pytest.MonkeyPatch()
    environment.setenv('DATA_DIR', directory.name)
    environment.setenv('LOG_FILE', str(Path(directory.name) / 'app.log'))
    config.add_cleanup(directory.cleanup)
    config.add_cleanup(environment.undo)


@pytest.fixture(autouse=True)
def isolated_agent_metrics(monkeypatch):
    # AgentManager keeps history under its repository's data directory, which
    # tests place in temporary directories. Never adopt or create files in the
    # real old default directories (the checkout's backend/data/metrics,
    # /opt/configflow/data/metrics); tests that need one patch LEGACY_DIRS.
    from backend.agents import metrics_history as module
    real_legacy = [Path(directory).resolve() for directory in module.LEGACY_DIRS]
    original_init = module.MetricsHistory.__init__

    def guarded_init(self, data_dir, *args, **kwargs):
        assert Path(data_dir).resolve() not in real_legacy, f'test would write into {data_dir}'
        original_init(self, data_dir, *args, **kwargs)

    monkeypatch.setattr(module.MetricsHistory, '__init__', guarded_init)
    monkeypatch.setattr(module, 'LEGACY_DIRS', ())


@pytest.fixture(autouse=True)
def fresh_agent_runtime_state():
    # Heartbeat state lives in process memory; start each test as a fresh process would.
    from backend.agents import runtime_state
    runtime_state.reset()
    yield
    runtime_state.reset()
