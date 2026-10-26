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
    # Every IP route is direct like the new fallback: the probe and its wrapper go,
    # the IP clauses and the custom middle stay.
    assert new_main == [entry for entry in main[:-1] if entry not in ({'exec': 'jump local_probe'}, {'exec': 'drop_resp'})] \
        + [{'exec': 'goto china_dns'}]
    assert document == before
    restored, _ = apply_routing(updated, settings, config, {'default_forward': 'forward_remote'})
    assert plugin(restored, 'sequence_main')['args'] == main


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


# Per-rule DNS actions in native YAML.

from backend.common.oxidns_common_routing import _recognize, remove_rule  # noqa: E402
from backend.common.rule_dns import BLACKHOLE_PLUGIN, route_key  # noqa: E402

UP = {'action': 'upstream', 'source': 'local', 'upstream': {'addr': 'udp://10.0.0.53:53'}}
UP_REMOTE = {'action': 'upstream', 'source': 'remote', 'upstream': {'addr': 'https://dns.test/dns-query'}}
ACTIONS = {'refused': {'action': 'refused'}, 'nxdomain': {'action': 'nxdomain'}, 'nodata': {'action': 'nodata'},
           'blackhole': {'action': 'blackhole'}, 'upstream_local': UP, 'upstream_remote': UP_REMOTE}
FEATURES = {'cache_enabled': True, 'cache_lazy_ttl': 300, 'remote_ttl': 60,
            'ip_selector': {'enabled': True}, 'rule_update': {'enabled': True}}


def policies(config=None):
    config = config or fixture_config()
    for item in config['rule_configs']:
        item['policy'] = 'DIRECT' if item['id'] in ('a', 'rs') else 'Proxy'
    return config


def native(config, **fields):
    settings = prepare_editor(fields, config)
    return yaml.safe_load(settings['native_config']), settings


def main_args(document):
    return plugin(document, 'sequence_main')['args']


def tags(document):
    return [item['tag'] for item in document['plugins']]


def helpers(document):
    return [tag for tag in tags(document) if tag.startswith('cf_dns_')]


def without(config, identity, **change):
    after = deepcopy(config)
    item = next(entry for entry in after['rule_configs'] if entry['id'] == identity)
    item.update(change)
    return after


def chain(action):
    """Helper tags of one pinned upstream with the default cache enabled."""
    key = route_key(action)[9:]
    return [f'cf_dns_fwd_{key}', f'cf_dns_cache_{key}', f'cf_dns_up_{key}']


def editable(result):
    return {key for key, cap in result['capabilities'].items() if cap['editable']}


@pytest.mark.parametrize('features', [{}, FEATURES], ids=['plain', 'features'])
@pytest.mark.parametrize('identity', ['b', 'ip', 'mixed'], ids=['domain', 'ip', 'classical'])
@pytest.mark.parametrize('action', list(ACTIONS))
def test_read_apply_roundtrip_each_action(action, identity, features):
    config = policies()
    document, settings = native(config, direct_rules=['a'], proxy_rules=['c'], direct_rulesets=['rs'],
                                rule_actions={identity: ACTIONS[action]}, **deepcopy(features))
    before, saved = deepcopy(document), deepcopy(settings)
    result = read_routing(document, settings, config)
    assert result['values']['rule_actions'] == {identity: ACTIONS[action]}
    assert result['values']['direct_rules'] == ['a'] and result['values']['proxy_rules'] == ['c']
    assert result['values']['direct_rulesets'] == ['rs']
    assert {'direct_rules', 'rule_actions', 'rule_dns_follow', 'custom_matches'} <= editable(result)
    updated, sources = apply_routing(document, settings, config, {}, before_config=config)
    assert updated == document
    assert sources['rule_actions'] == {identity: ACTIONS[action]}
    assert (document, settings) == (before, saved)


