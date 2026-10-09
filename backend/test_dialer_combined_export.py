"""Combined subscription output uses the same delivery boundary as single feeds."""
from unittest.mock import Mock

import pytest
import yaml

from backend.test_qa_integrity import make_app
from backend.test_dialer_delivery_snapshot import seed_profile
from backend.test_dialer_proxy import node, add_subscription
from backend.utils.subscription_cache import load_subscription_cache, save_subscription_nodes


def provider(name='Remote', **fields):
    return yaml.safe_dump({'proxies': [{'name': name, 'type': 'http',
        'server': 'fixture.test', 'port': 80, **fields}]})


@pytest.mark.parametrize('content', [
    provider(**{'dialer-proxy': 'missing-target'}),
    provider(**{'dialer-proxy': 'Entry'}),
    provider(**{'dialer-proxy': ['relay']}),
])
def test_combined_invalid_fetched_graph_rejected_without_side_effects(tmp_path, monkeypatch, content):
    app, repo = make_app(tmp_path)
    seed_profile(repo)
    cache_before = load_subscription_cache('s')
    profile_before = repo.path.read_bytes()
    artifact = repo.write_generated('default', 'config.yaml', 'last-good')
    cache_read = Mock(wraps=load_subscription_cache)
    monkeypatch.setattr('backend.routes.subscriptions.load_subscription_cache', cache_read)
    cache_write = Mock(side_effect=AssertionError('Combined export must not write cache'))
    monkeypatch.setattr('backend.routes.subscriptions.save_subscription_nodes', cache_write)
    monkeypatch.setattr('backend.routes.subscriptions.get_subscription_proxies_yaml',
                        Mock(return_value=(content, 'rendered_yaml')))
    network = Mock(side_effect=AssertionError('No network delivery'))
    monkeypatch.setattr('requests.get', network)
    response = app.test_client().get('/api/subscriptions/proxies')
    assert response.status_code == 400, response.data
    cache_read.assert_not_called()
    cache_write.assert_not_called()
    network.assert_not_called()
    assert load_subscription_cache('s') == cache_before
    assert repo.path.read_bytes() == profile_before
    assert artifact.read_text() == 'last-good'


@pytest.mark.parametrize('cached', [
    node('Cached', params=None),
    node('Cached', name=['invalid']),
    node('Cached', params={'dialer-proxy': ['relay']}),
    node('Cached', params={'dialer-proxy': 'Entry'}),
    node('Cached', params={'dialer-proxy': 'missing-target'}),
])
def test_combined_invalid_cache_rejected_on_transport_failure(tmp_path, monkeypatch, cached):
    app, repo = make_app(tmp_path)
    seed_profile(repo)
    save_subscription_nodes('s', [cached])
    before = load_subscription_cache('s')
    profile_before = repo.path.read_bytes()
    monkeypatch.setattr('backend.routes.subscriptions.get_subscription_proxies_yaml',
                        Mock(side_effect=ConnectionError('transport failure')))
    response = app.test_client().get('/api/subscriptions/proxies')
    assert response.status_code == 400, response.data
    assert load_subscription_cache('s') == before
    assert repo.path.read_bytes() == profile_before


def add_second_feed(repo):
    add_subscription(repo, {'id': 's2', 'name': 'Second', 'enabled': True, 'url': 'https://fixture.invalid/second'})
    repo.update_profile_transaction('default', lambda p: p['proxy_groups'][0].setdefault('subscriptions', []).append('s2'))


