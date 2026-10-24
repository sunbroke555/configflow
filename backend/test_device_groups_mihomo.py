"""Real converter coverage for source dispatch, safe copying and base coexistence."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml

from backend.common.device_group_capabilities import DeviceGroupMihomoVersionError
from backend.common.device_groups import compile_groups
from backend.converters.mihomo import DeviceGroupGenerationError, generate_mihomo_config


FIXTURES = Path(__file__).parent / 'fixtures' / 'device_group_compat_golden'


def config_with_groups():
    config = json.loads((FIXTURES / 'rich-mihomo.json').read_text(encoding='utf-8'))
    config['device_groups'] = {'version': 1, 'groups': [
        {'id': 'dg_aaaaaa', 'name': '直连设备', 'members': ['192.168.60.10'], 'mode': 'direct'},
        {'id': 'dg_bbbbbb', 'name': '保留拦截', 'members': ['192.168.60.20'], 'mode': 'direct', 'keep_blocking': True},
        {'id': 'dg_cccccc', 'name': '全部代理', 'members': ['192.168.60.30'], 'mode': 'proxy',
         'policy': '节点选择', 'keep_blocking': True},
        {'id': 'dg_dddddd', 'name': '跟随全局', 'members': ['192.168.60.40'], 'mode': 'follow'},
        {'id': 'default', 'name': '默认组', 'members': ['192.168.60.50']},
    ]}
    return config


def emit(config, **kwargs):
    return yaml.safe_load(generate_mihomo_config(config, preflight_providers=False, **kwargs))


def test_top_dispatch_and_terminating_segments_preserve_global_rules_and_user_subrules():
    config = config_with_groups()
    before = deepcopy(config)
    plain = deepcopy(config)
    plain.pop('device_groups')
    baseline = emit(plain)
    document = emit(config)
    assert document['rules'][:3] == [
        f'SUB-RULE,(RULE-SET,cf_dg_set_{suffix},src),cf_dg_seg_{suffix}'
        for suffix in ('aaaaaa', 'bbbbbb', 'cccccc')]
    assert document['rules'][3:] == baseline['rules']
    assert document['sub-rules']['lan-rules'] == ['MATCH,DIRECT']
    assert document['sub-rules']['cf_dg_seg_aaaaaa'] == ['MATCH,DIRECT']
    blocking = [
        'DOMAIN,refused.example.test,REJECT', 'DOMAIN,missing.example.test,REJECT',
        'DOMAIN,empty.example.test,REJECT-DROP', 'DOMAIN-KEYWORD,ads,REJECT',
        'RULE-SET,广告规则,REJECT',
    ]
    assert document['sub-rules']['cf_dg_seg_bbbbbb'] == blocking + ['MATCH,DIRECT']
    assert document['sub-rules']['cf_dg_seg_cccccc'] == blocking + ['MATCH,节点选择']
    assert document['rule-providers']['cf_dg_set_cccccc'] == {
        'type': 'inline', 'behavior': 'ipcidr', 'payload': ['192.168.60.30/32']}
    assert 'cf_dg_seg_dddddd' not in document['sub-rules']
    assert config == before


def test_infrastructure_and_more_specific_members_are_removed_before_inline_source_matching():
    config = config_with_groups()
    config['device_groups']['groups'][0]['members'] = ['192.168.60.0/24']
    config['agents'] = [{'id': 'core', 'host': '192.168.60.1', 'service_type': 'mihomo',
                         'profile_id': config['profile_id']}]
    compiled = compile_groups(config)
    document = emit(config)
    assert document['rule-providers']['cf_dg_set_aaaaaa']['payload'] == compiled['effective']['dg_aaaaaa']
    assert document['rule-providers']['cf_dg_set_aaaaaa']['payload'] != ['192.168.60.0/24']


@pytest.mark.parametrize('field,prefix', [('rule-providers', 'cf_dg_'), ('sub-rules', 'cf_scope_')])
def test_reserved_prefix_is_checked_on_original_base_before_managed_providers_replace_it(field, prefix):
    config = config_with_groups()
    base = yaml.safe_load(config['mihomo']['custom_config'])
    base[field] = {prefix + 'user': {} if field == 'rule-providers' else ['MATCH,DIRECT']}
    config['mihomo']['custom_config'] = yaml.safe_dump(base)
    with pytest.raises(DeviceGroupGenerationError, match='保留前缀'):
        emit(config)


def test_managed_ruleset_names_also_respect_the_reserved_namespace():
    config = config_with_groups()
    next(r for r in config['rule_configs'] if r['id'] == 'rs_ads')['name'] = 'cf_dg_ads'
    with pytest.raises(DeviceGroupGenerationError, match='保留前缀'):
        emit(config)


def test_global_match_reject_is_never_copied_into_any_group():
    config = config_with_groups()
    next(r for r in config['rule_configs'] if r['id'] == 'r_match')['policy'] = 'REJECT'
    document = emit(config)
    assert 'MATCH,REJECT' in document['rules']
    for name, rows in document['sub-rules'].items():
        if name.startswith('cf_dg_'):
            assert 'MATCH,REJECT' not in rows
            assert not any('after.example.test' in row for row in rows)


def test_version_checks_only_apply_when_a_nonempty_group_will_dispatch():
    config = config_with_groups()
    with pytest.raises(DeviceGroupMihomoVersionError):
        emit(config, target_core_version='v1.19.0')
    emit(config, target_core_version='v1.19.1')
    emit(config, target_core_version='unknown')
    config['device_groups']['groups'] = [config['device_groups']['groups'][-1]]
    emit(config, target_core_version='v1.18.8')


@pytest.mark.parametrize('field', ['rule-providers', 'sub-rules'])
def test_null_base_mapping_can_be_created_for_device_group_resources(field):
    config = config_with_groups()
    base = yaml.safe_load(config['mihomo']['custom_config'])
    base[field] = None
    config['mihomo']['custom_config'] = yaml.safe_dump(base)
    assert isinstance(emit(config)[field], dict)


@pytest.mark.parametrize('variable', ['MIHOMO_BINARY', 'MIHOMO_MIN_BINARY'])
def test_real_mihomo_accepts_actual_generated_device_groups(tmp_path, variable):
    binary = os.environ.get(variable)
    if not binary:
        pytest.skip(f'{variable} is required for real-core validation')
    config = config_with_groups()
    path = tmp_path / 'config.yaml'
    path.write_text(generate_mihomo_config(config, preflight_providers=False), encoding='utf-8')
    result = subprocess.run([binary, '-t', '-d', str(tmp_path), '-f', str(path)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('exit_kind', ['node', 'group'])
def test_all_proxy_accepts_logical_named_chain_after_lowering_its_exit(exit_kind):
    from backend.test_proxy_chains import profile
    config = profile()
    if exit_kind == 'group':
        config['proxy_groups'].append({'id': 'exit', 'name': '落地选择', 'type': 'select', 'manual_nodes': ['b']})
        config['proxy_groups'][0]['chain']['exit'] = {'type': 'group', 'id': 'exit'}
    config['device_groups'] = {'version': 1, 'groups': [
        {'id': 'dg_123456', 'name': '代理设备', 'members': ['192.168.60.50'], 'mode': 'proxy', 'policy': 'Chain'},
        {'id': 'default', 'name': '默认组', 'members': []}]}
    document = emit(config)
    assert document['sub-rules']['cf_dg_seg_123456'] == ['MATCH,Chain']
    emitted = document['proxies'] if exit_kind == 'node' else document['proxy-groups']
    assert 'Chain' in {row['name'] for row in emitted}
    assert config['proxy_groups'][0]['type'] == 'chain'
    # Compiled standalone nodes do not become selectable group policies.
    config['device_groups']['groups'][0]['policy'] = 'A'
    with pytest.raises(ValueError, match='已启用的策略组'):
        emit(config)
