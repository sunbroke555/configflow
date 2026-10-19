"""Health-check configuration: HTTPS defaults, preserved explicit legacy values."""
from copy import deepcopy

import pytest
from flask import Flask
from backend.common import config as config_module
from backend.common.config_repository import ProfileRepository
from backend.routes import register_blueprints

from backend.converters.mihomo import build_provider_health_check


def test_provider_explicit_health_check_is_emitted_without_losing_false():
    source = {'health_check': {'enable': False, 'url': 'https://probe.test/generate_204',
                              'interval': 61, 'timeout': 7000, 'lazy': False,
                              'expected-status': '204'}}
    before = deepcopy(source)
    assert build_provider_health_check(source) == source['health_check']
    assert source == before


def test_provider_opt_in_empty_object_uses_new_defaults():
    assert build_provider_health_check({'health_check': {}}) == {
        'enable': True, 'url': 'https://www.gstatic.com/generate_204',
        'interval': 300, 'timeout': 5000, 'lazy': True, 'expected-status': '204',
    }


def test_legacy_provider_uses_https_default_and_preserves_explicit_url():
    assert build_provider_health_check({}) == {
        'enable': True, 'url': 'https://www.gstatic.com/generate_204',
        'interval': 300, 'lazy': True,
    }
    assert build_provider_health_check({'health_check_url': 'http://old.test/check'})['url'] == 'http://old.test/check'


def test_new_api_group_receives_health_defaults_and_readback(tmp_path):
    repository = ProfileRepository(tmp_path)
    config_module.set_repository(repository)
    app = Flask(__name__)
    register_blueprints(app)
    client = app.test_client()
    response = client.post('/api/proxy-groups', json={
        'id': 'auto', 'name': 'Auto', 'type': 'url-test', 'manual_nodes': ['DIRECT'],
    })
    assert response.status_code == 200
    group = client.get('/api/proxy-groups').json[0]
    for key, value in {'url': 'https://www.gstatic.com/generate_204', 'interval': 60,
                       'timeout': 5000, 'lazy': False, 'expected-status': '204',
                       'tolerance': 50}.items():
        assert group.get(key) == value
