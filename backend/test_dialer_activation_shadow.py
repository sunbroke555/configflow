"""Provider overlap cannot deactivate strict validation of a captured chain graph."""
from unittest.mock import Mock

import pytest
import yaml

from backend.test_dialer_delivery_snapshot import agent_manager, seed_profile
from backend.test_qa_integrity import make_app
from backend.utils.dialer_references import DialerReferenceError
from backend.utils.provider_delivery import DeliverySnapshot
from backend.utils.subscription_cache import load_subscription_cache


def proxy(name, **fields):
    return {'name': name, 'type': 'http', 'server': 'fixture.test', 'port': 80, **fields}


def shadow_proxies(case):
    # This exact name removes the only main dialer from the overlap graph.
    proxies = [proxy('Via exit')]
    if case == 'reserved':
        proxies.append(proxy('DIRECT'))
    elif case == 'duplicate':
        proxies.extend([proxy('Same'), proxy('Same')])
    elif case == 'cycle':
        proxies.extend([proxy('A', proxies=['B']), proxy('B', proxies=['A'])])
    elif case == 'dangling':
        proxies.append(proxy('A', proxies=['missing']))
    elif case == 'membership_cycle':
        proxies.append(proxy('A', **{'dialer-proxy': 'Entry'}))
    elif case == 'malformed':
        proxies.append(proxy('A', **{'dialer-proxy': ['relay']}))
    return proxies


@pytest.mark.parametrize('force_chains', [False, True])
@pytest.mark.parametrize('case', ['reserved', 'duplicate', 'cycle', 'dangling', 'membership_cycle', 'malformed'])
def test_snapshot_shadow_cannot_deactivate_original_chain(case, force_chains):
    main = {'proxies': [proxy('Via exit', **{'dialer-proxy': 'relay'}), proxy('relay'), proxy('exit')],
            'proxy-groups': [{'name': 'Entry', 'type': 'select', 'use': ['Feed'], 'proxies': ['A'] if case == 'membership_cycle' else ['relay']}],
            'proxy-providers': {'Feed': {}}}
    snapshot = DeliverySnapshot.capture('default', main)
    # A caller changing its live graph cannot change captured activation.
    main['proxies'][0].pop('dialer-proxy')
    assert snapshot.has_chains
    with pytest.raises(DialerReferenceError):
        snapshot.validate(shadow_proxies(case), force_chains=force_chains)


@pytest.mark.parametrize('direct', [True, False])
@pytest.mark.parametrize('consumer', ['provider', 'preview', 'generate', 'download', 'combined', 'push'])
@pytest.mark.parametrize('case', ['reserved', 'duplicate', 'cycle', 'dangling', 'membership_cycle', 'malformed'])
def test_shadowed_main_chain_rejects_before_any_delivery_mutation(tmp_path, monkeypatch, direct, consumer, case):
    app, repo = make_app(tmp_path)
    seed_profile(repo, direct=direct)
    content = yaml.safe_dump({'proxies': shadow_proxies(case)})
    fetch = Mock(return_value=(content, 'rendered_yaml'))
    # Combined export always fetches subscriptions directly, even in an agg profile.
    for module in ('subscriptions', 'aggregations'):
        monkeypatch.setattr(f'backend.routes.{module}.get_subscription_proxies_yaml', fetch)
    manager = agent_manager(monkeypatch)
    network = Mock(side_effect=AssertionError('Rejected graph must not reach network'))
    monkeypatch.setattr('requests.get', network)
    cache_before = load_subscription_cache('s')
    profile_before = repo.path.read_bytes()
    artifacts = [repo.write_profile_text('default', name, 'last-good')
                 for name in ('generated/config.yaml', 'providers/agg.yaml')]
    # Read-back equality alone can miss a write followed by rollback.
    cache_writes = []
    for target in ('backend.routes.subscriptions.save_subscription_nodes',
                   'backend.utils.subscription_cache.save_subscription_nodes',
                   'backend.utils.provider_delivery.commit_cache_updates'):
        spy = Mock(side_effect=AssertionError('Validation must precede cache write'))
        monkeypatch.setattr(target, spy)
        cache_writes.append(spy)
    artifact_write = Mock(side_effect=AssertionError('Validation must precede artifact write'))
    monkeypatch.setattr(repo, 'write_profile_text', artifact_write)
    monkeypatch.setattr(repo, 'write_generated', artifact_write)
    path = {
        'provider': '/api/subscriptions/s/proxies' if direct else '/api/aggregations/agg/provider',
        'preview': '/api/generate/mihomo/preview', 'generate': '/api/generate/mihomo',
        'download': '/api/config/mihomo', 'combined': '/api/subscriptions/proxies',
        'push': '/api/agents/a/push-config',
    }[consumer]
    client = app.test_client()
    response = client.get(path) if consumer in ('provider', 'download', 'combined') else client.post(path, json={'restart': False})
    assert response.status_code == 400, response.get_data(as_text=True)
    assert fetch.called
    for spy in cache_writes:
        spy.assert_not_called()
    artifact_write.assert_not_called()
    network.assert_not_called()
    manager.push_config_to_agent.assert_not_called()
    assert load_subscription_cache('s') == cache_before
    assert repo.path.read_bytes() == profile_before
    assert all(path.read_text() == 'last-good' for path in artifacts)


@pytest.mark.parametrize('chain', [False, True])
def test_valid_shadow_preserves_overlap_and_no_chain_legacy_names(chain):
    main = {'proxies': [proxy('Via exit', **({'dialer-proxy': 'relay'} if chain else {})), proxy('relay'), proxy('exit')],
            'proxy-groups': []}
    snapshot = DeliverySnapshot.capture('default', main)
    proxies = [proxy('Via exit')]
    if not chain:
        proxies.extend([proxy('DIRECT'), proxy('Same'), proxy('Same')])
    before = yaml.safe_dump(proxies)
    snapshot.validate(proxies)
    assert yaml.safe_dump(proxies) == before