def test_native_action_write_matches_template_output():
    config = policies()
    document, settings = native(config, direct_rules=['a'])
    notices = []
    updated, sources = apply_routing(document, settings, config,
                                     {'rule_actions': {'b': {'action': 'blackhole'}, 'c': UP}}, notices=notices)
    template = yaml.safe_load(prepare_editor({'direct_rules': ['a'], 'rule_actions': {
        'b': {'action': 'blackhole'}, 'c': UP}}, config)['native_config'])
    assert main_args(updated) == main_args(template)
    assert {item['tag']: item for item in updated['plugins']} == {item['tag']: item for item in template['plugins']}
    # Helpers are created right before sequence_main, as the template places them.
    expected = ['cf_dns_blackhole', *chain(UP), 'sequence_main']
    assert tags(updated)[tags(updated).index('cf_dns_blackhole'):][:5] == expected
    assert tags(template)[tags(template).index('cf_dns_blackhole'):][:5] == expected
    assert sources['rule_actions'] == {'b': {'action': 'blackhole'}, 'c': UP}
    assert notices == []


def test_apply_routing_empty_changes_with_before_config():
    config = policies()
    document, settings = native(config, direct_rules=['a', 'ip'], direct_rulesets=['rs'],
                                rule_actions={'b': {'action': 'nxdomain'}}, rule_update={'enabled': True})
    assert apply_routing(document, settings, config, {}) == (document, settings)
    updated, sources = apply_routing(document, settings, config, {}, before_config=config)
    assert updated == document
    assert {key: sources[key] for key in ('rule_actions', 'rule_dns_parked', 'rule_dns_follow')} == {
        'rule_actions': {'b': {'action': 'nxdomain'}}, 'rule_dns_parked': {}, 'rule_dns_follow': []}


def test_reorder_with_empty_assignments_rebuilds_yaml():
    config = policies()
    document, settings = native(config, direct_rules=['a'], aggregate_rules=False,
                                rule_actions={'b': {'action': 'refused'}, 'c': {'action': 'blackhole'}})
    after = deepcopy(config)
    rules = after['rule_configs']
    rules[1], rules[2] = rules[2], rules[1]
    # Read against the new order alone, the old YAML cannot be matched any more.
    assert 'direct_rules' not in editable(read_routing(document, settings, after))
    updated, sources = apply_routing(document, settings, after, {}, before_config=config)
    result = read_routing(updated, sources, after)
    assert 'direct_rules' in editable(result)
    assert result['values']['rule_actions'] == {'c': {'action': 'blackhole'}, 'b': {'action': 'refused'}}
    execs = [entry['exec'] for entry in main_args(updated) if entry.get('matches', [''])[0].startswith('qname $rules_')]
    assert execs == ['goto china_dns', '$cf_dns_blackhole', 'reject']


def test_disable_routed_rule_does_not_raise_and_parks():
    config = policies()
    document, settings = native(config, direct_rules=['a', 'ip'], direct_rulesets=['rs'],
                                rule_actions={'b': {'action': 'blackhole'}})
    after = without(without(config, 'b', enabled=False), 'rs', enabled=False)
    updated, sources = apply_routing(document, settings, after, {}, before_config=config)
    assert sources['rule_dns_parked'] == {'b': {'action': 'blackhole'}, 'rs': {'action': 'direct'}}
    assert sources['rule_actions'] == {}
    values = read_routing(updated, sources, after)['values']
    assert values['direct_rules'] == ['a', 'ip'] and values['direct_rulesets'] == []
    assert helpers(updated) == []
    assert _rule_path({'id': 'rs'}, 'domain').removeprefix('./') not in yaml.safe_dump(updated)


def test_enable_restores_parked_choice():
    config = policies()
    document, settings = native(config, direct_rules=['a', 'ip'], direct_rulesets=['rs'],
                                rule_actions={'b': {'action': 'blackhole'}, 'c': UP})
    after = without(without(config, 'b', enabled=False), 'rs', enabled=False)
    disabled, sources = apply_routing(document, settings, after, {}, before_config=config)
    restored, sources = apply_routing(disabled, sources, config, {}, before_config=after)
    assert sources['rule_dns_parked'] == {}
    assert sources['rule_actions'] == {'b': {'action': 'blackhole'}, 'c': UP}
    values = read_routing(restored, sources, config)['values']
    assert values['direct_rulesets'] == ['rs']
    assert values['rule_actions'] == {'b': {'action': 'blackhole'}, 'c': UP}
    assert main_args(restored) == main_args(document)


