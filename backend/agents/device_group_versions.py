"""Read a pinned Mihomo target's existing version API before publication."""
from backend.agents import mihomo_panel
from backend.agents.device_group_activity import SnapshotManager, _binding
from backend.agents.oxidns_monitor import MonitorError
from backend.common.device_group_capabilities import (
    DeviceGroupMihomoVersionError, mihomo_group_capability,
)


def target_mihomo_capability(manager, agent_id, *, profile_id=None, strict=False, default_profile_id='default'):
    agent = manager.get_agent_by_id(agent_id)
    capability = mihomo_group_capability(None)
    bound = (agent.get('profile_id') or default_profile_id) if agent else None
    if not agent or agent.get('service_type') != 'mihomo' or (profile_id is not None and bound != profile_id):
        capability.update(reason_code='target_changed', message='Mihomo Agent 不存在或已改绑，请刷新发布目标。')
    elif agent.get('enabled') is False:
        capability.update(reason_code='target_disabled', message='Mihomo Agent 已停用，尚未确认目标内核版本。')
    else:
        try:
            response = mihomo_panel.call(SnapshotManager(agent), agent_id, 'version', 'GET', {})
            data = response.get('data')
            version = data.get('version') if isinstance(data, dict) else None
            capability = mihomo_group_capability(version)
            if _binding(manager.get_agent_by_id(agent_id), default_profile_id) != _binding(agent, default_profile_id):
                capability = mihomo_group_capability(None)
                capability.update(reason_code='target_changed', message='检查版本期间 Agent 绑定已变化，请刷新发布目标。')
        except MonitorError as error:
            capability.update(reason_code=error.code,
                              message='无法确认目标 Mihomo 版本，请检查 Agent 与内核控制接口，发布前需重试检查。')
    if strict and capability['supported'] is not True:
        raise DeviceGroupMihomoVersionError(capability)
    return capability
