"""Device-group invariants at every durable profile/document write boundary."""
from copy import deepcopy

import pytest
import yaml

from backend.common.config import get_default_config
from backend.common.config_repository import ProfileInUse, ProfileRepository, ProfileValidationError
from backend.common.device_groups import compile_groups
from backend.common.oxidns_settings import OxidnsSettingsError

DIRECT = 'dg_123456'


@pytest.fixture
def repository(tmp_path):
    return ProfileRepository(tmp_path, get_default_config)


def fields(*, broad=False, proxy=False):
    group = {'id': DIRECT, 'name': '设备组', 'members': ['10.0.0.0/24' if broad else '10.0.0.50/32'],
             'mode': 'proxy' if proxy else 'direct'}
    if proxy:
        group['policy'] = '节点选择'
    return {'device_groups': {'version': 1, 'groups': [
        {'id': 'default', 'name': '默认组', 'members': []}, group]},
        'policy_routing_sources': {'include': ['10.0.0.0/24'], 'exclude_groups': [DIRECT]}}


def write(repository, method, value):
    if method == 'transaction':
        return repository.update_profile_transaction('default', lambda current: current.update(deepcopy(value)))
    if method == 'fields':
        return repository.update_profile_fields('default', value)
    if method == 'save':
        return repository.save_profile('default', {**repository.get_profile('default'), **value})
    if method == 'profile_import':
        return repository.import_profile('default', value)
    if method == 'full_import':
        document = repository.export_all()
        document['profiles']['default'].update(value)
        return repository.import_all(document)
    if method == 'client_import':
        return repository.import_client_profile({'id': 'client', 'name': '导入'}, lambda shared: {'profile': value})
    if method == 'shared_and_profile':
        return repository.update_shared_and_profiles_transaction(lambda shared: None,
            lambda profile_id, before, after: after.update(value), ['default'])
    raise AssertionError(method)


@pytest.mark.parametrize('method', ['transaction', 'fields', 'save', 'profile_import', 'full_import',
                                   'client_import', 'shared_and_profile'])
def test_all_write_paths_normalize_members_and_preserve_independent_sources(repository, method):
    write(repository, method, fields())
    profile = repository.get_profile('client' if method == 'client_import' else 'default')
    assert [row['id'] for row in profile['device_groups']['groups']] == [DIRECT, 'default']
    assert profile['device_groups']['groups'][0]['members'] == ['10.0.0.50']
    assert profile['device_groups']['groups'][0]['dns'] == 'follow'
    assert profile['policy_routing_sources'] == {'include': ['10.0.0.0/24'], 'exclude_groups': [DIRECT]}


@pytest.mark.parametrize('method', ['transaction', 'fields', 'save', 'profile_import', 'full_import',
                                   'client_import', 'shared_and_profile'])
def test_invalid_write_paths_rollback_the_whole_document(repository, method):
    value = fields()
    value['device_groups']['groups'][0]['members'] = ['10.0.0.50']
    before = repository.path.read_bytes()
    with pytest.raises(ProfileValidationError, match='重复'):
        write(repository, method, value)
    assert repository.path.read_bytes() == before


def test_reading_legacy_storage_neither_migrates_nor_backfills_new_fields(repository):
    document = repository.export_all()
    profile = document['profiles']['default']
    profile.pop('device_groups')
    profile.pop('policy_routing_sources')
    profile['oxidns']['device_policies'] = [{'ip': '10.0.0.50', 'name': '设备', 'mode': 'direct'}]
    repository.import_all(document)
    before = repository.path.read_bytes()
    reopened = ProfileRepository(repository.data_dir, get_default_config)
    assert 'device_groups' not in reopened.get_profile('default')
    assert 'policy_routing_sources' not in reopened.get_compat_config('default')
    assert reopened.path.read_bytes() == before


def test_legacy_upgrade_requires_an_explicit_migration_transaction(repository):
    legacy = repository.get_profile('default')['oxidns']
    legacy['device_policies'] = [{'ip': '10.0.0.50', 'mode': 'direct', 'name': ''}]
    repository.update_profile_fields('default', {'oxidns': legacy})
    value = fields()
    value['oxidns'] = {**legacy, 'device_policies': [], 'template_settings': {'device_policies': []}}
    before = repository.path.read_bytes()
    with pytest.raises(ProfileInUse, match='升级为设备组'):
        repository.update_profile_fields('default', value)
    assert repository.path.read_bytes() == before
    repository.update_profile_transaction('default', lambda current: current.update(value),
                                         allow_device_group_migration=True)
    assert repository.get_profile('default')['device_groups'] is not None


