"""Per-request config reads: validate a profile once and copy no whole document, with the same semantics."""

import pytest
from flask import Flask

from backend.common import config as config_module
from backend.common.config_repository import ProfileNotFound, ProfileRepository
from backend.common.profile_context import resolve_profile_id


@pytest.fixture
def repository(tmp_path):
    previous = config_module._repository
    repository = ProfileRepository(tmp_path)
    repository.create_profile({'id': 'other', 'name': 'Other'})
    config_module.set_repository(repository)
    yield repository
    config_module.set_repository(previous)
    config_module.reset_config_context()


@pytest.fixture
def app():
    return Flask(__name__)


def test_request_reuses_the_validated_profile(repository, app, monkeypatch):
    calls = []
    metadata = repository._profile_metadata
    monkeypatch.setattr(repository, '_profile_metadata', lambda profile_id: calls.append(profile_id) or metadata(profile_id))
    with app.test_request_context('/api/rules', headers={'X-ConfigFlow-Profile': 'other'}):
        config_module.reset_config_context()
        assert resolve_profile_id() == 'other'
        assert config_module.get_config()['profile_id'] == 'other'
        assert config_module.get_config('other') is config_module.get_config()
        assert resolve_profile_id('other') == 'other'
        assert resolve_profile_id('default') == 'default'
        assert resolve_profile_id('default') == 'default'
    assert calls == ['other', 'default']


def test_deleting_a_profile_in_the_same_request_is_noticed(repository, app):
    with app.test_request_context('/api/rules', headers={'X-ConfigFlow-Profile': 'other'}):
        config_module.reset_config_context()
        assert resolve_profile_id() == 'other'
        assert resolve_profile_id('other') == 'other'
        config_module.get_config()
        repository.delete_profile('other')
        with pytest.raises(ProfileNotFound):
            resolve_profile_id()
        with pytest.raises(ProfileNotFound):
            resolve_profile_id('other')
        with pytest.raises(ProfileNotFound):
            config_module.get_config()


def test_switching_the_default_in_the_same_request_is_noticed(repository, app):
    with app.test_request_context('/api/rules'):
        config_module.reset_config_context()
        assert resolve_profile_id() == 'default'
        repository.set_default_profile('other')
        assert resolve_profile_id() == 'other'


def test_another_request_does_not_reuse_the_validated_profile(repository, app):
    with app.test_request_context('/api/rules', headers={'X-ConfigFlow-Profile': 'other'}):
        assert resolve_profile_id() == 'other'
        with app.test_request_context('/api/rules'):
            assert resolve_profile_id() == 'default'
        assert resolve_profile_id() == 'other'

