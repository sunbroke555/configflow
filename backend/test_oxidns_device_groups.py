from copy import deepcopy

import pytest
import yaml

from backend.common.oxidns_device_groups import (BASELINE, HOOK, build_plugins, normalize_transition,
                                                  patch_document, render_document)
from backend.common.oxidns_settings import OxidnsSettingsError
from backend.common.rule_dns import OxidnsRoutingLocked
from backend.converters.oxidns import generate_oxidns_config


def config(*, blocking=True):
    def rule(identity, domain, policy='REJECT', kind='DOMAIN'):
        return {'id': identity, 'itemType': 'rule', 'rule_type': kind, 'value': domain,
                'policy': policy, 'enabled': True}
    return {'proxy_groups': [{'id': 'pg1', 'name': '代理出口', 'type': 'select', 'enabled': True,
                               'manual_nodes': ['DIRECT']}],
            'rule_configs': [rule('early', 'overlap.test', 'DIRECT'),
                             rule('deny', 'overlap.test'), rule('nx', 'nx.test'),
                             rule('nodata', 'nodata.test'), rule('bh', 'bh.test'),
                             rule('stop', '', 'DIRECT', 'MATCH'), rule('late', 'after.test')],
            'oxidns': {'aggregate_rules': True, 'direct_rules': ['early'],
                       'rule_actions': {'deny': {'action': 'refused'}, 'nx': {'action': 'nxdomain'},
                                        'nodata': {'action': 'nodata'}, 'bh': {'action': 'blackhole'},
                                        'late': {'action': 'blackhole'}},
                       'custom_matches': [{'matches': ['qname full:head.test'], 'exec': '$lazy_cache'}],
                       'custom_match_position': 'head'},
            'device_groups': {'version': 1, 'groups': [
                {'id': 'dg_a123456789abcdef', 'name': '直连设备', 'members': ['192.168.60.0/24'],
                 'mode': 'direct', 'keep_blocking': blocking, 'dns': 'follow'},
                {'id': 'dg_b123456789abcdef', 'name': '代理设备', 'members': ['192.168.61.0/24'],
                 'mode': 'proxy', 'keep_blocking': blocking, 'policy': '代理出口'},
                {'id': 'default', 'name': '默认组', 'members': ['192.168.60.100']}]}}


def plugins(document):
    return {plugin['tag']: plugin for plugin in document['plugins']}


def native(source=None):
    value = deepcopy(source or config(blocking=False))
    document = yaml.safe_load(generate_oxidns_config(value))
    value['oxidns'].update(config_mode='native', native_config=yaml.safe_dump(document, sort_keys=False))
    normalize_transition({}, value)
    return value


def test_groups_dispatch_before_head_cache_and_terminate_their_paths():
    doc = yaml.safe_load(generate_oxidns_config(config()))
    found = plugins(doc)
    args = found['sequence_main']['args']
    assert args.index(HOOK) < next(i for i, step in enumerate(args) if step['exec'] == '$lazy_cache')
    direct = found['cf_dg_seg_a123456789abcdef']['args']
    proxy = found['cf_dg_seg_b123456789abcdef']['args']
    assert direct[-1] == {'exec': 'goto cf_dg_local'}
    assert proxy[-2:] == [{'exec': 'jump proxy_dns_seq'}, {'exec': 'accept'}]
    assert [row['exec'] for row in direct[:-1]] == ['reject', 'reject 3', 'reject 0', '$cf_dns_blackhole']
    assert found['cf_dg_local']['args'] == [
        {'exec': '$forward_local'}, {'matches': ['!has_resp'], 'exec': 'reject 3'}, {'exec': 'accept'}]
    assert all('ros_' not in repr(step) and 'cache' not in repr(step) for step in found['cf_dg_local']['args'])
    # The original aggregate contains both blackhole rules. Only the eligible
    # half may enter a group; copying its tag would leak after.test into groups.
    block_tag = direct[-2]['matches'][0].removeprefix('qname $')
    assert block_tag.startswith('cf_dg_block_')
    assert found[block_tag]['args'] == {'exps': ['full:bh.test']}
    assert found['rules_domain_5']['args']['exps'] == ['full:bh.test', 'full:after.test']
    assert '192.168.60.100/32' not in found['cf_dg_set_a123456789abcdef']['args']['ips']


def test_disabled_blocking_and_direct_full_explicit_termination():
    source = config(blocking=False)
    source['device_groups']['groups'][0]['dns'] = 'direct_full'
    source['oxidns']['custom_matches'] = []
    found = plugins(yaml.safe_load(generate_oxidns_config(source)))
    assert found['cf_dg_seg_a123456789abcdef']['args'] == [{'exec': 'jump china_dns'}, {'exec': 'accept'}]
    assert found['cf_dg_seg_b123456789abcdef']['args'] == [{'exec': 'jump proxy_dns_seq'}, {'exec': 'accept'}]
    assert 'cf_dg_local' not in found


