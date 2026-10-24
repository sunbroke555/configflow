"""Rule-only publications update copied blockers without publishing group drafts."""
from copy import deepcopy
import hashlib
from types import SimpleNamespace

import pytest
import yaml

from backend.agents import device_group_publish, rule_sync
from backend.agents.deployment_bundle import _pack
from backend.agents.rule_sync_projection import (
    RuleSyncProjectionError, isolate_rule_bundle, project_rule_changes, read_bundle_files,
)
from backend.converters.mihomo import generate_mihomo_config
from backend.converters.oxidns import generate_oxidns_config


def source():
    return {
        'profile_id': 'home', 'system_config': {'rule_proxy_token': 'test-token', 'server_domain': 'https://config.test'},
        'rule_library': [],
        'rule_configs': [
            {'id': 'block', 'itemType': 'rule', 'rule_type': 'DOMAIN', 'value': 'old.test', 'policy': 'REJECT'},
            {'id': 'list', 'itemType': 'ruleset', 'name': '过滤规则', 'behavior': 'domain',
             'url': 'https://rules.test/list.yaml', 'policy': 'REJECT'},
            {'id': 'end', 'itemType': 'rule', 'rule_type': 'MATCH', 'policy': 'DIRECT'},
            {'id': 'after', 'itemType': 'rule', 'rule_type': 'DOMAIN', 'value': 'after.test', 'policy': 'REJECT'},
        ],
        'oxidns': {'rule_actions': {identity: {'action': 'refused'} for identity in ('block', 'list', 'after')}},
        'device_groups': {'version': 1, 'groups': [
            {'id': 'default', 'name': '默认组', 'members': []},
            {'id': 'dg_aaaaaa', 'name': '加强过滤', 'members': ['192.168.60.10'],
             'mode': 'direct', 'keep_blocking': True},
        ]},
        'policy_routing_sources': {'include': [], 'exclude_groups': []},
    }


def emit(config, service):
    return yaml.safe_load(generate_mihomo_config(config, preflight_providers=False) if service == 'mihomo'
                          else generate_oxidns_config(config))


def bundle(document, service, marker=b'old rules'):
    content = yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode()
    paths = set()
    if service == 'mihomo':
        paths.update(item['path'].removeprefix('./') for item in document.get('rule-providers', {}).values()
                     if item.get('path'))
    else:
        paths.update(path.removeprefix('./') for plugin in document['plugins']
                     if plugin['type'] in ('domain_set', 'ip_set')
                     for path in (plugin.get('args') or {}).get('files', []))
    files = {'config.yaml': (content, 'config'), **{path: (marker, 'rule') for path in paths}}
    manifest = {'protocol_version': 1, 'agent_id': 'agent', 'profile_id': 'home', 'service_type': service,
                'deployment_id': 'attempt', 'config_path': 'config.yaml', 'files': [
                    {'path': path, 'role': role, 'size': len(value), 'sha256': hashlib.sha256(value).hexdigest()}
                    for path, (value, role) in sorted(files.items())]}
    return _pack(manifest, files, hashlib.sha256(content).hexdigest()[:8])


def document(value):
    return yaml.safe_load(read_bundle_files(value)['config.yaml'][0])


