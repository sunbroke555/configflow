"""Common routing edits operate on actual native state without regenerating it."""
from copy import deepcopy

import pytest
import yaml

from backend.common.oxidns_common_routing import (
    apply_routing, read_routing, resource_downloads,
)
from backend.common.oxidns_editor import prepare_editor
from backend.common.oxidns_settings import OxidnsSettingsError
from backend.converters.oxidns import _rule_path, _tag


def fixture_config():
    return {'system_config': {'server_domain': 'http://config.test', 'rule_proxy_token': 'test-only'},
            'rule_configs': [
                {'id': 'a', 'itemType': 'rule', 'rule_type': 'DOMAIN', 'value': 'a.test'},
                {'id': 'b', 'itemType': 'rule', 'rule_type': 'DOMAIN-SUFFIX', 'value': 'b.test'},
                {'id': 'c', 'itemType': 'rule', 'rule_type': 'DOMAIN', 'value': 'c.test'},
                {'id': 'ip', 'itemType': 'rule', 'rule_type': 'IP-CIDR', 'value': '192.0.2.0/24'},
                {'id': 'rs', 'itemType': 'ruleset', 'behavior': 'domain', 'url': 'https://example.test/list.txt'},
                {'id': 'rs2', 'itemType': 'ruleset', 'behavior': 'domain', 'url': 'https://example.test/other.txt'},
                {'id': 'mixed', 'itemType': 'ruleset', 'behavior': 'classical', 'url': 'https://example.test/mixed.txt'},
            ]}


def prepared(config=None, **fields):
    config = config or fixture_config()
    settings = prepare_editor({'direct_rules': ['a', 'b', 'ip'], 'proxy_rules': ['c'],
                               'direct_rulesets': ['rs'], **fields}, config)
    return yaml.safe_load(settings['native_config']), settings, config


def plugin(document, tag):
    return next(item for item in document['plugins'] if item['tag'] == tag)


def project(document, settings, config):
    return read_routing(document, settings, config)['values']


def test_projection_reads_native_rules_and_routing_instead_of_stale_metadata():
    document, settings, config = prepared()
    settings.update(direct_rules=[], proxy_rules=['a', 'b'], aggregate_rules=False,
                    default_forward='forward_local', custom_matches=[{'exec': 'stale'}])
    settings['template_settings'].update(direct_rules=[], proxy_rules=['a'])
    result = read_routing(document, settings, config)
    assert result['values']['direct_rules'] == ['a', 'b', 'ip']
    assert result['values']['proxy_rules'] == ['c']
    assert result['values']['direct_rulesets'] == ['rs']
    assert result['values']['aggregate_rules'] is True
    assert result['values']['default_forward'] == 'forward_remote'
    assert result['values']['custom_matches'] == []
    assert all(cap['editable'] for key, cap in result['capabilities'].items() if key != 'custom_match_position')
    assert result['values']['custom_match_position'] is None
    assert result['capabilities']['custom_match_position']['editable'] is False


def test_rule_selection_atomic_update_keeps_manual_fields_plugins_files_and_order():
    document, settings, config = prepared()
    document['future'] = [False, 0, None]
    extra = {'tag': 'manual', 'type': 'future', 'args': {'keep': [False, 0, None]}}
    document['plugins'].insert(3, extra)
    plugin(document, 'forward_local')['args']['future'] = False
    settings['additional_files'] = [{'path': 'manual.txt', 'content': 'keep', 'encoding': 'text'}]
    before, saved = deepcopy(document), deepcopy(settings)
    updated, sources = apply_routing(document, settings, config, {
        'direct_rules': ['b', 'ip'], 'proxy_rules': ['a', 'c'], 'proxy_rulesets': ['rs2'],
    })
    actual = project(updated, sources, config)
    assert actual['direct_rules'] == ['b', 'ip']
    assert actual['proxy_rules'] == ['a', 'c']
    assert actual['proxy_rulesets'] == ['rs2']
    assert updated['future'] == document['future']
    assert plugin(updated, 'manual') == extra
    assert plugin(updated, 'forward_local') == plugin(document, 'forward_local')
    old_unowned = [item for item in before['plugins'] if not item['tag'].startswith('rules_') and item['tag'] != 'sequence_main']
    new_unowned = [item for item in updated['plugins'] if not item['tag'].startswith('rules_') and item['tag'] != 'sequence_main']
    assert old_unowned == new_unowned
    assert sources['additional_files'] == settings['additional_files']
    assert document == before
    assert settings == saved