@pytest.mark.parametrize('target', ['remote_dns', 'remote-wrapper'])
def test_native_cross_path_shared_cache_is_rejected_before_generation_or_save(target):
    source = native()
    updated = deepcopy(source)
    doc = yaml.safe_load(updated['oxidns']['native_config'])
    found = plugins(doc)
    if target == 'remote_dns':
        found[target]['args'].insert(0, {'exec': '$lazy_cache'})
    else:
        doc['plugins'].append({'tag': 'remote-wrapper', 'type': 'sequence', 'args': [{'exec': '$lazy_cache'}]})
        found['remote_dns']['args'].insert(0, {'exec': 'jump remote-wrapper'})
    updated['oxidns']['native_config'] = yaml.safe_dump(doc)
    for operation in (lambda: normalize_transition(source, deepcopy(updated)), lambda: generate_oxidns_config(updated)):
        with pytest.raises(OxidnsSettingsError, match='共用缓存'):
            operation()


def test_direct_full_cannot_share_its_cache_with_global_custom_head():
    source = config(blocking=False)
    source['device_groups']['groups'][0]['dns'] = 'direct_full'
    with pytest.raises(OxidnsSettingsError, match='共用缓存'):
        generate_oxidns_config(source)
    with pytest.raises(OxidnsSettingsError, match='缓存隔离'):
        normalize_transition({}, source)


def test_native_private_remote_cache_is_supported():
    source = native()
    updated = deepcopy(source)
    doc = yaml.safe_load(updated['oxidns']['native_config'])
    doc['plugins'].append({'tag': 'remote_cache', 'type': 'cache', 'args': {'size': 1024}})
    plugins(doc)['remote_dns']['args'].insert(0, {'exec': '$remote_cache'})
    updated['oxidns']['native_config'] = yaml.safe_dump(doc)
    normalize_transition(source, updated)
    assert 'remote_cache' in generate_oxidns_config(updated)


def test_separate_native_caches_cannot_load_the_same_dump_file():
    source = native()
    updated = deepcopy(source)
    doc = yaml.safe_load(updated['oxidns']['native_config'])
    plugins(doc)['lazy_cache']['args']['dump_file'] = './shared.cache'
    doc['plugins'].append({'tag': 'remote_cache', 'type': 'cache',
                           'args': {'size': 1024, 'dump_file': 'shared.cache'}})
    plugins(doc)['remote_dns']['args'].insert(0, {'exec': '$remote_cache'})
    updated['oxidns']['native_config'] = yaml.safe_dump(doc)
    with pytest.raises(OxidnsSettingsError, match='共用缓存'):
        normalize_transition(source, updated)


def test_template_full_direct_shared_dump_is_rejected_at_write_boundary():
    source = config(blocking=False)
    source['device_groups']['groups'][0]['dns'] = 'direct_full'
    source['oxidns'].update(custom_matches=[], cache_dump_enabled=True,
        custom_config=yaml.safe_dump({'plugins': [{'tag': 'another_cache', 'type': 'cache',
            'args': {'size': 1024, 'dump_file': 'state/cache.dump'}}]}))
    with pytest.raises(OxidnsSettingsError, match='共用缓存'):
        normalize_transition({}, source)


def test_native_patch_is_idempotent_and_group_deletion_removes_owned_resources():
    source = native()
    text = generate_oxidns_config(source)
    assert text == generate_oxidns_config(source)
    updated = deepcopy(source)
    updated['device_groups']['groups'] = [updated['device_groups']['groups'][-1]]
    normalize_transition(source, updated)
    assert BASELINE not in updated['oxidns']
    assert 'cf_dg_' not in generate_oxidns_config(updated)


def test_native_last_segment_removal_cannot_leave_hidden_include_references():
    source = native()
    updated = deepcopy(source)
    updated['device_groups']['groups'] = [updated['device_groups']['groups'][-1]]
    document = yaml.safe_load(updated['oxidns']['native_config'])
    document['include'] = ['extra.yaml']
    updated['oxidns']['native_config'] = yaml.safe_dump(document)
    with pytest.raises(OxidnsSettingsError, match='include'):
        normalize_transition(source, updated)


def test_default_only_native_include_does_not_require_a_group_patch():
    source = {'device_groups': {'version': 1, 'groups': [{'id': 'default', 'name': '默认组', 'members': []}]},
              'oxidns': {'config_mode': 'native', 'native_config': 'include: [extra.yaml]\nplugins: []\n'}}
    before = deepcopy(source)
    normalize_transition({}, source)
    assert source == before
    # Rendering may receive an already-expanded native document.
    document = {'plugins': []}
    assert render_document(document, source) is document