def test_explicit_choice_for_disabled_rule_is_parked_not_refused():
    config = policies()
    document, settings = native(config, direct_rules=['a'])
    after = without(config, 'b', enabled=False)
    updated, sources = apply_routing(document, settings, after, {'rule_actions': {'b': {'action': 'nodata'}}})
    assert sources['rule_dns_parked'] == {'b': {'action': 'nodata'}}
    assert sources['rule_actions'] == {}
    assert main_args(updated) == main_args(document)
    with pytest.raises(OxidnsSettingsError, match='可用规则库'):
        apply_routing(document, settings, config, {'rule_actions': {'gone': {'action': 'nodata'}}})


def test_native_toggle_keeps_follow_and_reapplies_route():
    config = policies()
    document, settings = native(config, direct_rules=['a'], proxy_rules=['c'], rule_dns_follow=['c'])
    off = without(config, 'c', enabled=False)
    disabled, sources = apply_routing(document, settings, off, {}, before_config=config)
    assert sources['rule_dns_follow'] == ['c'] and sources['rule_dns_parked'] == {}
    assert read_routing(disabled, sources, off)['values']['proxy_rules'] == []
    # The policy changes while the rule is off; enabling routes by the new policy.
    on = without(config, 'c', policy='DIRECT')
    enabled, sources = apply_routing(disabled, sources, on, {}, before_config=off)
    assert read_routing(enabled, sources, on)['values']['direct_rules'] == ['a', 'c']
    assert sources['rule_dns_follow'] == ['c']


def test_policy_rederive_not_invalidated():
    config = policies()
    document, settings = native(config, direct_rules=['a'], proxy_rules=['c'], rule_dns_follow=['c'])
    after = without(config, 'c', policy='DIRECT')
    updated, sources = apply_routing(document, settings, after, {
        'direct_rules': ['a', 'c'], 'proxy_rules': [], 'rule_dns_follow': ['c']}, before_config=config)
    assert sources['rule_dns_follow'] == ['c']
    assert read_routing(updated, sources, after)['values']['direct_rules'] == ['a', 'c']


def test_manual_list_change_drops_follow():
    config = policies()
    document, settings = native(config, direct_rules=['a'], proxy_rules=['c'], rule_dns_follow=['a', 'c'])
    # Re-sending an unchanged route keeps the intent; changing it ends it.
    _, sources = apply_routing(document, settings, config, {'direct_rules': ['a'], 'proxy_rules': ['c']})
    assert sources['rule_dns_follow'] == ['a', 'c']
    _, sources = apply_routing(document, settings, config, {'direct_rules': ['a', 'c'], 'proxy_rules': []})
    assert sources['rule_dns_follow'] == ['a']
    _, sources = apply_routing(document, settings, config, {'rule_actions': {'a': {'action': 'refused'}}})
    assert sources['rule_dns_follow'] == ['c']
    with pytest.raises(OxidnsSettingsError, match='不能同时跟随策略'):
        apply_routing(document, settings, config, {'rule_actions': {'a': {'action': 'refused'}},
                                                   'rule_dns_follow': ['a', 'c']})


def test_follow_only_change_does_not_touch_yaml():
    config = policies()
    document, settings = native(config, direct_rules=['a'], proxy_rules=['c'])
    updated, sources = apply_routing(document, settings, config, {'rule_dns_follow': ['c', 'c', 'a']})
    assert updated == document
    assert sources['rule_dns_follow'] == ['c', 'a']
    assert read_routing(updated, sources, config)['values']['rule_dns_follow'] == ['c', 'a']


