"""Generated node probes use HTTPS defaults and keep user-selected HTTP URLs."""
from copy import deepcopy

import pytest
import yaml

from backend.converters.loon import generate_loon_config
from backend.converters.mihomo import build_provider_health_check, generate_mihomo_config
from backend.converters.surge import generate_surge_config


HTTPS_PROBE = 'https://www.gstatic.com/generate_204'
CUSTOM_PROBE = 'http://probe.test/generate_204'
GENERATORS = {
    'mihomo': lambda config: generate_mihomo_config(config, preflight_providers=False),
    'surge': generate_surge_config,
    'loon': generate_loon_config,
}


def _config():
    return {
        'nodes': [{
            'id': 'node', 'name': 'Node', 'type': 'ss', 'server': 'node.test',
            'port': 8388, 'params': {'cipher': 'aes-128-gcm', 'password': 'secret'},
        }],
        'proxy_groups': [],
    }


def _group_line(config, name):
    return next(line for line in config.splitlines() if line.startswith(f'{name} = '))


def test_surge_automatic_group_uses_https_probe():
    config = _config()
    before = deepcopy(config)
    generated = generate_surge_config(config)
    assert HTTPS_PROBE in _group_line(generated, 'Auto')
    assert config == before


@pytest.mark.parametrize('client', ['surge', 'loon'])
def test_generated_general_node_probe_uses_https(client):
    generated = GENERATORS[client](_config())
    assert f'proxy-test-url = {HTTPS_PROBE}' in generated


@pytest.mark.parametrize('client', ['surge', 'loon'])
@pytest.mark.parametrize('group_type', ['url-test', 'fallback', 'load-balance'])
def test_omitted_group_probe_uses_https_fallback(client, group_type):
    config = _config()
    config['proxy_groups'] = [{
        'id': 'group', 'name': 'Probe', 'type': group_type, 'manual_nodes': ['node'],
    }]
    before = deepcopy(config)
    assert HTTPS_PROBE in _group_line(GENERATORS[client](config), 'Probe')
    assert config == before


@pytest.mark.parametrize('client', GENERATORS)
@pytest.mark.parametrize('group_type', ['url-test', 'fallback', 'load-balance'])
def test_explicit_http_group_probe_is_preserved(client, group_type):
    config = _config()
    config['proxy_groups'] = [{
        'id': 'group', 'name': 'Probe', 'type': group_type,
        'manual_nodes': ['node'], 'url': CUSTOM_PROBE,
    }]
    before = deepcopy(config)
    generated = GENERATORS[client](config)
    if client == 'mihomo':
        assert yaml.safe_load(generated)['proxy-groups'][0]['url'] == CUSTOM_PROBE
    else:
        assert CUSTOM_PROBE in _group_line(generated, 'Probe')
    assert config == before


@pytest.mark.parametrize('client', ['surge', 'loon'])
def test_custom_general_probe_is_preserved(client):
    config = _config()
    config[client] = {'custom_config': f'[General]\nproxy-test-url = {CUSTOM_PROBE}'}
    assert f'proxy-test-url = {CUSTOM_PROBE}' in GENERATORS[client](config)


@pytest.mark.parametrize('source', [
    {'health_check_url': CUSTOM_PROBE},
    {'health_check': {'url': CUSTOM_PROBE}},
])
def test_explicit_http_provider_probe_is_preserved(source):
    before = deepcopy(source)
    assert build_provider_health_check(source)['url'] == CUSTOM_PROBE
    assert source == before
