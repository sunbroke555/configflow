"""Dynamic provider dialers must reject membership cycles before delivery."""
from unittest.mock import Mock
import pytest
from backend.test_qa_integrity import make_app
from backend.test_dialer_delivery_snapshot import seed_profile, agent_manager
from backend.utils.subscription_cache import load_subscription_cache
from backend.utils.dialer_references import DialerReferenceError, validate_emitted


@pytest.mark.parametrize('direct', [True, False])
@pytest.mark.parametrize('consumer', ['preview', 'generate', 'download', 'provider', 'push'])
def test_provider_membership_cycle_rejected_before_side_effects(tmp_path, monkeypatch, direct, consumer):
    app, repo = make_app(tmp_path)
    seed_profile(repo, direct=direct)
    content = 'proxies: [{name: Remote, type: http, server: fixture.test, port: 80, dialer-proxy: Entry}]'
    monkeypatch.setattr(f'backend.routes.{"subscriptions" if direct else "aggregations"}.get_subscription_proxies_yaml', Mock(return_value=(content, 'rendered_yaml')))
    manager = agent_manager(monkeypatch)
    network = Mock(side_effect=AssertionError('No network'))
    monkeypatch.setattr('requests.get', network)
    before = load_subscription_cache('s')
    profile = repo.path.read_bytes()
    artifacts = [repo.write_profile_text('default', name, 'last-good') for name in ('generated/config.yaml', 'providers/agg.yaml')]
    path = {'preview': '/api/generate/mihomo/preview', 'generate': '/api/generate/mihomo', 'download': '/api/config/mihomo', 'provider': '/api/subscriptions/s/proxies' if direct else '/api/aggregations/agg/provider', 'push': '/api/agents/a/push-config'}[consumer]
    client = app.test_client()
    response = client.get(path) if consumer in ('download', 'provider') else client.post(path, json={'restart': False})
    assert response.status_code == 400, response.get_data(as_text=True)
    manager.push_config_to_agent.assert_not_called()
    network.assert_not_called()
    assert load_subscription_cache('s') == before
    assert repo.path.read_bytes() == profile
    assert all(p.read_text() == 'last-good' for p in artifacts)


@pytest.mark.parametrize('nested', [False, True])
@pytest.mark.parametrize('cycle', [False, True])
def test_raw_dialer_dynamic_group_checks_real_candidate_edges(nested, cycle):
    # These are emitted groups: subscription/aggregation/follow configuration
    # has already been lowered to providers and concrete candidate edges.
    groups = [{'name': 'Dynamic', 'use': ['Feed']}]
    if nested:
        groups.append({'name': 'Outer', 'proxies': ['Dynamic']})
    target = 'Outer' if nested else 'Dynamic'
    main = {'proxies': [{'name': 'Exit', 'dialer-proxy': target}],
            'proxy-groups': groups, 'proxy-providers': {'Feed': {}}}
    candidate = {'name': 'Remote'}
    if cycle:
        candidate['dialer-proxy'] = target
    candidates = {'Feed': [candidate]}
    if cycle:
        with pytest.raises(DialerReferenceError):
            validate_emitted(main, provider_proxies=candidates, require_providers=True)
    else:
        validate_emitted(main, provider_proxies=candidates, require_providers=True)


@pytest.mark.parametrize('target', ['DIRECT', 'Relay'])
def test_static_provider_dialer_and_builtin_allowed(target):
    validate_emitted({'proxies': [{'name': 'Remote', 'dialer-proxy': target}], 'proxy-groups': [{'name': 'Relay', 'proxies': ['DIRECT']}, {'name': 'UnrelatedDynamic', 'use': ['Feed']}], 'proxy-providers': {'Feed': {}}})


def test_no_chain_dynamic_group_legacy_allowed():
    validate_emitted({'proxies': [{'name': 'Remote'}], 'proxy-groups': [{'name': 'Entry', 'use': ['Feed']}], 'proxy-providers': {'Feed': {}}})