def test_library_disable_parks_actions():
    config = policies()
    document, settings = native(config, direct_rules=['a'], rule_actions={'rs': {'action': 'nxdomain'}},
                                rule_update={'enabled': True})
    after = without(config, 'rs', library_enabled=False)
    updated, sources = apply_routing(document, settings, after, {}, before_config=config)
    assert sources['rule_dns_parked'] == {'rs': {'action': 'nxdomain'}}
    assert read_routing(updated, sources, after)['values']['rule_actions'] == {}
    restored, sources = apply_routing(updated, sources, config, {}, before_config=after)
    assert read_routing(restored, sources, config)['values']['rule_actions'] == {'rs': {'action': 'nxdomain'}}


def test_value_edit_rebuilds_provider():
    config = policies()
    document, settings = native(config, direct_rules=['a'], rule_actions={'b': {'action': 'nxdomain'}})
    after = without(config, 'b', value='bb.test')
    updated, sources = apply_routing(document, settings, after, {}, before_config=config)
    result = read_routing(updated, sources, after)
    assert result['values']['rule_actions'] == {'b': {'action': 'nxdomain'}}
    assert 'bb.test' in yaml.safe_dump(updated) and 'domain:b.test' not in yaml.safe_dump(updated)


def test_rebuild_of_unrecognized_routing_raises_its_reason():
    config = policies()
    document, settings = native(config, direct_rules=['a'])
    next(item for item in document['plugins'] if item['type'] == 'domain_set')['args']['exps'].append('full:x.test')
    reason = read_routing(document, settings, config)['capabilities']['direct_rules']['reason']
    with pytest.raises(OxidnsSettingsError) as error:
        apply_routing(document, settings, config, {}, before_config=config)
    assert str(error.value) == reason


def test_apply_creates_and_removes_owned_helpers():
    config = policies()
    document, settings = native(config, direct_rules=['a'])
    key = route_key(UP)[9:]
    updated, sources = apply_routing(document, settings, config, {'rule_actions': {
        'b': {'action': 'blackhole'}, 'ip': UP, 'c': UP}})
    assert helpers(updated) == ['cf_dns_blackhole', *chain(UP)]
    assert plugin(updated, 'cf_dns_blackhole') == BLACKHOLE_PLUGIN
    assert [entry['exec'] for entry in main_args(updated) if entry.get('matches', [''])[0].startswith('resp_ip $rules_')] \
        == [f'goto cf_dns_up_{key}']
    updated, sources = apply_routing(updated, sources, config, {'rule_actions': {'c': UP}})
    assert helpers(updated) == chain(UP)
    updated, sources = apply_routing(updated, sources, config, {'rule_actions': {}})
    assert helpers(updated) == []
    assert main_args(updated) == main_args(document)


def test_helper_kept_when_custom_match_references_it():
    config = policies()
    document, settings = native(config, direct_rules=['a'], rule_actions={'b': {'action': 'blackhole'}},
                                custom_matches=[{'matches': ['qname full:ads.test'], 'exec': '$cf_dns_blackhole'}])
    assert read_routing(document, settings, config)['values']['custom_matches'] == [
        {'matches': ['qname full:ads.test'], 'exec': '$cf_dns_blackhole'}]
    updated, _ = apply_routing(document, settings, config, {'rule_actions': {}})
    assert plugin(updated, 'cf_dns_blackhole') == BLACKHOLE_PLUGIN
    assert {'matches': ['qname full:ads.test'], 'exec': '$cf_dns_blackhole'} in main_args(updated)


def test_same_name_plugin_blocks_helper_creation():
    config = policies()
    document, settings = native(config, direct_rules=['a'])
    document['plugins'].insert(0, {'tag': 'cf_dns_blackhole', 'type': 'black_hole', 'args': {'mode': 'nxdomain'}})
    before = deepcopy(document)
    with pytest.raises(OxidnsSettingsError, match='已有同名插件 cf_dns_blackhole'):
        apply_routing(document, settings, config, {'rule_actions': {'b': {'action': 'blackhole'}}})
    assert document == before