@pytest.mark.parametrize('mirror', [False, True])
def test_old_policy_cannot_be_reintroduced_through_whole_oxidns_saves(repository, mirror):
    repository.update_profile_fields('default', fields())
    settings = repository.get_profile('default')['oxidns']
    target = settings.setdefault('template_settings', {}) if mirror else settings
    target['device_policies'] = [{'ip': '10.0.0.50', 'mode': 'direct', 'name': ''}]
    before = repository.path.read_bytes()
    with pytest.raises(ProfileValidationError, match='不能同时'):
        repository.update_profile_fields('default', {'oxidns': settings})
    assert repository.path.read_bytes() == before


def test_delete_cleans_only_removed_group_exclusions_atomically(repository):
    repository.update_profile_fields('default', fields())
    model = repository.get_profile('default')['device_groups']
    model['groups'] = [row for row in model['groups'] if row['id'] == 'default']
    repository.update_profile_fields('default', {'device_groups': model})
    assert repository.get_profile('default')['policy_routing_sources']['exclude_groups'] == []
    before = repository.path.read_bytes()
    with pytest.raises(ProfileValidationError, match='不存在'):
        repository.update_profile_fields('default', {'policy_routing_sources': {'include': [], 'exclude_groups': ['dg_deadbe']}})
    assert repository.path.read_bytes() == before


def enable_proxy(repository):
    repository.update_profile_fields('default', {'proxy_groups': [
        {'id': 'policy', 'name': '节点选择', 'type': 'select', 'manual_nodes': ['DIRECT']}]})
    repository.update_profile_fields('default', fields(proxy=True))


def test_strategy_rename_is_rewritten_at_repository_gate(repository):
    enable_proxy(repository)
    policy = repository.get_profile('default')['proxy_groups'][0]
    policy['name'] = '新策略组'
    repository.update_profile_fields('default', {'proxy_groups': [policy]})
    assert repository.get_profile('default')['device_groups']['groups'][0]['policy'] == '新策略组'


@pytest.mark.parametrize('delete', [True, False])
def test_strategy_delete_or_disable_is_a_dependency_with_no_partial_write(repository, delete):
    enable_proxy(repository)
    policies = repository.get_profile('default')['proxy_groups']
    value = [] if delete else [{**policies[0], 'enabled': False}]
    before = repository.path.read_bytes()
    with pytest.raises(ProfileInUse, match='设备组') as error:
        repository.update_profile_fields('default', {'proxy_groups': value})
    assert error.value.usages[0]['kind'] == 'device_group'
    assert repository.path.read_bytes() == before


def test_clone_export_import_and_full_backup_keep_models_without_cross_profile_mutation(repository):
    repository.update_profile_fields('default', fields())
    repository.clone_profile('default', {'id': 'copy', 'name': '副本'})
    original = repository.get_profile('default')
    copied = repository.get_profile('copy')
    assert copied['device_groups'] == original['device_groups']
    assert copied['policy_routing_sources'] == original['policy_routing_sources']
    copied['device_groups']['groups'][0]['members'] = ['10.0.0.70']
    repository.import_profile('copy', copied)
    assert repository.get_profile('default')['device_groups'] == original['device_groups']
    backup = repository.export_all()
    repository.import_all(backup)
    assert repository.export_profile('copy')['device_groups']['groups'][0]['members'] == ['10.0.0.70']


def test_agent_topology_updates_new_publish_revision_but_not_legacy_profiles(repository):
    repository.create_profile({'id': 'legacy', 'name': '旧空间'})
    repository.update_profile_fields('default', fields(broad=True))
    _, before = repository.get_publish_snapshot('default')
    legacy = repository.config_revision('legacy')
    def add_agent(system):
        system['agents'].append({'id': 'a', 'profile_id': 'default', 'service_type': 'oxidns', 'host': '10.0.0.10'})
    repository.update_system_transaction(add_agent)
    _, after = repository.get_publish_snapshot('default')
    assert after == before + 1
    assert repository.config_revision('legacy') == legacy
    before = after
    repository.update_system_transaction(lambda system: system['agents'][0].update(last_heartbeat='now', cpu=90))
    assert repository.config_revision('default') == before
    repository.update_system_transaction(lambda system: system['agents'][0].update(host='10.0.0.11'))
    assert repository.config_revision('default') == before + 1


def test_system_agent_write_cannot_make_an_explicit_member_an_infrastructure_ip(repository):
    repository.update_profile_fields('default', fields())
    before = repository.path.read_bytes()
    with pytest.raises(ProfileValidationError, match='自身地址'):
        repository.update_system_transaction(lambda system: system['agents'].append(
            {'id': 'a', 'profile_id': 'default', 'service_type': 'mihomo', 'host': '10.0.0.50'}))
    assert repository.path.read_bytes() == before