def test_aggregate_toggle_changes_only_owned_rule_grouping_and_is_inferred_again():
    document, settings, config = prepared()
    updated, sources = apply_routing(document, settings, config, {'aggregate_rules': False})
    assert project(updated, sources, config)['aggregate_rules'] is False
    assert project(updated, sources, config)['direct_rules'] == ['a', 'b', 'ip']
    restored, sources = apply_routing(updated, sources, config, {'aggregate_rules': True})
    assert project(restored, sources, config)['aggregate_rules'] is True
    assert plugin(restored, 'forward_local') == plugin(document, 'forward_local')


def test_default_change_updates_ip_fallback_without_touching_custom_middle():
    document, settings, config = prepared()
    main = plugin(document, 'sequence_main')['args']
    # An unusual custom clause prevents projection of rule selection, but does
    # not require rewriting it when changing the final fallback destination.
    main.insert(4, {'exec': 'custom_future', 'options': [False, 0]})
    before = deepcopy(document)
    updated, _ = apply_routing(document, settings, config, {'default_forward': 'forward_local'})
    new_main = plugin(updated, 'sequence_main')['args']
    assert new_main[:-2] == main[:-2]
    assert new_main[-2:] == [{'matches': ['has_resp'], 'exec': 'goto default_direct_ip'}, {'exec': 'goto china_dns'}]
    assert document == before


def test_custom_match_extension_fields_and_position_are_actual_and_roundtrip():
    document, settings, config = prepared(custom_matches=[{'matches': ['qname full:custom.test'], 'exec': 'reject 0'}])
    main = plugin(document, 'sequence_main')['args']
    custom = next(item for item in main if item.get('matches') == ['qname full:custom.test'])
    custom.update(future={'off': False, 'zero': 0, 'nil': None})
    actual = project(document, settings, config)
    assert actual['custom_matches'] == [custom]
    assert actual['custom_match_position'] == 'tail'
    updated, _ = apply_routing(document, settings, config, {'custom_match_position': 'head'})
    projected = project(updated, settings, config)
    assert projected['custom_match_position'] == 'head'
    assert projected['custom_matches'] == [custom]
    rewritten = {**custom, 'exec': 'reject 3'}
    updated, _ = apply_routing(updated, settings, config, {'custom_matches': [rewritten]})
    assert project(updated, settings, config)['custom_matches'] == [rewritten]


def test_custom_matches_reject_paused_rows_and_invalid_batches_without_mutation():
    document, settings, config = prepared()
    before, sources = deepcopy(document), deepcopy(settings)
    with pytest.raises(OxidnsSettingsError, match='enabled: false'):
        apply_routing(document, settings, config, {'direct_rules': ['a'], 'custom_matches': [
            {'matches': ['qtype 1'], 'exec': 'reject 0', 'enabled': False}]})
    with pytest.raises(OxidnsSettingsError, match='同时'):
        apply_routing(document, settings, config, {'proxy_rules': ['a']})
    assert document == before
    assert settings == sources


def test_unknown_provider_and_duplicate_catalog_values_disable_ambiguous_selection():
    document, settings, config = prepared()
    group = next(item for item in document['plugins'] if item['type'] == 'domain_set')
    group['args']['exps'].append('full:manual.test')
    result = read_routing(document, settings, config)
    assert result['capabilities']['direct_rules']['editable'] is False
    before = deepcopy(document)
    with pytest.raises(OxidnsSettingsError, match='完整参数'):
        apply_routing(document, settings, config, {'direct_rules': []})
    assert document == before
    document, settings, config = prepared()
    config['rule_configs'].append({'id': 'duplicate', 'itemType': 'rule', 'rule_type': 'DOMAIN', 'value': 'a.test'})
    assert read_routing(document, settings, config)['capabilities']['direct_rules']['editable'] is False


def test_hand_reordered_priority_is_not_overwritten_by_rule_selection():
    document, settings, config = prepared(aggregate_rules=False)
    main = plugin(document, 'sequence_main')['args']
    positions = [index for index, item in enumerate(main) if item.get('matches', [''])[0].startswith('qname $rules_domain')]
    main[positions[0]], main[positions[1]] = main[positions[1]], main[positions[0]]
    assert read_routing(document, settings, config)['capabilities']['aggregate_rules']['editable'] is False