BLACKHOLE_REASON = ('黑洞插件 cf_dns_blackhole 已被手动修改（应为 mode: "null"、short_circuit: true），'
                    '请在完整参数中恢复或改名')


def assert_blackhole_locked(args):
    config = policies()
    document, settings = native(config, direct_rules=['a'], rule_actions={'b': {'action': 'blackhole'}})
    plugin(document, 'cf_dns_blackhole')['args'] = args
    result = read_routing(document, settings, config)
    for key in ('direct_rules', 'rule_actions', 'rule_dns_follow', 'aggregate_rules', 'custom_matches', 'custom_match_position'):
        assert result['capabilities'][key] == {'editable': False, 'reason': BLACKHOLE_REASON, 'target': 'sequence_main'}
    # The default target is edited in place and keeps the hand-edited entry.
    assert result['capabilities']['default_forward']['editable'] is True
    updated, _ = apply_routing(document, settings, config, {'default_forward': 'forward_local'})
    assert main_args(updated)[:-1] == main_args(document)[:-1]
    assert plugin(updated, 'cf_dns_blackhole')['args'] == args


def test_modified_blackhole_helper_not_editable_with_reason():
    assert_blackhole_locked({'mode': 'nxdomain', 'short_circuit': True})


def test_bare_null_blackhole_helper_not_editable():
    assert_blackhole_locked({'mode': None, 'short_circuit': True})


def test_multi_upstream_pinned_forward_not_editable():
    config = policies()
    document, settings = native(config, direct_rules=['a'], rule_actions={'b': UP})
    key = route_key(UP)[9:]
    plugin(document, f'cf_dns_fwd_{key}')['args']['upstreams'].append({'addr': 'udp://10.0.0.54:53'})
    capability = read_routing(document, settings, config)['capabilities']['rule_actions']
    assert capability['reason'] == f'指定上游处理链 cf_dns_up_{key} 已被手动修改，请在完整参数中恢复'
    assert capability['editable'] is False


def test_hand_edited_pinned_addr_is_recognised_and_renamed_on_write():
    config = policies()
    document, settings = native(config, direct_rules=['a'], rule_actions={'b': UP})
    old = route_key(UP)[9:]
    plugin(document, f'cf_dns_fwd_{old}')['args']['upstreams'][0]['addr'] = 'udp://10.0.0.99:53'
    edited = {**UP, 'upstream': {'addr': 'udp://10.0.0.99:53'}}
    result = read_routing(document, settings, config)
    assert result['values']['rule_actions'] == {'b': edited}
    assert 'rule_actions' in editable(result)
    updated, sources = apply_routing(document, settings, config, {}, before_config=config)
    new = route_key(edited)[9:]
    assert helpers(updated) == chain(edited)
    assert plugin(updated, f'cf_dns_fwd_{new}')['args']['upstreams'] == [{'addr': 'udp://10.0.0.99:53'}]
    assert any(entry['exec'] == f'goto cf_dns_up_{new}' for entry in main_args(updated))
    assert sources['rule_actions'] == {'b': edited}


def test_hand_edited_chain_tag_is_reused_for_the_original_upstream():
    config = policies()
    document, settings = native(config, direct_rules=['a'], rule_actions={'b': UP})
    old = route_key(UP)[9:]
    plugin(document, f'cf_dns_fwd_{old}')['args']['upstreams'][0]['addr'] = 'udp://10.0.0.99:53'
    edited = {**UP, 'upstream': {'addr': 'udp://10.0.0.99:53'}}
    # Another rule now pins the original address: its tag is the edited chain's.
    updated, sources = apply_routing(document, settings, config, {'rule_actions': {'b': edited, 'c': UP}})
    assert sorted(helpers(updated)) == sorted(chain(edited) + chain(UP))
    assert plugin(updated, f'cf_dns_fwd_{old}')['args']['upstreams'] == [UP['upstream']]
    assert read_routing(updated, sources, config)['values']['rule_actions'] == {'b': edited, 'c': UP}