@pytest.mark.parametrize('chain', ['none', 'managed'])
@pytest.mark.parametrize('case', ['duplicate', 'reserved', 'cycle'])
def test_combined_final_graph_rejects_cross_source_topology(tmp_path, monkeypatch, chain, case):
    app, repo = make_app(tmp_path)
    seed_profile(repo, chain=chain)
    add_second_feed(repo)
    if case == 'duplicate':
        contents = [provider('Same', **{'dialer-proxy': 'DIRECT'}), provider('Same')]
    elif case == 'reserved':
        contents = [provider('Remote', **{'dialer-proxy': 'DIRECT'}), provider('DIRECT')]
    else:
        contents = [provider('A', **{'dialer-proxy': 'B'}), provider('B', **{'dialer-proxy': 'A'})]
    fetch = Mock(side_effect=[(text, 'rendered_yaml') for text in contents])
    monkeypatch.setattr('backend.routes.subscriptions.get_subscription_proxies_yaml', fetch)
    before = repo.path.read_bytes()
    response = app.test_client().get('/api/subscriptions/proxies')
    assert response.status_code == 400, response.data
    assert fetch.call_count == 2
    assert repo.path.read_bytes() == before


@pytest.mark.parametrize('content', ['proxies: [null]', 'proxies: not-an-array',
    'proxies: [{name: [invalid]}]', 'proxies: [{name: Remote, type: [http]}]',
    'proxies: [{name: Remote, proxies: null}]', 'proxies: [', '- not-an-object'])
def test_combined_bad_content_never_falls_back_to_valid_cache(tmp_path, monkeypatch, content):
    app, repo = make_app(tmp_path)
    seed_profile(repo, chain='none')
    cache_read = Mock(side_effect=AssertionError('Invalid content must not fall back'))
    monkeypatch.setattr('backend.routes.subscriptions.load_subscription_cache', cache_read)
    monkeypatch.setattr('backend.routes.subscriptions.get_subscription_proxies_yaml',
                        Mock(return_value=(content, 'rendered_yaml')))
    response = app.test_client().get('/api/subscriptions/proxies')
    assert response.status_code == 400, response.data
    cache_read.assert_not_called()


@pytest.mark.parametrize('target', ['relay', 'Static', 'DIRECT', 'REJECT'])
def test_combined_accepts_manual_static_and_builtin_targets(tmp_path, monkeypatch, target):
    app, repo = make_app(tmp_path)
    seed_profile(repo, chain='none')
    repo.update_profile_transaction('default', lambda p: p['proxy_groups'].append(
        {'id': 'static', 'name': 'Static', 'type': 'select', 'manual_nodes': ['relay']}))
    content = provider(**{'dialer-proxy': target})
    monkeypatch.setattr('backend.routes.subscriptions.get_subscription_proxies_yaml',
                        Mock(return_value=(content, 'rendered_yaml')))
    response = app.test_client().get('/api/subscriptions/proxies')
    assert response.status_code == 200, response.data
    assert yaml.safe_load(response.data) == yaml.safe_load(content)


@pytest.mark.parametrize('cached_target', [False, True])
def test_combined_cross_feed_reference_validates_only_final_collection(tmp_path, monkeypatch, cached_target):
    app, repo = make_app(tmp_path)
    seed_profile(repo, chain='none')
    add_second_feed(repo)
    source = provider('Remote', **{'dialer-proxy': 'OtherFeedRelay'})
    target = provider('OtherFeedRelay')
    save_subscription_nodes('s2', [node('OtherFeedRelay')])
    fetch = Mock(side_effect=[(source, 'rendered_yaml'),
        ConnectionError('transport failure') if cached_target else (target, 'rendered_yaml')])
    monkeypatch.setattr('backend.routes.subscriptions.get_subscription_proxies_yaml', fetch)
    response = app.test_client().get('/api/subscriptions/proxies')
    assert response.status_code == 200, response.data
    proxies = yaml.safe_load(response.data)['proxies']
    assert [p['name'] for p in proxies] == ['Remote', 'OtherFeedRelay']
    assert proxies[0]['dialer-proxy'] == proxies[1]['name']


