import pytest

from backend.common.device_group_capabilities import (
    DeviceGroupMihomoVersionError, mihomo_group_capability, require_mihomo_group_version,
)


@pytest.mark.parametrize('version', ['v1.19.1', '1.19.32', ' v2.0.0 '])
def test_confirmed_stable_version_supports_group_syntax(version):
    assert require_mihomo_group_version(version)['supported'] is True


@pytest.mark.parametrize('version', ['v1.19.0', '1.18.8', '0.99.99'])
def test_old_core_is_rejected_even_when_unknown_versions_are_allowed(version):
    with pytest.raises(DeviceGroupMihomoVersionError) as error:
        require_mihomo_group_version(version, allow_unknown=True)
    assert error.value.capability['state'] == 'upgrade_required'
    assert 'v1.19.1' in str(error.value)


@pytest.mark.parametrize('version', [None, {}, False, '', 'alpha-abcdef', 'v1.19.1-alpha', 'garbage 9.0.0'])
def test_unknown_versions_may_save_drafts_but_cannot_pass_publish_check(version):
    assert mihomo_group_capability(version)['supported'] is None
    assert require_mihomo_group_version(version, allow_unknown=True)['state'] == 'unknown'
    with pytest.raises(DeviceGroupMihomoVersionError) as error:
        require_mihomo_group_version(version)
    assert error.value.code == 'mihomo_device_groups_version_unknown'
