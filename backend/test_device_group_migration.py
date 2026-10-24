"""Explicit upgrade previews, native ownership and exact RouterOS compatibility."""
from copy import deepcopy

import pytest
import yaml

from backend.common.config import get_default_config
from backend.common.config_repository import ProfileRepository
from backend.common.device_group_migration import plan_migration
from backend.common.device_notes import read_notes
from backend.common.device_groups import source_networks
from backend.common.oxidns_device_groups import normalize_transition
from backend.common.oxidns_device_policy import normalize_policies
from backend.converters.oxidns import generate_oxidns_config
from backend.routers.ros_policy import ADDRESSES
from backend.test_ros_policy import GATEWAY, MemoryROS, desired_for, inspect


def legacy(rows, *, native=False):
    cfg = get_default_config()
    cfg.update(id='default', profile_id='default')
    cfg['oxidns']['device_policies'] = normalize_policies(rows)
    cfg['oxidns']['template_settings'] = {'device_policies': deepcopy(cfg['oxidns']['device_policies'])}
    cfg['agents'] = [{'id': 'gateway', 'host': GATEWAY, 'service_type': 'mihomo', 'profile_id': 'default'}]
    cfg['routers'] = {'items': [{'id': 'ros', 'name': '测试路由器', 'type': 'routeros',
        'endpoint': 'http://192.168.0.1', 'proxy_gateway_agent_id': 'gateway'}]}
    if native:
        cfg['oxidns']['native_config'] = generate_oxidns_config(cfg)
        cfg['oxidns']['config_mode'] = 'native'
    return cfg


@pytest.mark.parametrize('rows', [
    [{'ip': '192.168.0.0/24', 'mode': 'smart'}, {'ip': '192.168.0.0/25', 'mode': 'direct'},
     {'ip': '192.168.0.101', 'mode': 'smart'}],
    [{'ip': '192.168.0.0/24', 'mode': 'direct'}, {'ip': '192.168.0.0/25', 'mode': 'smart'},
     {'ip': '192.168.0.0/26', 'mode': 'direct'}],
    [{'ip': '192.168.0.0/24', 'mode': 'smart'}, {'ip': '192.168.0.0/25', 'mode': 'direct'},
     {'ip': '192.168.0.64/26', 'mode': 'smart'}, {'ip': '192.168.0.80/28', 'mode': 'direct'},
     {'ip': '192.168.0.84/30', 'mode': 'smart'}, {'ip': 'fd00::/64', 'mode': 'smart'}],
])
def test_nested_preview_is_pure_deterministic_and_exactly_preserves_sources(rows):
    cfg = legacy(rows)
    before = deepcopy(cfg)
    plan = plan_migration(cfg)
    assert not plan['blockers'], plan
    assert cfg == before
    assert plan['sources_before']['ros'] == plan['sources_after']['ros']
    assert plan['sources_before']['ros'] == source_networks(cfg, GATEWAY)
    assert len(plan['groups']) == 2
    direct, default = plan['groups']
    assert direct['name'] == '直连设备' and direct['mode'] == 'direct'
    assert direct['keep_blocking'] is False and direct['dns'] == 'follow'
    assert default['id'] == 'default' and all(group['name'] != '智能分流' for group in plan['groups'])
    reordered = deepcopy(cfg)
    reordered['oxidns']['device_policies'].reverse()
    reordered['oxidns']['device_policies'][0]['name'] = '改备注'
    assert plan_migration(reordered)['groups'][0]['id'] == direct['id']
    assert {row['ip'] for row in plan['infrastructure_in_direct']} == {'192.168.0.1', GATEWAY}


def test_smart_only_and_stale_inactive_mirror_never_create_a_smart_group():
    cfg = legacy([{'ip': '192.168.0.0/24', 'mode': 'smart'}, {'ip': 'fd00::/64', 'mode': 'smart'}])
    plan = plan_migration(cfg)
    assert not plan['blockers']
    assert plan['groups'] == [{'id': 'default', 'name': '默认组', 'members': []}]
    assert plan['policy_routing_sources'] == {'include': ['192.168.0.0/24', 'fd00::/64'], 'exclude_groups': []}
    cfg['oxidns']['device_policies'] = []
    cfg['oxidns']['template_settings']['device_policies'] = [{'ip': '192.168.0.50', 'mode': 'direct'}]
    plan = plan_migration(cfg)
    assert not plan['blockers']
    assert plan['groups'] == [{'id': 'default', 'name': '默认组', 'members': []}]
    assert plan['config']['oxidns']['template_settings']['device_policies'] == []


def test_notes_keep_the_current_display_semantics_and_both_policy_arrays_clear():
    cfg = legacy([{'ip': '192.168.0.50', 'mode': 'direct', 'name': '策略备注'},
                  {'ip': '192.168.0.51', 'mode': 'smart', 'name': ''}])
    cfg['device_notes'] = {'192.168.0.50': '旧备注', '192.168.0.51': '会被旧空名隐藏', '192.168.0.60': '其他备注'}
    expected = read_notes(cfg)
    cfg['oxidns']['native_config'] = 'inactive custom archive'
    plan = plan_migration(cfg)
    assert not plan['blockers']
    candidate = plan['config']
    assert candidate['device_notes'] == expected == {'192.168.0.50': '策略备注', '192.168.0.60': '其他备注'}
    assert candidate['oxidns']['device_policies'] == candidate['oxidns']['template_settings']['device_policies'] == []
    assert candidate['oxidns']['config_mode'] == 'template'
    assert candidate['oxidns']['native_config'] == 'inactive custom archive'


