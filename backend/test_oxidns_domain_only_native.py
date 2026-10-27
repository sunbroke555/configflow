"""Upgrade the recognizable legacy IP suffix without replacing custom native YAML."""
from copy import deepcopy

import pytest
import yaml

from backend.common.oxidns_common_routing import (
    apply_routing, migrate_domain_only_routing, read_routing, resource_downloads,
)
from backend.common.oxidns_settings import normalize_oxidns_settings
from backend.converters.oxidns import _routing_groups, generate_oxidns_config, get_oxidns_ruleset_downloads
from backend.common.rule_dns import route_exec


def plugin(document, tag):
    return next(item for item in document['plugins'] if item['tag'] == tag)


def legacy_config(*, content_aware=True, only_ip=False):
    config = {'system_config': {'server_domain': 'http://config.test', 'rule_proxy_token': 'test-only'},
              'rule_configs': [
                  {'id': 'domain', 'itemType': 'rule', 'rule_type': 'DOMAIN', 'value': 'known.test'},
                  {'id': 'ip', 'itemType': 'rule', 'rule_type': 'IP-CIDR', 'value': '192.0.2.0/24'},
                  {'id': 'mixed', 'itemType': 'ruleset', 'behavior': 'classical', 'source_type': 'content',
                   'url': 'https://rules.test/mixed', 'content': 'DOMAIN,mixed.test\nIP-CIDR,203.0.113.0/24'},
                  {'id': 'domain_only', 'itemType': 'ruleset', 'behavior': 'classical', 'source_type': 'content',
                   'url': 'https://rules.test/domain', 'content': 'DOMAIN,domain.test'},
                  {'id': 'cidr', 'itemType': 'ruleset', 'behavior': 'ipcidr',
                   'url': 'https://rules.test/ip'},
              ]}
    settings = normalize_oxidns_settings({
        'direct_rules': ['ip'] if only_ip else ['domain', 'ip'],
        'direct_rulesets': ['cidr'] if only_ip else ['mixed', 'domain_only', 'cidr'],
        'custom_hosts': 'full:host.test 192.0.2.2',
        'custom_matches': [{'matches': ['qname full:custom.test'], 'exec': 'reject 3'}],
    })
    config['oxidns'] = settings
    document = yaml.safe_load(generate_oxidns_config(config))
    groups = _routing_groups(config, settings, legacy='content-aware' if content_aware else True)
    ip_groups = groups['ip']
    document['plugins'][0:0] = [
        {'tag': group['tag'], 'type': 'ip_set', 'args': group['args']} for group in ip_groups]
    main = plugin(document, 'sequence_main')['args']
    main[-1:-1] = [{'exec': 'jump local_probe'},
                  *[{'matches': [f'resp_ip ${group["tag"]}'], 'exec': route_exec(group['route'], 'ip')}
                    for group in ip_groups], {'exec': 'drop_resp'}]
    # This recreates old resources independently of the current domain-only
    # generator so the regression remains meaningful after the implementation.
    desired = resource_downloads(document, settings, config)
    downloads = next((item for item in document['plugins'] if item['tag'] == 'rules_download'), None)
    if downloads:
        downloads['args']['downloads'] = desired
        plugin(document, 'rules_reload')['args'].extend(f'${group["tag"]}' for group in ip_groups)
    else:
        document['plugins'].extend([
            {'tag': 'rules_download', 'type': 'download', 'args': {'downloads': desired}},
            {'tag': 'rules_reload', 'type': 'reload_provider', 'args': [f'${group["tag"]}' for group in ip_groups]},
            {'tag': 'rules_refresh', 'type': 'sequence', 'args': [{'exec': '$rules_download'}, {'exec': '$rules_reload'}]},
        ])
    settings.update(config_mode='native', native_config=yaml.safe_dump(document))
    return config, document, settings


def assert_domain_only(document):
    main = plugin(document, 'sequence_main')['args']
    assert {'exec': 'jump local_probe'} not in main
    assert not any('resp_ip ' in str(entry.get('matches')) for entry in main)
    assert main[-1] == {'exec': 'goto proxy_dns_seq'}
    assert not any(item['type'] == 'ip_set' for item in document['plugins'])
    assert '_ip.txt' not in yaml.safe_dump(document)


@pytest.mark.parametrize('content_aware', [False, True], ids=['old-classical', 'content-aware'])
def test_upgrade_recognizes_both_legacy_shapes_and_preserves_domain_handling(content_aware):
    config, document, settings = legacy_config(content_aware=content_aware)
    original = deepcopy((document, settings))
    assert read_routing(document, settings, config)['capabilities']['direct_rules']['editable']
    updated, sources = migrate_domain_only_routing(document, settings, config)
    assert_domain_only(updated)
    assert (document, settings) == original
    values = read_routing(updated, sources, config)['values']
    assert values['direct_rules'] == ['domain']
    assert values['direct_rulesets'] == ['mixed', 'domain_only']
    assert values['custom_matches'] == settings['custom_matches']
    assert plugin(updated, 'custom_hosts') == plugin(document, 'custom_hosts')
    assert plugin(updated, 'forward_local') == plugin(document, 'forward_local')
    assert migrate_domain_only_routing(updated, sources, config) == (updated, sources)