@pytest.mark.parametrize('mutation', ['edited', 'moved', 'extra-reference', 'metadata', 'missing-target', 'wrong-target', 'cycle'])
def test_native_altered_owned_or_required_chains_are_rejected(mutation):
    source = native()
    updated = deepcopy(source)
    doc = yaml.safe_load(updated['oxidns']['native_config'])
    found = plugins(doc)
    if mutation == 'edited':
        found['cf_dg_seg_b123456789abcdef']['args'].append({'exec': 'return'})
    elif mutation == 'moved':
        args = found['sequence_main']['args']
        args.remove(HOOK)
        args.insert(-1, HOOK)
    elif mutation == 'extra-reference':
        found['remote_dns']['args'].append({'exec': 'jump cf_dg_dispatch'})
    elif mutation == 'metadata':
        updated['oxidns'][BASELINE][0]['tag'] = 'manual_baseline'
    elif mutation == 'missing-target':
        doc['plugins'] = [p for p in doc['plugins'] if p['tag'] != 'proxy_dns_seq']
    elif mutation == 'wrong-target':
        found['proxy_dns_seq'].update(type='cache', args={'size': 1024})
    else:
        found['remote_dns']['args'].append({'exec': 'jump sequence_main'})
    updated['oxidns']['native_config'] = yaml.safe_dump(doc, sort_keys=False)
    with pytest.raises(OxidnsSettingsError):
        normalize_transition(source, updated)
    assert source['oxidns']['native_config'] != updated['oxidns']['native_config'] or mutation == 'metadata'


@pytest.mark.parametrize('target', ['hosts', 'adguard'])
def test_known_prefix_name_cannot_hide_a_cache(target):
    source = config(blocking=False)
    source['oxidns'].update(custom_hosts='full:hosts.test 192.0.2.9',
                            adguard={'enabled': True, 'content': '||blocked.test^'})
    source = native(source)
    updated = deepcopy(source)
    doc = yaml.safe_load(updated['oxidns']['native_config'])
    found = plugins(doc)
    found['custom_hosts' if target == 'hosts' else 'adguard_block'].update(type='cache', args={'size': 1024})
    updated['oxidns']['native_config'] = yaml.safe_dump(doc, sort_keys=False)
    with pytest.raises(OxidnsSettingsError, match='缓存隔离'):
        normalize_transition(source, updated)


def test_native_blocking_rejected_in_generation_write_and_common_editor():
    from backend.common.oxidns_common import apply_common_settings
    source = native()
    source['device_groups']['groups'][0]['keep_blocking'] = True
    for operation in (lambda: generate_oxidns_config(source), lambda: normalize_transition({}, source),
                      lambda: apply_common_settings(config()['oxidns'], {'cache_enabled': False}, config())):
        with pytest.raises(OxidnsRoutingLocked, match='完整参数'):
            operation()


def test_namespace_collision_is_not_claimed_on_import():
    source = config(blocking=False)
    raw = deepcopy(source)
    raw.pop('device_groups')
    doc = yaml.safe_load(generate_oxidns_config(raw))
    doc['plugins'].append({'tag': 'cf_dg_dispatch', 'type': 'sequence', 'args': [{'exec': 'reject'}]})
    source['oxidns'].update(config_mode='native', native_config=yaml.safe_dump(doc, sort_keys=False),
                            **{BASELINE: [doc['plugins'][-1]]})
    with pytest.raises(OxidnsSettingsError, match='命名冲突'):
        normalize_transition({}, source)


def test_source_group_changes_recompile_native_using_trusted_baseline():
    source = native()
    updated = deepcopy(source)
    updated['device_groups']['groups'][0]['members'] = ['192.168.90.0/24']
    normalize_transition(source, updated)
    found = plugins(yaml.safe_load(generate_oxidns_config(updated)))
    assert found['cf_dg_set_a123456789abcdef']['args']['ips'] == ['192.168.90.0/24']
    assert updated['oxidns'][BASELINE] != source['oxidns'][BASELINE]


def test_raw_patch_rejects_duplicate_tags_instead_of_losing_the_main_chain():
    source = config(blocking=False)
    source.pop('device_groups')
    doc = yaml.safe_load(generate_oxidns_config(source))
    doc['plugins'].append(deepcopy(doc['plugins'][0]))
    with pytest.raises(OxidnsSettingsError):
        patch_document(doc, [], [{'tag': 'cf_dg_dispatch', 'type': 'sequence', 'args': [{'exec': 'return'}]}])
