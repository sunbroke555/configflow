"""Persistence boundaries: migration, independent writers, and recoverable imports."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from backend.common.config import get_default_config
from backend.common.config_repository import ProfileRepository, ProfileInUse, ProfileValidationError


class ProfileRepositoryTest(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def legacy(self, hostname='one.example'):
        data = get_default_config()
        data['subscriptions'] = [{'id': 'sub', 'name': 'Source', 'url': f'https://{hostname}/subscription'}]
        data['nodes'] = [{'id': 'node', 'name': 'Node', 'server': hostname, 'type': 'socks5', 'port': 1080}]
        data['subscription_aggregations'] = [{'id': 'agg', 'name': 'Aggregate', 'subscriptions': ['sub'], 'nodes': ['node']}]
        data['rule_library'] = [{'id': 'lib', 'name': 'Domains', 'source_type': 'content', 'content': hostname, 'behavior': 'domain'}]
        data['proxy_groups'] = [{'id': 'group', 'name': 'Proxy', 'type': 'select', 'subscriptions': ['sub'], 'manual_nodes': ['node', 'DIRECT'], 'aggregations': ['agg']}]
        data['rule_configs'] = [
            {'id': 'r1', 'itemType': 'ruleset', 'library_rule_id': 'lib', 'name': 'Domains', 'behavior': 'domain', 'policy': 'DIRECT'},
            {'id': 'r2', 'itemType': 'rule', 'rule_type': 'MATCH', 'policy': 'Proxy'},
        ]
        data['mosdns']['direct_rulesets'] = ['r1']
        return data

    def test_single_config_migration_retains_behavior_and_original_backup(self):
        legacy = self.legacy()
        legacy['system_config']['config_token'] = 'secret'
        legacy['agents'] = [{'id': 'agent', 'name': 'Agent', 'token': 'agent-secret'}]
        (self.root / 'config.json').write_text(json.dumps(legacy), encoding='utf-8')
        repository = ProfileRepository(self.root, get_default_config)
        resolved = repository.get_compat_config('default')
        self.assertEqual(resolved['subscriptions'], legacy['subscriptions'])
        self.assertEqual(resolved['proxy_groups'], legacy['proxy_groups'])
        self.assertEqual([rule['id'] for rule in resolved['rule_configs']], ['r1', 'r2'])
        self.assertEqual([rule['policy'] for rule in resolved['rule_configs']], ['DIRECT', 'Proxy'])
        self.assertEqual(resolved['mosdns']['direct_rulesets'], ['r1'])
        self.assertEqual(repository.get_system()['agents'][0]['profile_id'], 'default')
        self.assertEqual(repository.get_system()['system_config']['config_token'], 'secret')
        snapshots = list((self.root / 'migrations').glob('*/migration/config.json'))
        self.assertEqual(json.loads(snapshots[0].read_text()), legacy)
        reopened = ProfileRepository(self.root, get_default_config)
        self.assertEqual(reopened.export_all(), repository.export_all())


    def test_original_inline_rules_become_shared_sources_without_changing_policies(self):
        legacy = self.legacy()
        legacy['rule_configs'].append({
            'id': 'inline', 'itemType': 'ruleset', 'name': 'Domains',
            'content': 'private.example', 'behavior': 'domain',
            'policy': 'Proxy', 'enabled': False,
        })
        repository = ProfileRepository(self.root, get_default_config)
        repository.import_all(legacy)
        resolved = repository.get_compat_config('default')
        rules = {rule['id']: rule for rule in resolved['rule_configs']}
        self.assertEqual(rules['r1']['content'], 'one.example')
        self.assertEqual(rules['inline']['content'], 'private.example')
        self.assertEqual(rules['inline']['policy'], 'Proxy')
        self.assertFalse(rules['inline']['enabled'])
        self.assertNotEqual(rules['inline']['library_rule_id'], rules['r1']['library_rule_id'])
        self.assertNotIn('content', repository.get_profile('default')['rule_configs'][-1])

    def test_unsupported_import_preserves_existing_data_and_original_file(self):
        repository = ProfileRepository(self.root, get_default_config)
        repository.import_all(self.legacy())
        before = repository._store.fingerprint()
        invalid = repository.export_all()
        invalid['schema_version'] = 2
        with self.assertRaises(ProfileValidationError):
            repository.import_all(invalid)
        self.assertEqual(repository._store.fingerprint(), before)

    def test_independent_concurrent_profile_writes_and_shared_updates_survive(self):
        repository = ProfileRepository(self.root, get_default_config)
        repository.create_profile({'name': 'Second', 'id': 'second'})
        other_writer = ProfileRepository(self.root, get_default_config)
        one = repository.get_profile('default')
        two = other_writer.get_profile('second')
        one['mihomo']['custom_config'] = 'mixed-port: 7901'
        two['mihomo']['custom_config'] = 'mixed-port: 7902'
        shared = repository.get_shared()
        shared['nodes'] = [{'id': 'node', 'name': 'Node', 'server': 'new.example', 'port': 1080, 'type': 'socks5'}]
        with ThreadPoolExecutor(max_workers=3) as pool:
            jobs = [pool.submit(repository.save_profile, 'default', one),
                    pool.submit(other_writer.save_profile, 'second', two),
                    pool.submit(repository.save_shared, shared)]
            for job in jobs:
                job.result()
        self.assertEqual(repository.get_compat_config('default')['mihomo']['custom_config'], 'mixed-port: 7901')
        self.assertEqual(repository.get_compat_config('second')['mihomo']['custom_config'], 'mixed-port: 7902')
        self.assertEqual(repository.get_shared()['nodes'][0]['server'], 'new.example')
        with self.assertRaises(ProfileInUse):
            repository.save_profile('default', one)

    def test_import_is_whole_system_atomic_and_invalidates_old_snapshots(self):
        repository = ProfileRepository(self.root, get_default_config)
        repository.import_all(self.legacy())
        repository.create_profile({'name': 'Second', 'id': 'second'}, clone_from='default')
        system = repository.get_system()
        system['agents'] = [{'id': 'agent', 'profile_id': 'second', 'token': 'secret'}]
        system['backup'] = {'webdav_password': 'restore-me'}
        repository.save_system(system)
        backup = repository.export_all()
        stale = repository.get_profile('default')
        changed = repository.get_profile('second')
        changed['mihomo']['custom_config'] = 'mixed-port: 9900'
        repository.save_profile('second', changed)
        invalid = copy.deepcopy(backup)
        invalid['profiles']['second']['proxy_groups'][0]['manual_nodes'] = ['missing']
        before = repository.export_all()
        with self.assertRaises(ProfileInUse):
            repository.import_all(invalid)
        self.assertEqual(repository.export_all(), before)
        repository.import_all(backup)
        self.assertEqual(repository.get_profile('second')['mihomo'], backup['profiles']['second']['mihomo'])
        self.assertEqual(repository.get_system()['agents'], backup['system']['agents'])
        self.assertEqual(repository.get_system()['backup']['webdav_password'], 'restore-me')
        with self.assertRaises(ProfileInUse):
            repository.save_profile('default', stale)

    def test_profile_only_accepts_reference_ids_and_never_persists_source_copies(self):
        repository = ProfileRepository(self.root, get_default_config)
        repository.import_all(self.legacy())
        snapshot = repository.get_compat_config('default')
        snapshot['nodes'][0]['server'] = 'override.example'
        snapshot['rule_configs'][0]['content'] = 'override.example'
        repository.save_profile('default', snapshot)
        resolved = repository.get_compat_config('default')
        self.assertEqual(resolved['nodes'][0]['server'], 'one.example')
        self.assertEqual(resolved['rule_configs'][0]['content'], 'one.example')
        raw = repository.get_profile('default')
        self.assertNotIn('nodes', raw)
        self.assertNotIn('content', raw['rule_configs'][0])
        raw['proxy_groups'][0]['manual_nodes'] = [{'id': 'node', 'server': 'override.example'}]
        with self.assertRaises(ProfileValidationError):
            repository.save_profile('default', raw)




def test_shared_deletion_and_disable_reports_all_profile_usage(tmp_path):
    import pytest
    repository = ProfileRepository(tmp_path)
    repository.update_shared_transaction(lambda shared: shared['nodes'].append({'id': 'n', 'name': 'Node'}))
    repository.update_profile_fields('default', {'proxy_groups': [
        {'id': 'choice', 'name': 'Choice', 'type': 'select', 'manual_nodes': ['n']} ]})
    repository.clone_profile('default', {'id': 'other', 'name': 'Other'})
    for updater in (lambda shared: shared['nodes'].clear(),
                    lambda shared: shared['nodes'][0].update(enabled=False)):
        with pytest.raises(ProfileInUse) as error:
            repository.update_shared_transaction(updater)
        assert {usage['id'] for usage in error.value.usages} == {'default', 'other'}


def test_system_transactions_validate_nested_profile_reads_and_bindings(tmp_path):
    import pytest
    repository = ProfileRepository(tmp_path)
    repository.create_profile({'id': 'bound', 'name': 'Bound'})
    def bind(system):
        assert repository._profile_metadata('bound')['name'] == 'Bound'
        system['agents'].append({'id': 'a', 'name': 'Agent', 'profile_id': 'bound'})
    repository.update_system_transaction(bind)
    with pytest.raises(ProfileInUse):
        repository.delete_profile('bound')
    before = repository.export_all()
    with pytest.raises(ProfileValidationError):
        repository.update_system_transaction(lambda system: system['agents'][0].update(profile_id='missing'))
    assert repository.export_all() == before
    assert set(repository.get_system()) == {'system_config', 'backup', 'agents', '_revision'}


def test_shared_cache_paths_are_atomic_and_reject_escape(tmp_path):
    import pytest
    repository = ProfileRepository(tmp_path)
    repository.write_shared_json('subscribes/sub.json', {'nodes': []})
    assert repository.read_shared_json('subscribes/sub.json') == {'nodes': []}
    repository.write_shared_text('rules/domains.txt', 'example.com')
    assert (repository.shared_rules_dir() / 'domains.txt').read_text() == 'example.com'
    for path in ('../escape.json', '/tmp/escape.json', ''):
        with pytest.raises(ProfileValidationError):
            repository.write_shared_json(path, {})




def test_global_settings_three_way_merge_preserves_concurrent_agent_updates(tmp_path):
    from backend.common import config
    previous = config._repository
    repository = ProfileRepository(tmp_path)
    try:
        config.set_repository(repository)
        snapshot = config.get_system_config()
        snapshot['system_config']['server_domain'] = 'https://configured.example'
        repository.update_system_transaction(lambda system: system['agents'].append({'id': 'a', 'profile_id': 'default'}))
        config.save_system_config(snapshot)
        assert repository.get_system()['agents'] == [{'id': 'a', 'profile_id': 'default'}]
        assert repository.get_system()['system_config']['server_domain'] == 'https://configured.example'
    finally:
        config.set_repository(previous)
        config.reset_config_context()



def _rule_dns_repository(tmp_path):
    repository = ProfileRepository(tmp_path)
    repository.save_shared({'rule_library': [
        {'id': 'lib', 'name': 'Ads', 'source_type': 'content', 'content': 'ads.test', 'behavior': 'domain'}]})
    repository.save_profile('default', {'rule_configs': [
        {'id': 'rule', 'itemType': 'rule', 'rule_type': 'DOMAIN', 'value': 'a.test', 'policy': 'DIRECT'},
        {'id': 'set', 'itemType': 'ruleset', 'library_rule_id': 'lib', 'policy': 'REJECT'}]})
    repository.clone_profile('default', {'id': 'other', 'name': 'Other'})
    return repository


def test_shared_and_profiles_transaction_commits_once(tmp_path):
    repository = _rule_dns_repository(tmp_path)
    shared_revision = repository.get_shared()['_revision']
    revisions = {pid: repository.get_profile(pid)['_revision'] for pid in ('default', 'other')}
    seen = {}

    def add(shared):
        shared['rule_library'].append({'id': 'new', 'name': 'New', 'source_type': 'content',
                                       'content': 'new.test', 'behavior': 'classical'})

    def compose(profile_id, before, after):
        # before is resolved against the shared data as it was, after against the new one.
        seen[profile_id] = ([item['id'] for item in before['rule_library']], [item['id'] for item in after['rule_library']])
        after['rule_configs'].insert(0, {'id': 'ref', 'itemType': 'ruleset', 'library_rule_id': 'new', 'policy': 'DIRECT'})
        after['rule_configs'][0]['behavior'] = 'classical'  # Hydrated fields never persist.

    shared, profiles = repository.update_shared_and_profiles_transaction(
        add, compose, lambda shared, profiles: [pid for pid in profiles if pid == 'other'])
    assert seen == {'other': (['lib'], ['lib', 'new'])}
    assert set(profiles) == {'other'} and shared['_revision'] == shared_revision + 1
    assert repository.get_profile('other')['_revision'] == revisions['other'] + 1
    assert repository.get_profile('default')['_revision'] == revisions['default']
    stored = repository.get_profile('other')['rule_configs'][0]
    assert stored == {'id': 'ref', 'itemType': 'ruleset', 'library_rule_id': 'new', 'policy': 'DIRECT'}
    resolved = repository.get_compat_config('other')['rule_configs'][0]
    assert resolved['behavior'] == 'classical' and resolved['url'].endswith('/rule-library/content/new')


def test_shared_and_profiles_transaction_rolls_back_all(tmp_path):
    import pytest
    repository = _rule_dns_repository(tmp_path)
    before = repository.export_all()

    def fail(profile_id, _, after):
        after['rule_configs'].clear()
        if profile_id == 'other':
            raise ProfileValidationError('boom')

    with pytest.raises(ProfileValidationError):
        repository.update_shared_and_profiles_transaction(
            lambda shared: shared['rule_library'][0].update(name='Renamed'), fail, ['default', 'other'])
    assert repository.export_all() == before
    # Whole-document validation also rejects everything at once.
    with pytest.raises(ProfileInUse):
        repository.update_shared_and_profiles_transaction(
            lambda shared: shared['rule_library'][0].update(name='Renamed'),
            lambda pid, _, after: after['oxidns'].update(rule_dns_follow=['missing']), ['default'])
    assert repository.export_all() == before


def test_rule_dns_ids_must_exist(tmp_path):
    import pytest
    repository = _rule_dns_repository(tmp_path)
    repository.update_profile_transaction('default', lambda profile: profile['oxidns'].update(
        rule_actions={'set': {'action': 'blackhole'}}, rule_dns_follow=['rule'],
        rule_dns_parked={'rule': {'action': 'direct'}}))
    for field, value in (('rule_actions', {'gone': {'action': 'refused'}}), ('rule_dns_follow', ['gone']),
                         ('rule_dns_parked', {'gone': {'action': 'proxy'}})):
        with pytest.raises(ProfileInUse, match='OxiDNS DNS 处理仍引用不存在的规则 gone'):
            repository.update_profile_transaction('default', lambda profile: profile['oxidns'].update({field: value}))
    with pytest.raises(ProfileValidationError):
        repository.update_profile_transaction('default', lambda profile: profile['oxidns'].update(rule_actions='x'))


def test_rule_dns_action_shapes_are_validated(tmp_path):
    import pytest
    repository = _rule_dns_repository(tmp_path)
    for field, value in (('rule_actions', {'set': {'kind': 'blackhole'}}), ('rule_actions', {'set': 'reject'}),
                         ('rule_actions', {'set': {'action': 'direct'}}),
                         ('rule_actions', {'set': {'action': 'upstream', 'upstream': {'addr': '1.1.1.1'}}}),
                         ('rule_dns_parked', {'rule': {'action': 'drop'}})):
        with pytest.raises(ProfileValidationError, match=f'OxiDNS {field} 中规则'):
            repository.update_profile_transaction('default', lambda profile: profile['oxidns'].update({field: value}))
    # Imports skip the settings normalizer and must be refused the same way.
    exported = repository.export_profile('default')
    exported['oxidns']['rule_actions'] = {'set': {'kind': 'blackhole'}}
    with pytest.raises(ProfileValidationError):
        repository.import_profile('default', exported)
    assert repository.get_profile('default')['oxidns'].get('rule_actions', {}) == {}


def test_every_delete_path_cleans_rule_dns_fields(tmp_path):
    from flask import Flask
    from backend.common import config as config_module
    from backend.routes import register_blueprints
    repository = _rule_dns_repository(tmp_path)
    fields = {'rule_actions': {'set': {'action': 'blackhole'}}, 'rule_dns_follow': ['rule'], 'rule_dns_parked': {}}
    for profile_id in ('default', 'other'):
        repository.update_profile_transaction(profile_id, lambda profile: profile['oxidns'].update(copy.deepcopy(fields)))
    previous = config_module._repository
    config_module.set_repository(repository)
    try:
        app = Flask(__name__)
        register_blueprints(app)
        client = app.test_client()
        headers = {'X-ConfigFlow-Profile': 'other'}
        assert client.delete('/api/rules/set', headers=headers).status_code == 200
        assert client.delete('/api/rules/rule', headers=headers).status_code == 200
        oxidns = repository.get_profile('other')['oxidns']
        assert (oxidns['rule_actions'], oxidns['rule_dns_follow'], oxidns['rule_dns_parked']) == ({}, [], {})
    finally:
        config_module.set_repository(previous)
        config_module.reset_config_context()
    # An import replacing the composition drops the DNS entries of rules it lacks.
    exported = repository.export_profile('default')
    exported['rule_configs'] = [item for item in exported['rule_configs'] if item['id'] != 'set']
    del exported['oxidns']
    repository.import_profile('default', exported)
    oxidns = repository.get_profile('default')['oxidns']
    assert oxidns['rule_actions'] == {} and oxidns['rule_dns_follow'] == ['rule']


if __name__ == '__main__':
    unittest.main()