def test_removing_routing_keeps_provider_referenced_by_another_manual_plugin():
    document, settings, config = prepared(aggregate_rules=False, direct_rules=['a'], proxy_rules=[], direct_rulesets=[])
    provider = next(item for item in document['plugins'] if item['type'] == 'domain_set')
    extra = {'tag': 'manual', 'type': 'sequence', 'args': [{'matches': [f'qname ${provider["tag"]}'], 'exec': 'accept'}]}
    document['plugins'].append(extra)
    updated, _ = apply_routing(document, settings, config, {'direct_rules': []})
    assert plugin(updated, provider['tag']) == provider
    assert plugin(updated, 'manual') == extra
    assert project(updated, settings, config)['direct_rules'] == []


@pytest.mark.parametrize('protection', ['custom_match', 'extension', 'include', 'global'])
def test_removing_rule_keeps_provider_with_unmanaged_references_or_fields(protection):
    document, settings, config = prepared(aggregate_rules=False, direct_rules=['a'], proxy_rules=[], direct_rulesets=[])
    provider = next(item for item in document['plugins'] if item['type'] == 'domain_set')
    if protection == 'custom_match':
        plugin(document, 'sequence_main')['args'].insert(-1, {'matches': ['qtype 1', f'qname ${provider["tag"]}'], 'exec': 'accept'})
    elif protection == 'extension':
        provider['future_field'] = {'off': False}
    elif protection == 'include':
        document['include'] = ['private.yaml']
    else:
        document['future'] = {'provider': provider['tag']}
    updated, _ = apply_routing(document, settings, config, {'direct_rules': []})
    assert plugin(updated, provider['tag']) == provider
    if protection == 'custom_match':
        assert project(updated, settings, config)['custom_matches'] == project(document, settings, config)['custom_matches']


def test_rule_update_downloads_reload_and_sources_follow_actual_selected_rules():
    document, settings, config = prepared(rule_update={'enabled': True})
    plugin(document, 'rules_download')['args']['future'] = False
    plugin(document, 'rules_download')['args']['downloads'].append({'url': 'https://manual.test/x', 'dir': 'extra', 'filename': 'x'})
    plugin(document, 'rules_reload')['args'].append('$manual_provider')
    updated, sources = apply_routing(document, settings, config, {'direct_rulesets': ['rs2']})
    assert project(updated, sources, config)['direct_rulesets'] == ['rs2']
    downloads = plugin(updated, 'rules_download')['args']['downloads']
    new_path = _rule_path(config['rule_configs'][5], 'domain').removeprefix('./')
    old_path = _rule_path(config['rule_configs'][4], 'domain').removeprefix('./')
    paths = {item['dir'].removeprefix('./') + '/' + item['filename'] for item in downloads}
    assert new_path in paths and old_path not in paths
    assert 'extra/x' in paths
    assert plugin(updated, 'rules_download')['args']['future'] is False
    reload = plugin(updated, 'rules_reload')['args']
    assert '$manual_provider' in reload
    assert all(not value.startswith('$rules_') or any(item['tag'] == value[1:] for item in updated['plugins']) for value in reload)
    assert sources['direct_rulesets'] == ['rs', 'rs2']  # old resource source stays available


def test_download_projection_accepts_unfinished_other_plugins_and_adguard_actual_references():
    url = 'https://adguard.test/rules.txt'
    document, settings, config = prepared(adguard={'enabled': True, 'urls': [url]})
    document['plugins'].append({'tag': 'unfinished', 'type': 'http_server', 'args': {}})
    settings['adguard']['enabled'] = False
    downloads = resource_downloads(document, settings, config)
    assert len(downloads) == 2
    assert any(item['url'] == url for item in downloads)
    updated, _ = apply_routing(document, settings, config, {'direct_rules': ['a', 'ip']})
    assert plugin(updated, 'unfinished')['args'] == {}


def test_mixed_ruleset_projection_requires_both_domain_and_ip_parts():
    document, settings, config = prepared(direct_rulesets=['mixed'])
    result = read_routing(document, settings, config)
    assert result['values']['direct_rulesets'] == ['mixed']
    assert result['capabilities']['direct_rulesets']['editable'] is True
    provider = next(item for item in document['plugins'] if item['type'] == 'ip_set')
    provider['args']['files'] = []
    assert read_routing(document, settings, config)['capabilities']['direct_rulesets']['editable'] is False