def test_custom_match_edit_preserves_invalid_helper_entry():
    config = policies()
    document, settings = native(config, direct_rules=['a'], rule_actions={'b': {'action': 'blackhole'}})
    plugin(document, 'cf_dns_blackhole')['args']['mode'] = 'nxdomain'
    before = deepcopy(document)
    with pytest.raises(OxidnsSettingsError, match='黑洞插件'):
        apply_routing(document, settings, config, {'custom_matches': [{'matches': ['qtype 1'], 'exec': 'accept'}]})
    with pytest.raises(OxidnsSettingsError, match='黑洞插件'):
        apply_routing(document, settings, config, {}, before_config=config)
    assert document == before


def test_alias_rejections_are_recognised_and_written_canonically():
    config = policies()
    document, settings = native(config, direct_rules=['a'], aggregate_rules=False, rule_actions={
        'b': {'action': 'refused'}, 'c': {'action': 'nxdomain'}, 'ip': {'action': 'nodata'}})
    spelled = {'reject': 'REJECT 5', 'reject 3': 'reject NXDOMAIN', 'reject 0': 'reject noerror'}
    for entry in main_args(document):
        entry['exec'] = spelled.get(entry['exec'], entry['exec'])
    result = read_routing(document, settings, config)
    assert result['values']['rule_actions'] == {'b': {'action': 'refused'}, 'c': {'action': 'nxdomain'},
                                                'ip': {'action': 'nodata'}}
    updated, _ = apply_routing(document, settings, config, {}, before_config=config)
    assert [entry['exec'] for entry in main_args(updated) if entry['exec'].lower().startswith('reject')
            and entry.get('matches', [''])[0].startswith(('qname $rules', 'resp_ip $rules'))] == ['reject', 'reject 3', 'reject 0']


def test_existing_custom_reject_on_rule_file_stays_editable():
    """review/claim_check.py: a user provider over a generated rule file plus a tail reject."""
    items = [{'id': 'ruleset_ads', 'itemType': 'ruleset', 'library_rule_id': 'l1', 'policy': 'REJECT', 'enabled': True,
              'url': '/x/ads', 'behavior': 'domain', 'name': 'ads'},
             {'id': 'ruleset_cn', 'itemType': 'ruleset', 'library_rule_id': 'l2', 'policy': 'DIRECT', 'enabled': True,
              'url': '/x/cn', 'behavior': 'domain', 'name': 'cn'}]
    config = {'rule_configs': items, 'system_config': {'server_domain': 'http://cf', 'rule_proxy_token': 'test-only'}}
    for name in ('my_ads', 'rules_domain_9'):
        document, settings = native(config, direct_rulesets=['ruleset_cn'])
        document['plugins'].insert(0, {'tag': name, 'type': 'domain_set',
                                       'args': {'files': [_rule_path(items[0], 'domain')]}})
        main = main_args(document)
        index = next(i for i, entry in enumerate(main) if entry.get('exec') == 'goto china_dns' and entry.get('matches'))
        custom = {'matches': [f'qname ${name}'], 'exec': 'reject 3'}
        main.insert(index + 1, custom)
        result = read_routing(document, settings, config)
        assert result['values']['direct_rulesets'] == ['ruleset_cn']
        assert result['values']['rule_actions'] == {}
        assert result['values']['custom_matches'] == [custom]
        assert {'direct_rulesets', 'rule_actions', 'custom_matches'} <= editable(result)


def test_custom_match_reusing_generated_provider_not_claimed():
    config = policies()
    document, settings = native(config, direct_rules=['a'], aggregate_rules=False)
    provider = main_args(document)[-2]['matches'][0].split('$')[1]
    custom = {'matches': [f'qname ${provider}'], 'exec': 'reject 3'}
    main_args(document).insert(-1, custom)
    result = read_routing(document, settings, config)
    assert result['values']['direct_rules'] == ['a'] and result['values']['rule_actions'] == {}
    assert result['values']['custom_matches'] == [custom]
    assert {'direct_rules', 'custom_matches'} <= editable(result)