def enable_native(repository):
    from backend.converters.oxidns import generate_oxidns_config
    value = fields(broad=True)
    cfg = repository.get_compat_config('default')
    cfg.update(value)
    native = generate_oxidns_config(cfg)
    value['oxidns'] = {**cfg['oxidns'], 'config_mode': 'native', 'native_config': native}
    repository.update_profile_fields('default', value)


def test_native_clone_uses_trusted_source_baseline_and_recompiles_new_infrastructure(repository):
    repository.update_system_transaction(lambda system: system['agents'].append(
        {'id': 'a', 'profile_id': 'default', 'service_type': 'oxidns', 'host': '10.0.0.10'}))
    enable_native(repository)
    source = repository.get_profile('default')
    repository.clone_profile('default', {'id': 'copy', 'name': '副本'})
    copied = repository.get_profile('copy')
    assert source['oxidns']['_device_groups_patch'] != copied['oxidns']['_device_groups_patch']
    source_doc = yaml.safe_load(source['oxidns']['native_config'])
    copied_doc = yaml.safe_load(copied['oxidns']['native_config'])
    by_tag = lambda doc: next(p for p in doc['plugins'] if p['tag'] == 'cf_dg_set_123456')['args']['ips']
    assert by_tag(source_doc) != by_tag(copied_doc)
    assert by_tag(copied_doc) == ['10.0.0.0/24']
    assert repository.get_profile('default') == source


def test_native_baseline_forgery_cannot_authorize_an_edited_owned_plugin(repository):
    enable_native(repository)
    settings = repository.get_profile('default')['oxidns']
    document = yaml.safe_load(settings['native_config'])
    owned = next(plugin for plugin in document['plugins'] if plugin['tag'] == 'cf_dg_local')
    owned['args'][0]['exec'] = 'reject 5'
    baseline = next(plugin for plugin in settings['_device_groups_patch'] if plugin['tag'] == 'cf_dg_local')
    baseline['args'][0]['exec'] = 'reject 5'
    settings['native_config'] = yaml.safe_dump(document, sort_keys=False)
    before = repository.path.read_bytes()
    with pytest.raises(OxidnsSettingsError, match='基线不能手动修改'):
        repository.update_profile_fields('default', {'oxidns': settings})
    assert repository.path.read_bytes() == before


def test_native_infrastructure_change_repatches_without_losing_baseline_ownership(repository):
    enable_native(repository)
    before = repository.get_profile('default')
    repository.update_system_transaction(lambda system: system['agents'].append(
        {'id': 'a', 'profile_id': 'default', 'service_type': 'oxidns', 'host': '10.0.0.10'}))
    after = repository.get_profile('default')
    assert after['oxidns']['_device_groups_patch'] != before['oxidns']['_device_groups_patch']
    compiled = compile_groups(repository.get_compat_config('default'))
    document = yaml.safe_load(after['oxidns']['native_config'])
    ips = next(p for p in document['plugins'] if p['tag'] == 'cf_dg_set_123456')['args']['ips']
    assert ips == compiled['effective'][DIRECT]


@pytest.mark.parametrize('operation', ['import', 'gateway_change'])
def test_actual_gateway_subtraction_cannot_exceed_source_limit_after_a_write(repository, operation):
    import ipaddress
    import random
    from backend.common.device_groups import source_networks

    # Real limits: these 272 sparse exclusions leave 4092 ranges. Removing
    # 10.0.0.10 adds 12 more, so validating only the gateway-free set is unsafe.
    rng = random.Random(20261024)
    members = [str(ipaddress.ip_address(int(ipaddress.ip_address('10.0.0.0')) +
                                      rng.randrange(256, 2 ** 24))) for _ in range(272)]
    config = repository.get_compat_config('default')
    config.update(fields())
    config['device_groups']['groups'][1]['members'] = members
    config['policy_routing_sources'] = {'include': ['10.0.0.0/8'], 'exclude_groups': [DIRECT]}
    config['agents'] = [{'id': 'gateway', 'host': '10.0.0.10' if operation == 'import' else '11.0.0.10',
                         'service_type': 'mihomo', 'profile_id': 'default'}]
    config['routers'] = {'items': [{'id': 'ros', 'name': '测试路由器', 'type': 'routeros',
        'endpoint': 'http://10.0.0.1', 'username': 'admin', 'proxy_gateway_agent_id': 'gateway'}]}
    assert len(source_networks(config, None)) == 4092
    if operation == 'gateway_change':
        repository.import_all(config)
        assert len(source_networks(repository.get_compat_config('default'), '11.0.0.10')) == 4092
    before = repository.path.read_bytes()
    with pytest.raises(ProfileValidationError, match='来源展开不能超过 4096'):
        if operation == 'import':
            repository.import_all(config)
        else:
            repository.update_system_transaction(lambda system: system['agents'][0].update(host='10.0.0.10'))
    assert repository.path.read_bytes() == before