def test_features_dynamic_routing_and_custom_fields_survive_rule_and_default_batch():
    document, settings, config = prepared(query_recorder={'enabled': True},
        dynamic_domain_set={'enabled': True, 'learn': True, 'route': 'proxy'},
        ip_selector={'enabled': True}, adguard={'enabled': True, 'content': '||ad.test^'})
    assert all(cap['editable'] for key, cap in read_routing(document, settings, config)['capabilities'].items() if key != 'custom_match_position')
    before = deepcopy(document)
    updated, sources = apply_routing(document, settings, config, {
        'direct_rules': ['a', 'ip'], 'default_forward': 'forward_local',
        'custom_matches': [{'matches': ['qname full:local.test'], 'exec': 'reject 0', 'future': [0, False]}],
    })
    updated, sources = apply_routing(updated, sources, config, {'custom_match_position': 'head'})
    values = project(updated, sources, config)
    assert values['direct_rules'] == ['a', 'ip']
    assert values['default_forward'] == 'forward_local'
    assert values['custom_match_position'] == 'head'
    for tag in ['dynamic_domains', 'learn_classified_domain', 'ip_selector', 'adguard_rules', 'query_recorder', 'china_dns', 'ip_requery_proxy']:
        assert plugin(updated, tag) == plugin(before, tag)


def test_adding_rule_never_creates_a_reference_to_missing_standard_helper():
    document, settings, config = prepared(direct_rules=[], proxy_rules=[], direct_rulesets=[])
    document['plugins'] = [item for item in document['plugins'] if item['tag'] != 'direct_ip']
    before = deepcopy(document)
    with pytest.raises(OxidnsSettingsError, match='direct_ip'):
        apply_routing(document, settings, config, {'direct_rules': ['ip']})
    assert document == before


def test_equivalent_absent_controls_are_explicitly_unset_until_meaningful():
    document, settings, config = prepared(direct_rules=[], proxy_rules=[], direct_rulesets=[])
    projection = read_routing(document, settings, config)
    for field in ('aggregate_rules', 'custom_match_position'):
        assert projection['values'][field] is None
        assert projection['capabilities'][field]['editable'] is False
        assert projection['capabilities'][field]['reason']
    updated, sources = apply_routing(document, settings, config, {'direct_rules': ['a', 'b']})
    projection = read_routing(updated, sources, config)
    assert projection['values']['aggregate_rules'] is True
    assert projection['capabilities']['aggregate_rules']['editable'] is True


@pytest.mark.parametrize('change, reason', [
    (lambda item, rules: rules.remove(item), f'1 个已删除的规则集（{_tag("rule", "mixed")}）：请在 OxiDNS 设置的 YAML 中删除'),
    (lambda item, rules: item.update(enabled=False), '「mixed」已停用：请在规则管理中重新启用'),
    (lambda item, rules: item.update(behavior='ipcidr'), '「mixed」的类型被改成了 ipcidr：请改回原来的类型'),
])
def test_locked_routing_says_which_ruleset_to_fix_and_where(change, reason):
    document, settings, config = prepared(direct_rulesets=['mixed'])
    item = next(entry for entry in config['rule_configs'] if entry['id'] == 'mixed')
    change(item, config['rule_configs'])
    capability = read_routing(document, settings, config)['capabilities']['direct_rulesets']
    assert capability['editable'] is False
    assert capability['reason'].startswith('分流里有不可用的规则集，暂时不能在这里编辑。')
    assert reason in capability['reason']


def test_remove_rule_ignores_unused_rules_and_drops_used_ones():
    from backend.common.oxidns_common_routing import remove_rule
    document, settings, config = prepared(direct_rulesets=['mixed', 'rs'])
    assert remove_rule(settings, config, 'rs2') is None
    updated = remove_rule(settings, config, 'mixed')
    assert not any(_rule_path({'id': 'mixed'}, part).removeprefix('./') in updated['native_config']
                   for part in ('domain', 'ip'))
    assert project(yaml.safe_load(updated['native_config']), updated, config)['direct_rulesets'] == ['rs']


def test_native_downloads_follow_yaml_references_not_stored_selectors():
    from backend.converters.oxidns import get_oxidns_ruleset_downloads
    document, settings, config = prepared(direct_rulesets=['rs'])
    stale = {**settings, 'direct_rulesets': [], 'proxy_rulesets': ['rs2']}
    downloads = get_oxidns_ruleset_downloads({**config, 'oxidns': stale})
    assert [item['local_path'] for item in downloads] == [_rule_path({'id': 'rs'}, 'domain')]