@pytest.mark.parametrize('member', ['0.0.0.0/0', '127.0.0.1', '198.18.0.50', '192.168.0.1', GATEWAY])
def test_newly_forbidden_legacy_direct_members_block_instead_of_silently_dropping(member):
    cfg = legacy([{'ip': member, 'mode': 'direct'}])
    before = deepcopy(cfg)
    plan = plan_migration(cfg)
    assert plan['blockers'] and cfg == before


def test_no_policy_and_already_migrated_are_explicit_blockers():
    assert plan_migration(legacy([]))['blockers']
    cfg = legacy([{'ip': '192.168.0.50', 'mode': 'direct'}])
    migrated = plan_migration(cfg)['config']
    assert plan_migration(migrated)['blockers']


def test_existing_independent_sources_cannot_be_overwritten_by_upgrade():
    cfg = legacy([{'ip': '192.168.0.50', 'mode': 'direct'}])
    cfg['policy_routing_sources'] = {'include': ['192.168.0.60'], 'exclude_groups': []}
    assert '独立策略路由来源' in str(plan_migration(cfg)['blockers'])


def test_native_candidate_stays_unpatched_and_preview_is_patched_on_a_copy():
    cfg = legacy([{'ip': '192.168.0.50', 'mode': 'direct'}], native=True)
    plan = plan_migration(cfg)
    assert not plan['blockers']
    assert plan['config']['oxidns']['native_config'] == cfg['oxidns']['native_config']
    preview = deepcopy(plan['config'])
    normalize_transition(cfg, preview)
    tags = {plugin['tag'] for plugin in yaml.safe_load(preview['oxidns']['native_config'])['plugins']}
    assert 'cf_dg_dispatch' in tags and 'cf_device_policy' not in tags and 'cf_device_direct' not in tags
    assert plan['config']['oxidns']['native_config'] == cfg['oxidns']['native_config']


def test_modified_native_policy_is_blocked_before_any_upgrade():
    cfg = legacy([{'ip': '192.168.0.50', 'mode': 'direct'}], native=True)
    document = yaml.safe_load(cfg['oxidns']['native_config'])
    plugin = next(plugin for plugin in document['plugins'] if plugin['tag'] == 'cf_device_direct')
    plugin['args'][0]['exec'] = 'reject 5'
    cfg['oxidns']['native_config'] = yaml.safe_dump(document, sort_keys=False)
    assert '手动修改' in str(plan_migration(cfg)['blockers'])


@pytest.mark.parametrize('native', [False, True])
def test_repository_execution_is_one_transaction_and_retains_native_or_template_mode(tmp_path, native):
    cfg = legacy([{'ip': '192.168.0.0/24', 'mode': 'smart'}, {'ip': '192.168.0.0/25', 'mode': 'direct'},
                  {'ip': '192.168.0.101', 'mode': 'smart', 'name': '例外'}], native=native)
    repository = ProfileRepository(tmp_path, get_default_config)
    repository.import_all(cfg)
    config = repository.get_compat_config('default')
    plan = plan_migration(config)
    assert not plan['blockers']
    mode = config['oxidns']['config_mode']
    old_revision = repository.config_revision('default')
    def execute(current):
        for key in ('device_groups', 'policy_routing_sources', 'device_notes', 'oxidns'):
            current[key] = deepcopy(plan['config'][key])
    repository.update_profile_transaction('default', execute, allow_device_group_migration=True)
    saved = repository.get_compat_config('default')
    assert saved['oxidns']['config_mode'] == mode
    assert repository.config_revision('default') == old_revision + 1
    assert saved['oxidns']['device_policies'] == saved['oxidns']['template_settings']['device_policies'] == []
    assert source_networks(saved, GATEWAY) == source_networks(config, GATEWAY)
    if native:
        assert 'cf_dg_dispatch' in saved['oxidns']['native_config']


def test_memory_routeros_source_entries_and_order_survive_migration_with_no_republish_changes():
    cfg = legacy([{'ip': '192.168.0.0/24', 'mode': 'smart'}, {'ip': '192.168.0.0/25', 'mode': 'direct'},
                  {'ip': '192.168.0.101', 'mode': 'smart'}])
    plan = plan_migration(cfg)
    assert not plan['blockers']
    driver = MemoryROS()
    desired = desired_for(driver)
    desired['ros_policy']['sources'] = source_networks(cfg, GATEWAY)
    _, before_plan = inspect(driver, desired)
    assert not before_plan['blockers']
    for stage in before_plan['stages']:
        for operation in stage['ops']:
            driver.apply(operation)
    actual = lambda: [row['address'] for row in driver.rows[ADDRESSES] if row['list'].startswith('CF_Source_')]
    before = actual()
    desired['ros_policy'].update(sources=source_networks(plan['config'], GATEWAY),
                                 previous=deepcopy(driver.ros_policy_manifest), source_kind='device_groups')
    _, after_plan = inspect(driver, desired)
    assert not after_plan['blockers']
    assert not any(stage['ops'] for stage in after_plan['stages'])
    assert actual() == before