@pytest.mark.parametrize('service', ['oxidns', 'mihomo'])
def test_projection_and_bundle_update_copied_blockers_but_pin_saved_group_and_route_drafts(service):
    base = source()
    current = deepcopy(base)
    current['rule_configs'][0]['value'] = 'new.test'
    current['device_groups']['groups'][1].update(members=['192.168.60.99'], keep_blocking=False)
    current['policy_routing_sources']['include'] = ['192.168.60.0/24']
    projected = project_rule_changes(base, current, {'rules': True, 'library': True}, service_type=service)
    assert projected['device_groups'] == base['device_groups']
    assert projected['policy_routing_sources'] == base['policy_routing_sources']
    previous, candidate = emit(base, service), emit(projected, service)
    isolated = isolate_rule_bundle(bundle(previous, service), bundle(candidate, service, b'new rules'), service,
                                  device_groups_enabled=True)
    actual = document(isolated)
    assert actual == candidate
    assert all(value == b'new rules' for _, (value, role) in read_bundle_files(isolated).items() if role == 'rule')
    if service == 'mihomo':
        assert actual['sub-rules']['cf_dg_seg_aaaaaa'] == [
            'DOMAIN,new.test,REJECT', 'RULE-SET,过滤规则,REJECT', 'MATCH,DIRECT']
        assert actual['rule-providers']['cf_dg_set_aaaaaa']['payload'] == ['192.168.60.10/32']
    else:
        plugins = {plugin['tag']: plugin for plugin in actual['plugins']}
        block = next(plugin for tag, plugin in plugins.items() if tag.startswith('cf_dg_block_'))
        assert 'new.test' in str(block) and 'after.test' not in str(block)
        assert plugins['cf_dg_set_aaaaaa']['args']['ips'] == ['192.168.60.10/32']


@pytest.mark.parametrize('service', ['oxidns', 'mihomo'])
@pytest.mark.parametrize('change', ['member', 'tail', 'nonblocking', 'add_group'])
def test_second_boundary_rejects_group_members_default_route_and_nonblocking_changes(service, change):
    previous = emit(source(), service)
    candidate = deepcopy(previous)
    if service == 'mihomo':
        if change == 'member':
            candidate['rule-providers']['cf_dg_set_aaaaaa']['payload'] = ['192.168.60.99/32']
        elif change == 'tail':
            candidate['sub-rules']['cf_dg_seg_aaaaaa'][-1] = 'MATCH,其他策略'
        elif change == 'nonblocking':
            candidate['sub-rules']['cf_dg_seg_aaaaaa'].insert(0, 'DOMAIN,leak.test,DIRECT')
        else:
            candidate['sub-rules']['cf_dg_seg_bbbbbb'] = ['MATCH,DIRECT']
    else:
        plugins = {plugin['tag']: plugin for plugin in candidate['plugins']}
        if change == 'member':
            plugins['cf_dg_set_aaaaaa']['args']['ips'] = ['192.168.60.99/32']
        elif change == 'tail':
            plugins['cf_dg_seg_aaaaaa']['args'][-1] = {'exec': 'goto proxy_dns_seq'}
        elif change == 'nonblocking':
            plugins['cf_dg_seg_aaaaaa']['args'].insert(0, {'matches': ['qname $rules_domain_1'], 'exec': 'goto china_dns'})
        else:
            candidate['plugins'].append({'tag': 'cf_dg_seg_bbbbbb', 'type': 'sequence', 'args': [{'exec': 'goto cf_dg_local'}]})
    with pytest.raises(RuleSyncProjectionError):
        isolate_rule_bundle(bundle(previous, service), bundle(candidate, service), service, device_groups_enabled=True)


def test_candidate_entry_rejects_unprojected_group_draft_before_generating_or_fetching():
    base = source()
    draft = deepcopy(base)
    draft['device_groups']['groups'][1]['members'] = ['192.168.60.99']
    with pytest.raises(ValueError, match='草稿必须手动发布'):
        rule_sync.prepare_candidate({'service_type': 'mihomo'}, draft, {'source': base}, {}, 'id')


@pytest.mark.parametrize('service', ['oxidns', 'mihomo'])
def test_full_candidate_materialization_preserves_offline_files_and_updates_group_blockers(service, monkeypatch):
    from backend.test_rule_sync_projection import no_network, real_baseline
    no_network(monkeypatch)
    base = source()
    generated = emit(base, service)
    files = read_bundle_files(bundle(generated, service))
    pinned = [{'path': path, 'content': value, 'role': role}
              for path, (value, role) in files.items() if role == 'rule']
    agent, receipt = real_baseline(base, service, pinned_files=pinned)
    current = deepcopy(base)
    current['rule_configs'][0]['value'] = 'updated.test'
    current['device_groups']['groups'][1]['members'] = ['192.168.60.99']
    settings = {'library': False, 'rules': True}
    projected = project_rule_changes(base, current, settings, service_type=service)
    candidate = rule_sync.prepare_candidate(agent, projected, receipt, settings, 'group-rule-update')
    result = read_bundle_files(candidate)
    actual = yaml.safe_load(result[candidate.manifest['config_path']][0])
    assert candidate.manifest['reload_only'] and candidate.manifest['config_path'] == 'installed.yml'
    assert 'updated.test' in str(actual) and '192.168.60.99' not in str(actual)
    assert {path: value for path, value in result.items() if value[1] == 'rule'} == {
        path: value for path, value in read_bundle_files(receipt['bundle']).items() if value[1] == 'rule'}


