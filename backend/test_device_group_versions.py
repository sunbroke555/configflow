import pytest

from backend.agents import device_group_versions as service
from backend.common.device_group_capabilities import DeviceGroupMihomoVersionError
from backend.test_device_group_activity import Manager, agent


@pytest.mark.parametrize('version,state', [('v1.19.1', 'supported'), ('v1.19.0', 'upgrade_required'),
                                          ('alpha-unknown', 'unknown')])
def test_live_version_capability_uses_existing_api(monkeypatch, version, state):
    a = agent('a', service_type='mihomo')
    monkeypatch.setattr(service.mihomo_panel, 'call', lambda *args: {'data': {'version': version}})
    result = service.target_mihomo_capability(Manager(a), 'a', profile_id='office')
    assert result['state'] == state


def test_rebinding_during_version_read_cannot_approve_a_different_target(monkeypatch):
    a = agent('a', service_type='mihomo')
    manager = Manager(a)
    def call(pinned, *_args):
        manager.agents['a']['profile_id'] = 'elsewhere'
        assert pinned.get_agent_by_id('a')['profile_id'] == 'office'
        return {'data': {'version': 'v1.19.32'}}
    monkeypatch.setattr(service.mihomo_panel, 'call', call)
    with pytest.raises(DeviceGroupMihomoVersionError) as error:
        service.target_mihomo_capability(manager, 'a', profile_id='office', strict=True)
    assert error.value.capability['reason_code'] == 'target_changed'


def test_wrong_profile_is_not_contacted(monkeypatch):
    a = agent('a', service_type='mihomo', profile_id='elsewhere')
    monkeypatch.setattr(service.mihomo_panel, 'call', lambda *_: pytest.fail('wrong profile contacted'))
    assert service.target_mihomo_capability(Manager(a), 'a', profile_id='office')['state'] == 'unknown'
