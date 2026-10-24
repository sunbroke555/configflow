from copy import deepcopy
import json

import pytest

from backend.agents import device_group_activity as service
from backend.agents.oxidns_monitor import MonitorError


class Manager:
    def __init__(self, *agents):
        self.agents = {a['id']: deepcopy(a) for a in agents}

    def get_all_agents(self):
        return deepcopy(list(self.agents.values()))

    def get_agent_by_id(self, identity):
        return deepcopy(self.agents.get(identity))


def agent(identity, **changes):
    return {'id': identity, 'name': identity, 'host': '192.168.60.53', 'port': 8080,
            'profile_id': 'office', 'service_type': 'oxidns', 'status': 'online',
            'version': '1.7.1-go', 'token': 'private-token', **changes}


def config(*agents):
    return {'profile_id': 'office', 'agents': list(agents)}


def sample(*clients):
    return {'recorder': 'queries', 'clients': [{'ip': ip, 'count': count} for ip, count in clients],
            'sample_size': sum(count for _, count in clients), 'sample_limited': False}


@pytest.fixture(autouse=True)
def no_shared_cache():
    service.clear_activity_cache()
    yield
    service.clear_activity_cache()


def test_merges_only_bound_oxidns_agents_with_one_sample_denominator(monkeypatch):
    a, b = agent('a'), agent('b')
    foreign = agent('foreign', profile_id='elsewhere')
    mihomo = agent('mihomo', service_type='mihomo')
    manager = Manager(a, b, foreign, mihomo)
    calls = []

    def clients(pinned, identity, args):
        calls.append(identity)
        assert pinned.get_agent_by_id(identity)['token'] == 'private-token'
        return sample(('192.168.60.1', 2), ('::ffff:192.168.60.20', 1)) if identity == 'a' else sample(('192.168.60.20', 2))

    monkeypatch.setattr(service.oxidns_monitor, 'clients', clients)
    infrastructure = {'addresses': [{'ip': '192.168.60.1', 'kind': 'router'}]}
    result = service.activity(config(a, b, foreign, mihomo), manager, infrastructure=infrastructure)
    assert sorted(calls) == ['a', 'b']
    assert result['sample_size'] == 5 and result['partial'] is False
    assert result['clients'][0] == {'ip': '192.168.60.20', 'count': 3, 'agent_ids': ['a', 'b']}
    assert result['infrastructure_samples'] == [{'ip': '192.168.60.1', 'kind': 'router', 'count': 2, 'share': .4}]
    assert 'private-token' not in json.dumps(result)
    assert 'last_seen' not in json.dumps(result)


@pytest.mark.parametrize('code,state', [('UPGRADE_REQUIRED', 'upgrade_required'),
                                       ('RECORDER_NOT_CONFIGURED', 'recorder_disabled'),
                                       ('CORE_UNREACHABLE', 'unavailable')])
def test_individual_failure_keeps_other_agent_samples(monkeypatch, code, state):
    a, b = agent('a'), agent('b')
    def clients(_manager, identity, _args):
        if identity == 'a':
            raise MonitorError(code)
        return sample(('192.168.60.20', 3))
    monkeypatch.setattr(service.oxidns_monitor, 'clients', clients)
    result = service.activity(config(a, b), Manager(a, b))
    assert result['partial'] and result['sample_size'] == 3
    assert result['agents'][0]['state'] == state


def test_short_cache_isolated_by_binding_and_returns_copies(monkeypatch):
    a = agent('a')
    manager = Manager(a)
    calls = []
    monkeypatch.setattr(service.oxidns_monitor, 'clients', lambda *args: calls.append(1) or sample(('192.168.60.20', 1)))
    first = service.activity(config(a), manager)
    first['clients'].clear()
    assert service.activity(config(a), manager)['clients']
    assert len(calls) == 1
    manager.agents['a']['profile_id'] = 'elsewhere'
    result = service.activity(config(a), manager)
    assert result['sample_size'] == 0 and result['partial']
    assert len(calls) == 1


def test_rebind_during_collection_discards_data_and_pins_network_target(monkeypatch):
    a = agent('a')
    manager = Manager(a)
    def clients(pinned, identity, _args):
        manager.agents[identity].update(profile_id='elsewhere', host='192.0.2.99')
        assert pinned.get_agent_by_id(identity)['host'] == a['host']
        return sample(('192.168.60.20', 99))
    monkeypatch.setattr(service.oxidns_monitor, 'clients', clients)
    result = service.activity(config(a), manager)
    assert result['partial'] and result['clients'] == [] and result['sample_size'] == 0
    assert result['agents'][0]['state'] == 'unavailable'


def test_no_valid_sample_does_not_fabricate_infrastructure_percentage(monkeypatch):
    a = agent('a', status='offline')
    monkeypatch.setattr(service.oxidns_monitor, 'clients', lambda *_: pytest.fail('offline target contacted'))
    result = service.activity(config(a), Manager(a), infrastructure={
        'addresses': [{'ip': '192.168.60.1', 'kind': 'router'}]})
    assert result['agents'][0]['state'] == 'offline'
    assert result['infrastructure_samples'] == []


def test_agent_cap_is_explicit_and_sample_limit_propagates(monkeypatch):
    agents = [agent(str(i)) for i in range(3)]
    monkeypatch.setattr(service, 'MAX_AGENTS', 2)
    def clients(*_):
        return {**sample(('192.168.60.20', 10000)), 'sample_limited': True}
    monkeypatch.setattr(service.oxidns_monitor, 'clients', clients)
    result = service.activity(config(*agents), Manager(*agents))
    assert result['sample_limited'] and result['partial'] and result['sample_size'] == 20000
    assert result['agents'][-1]['state'] == 'unavailable'