def test_legacy_mihomo_custom_device_prefixes_keep_existing_isolation_semantics():
    previous = {'rules': ['MATCH,DIRECT'], 'sub-rules': {'cf_dg_seg_aaaaaa': ['MATCH,REJECT']},
                'rule-providers': {'cf_dg_set_aaaaaa': {'type': 'inline', 'behavior': 'ipcidr', 'payload': ['192.0.2.0/24']}}}
    proposed = deepcopy(previous)
    proposed['sub-rules']['cf_dg_seg_aaaaaa'] = ['DOMAIN,new.test,DIRECT']
    proposed['rule-providers']['cf_dg_set_aaaaaa']['payload'] = ['198.51.100.0/24']
    proposed['rules'] = ['DOMAIN,new.test,REJECT', 'MATCH,DIRECT']
    actual = document(isolate_rule_bundle(bundle(previous, 'mihomo'), bundle(proposed, 'mihomo'), 'mihomo'))
    assert actual['sub-rules'] == previous['sub-rules']
    assert actual['rule-providers'] == proposed['rule-providers']
    assert actual['rules'] == proposed['rules']


def test_legacy_oxidns_custom_device_prefixes_are_frozen_custom_plugins_and_files():
    config = source()
    config.pop('device_groups')
    previous = emit(config, 'oxidns')
    previous['plugins'].extend([
        {'tag': 'cf_dg_seg_aaaaaa', 'type': 'sequence', 'args': [{'exec': 'reject 3'}]},
        {'tag': 'cf_dg_block_aaaaaaaaaaaaaaaa', 'type': 'domain_set', 'args': {'files': ['./rules/custom.list']}},
    ])
    proposed = deepcopy(previous)
    normal = next(plugin for plugin in proposed['plugins'] if plugin['tag'].startswith('rules_domain_'))
    normal['args'].setdefault('exps', []).append('full:new.test')
    actual_bundle = isolate_rule_bundle(bundle(previous, 'oxidns'), bundle(proposed, 'oxidns', b'new rules'), 'oxidns')
    actual = document(actual_bundle)
    assert next(plugin for plugin in actual['plugins'] if plugin['tag'] == 'cf_dg_seg_aaaaaa') == previous['plugins'][-2]
    assert read_bundle_files(actual_bundle)['rules/custom.list'][0] == b'old rules'
    assert 'new.test' in str(actual)


def test_active_batch_pauses_rule_worker_and_rechecks_before_mutation(monkeypatch):
    agent = {'id': 'agent', 'profile_id': 'home', 'service_type': 'mihomo',
             'rule_sync': {'settings': {'rules': True}, 'state': {'native_task': 'accepted-task', 'key': 'old-key'}}}
    manager = SimpleNamespace(get_all_agents=lambda: [agent], get_agent_by_id=lambda _: agent)
    monkeypatch.setattr(device_group_publish, 'blocks_follow', lambda *_: True)
    worker = rule_sync.RuleSyncWorker(manager)
    states = []
    monkeypatch.setattr(worker, '_state', lambda *args, **kwargs: states.append((args, kwargs)))
    worker._run('agent')
    assert states[0][0][1] == 'pending' and '设备组批次' in states[0][0][2]
    assert states[0][1]['native_task'] == 'accepted-task'
    assert not rule_sync._unchanged(manager, agent, rule_sync.settings_for(agent))