def test_old_native_publication_and_downloads_use_the_same_domain_only_resources():
    config, document, settings = legacy_config()
    original = deepcopy(config)
    exported = yaml.safe_load(generate_oxidns_config(config))
    assert_domain_only(exported)
    downloads = get_oxidns_ruleset_downloads(config)
    assert len(downloads) == 2
    assert all(item['local_path'].endswith('_domain.txt') for item in downloads)
    assert config == original


def test_only_old_ip_subscriptions_withdraw_the_empty_update_pipeline():
    config, document, settings = legacy_config(only_ip=True)
    updated, sources = migrate_domain_only_routing(document, settings, config)
    assert_domain_only(updated)
    assert not any(item['tag'] in ('rules_download', 'rules_reload', 'rules_refresh') for item in updated['plugins'])
    assert not read_routing(updated, sources, config)['values']['direct_rulesets']


@pytest.mark.parametrize('changes', [
    {'default_forward': 'forward_local'},
    {'custom_matches': [{'matches': ['qname full:custom.test'], 'exec': 'reject'}]},
    {'rule_dns_follow': ['domain']},
])
def test_editing_a_recognizable_old_document_does_not_reintroduce_probe(changes):
    config, document, settings = legacy_config()
    updated, sources = apply_routing(document, settings, config, changes)
    main = plugin(updated, 'sequence_main')['args']
    assert {'exec': 'jump local_probe'} not in main
    assert not any('resp_ip ' in str(entry.get('matches')) for entry in main)
    assert read_routing(updated, sources, config)['capabilities']['direct_rules']['editable']


def test_custom_ip_suffix_is_preserved_and_stays_explicitly_locked():
    config, document, settings = legacy_config()
    main = plugin(document, 'sequence_main')['args']
    main.insert(-1, {'matches': ['qtype 1'], 'exec': 'reject'})
    original = deepcopy((document, settings))
    assert migrate_domain_only_routing(document, settings, config) == original
    cap = read_routing(document, settings, config)['capabilities']['direct_rules']
    assert not cap['editable']
    assert '完整参数' in cap['reason']


def test_reenabling_a_legacy_ip_rule_cannot_restore_its_parked_dns_choice():
    config, document, settings = legacy_config()
    updated, sources = migrate_domain_only_routing(document, settings, config)
    disabled_config = deepcopy(config)
    disabled_config['rule_configs'][1]['enabled'] = False
    disabled, parked = apply_routing(updated, sources, disabled_config, {}, before_config=config)
    restored, restored_settings = apply_routing(disabled, parked, config, {}, before_config=disabled_config)
    assert_domain_only(restored)
    values = read_routing(restored, restored_settings, config)['values']
    assert 'ip' not in values['direct_rules']


def test_expanded_includes_are_not_mistaken_for_a_managed_draft():
    from backend.common.oxidns_native import native_document
    config, document, settings = legacy_config()
    document['include'] = ['manual.yaml']
    settings['native_config'] = yaml.safe_dump(document)
    settings['additional_files'] = [{'path': 'manual.yaml', 'encoding': 'text',
                                     'content': 'plugins: []'}]
    expanded = native_document(settings)
    assert 'include' not in expanded
    original = deepcopy((expanded, settings))
    assert migrate_domain_only_routing(expanded, settings, config) == original


@pytest.mark.parametrize('custom', ['refresh', 'reload'])
def test_custom_update_pipeline_preserves_legacy_export_and_reports_explicit_edit(custom):
    from backend.common.oxidns_common import apply_common_settings
    from backend.common.oxidns_settings import OxidnsSettingsError
    config, document, settings = legacy_config(only_ip=True)
    if custom == 'refresh':
        plugin(document, 'rules_refresh')['args'].append({'exec': 'return'})
    else:
        plugin(document, 'rules_reload').update(type='sequence', args=[{'exec': 'return'}])
    settings['native_config'] = yaml.safe_dump(document)
    original = deepcopy((document, settings))
    assert migrate_domain_only_routing(document, settings, config) == original
    exported = yaml.safe_load(generate_oxidns_config(config))
    assert plugin(exported, 'sequence_main')['args'] == plugin(document, 'sequence_main')['args']
    assert get_oxidns_ruleset_downloads(config)[0]['local_path'].endswith('_ip.txt')
    with pytest.raises(OxidnsSettingsError, match='完整参数'):
        apply_common_settings(settings, {'direct_rulesets': []}, config)
    assert (document, settings) == original


def test_rule_sync_stops_before_publishing_an_empty_legacy_ip_update_pipeline():
    from backend.agents.rule_sync_projection import project_rule_changes, RuleSyncProjectionError
    config, document, settings = legacy_config(only_ip=True)
    current = deepcopy(config)
    current['rule_configs'][-1]['url'] = 'https://rules.test/new-ip'
    original = deepcopy((config, current))
    with pytest.raises(RuleSyncProjectionError, match='请手动完整发布'):
        project_rule_changes(config, current, {'library': True, 'rules': True}, service_type='oxidns')
    assert (config, current) == original
