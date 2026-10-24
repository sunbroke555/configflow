"""Pure target-core checks; fetching Agent state belongs to the publish layer."""
import re


MIN_MIHOMO_GROUP_VERSION = '1.19.1'
_STABLE_VERSION = re.compile(r'v?(\d+)\.(\d+)\.(\d+)')


class DeviceGroupMihomoVersionError(ValueError):
    def __init__(self, capability):
        super().__init__(capability['message'])
        self.capability = capability
        self.code = ('mihomo_device_groups_upgrade_required' if capability['state'] == 'upgrade_required'
                     else 'mihomo_device_groups_version_unknown')


def mihomo_group_capability(version):
    """Only confirmed stable versions satisfy inline-source SUB-RULE support.

    This is the device-group syntax minimum, not a promise that every unrelated
    rule feature is supported on that historical core.
    """
    match = _STABLE_VERSION.fullmatch(version.strip()) if isinstance(version, str) else None
    state, supported = 'unknown', None
    if match:
        supported = tuple(map(int, match.groups())) >= (1, 19, 1)
        state = 'supported' if supported else 'upgrade_required'
    messages = {
        'supported': '内核支持设备组来源分发。',
        'upgrade_required': f'设备组要求 Mihomo 稳定版 v{MIN_MIHOMO_GROUP_VERSION} 或更新，请先升级目标内核。',
        'unknown': f'尚未确认目标 Mihomo 版本；发布设备组前请确认稳定版 v{MIN_MIHOMO_GROUP_VERSION} 或更新。',
    }
    return {'minimum_version': MIN_MIHOMO_GROUP_VERSION, 'version': version if isinstance(version, str) else '',
            'state': state, 'supported': supported, 'message': messages[state]}


def require_mihomo_group_version(version, *, allow_unknown=False):
    capability = mihomo_group_capability(version)
    if capability['supported'] is False or capability['supported'] is None and not allow_unknown:
        raise DeviceGroupMihomoVersionError(capability)
    return capability
