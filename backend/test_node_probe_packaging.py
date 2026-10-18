"""The worker follows image/auto-update rollback, rather than a mutable core path."""
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def updater(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('probe_auto_update', Path(__file__).parents[1] / 'docker/auto_update.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ('STATE', 'BACKEND', 'HTML', 'AGENTS', 'NODE_PROBE', 'IMAGE_COPY', 'IMAGE_TREES'):
        monkeypatch.setattr(module, name, str(tmp_path / name.lower()))
    Path(module.STATE).mkdir()
    return module


def source_tree(tmp_path):
    source = tmp_path / 'source'
    probe = source / 'backend/node-probe'
    probe.mkdir(parents=True)
    (probe / 'go.mod').write_text('module probe\ngo 1.26.0\n')
    (probe / 'main.go').write_text('package main')
    (source / 'backend/version.py').write_text('__version__ = "dev"')
    return source, probe


def test_changed_probe_without_compiler_refuses_mixed_versions(updater, tmp_path, monkeypatch):
    source, probe = source_tree(tmp_path)
    monkeypatch.setattr(updater.os.path, 'exists', lambda path: False)
    key = updater.tree_hash(probe, updater._agent_input)
    monkeypatch.setattr(updater, 'image_trees', lambda: {'node_probe': key})
    manifest = {}
    assert updater.build_node_probe(str(source), manifest) is None
    assert manifest['node_probe'] is None
    (probe / 'main.go').write_text('package main // changed')
    with pytest.raises(updater.MissingToolchain):
        updater.build_node_probe(str(source), manifest)


def test_probe_build_is_for_host_and_reused(updater, tmp_path, monkeypatch):
    source, _ = source_tree(tmp_path)
    monkeypatch.setattr(updater.os.path, 'exists', lambda path: True)
    monkeypatch.setattr(updater.platform, 'machine', lambda: 'aarch64')
    calls = []
    def build(args, cwd, env):
        calls.append(env)
        Path(args[args.index('-o') + 1]).write_text('new worker')
    monkeypatch.setattr(updater, 'run', build)
    manifest = {}
    built = updater.build_node_probe(str(source), manifest)
    assert calls[0]['GOARCH'] == 'arm64'
    assert calls[0]['CGO_ENABLED'] == '0'
    assert updater.build_node_probe(str(source), manifest) == built
    assert len(calls) == 1


def test_restore_and_install_keep_worker_with_matching_sources(updater, tmp_path):
    source, _ = source_tree(tmp_path)
    bundled = Path(updater.NODE_PROBE)
    bundled.mkdir()
    (bundled / 'configflow-node-probe').write_text('bundled worker')
    updater.preserve_image()
    built = Path(updater.STATE) / 'node-probe-new'
    built.mkdir()
    (built / 'configflow-node-probe').write_text('new worker')
    manifest = {'source': str(source), 'sha': '1234567', 'node_probe': str(built)}
    assert updater.install(manifest)
    assert (bundled / 'configflow-node-probe').read_text() == 'new worker'
    manifest['node_probe'] = None
    assert updater.install(manifest)
    assert (bundled / 'configflow-node-probe').read_text() == 'bundled worker'
    updater.restore_image()
    assert (bundled / 'configflow-node-probe').read_text() == 'bundled worker'


def test_prune_keeps_current_worker(updater):
    current = Path(updater.STATE) / 'node-probe-current'
    current.mkdir()
    old = Path(updater.STATE) / 'node-probe-old'
    old.mkdir()
    updater.prune({'node_probe': str(current)})
    assert current.exists() and not old.exists()