def test_renamed_generated_provider_is_claimed():
    config = policies()
    document, settings = native(config, direct_rules=['a'], rule_actions={'b': {'action': 'refused'}})
    provider = next(entry for entry in main_args(document) if entry['exec'] == 'reject')['matches'][0].split('$')[1]
    plugin(document, provider)['tag'] = provider + '_2'
    for entry in main_args(document):
        if entry['exec'] == 'reject':
            entry['matches'] = [f'qname ${provider}_2']
    assert read_routing(document, settings, config)['values']['rule_actions'] == {'b': {'action': 'refused'}}


def legacy_documents():
    """Documents a pre-upgrade ConfigFlow could produce or a user could hand-edit."""
    config = policies()
    documents = []
    for fields in ({}, {'aggregate_rules': False}, {'direct_rulesets': ['mixed']}, FEATURES,
                   {'custom_matches': [{'matches': ['qname full:custom.test'], 'exec': 'reject 0'}]},
                   {'custom_matches': [{'matches': ['qname full:c.test'], 'exec': 'reject 3'}],
                    'custom_match_position': 'head'}):
        document, settings, _ = prepared(config, **deepcopy(fields))
        documents.append((document, settings))
        for name in ('rules_domain_7', 'mine'):
            # A hand-written reject at the end of the domain rules, on a provider
            # that may look generated and duplicates a catalog rule.
            extra = deepcopy(document)
            extra['plugins'].insert(0, {'tag': name, 'type': 'domain_set', 'args': {'exps': ['full:c.test']}})
            main = main_args(extra)
            index = next((i for i, entry in enumerate(main) if entry['exec'] == 'jump local_probe' or
                          entry.get('matches') == ['qname $dynamic_domains']), len(main) - 1)
            main.insert(index, {'matches': [f'qname ${name}'], 'exec': 'reject'})
            documents.append((extra, settings))
    return config, documents


def test_documents_editable_before_upgrade_stay_editable():
    config, documents = legacy_documents()
    claimed = 0
    for document, settings in documents:
        before = _recognize(document, settings, config, claim=False)
        claimed += _recognize(document, settings, config, claim=True)['claimed']
        after = read_routing(document, settings, config)
        assert {key for key, cap in before['capabilities'].items() if cap['editable']} <= editable(after)
        if after['values']['rule_actions'] == {}:
            assert all(after['values'][key] == before['values'][key] for key in ('direct_rules', 'proxy_rules',
                                                                                  'custom_matches', 'direct_rulesets'))
    assert claimed  # The fallback, not the claim rule alone, kept some of them editable.


def test_final_entry_never_accepts_reject_as_default():
    config = policies()
    document, settings = native(config, direct_rules=['a'])
    main_args(document)[-1] = {'exec': 'reject 3'}
    result = read_routing(document, settings, config)
    assert result['values']['default_forward'] is None
    assert not editable(result)


def test_changed_list_key_wins_over_rule_action():
    config = policies()
    document, settings = native(config, direct_rules=['a'], rule_actions={'b': {'action': 'blackhole'}})
    notices = []
    updated, sources = apply_routing(document, settings, config, {'direct_rules': ['a', 'b']}, notices=notices)
    assert read_routing(updated, sources, config)['values']['rule_actions'] == {}
    assert sources['rule_actions'] == {}
    assert notices == ['规则「b.test」的 DNS 处理已从「黑洞」改为「直连 DNS」']
    assert helpers(updated) == []
    notices = []
    updated, sources = apply_routing(updated, sources, config, {'rule_actions': {'a': {'action': 'refused'}}},
                                     notices=notices)
    values = read_routing(updated, sources, config)['values']
    assert values['direct_rules'] == ['b'] and values['rule_actions'] == {'a': {'action': 'refused'}}
    assert notices == ['规则「a.test」的 DNS 处理已从「直连 DNS」改为「拒绝」']
    with pytest.raises(OxidnsSettingsError, match='多种 DNS 处理'):
        apply_routing(updated, sources, config, {'direct_rules': ['b', 'c'], 'rule_actions': {'c': UP}})


