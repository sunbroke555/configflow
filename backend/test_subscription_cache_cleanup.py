"""Node caches of removed subscriptions hold node credentials and must not linger."""
import os

import pytest
from flask import Flask

from backend.common import config as config_module
from backend.common.config_repository import ProfileRepository
from backend.routes import register_blueprints
from backend.utils import subscription_cache
from backend.utils.subscription_cache import load_subscription_cache, prune_orphan_caches, save_subscription_nodes

NODE = {'id': 'n', 'name': 'HK', 'type': 'ss', 'server': '192.0.2.1', 'port': 443, 'cipher': 'aes-128-gcm', 'password': 'secret'}


@pytest.fixture
def shared(tmp_path):
    repository = ProfileRepository(tmp_path)
    repository.save_shared({'subscriptions': [
        {'id': 'keep', 'name': 'Keep', 'url': 'https://example.test/keep', 'enabled': True},
        {'id': 'drop', 'name': 'Drop', 'url': 'https://example.test/drop', 'enabled': True}]})
    previous = config_module._repository
    config_module.set_repository(repository)
    app = Flask(__name__)
    register_blueprints(app)
    for sub_id in ('keep', 'drop'):
        save_subscription_nodes(sub_id, [NODE])
    yield app.test_client(), repository
    config_module.set_repository(previous)


def files(repository):
    return sorted(os.listdir(repository.shared_cache_dir()))


def test_deleting_a_subscription_removes_its_node_cache(shared):
    client, repository = shared
    assert client.delete('/api/subscriptions/drop').status_code == 200
    assert load_subscription_cache('drop') is None
    assert files(repository) == ['keep.json', 'keep.json.lock']
    assert load_subscription_cache('keep')['nodes'] == [NODE]


def test_a_subscription_still_in_use_keeps_its_cache(shared):
    client, repository = shared
    repository.save_profile('default', {'proxy_groups': [{'id': 'g', 'name': 'G', 'type': 'select', 'subscriptions': ['drop']}]})
    assert client.delete('/api/subscriptions/drop').status_code == 409
    assert load_subscription_cache('drop') is not None


def test_startup_prunes_only_caches_without_a_subscription(shared):
    _, repository = shared
    save_subscription_nodes('removed/earlier', [NODE])  # Stored with a path-safe name.
    repository.update_shared_transaction(lambda data: {**data, 'subscriptions': [s for s in data['subscriptions'] if s['id'] != 'drop']})
    (repository.shared_cache_dir() / 'notes.txt').write_text('unrelated')
    assert sorted(prune_orphan_caches()) == ['drop.json', 'removed_earlier.json']
    assert files(repository) == ['keep.json', 'keep.json.lock', 'notes.txt']
    assert prune_orphan_caches() == []


def test_pruning_deletes_nothing_when_subscriptions_cannot_be_read(shared, monkeypatch):
    _, repository = shared
    from backend.common.config_repository import ProfileRepositoryError
    def broken():
        raise ProfileRepositoryError('unreadable')
    monkeypatch.setattr(repository, 'get_shared', broken)
    assert prune_orphan_caches() == []
    assert 'drop.json' in files(repository) and 'keep.json' in files(repository)