@pytest.mark.parametrize('case', ['empty', 'transport-cache', 'transport-no-cache', 'duplicate-no-chain'])
def test_combined_legacy_compatible_controls(tmp_path, monkeypatch, case):
    app, repo = make_app(tmp_path)
    seed_profile(repo, chain='none')
    if case == 'duplicate-no-chain':
        add_second_feed(repo)
        fetch = Mock(return_value=(provider('Same'), 'rendered_yaml'))
        expected = ['Same', 'Same']
    elif case == 'empty':
        fetch = Mock(return_value=('proxies: []', 'rendered_yaml'))
        expected = []
    else:
        fetch = Mock(side_effect=ConnectionError('transport failure'))
        expected = ['old'] if case == 'transport-cache' else []
        if case == 'transport-no-cache':
            from pathlib import Path
            from backend.utils.subscription_cache import _get_cache_path
            Path(_get_cache_path('s')).unlink()
    monkeypatch.setattr('backend.routes.subscriptions.get_subscription_proxies_yaml', fetch)
    before = load_subscription_cache('s')
    response = app.test_client().get('/api/subscriptions/proxies')
    assert response.status_code == 200, response.data
    assert [p['name'] for p in yaml.safe_load(response.data)['proxies']] == expected
    assert load_subscription_cache('s') == before


@pytest.mark.parametrize('selector', ['header', 'query'])
def test_combined_profile_selection_isolated_and_context_cleared(tmp_path, monkeypatch, selector):
    app, repo = make_app(tmp_path)
    seed_profile(repo, chain='none')
    repo.create_profile({'id': 'other', 'name': 'Other'}, clone_from='default')
    repo.update_shared_transaction(lambda shared: shared['nodes'].append(node('other-relay')))
    def add_other_group(p):
        p['proxy_groups'].append({'id': 'other-entry', 'name': 'Other entry', 'type': 'select',
                                  'manual_nodes': ['other-relay']})
    repo.update_profile_transaction('other', add_other_group)
    monkeypatch.setattr('backend.routes.subscriptions.get_subscription_proxies_yaml',
                        Mock(return_value=(provider(**{'dialer-proxy': 'Other entry'}), 'rendered_yaml')))
    kwargs = {'headers': {'X-ConfigFlow-Profile': 'other'}} if selector == 'header' else {'query_string': {'profile': 'other'}}
    client = app.test_client()
    assert client.get('/api/subscriptions/proxies', **kwargs).status_code == 200
    assert client.get('/api/subscriptions/proxies').status_code == 400


def test_combined_snapshot_and_cache_stay_bound_when_fetch_changes_context(tmp_path, monkeypatch):
    from backend.common.config import get_config
    app, repo = make_app(tmp_path)
    seed_profile(repo, chain='none')
    add_second_feed(repo)
    repo.create_profile({'id': 'other', 'name': 'Other'}, clone_from='default')
    save_subscription_nodes('s2', [node('Bound', params={'dialer-proxy': 'relay'})])
    calls = []
    def fetch(*_args):
        calls.append(1)
        if len(calls) == 1:
            get_config()['nodes'][0]['name'] = 'mutated-relay'
            def change_group(p):
                p['proxy_groups'][0]['manual_nodes'] = ['exit']
            repo.update_profile_transaction('default', change_group)
            get_config('other')['proxy_groups'][0]['name'] = 'Other snapshot'
            return provider(), 'rendered_yaml'
        raise ConnectionError('transport failure')
    cache_read = Mock(wraps=load_subscription_cache)
    monkeypatch.setattr('backend.routes.subscriptions.load_subscription_cache', cache_read)
    monkeypatch.setattr('backend.routes.subscriptions.get_subscription_proxies_yaml', fetch)
    response = app.test_client().get('/api/subscriptions/proxies', headers={'X-ConfigFlow-Profile': 'default'})
    assert response.status_code == 200, response.data
    assert [p['name'] for p in yaml.safe_load(response.data)['proxies']] == ['Remote', 'Bound']
    cache_read.assert_called_once_with('s2')
    assert load_subscription_cache('s2')['nodes'][0]['name'] == 'Bound'
    assert repo.get_profile('default')['proxy_groups'][0]['manual_nodes'] == ['exit']