def test_remove_rule_clears_action_and_unused_helper():
    config = policies()
    document, settings = native(config, direct_rules=['a'], rule_actions={'b': {'action': 'blackhole'}, 'c': UP})
    result = remove_rule(settings, config, 'b')
    updated = yaml.safe_load(result['native_config'])
    assert helpers(updated) == chain(UP)
    assert 'domain:b.test' not in result['native_config']
    assert result['rule_actions'] == {'c': UP}
    after = {**config, 'rule_configs': [item for item in config['rule_configs'] if item['id'] != 'b']}
    assert read_routing(updated, result, after)['values']['rule_actions'] == {'c': UP}
    assert remove_rule(settings, config, 'rs2') is None


def test_sync_updates_removes_downloads_of_disabled_rulesets():
    config = policies()
    document, settings = native(config, direct_rulesets=['rs'], rule_actions={'rs2': {'action': 'refused'}},
                                rule_update={'enabled': True})
    path = _rule_path({'id': 'rs2'}, 'domain').removeprefix('./')

    def downloads(value):
        return {item['dir'].removeprefix('./') + '/' + item['filename'] for item in plugin(value, 'rules_download')['args']['downloads']}
    assert path in downloads(document)
    after = without(config, 'rs2', enabled=False)
    updated, _ = apply_routing(document, settings, after, {}, before_config=config)
    assert path not in downloads(updated)
    assert _rule_path({'id': 'rs'}, 'domain').removeprefix('./') in downloads(updated)
    assert all(any(item['tag'] == value[1:] for item in updated['plugins']) for value in plugin(updated, 'rules_reload')['args'])


def test_read_never_writes_existing_documents():
    config, documents = legacy_documents()
    document, settings = native(config, direct_rules=['a'], rule_actions={'b': {'action': 'blackhole'}, 'c': UP})
    invalid = deepcopy(document)
    plugin(invalid, 'cf_dns_blackhole')['args']['mode'] = None
    for value, sources in [*documents, (document, settings), (invalid, settings)]:
        before = deepcopy((value, sources))
        read_routing(value, sources, config)
        assert (value, sources) == before


def test_common_feature_toggles_rebuild_pinned_chains():
    from backend.common.oxidns_common import apply_common_settings
    config = policies()
    _, settings = native(config, direct_rules=['a'], rule_actions={'b': UP, 'c': UP_REMOTE}, cache_enabled=False)
    local, remote = route_key(UP)[9:], route_key(UP_REMOTE)[9:]
    result = apply_common_settings(settings, {'ip_selector.enabled': True, 'cache_enabled': True, 'remote_ttl': 30}, config)
    document = yaml.safe_load(result['settings']['native_config'])
    assert plugin(document, f'cf_dns_up_{local}')['args'] == [
        {'exec': 'drop_resp'}, {'exec': '$ip_selector'}, {'exec': f'$cf_dns_cache_{local}'},
        {'exec': f'$cf_dns_fwd_{local}'}, {'matches': ['!has_resp'], 'exec': 'reject 3'}, {'exec': 'accept'}]
    assert plugin(document, f'cf_dns_up_{remote}')['args'][-2] == {'exec': 'ttl 30'}
    # The cache is declared before the chain that calls it.
    assert tags(document).index(f'cf_dns_cache_{local}') < tags(document).index(f'cf_dns_up_{local}')
    assert result['capabilities']['ip_selector.enabled']['editable'] is True
    assert result['values']['ip_selector']['enabled'] is True
    assert 'rule_dns_parked' not in result['values']
    assert result['values']['rule_actions'] == {'b': UP, 'c': UP_REMOTE}
    result = apply_common_settings(result['settings'], {'ip_selector.enabled': False, 'cache_enabled': False}, config)
    document = yaml.safe_load(result['settings']['native_config'])
    assert '$ip_selector' not in yaml.safe_dump(document)
    assert helpers(document) == [*chain(UP)[::2], *chain(UP_REMOTE)[::2]]
    assert 'rule_actions' in editable(read_routing(document, result['settings'], config))
