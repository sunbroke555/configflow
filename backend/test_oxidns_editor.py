"""Unified editor migration and template preview preserve active native state."""
from copy import deepcopy

import pytest
import yaml

from backend.common.oxidns_editor import prepare_editor, preview_template, _merge
from backend.common.oxidns_native import load_native_yaml
from backend.common.oxidns_settings import OxidnsSettingsError, normalize_oxidns_settings
from backend.converters.oxidns import generate_oxidns_config, get_oxidns_custom_files, get_oxidns_ruleset_downloads
from backend.test_oxidns_integration import oxidns_app


def prepared(**fields):
    return prepare_editor(fields, {})


def document(settings):
    return load_native_yaml(settings['native_config'])


def edit_document(settings, edit):
    result = deepcopy(settings)
    value = document(result)
    edit(value)
    result['native_config'] = yaml.safe_dump(value, allow_unicode=True, sort_keys=False)
    return result


def plugin(value, tag):
    return next(item for item in value['plugins'] if item['tag'] == tag)


def test_template_migration_keeps_active_generated_values_and_backups_inactive_native():
    native = '# inactive draft\nplugins: [{tag: private, type: future_plugin, args: {flag: false}}]\n'
    original = {'worker_threads': 7, 'cache_lazy_ttl': 0, 'prefer_ipv4': False,
                'native_config': native, 'additional_files': [{'path': 'private.txt', 'content': 'secret', 'encoding': 'text'}]}
    before = deepcopy(original)
    result = prepare_editor(original, {})
    assert result['config_mode'] == 'native'
    assert result['legacy_native_config'] == native
    assert result['template_base_config'] == result['native_config']
    assert result['template_settings']['cache_lazy_ttl'] == 0
    assert result['template_settings']['prefer_ipv4'] is False
    assert not {'native_config', 'additional_files', 'template_settings', 'template_base_config'} & result['template_settings'].keys()
    assert result['additional_files'] == original['additional_files']
    assert document(result)['runtime']['worker_threads'] == 7
    assert document(result) == yaml.safe_load(generate_oxidns_config({'oxidns': original}))
    assert original == before


def test_existing_backup_never_overwritten_and_identical_inactive_native_needs_no_backup():
    result = prepared()
    current = {**result['template_settings'], 'native_config': result['native_config']}
    assert 'legacy_native_config' not in prepare_editor(current, {})
    current.update(native_config='plugins: []', legacy_native_config='earlier draft')
    assert prepare_editor(current, {})['legacy_native_config'] == 'earlier draft'


def test_native_open_keeps_exact_text_and_does_not_validate_or_render_old_template():
    text = '# keep comment and spelling\nplugins: [{tag: incomplete, type: http_server, args: {}}]\nfuture: [0, false, null]\n'
    settings = {'config_mode': 'native', 'native_config': text, 'worker_threads': -99, 'adguard': None,
                'custom_config': 'bad: [yaml', 'listen': '', 'legacy_native_config': 'backup'}
    before = deepcopy(settings)
    result = prepare_editor(settings, {})
    assert result['native_config'] == text
    assert result['legacy_native_config'] == 'backup'
    assert 'template_base_config' not in result
    assert settings == before


def test_native_prepare_is_idempotent():
    first = prepared(worker_threads=8, native_config='not valid [')
    assert prepare_editor(first, {}) == first


def test_three_way_merge_retains_manual_unknown_fields_and_native_scalar_types():
    start = prepared()
    current = edit_document(start, lambda value: (
        value.update(future={'false': False, 'zero': 0, 'null': None}),
        plugin(value, 'lazy_cache')['args'].update(size=321, experimental={'n': 0, 'enabled': False}),
        value['plugins'].append({'tag': 'manual', 'type': 'future_plugin', 'args': {'nested': [0, False, None]}}),
    ))
    next_inputs = {**start['template_settings'], 'worker_threads': 9, 'cache_lazy_ttl': 600}
    before = deepcopy(current)
    preview = preview_template(current, next_inputs, {})
    value = document(preview['settings'])
    assert value['runtime']['worker_threads'] == 9
    assert plugin(value, 'lazy_cache')['args'] == {'size': 321, 'lazy_cache_ttl': 600, 'short_circuit': True,
                                                  'experimental': {'n': 0, 'enabled': False}}
    assert value['future'] == {'false': False, 'zero': 0, 'null': None}
    assert plugin(value, 'manual')['args'] == {'nested': [0, False, None]}
    assert preview['conflicts'] == []
    assert current == before


def test_conflicts_default_to_current_and_template_resolution_is_explicit():
    start = prepared(worker_threads=2)
    current = edit_document(start, lambda value: value['runtime'].update(worker_threads=5))
    next_inputs = {**start['template_settings'], 'worker_threads': 7}
    preview = preview_template(current, next_inputs, {})
    assert document(preview['settings'])['runtime']['worker_threads'] == 5
    assert preview['conflicts'] == [{'path': '/runtime/worker_threads', 'label': 'runtime.worker_threads', 'choice': 'current'}]
    resolved = preview_template(current, next_inputs, {}, resolutions={'/runtime/worker_threads': 'template'})
    assert document(resolved['settings'])['runtime']['worker_threads'] == 7
    assert resolved['conflicts'][0]['choice'] == 'template'
    assert {'path': '/runtime/worker_threads', 'kind': 'update'} in resolved['changes']
    # Conflict information never contains credential values.
    assert all(set(conflict) == {'path', 'label', 'choice'} for conflict in preview['conflicts'])


def test_bool_and_number_are_distinct_in_three_way_merge():
    conflicts = []
    merged = _merge({'value': 1}, {'value': False}, {'value': 0}, (), {}, conflicts)
    assert merged['value'] is False
    assert conflicts[0]['path'] == '/value'


def test_plugin_order_preserves_current_and_inserts_new_near_template_neighbors():
    def item(tag, value=1):
        return {'tag': tag, 'type': 'future_plugin', 'args': {'value': value}}
    base = {'plugins': [item('a'), item('b'), item('c')]}
    current = {'plugins': [item('b'), item('manual'), item('a'), item('c')]}
    next_value = {'plugins': [item('first'), item('a'), item('added'), item('b'), item('c'), item('last')]}
    merged = _merge(base, current, next_value, (), {}, [])
    tags = [entry['tag'] for entry in merged['plugins']]
    assert [tag for tag in tags if tag not in ('first', 'added', 'last')] == ['b', 'manual', 'a', 'c']
    assert tags.index('first') + 1 == tags.index('a')
    assert tags.index('a') + 1 == tags.index('added')
    assert tags.index('c') + 1 == tags.index('last')


def test_template_reordering_alone_does_not_reorder_current_plugins():
    base = {'plugins': [{'tag': tag, 'type': 'debug_print'} for tag in ['a', 'b']]}
    next_value = {'plugins': list(reversed(base['plugins']))}
    assert _merge(base, base, next_value, (), {}, []) == base


def test_removed_unchanged_plugin_is_deleted_but_manually_changed_plugin_conflicts():
    base = {'plugins': [{'tag': 'a', 'type': 'cache', 'args': {'size': 1}}, {'tag': 'b', 'type': 'cache'}]}
    current = deepcopy(base)
    current['plugins'][0]['args']['size'] = 2
    conflicts = []
    result = _merge(base, current, {'plugins': []}, (), {}, conflicts)
    assert result == {'plugins': [current['plugins'][0]]}
    assert conflicts == [{'path': '/plugins/a', 'label': '插件「a」', 'choice': 'current'}]
    assert _merge(base, current, {'plugins': []}, (), {'/plugins/a': 'template'}, []) == {'plugins': []}


def test_other_arrays_merge_atomically_and_tag_paths_are_escaped():
    base = {'plugins': [{'tag': 'dots.tag', 'type': 'sequence', 'args': [{'exec': 'accept'}]}]}
    current = deepcopy(base)
    current['plugins'][0]['args'] = [{'exec': 'reject 3'}]
    proposed = deepcopy(base)
    proposed['plugins'][0]['args'] = [{'exec': 'reject 5'}]
    conflicts = []
    assert _merge(base, current, proposed, (), {}, conflicts) == current
    assert conflicts[0]['path'] == '/plugins/dots.tag/args'
    assert _merge({'a/b': 1}, {'a/b': 2}, {'a/b': 3}, (), {'/a~1b': 'template'}, []) == {'a/b': 3}


def test_unchanged_preview_keeps_native_comments_and_does_not_add_defaults():
    start = prepared()
    start['native_config'] = '# Keep this comment\n' + start['native_config']
    result = preview_template(start, start['template_settings'], {})
    assert result['settings']['native_config'] == start['native_config']
    assert result['changes'] == []
    assert result['conflicts'] == []


def test_preview_keeps_incomplete_legacy_plugin_editable_and_all_attachments():
    start = prepared()
    start['additional_files'] = [{'path': 'manual.txt', 'content': 'private content', 'encoding': 'text'}]
    current = edit_document(start, lambda value: value['plugins'].append({'tag': 'unfinished', 'type': 'http_server', 'args': {}}))
    result = preview_template(current, {**start['template_settings'], 'worker_threads': 3}, {})
    assert plugin(document(result['settings']), 'unfinished')['args'] == {}
    assert result['settings']['additional_files'] == start['additional_files']


def test_repeated_template_application_updates_baseline_without_replaying_old_changes():
    start = prepared(worker_threads=2)
    current = edit_document(start, lambda value: value['runtime'].update(worker_threads=5))
    first = preview_template(current, {**start['template_settings'], 'worker_threads': 7}, {})
    second = preview_template(first['settings'], first['settings']['template_settings'], {})
    assert second['conflicts'] == []
    assert second['changes'] == []
    assert document(second['settings'])['runtime']['worker_threads'] == 5
    third_inputs = {**second['settings']['template_settings'], 'worker_threads': 8}
    third = preview_template(second['settings'], third_inputs, {})
    assert third['conflicts'][0]['path'] == '/runtime/worker_threads'
    assert document(third['settings'])['runtime']['worker_threads'] == 5


def test_resource_sources_union_keeps_previous_refs_and_updates_same_path_content():
    start = prepared(direct_rules=['a'], direct_rulesets=['old'], custom_hosts='full:test 192.0.2.1',
                     adguard={'enabled': True, 'urls': ['https://old.test/rules'], 'content': '||old.test^'})
    inputs = deepcopy(start['template_settings'])
    inputs.update(direct_rules=['b'], direct_rulesets=['new'], custom_hosts='full:test 192.0.2.2')
    inputs['adguard'].update(urls=['https://new.test/rules'], content='||new.test^')
    result = preview_template(start, inputs, {})
    settings = result['settings']
    assert settings['direct_rules'] == ['a', 'b']
    assert settings['direct_rulesets'] == ['old', 'new']
    assert settings['adguard']['urls'] == ['https://old.test/rules', 'https://new.test/rules']
    assert settings['custom_hosts'] == 'full:test 192.0.2.2'
    assert settings['adguard']['content'] == '||new.test^'
    paths = {change['path'] for change in result['changes']}
    assert {'/resources/custom_hosts', '/resources/adguard/content'} <= paths
    files = {item['path']: item['content'] for item in get_oxidns_custom_files({'oxidns': settings})}
    assert files['./rules/custom_hosts.txt'] == 'full:test 192.0.2.2'
    assert files['./rules/adguard-custom.txt'] == '||new.test^'


def test_resource_conflicts_are_reviewable_and_can_choose_template():
    start = prepared(custom_hosts='full:test 192.0.2.1', adguard={'enabled': True, 'content': '||old.test^'})
    start['custom_hosts'] = 'full:test 192.0.2.9'
    start['adguard']['content'] = '||manual.test^'
    inputs = deepcopy(start['template_settings'])
    inputs['custom_hosts'] = 'full:test 192.0.2.2'
    inputs['adguard']['content'] = '||new.test^'
    result = preview_template(start, inputs, {})
    assert result['settings']['custom_hosts'] == start['custom_hosts']
    assert result['settings']['adguard']['content'] == start['adguard']['content']
    paths = {conflict['path'] for conflict in result['conflicts']}
    assert paths == {'/resources/custom_hosts', '/resources/adguard/content'}
    resolved = preview_template(start, inputs, {}, resolutions={path: 'template' for path in paths})
    assert resolved['settings']['custom_hosts'] == inputs['custom_hosts']
    assert resolved['settings']['adguard']['content'] == inputs['adguard']['content']


def test_adguard_disabling_preserves_source_for_manually_kept_native_provider():
    start = prepared(adguard={'enabled': True, 'urls': ['https://old.test/rules'], 'content': ''})
    current = edit_document(start, lambda value: plugin(value, 'adguard_rules')['args'].update(future=True))
    inputs = deepcopy(start['template_settings'])
    inputs['adguard']['enabled'] = False
    result = preview_template(current, inputs, {})
    assert result['settings']['adguard']['enabled'] is True
    assert get_oxidns_ruleset_downloads({'oxidns': result['settings']})[0]['url'] == 'https://old.test/rules'


@pytest.mark.parametrize('resolutions', [[], {'/x': 'discard'}, {'/x': None}])
def test_invalid_conflict_resolution_is_rejected(resolutions):
    start = prepared()
    with pytest.raises(OxidnsSettingsError, match='resolutions'):
        preview_template(start, start['template_settings'], {}, resolutions=resolutions)


def test_api_prepare_preview_only_save_when_requested_and_preserve_profile(oxidns_app):
    app, repository = oxidns_app
    client = app.test_client()
    before = deepcopy(repository.get_profile('office'))
    response = client.post('/api/profiles/office/oxidns/editor', json={'settings': before['oxidns']})
    assert response.status_code == 200, response.get_json()
    assert response.headers['Cache-Control'] == 'no-store'
    settings = response.get_json()['settings']
    assert settings['config_mode'] == 'native'
    assert repository.get_profile('office') == before
    response = client.post('/api/profiles/office/oxidns/template-preview', json={
        'settings': settings, 'template_settings': {**settings['template_settings'], 'worker_threads': 7},
    })
    assert response.status_code == 200, response.get_json()
    assert response.headers['Cache-Control'] == 'no-store'
    applied = response.get_json()['settings']
    assert document(applied)['runtime']['worker_threads'] == 7
    assert repository.get_profile('office') == before
    saved = client.post('/api/profiles/office/oxidns/settings', json=applied)
    assert saved.status_code == 200, saved.get_json()
    assert repository.get_profile('office')['oxidns']['native_config'] == applied['native_config']
    assert repository.get_profile('office')['mosdns'] == before['mosdns']
    assert client.get('/api/profiles/office/oxidns/settings').get_json()['template_settings']['worker_threads'] == 7


def test_api_native_open_and_save_accept_incomplete_draft_but_render_is_blocked(oxidns_app):
    app, repository = oxidns_app
    client = app.test_client()
    before = deepcopy(repository.get_profile('office'))
    invalid = {'config_mode': 'native', 'native_config': 'plugins: [{tag: http, type: http_server, args: {}}]', 'worker_threads': -1}
    response = client.post('/api/profiles/office/oxidns/editor', json={'settings': invalid})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()['settings']['native_config'] == invalid['native_config']
    assert repository.get_profile('office') == before
    settings = response.get_json()['settings']
    saved = client.post('/api/profiles/office/oxidns/settings', json=settings)
    assert saved.status_code == 200, saved.get_json()
    assert saved.get_json()['draft'] is True
    assert repository.get_profile('office')['oxidns']['native_config'] == invalid['native_config']
    assert repository.get_profile('office')['oxidns']['worker_threads'] == -1
    assert repository.get_profile('office')['mosdns'] == before['mosdns']
    persisted = deepcopy(repository.get_profile('office'))
    rendered = client.post('/api/profiles/office/oxidns/render', json={'settings': settings})
    assert rendered.status_code == 400, rendered.get_json()
    assert 'plugins[0].args.listen' in rendered.get_json()['message']
    assert repository.get_profile('office') == persisted


def test_failed_template_generation_does_not_mutate_active_or_saved_settings(oxidns_app):
    app, repository = oxidns_app
    client = app.test_client()
    before = deepcopy(repository.get_profile('office'))
    start = prepared()
    snapshot = deepcopy(start)
    response = client.post('/api/profiles/office/oxidns/template-preview', json={
        'settings': start, 'template_settings': {**start['template_settings'], 'worker_threads': -1},
    })
    assert response.status_code == 400
    assert start == snapshot
    assert repository.get_profile('office') == before
    assert client.post('/api/oxidns/editor', json={'settings': []}).status_code == 400
    assert client.post('/api/oxidns/template-preview', json={'settings': start}).status_code == 400


def test_editor_endpoints_require_authentication_when_login_enabled(oxidns_app, monkeypatch):
    from backend.common import auth
    app, _ = oxidns_app
    monkeypatch.setattr(auth, 'ADMIN_USERNAME', 'admin')
    monkeypatch.setattr(auth, 'ADMIN_PASSWORD', 'secret')
    for endpoint in ('editor', 'template-preview'):
        assert app.test_client().post('/api/oxidns/' + endpoint, json={'settings': {}}).status_code == 401


@pytest.mark.parametrize('resource', ['custom_hosts', 'adguard'])
def test_clearing_template_resource_keeps_content_still_referenced_by_manual_plugin(resource):
    start = prepared(custom_hosts='full:test 192.0.2.1', adguard={'enabled': True, 'content': '||test^'})
    provider_tag = 'custom_hosts' if resource == 'custom_hosts' else 'adguard_rules'
    current = edit_document(start, lambda value: plugin(value, provider_tag)['args'].update(manual=True))
    inputs = deepcopy(start['template_settings'])
    if resource == 'custom_hosts':
        inputs['custom_hosts'] = ''
        path = '/resources/custom_hosts'
    else:
        inputs['adguard'].update(enabled=False, content='')
        path = '/resources/adguard/content'
    result = preview_template(current, inputs, {})
    assert path in {conflict['path'] for conflict in result['conflicts']}
    assert result['settings']['custom_hosts'] == start['custom_hosts']
    assert result['settings']['adguard']['content'] == start['adguard']['content']
    assert len(get_oxidns_custom_files({'oxidns': result['settings']})) == 2
    with pytest.raises(OxidnsSettingsError, match='仍被完整配置引用'):
        preview_template(current, inputs, {}, resolutions={path: 'template'})
    # Explicitly removing both the provider and its resource is allowed.
    resolved = preview_template(current, inputs, {}, resolutions={
        path: 'template', '/plugins/' + provider_tag: 'template',
    })
    assert provider_tag not in [entry['tag'] for entry in document(resolved['settings'])['plugins']]


def test_editor_metadata_with_internal_tokens_stays_exact_on_authenticated_endpoints(oxidns_app):
    from backend.common.config_export import redact_oxidns_settings
    app, repository = oxidns_app
    client = app.test_client()
    token = repository.get_compat_config('office')['system_config']['rule_proxy_token']
    original = deepcopy(repository.get_profile('office')['oxidns'])
    original.update(rule_update={'enabled': True}, native_config='plugins: []\nprivate: ' + token)
    response = client.post('/api/profiles/office/oxidns/editor', json={'settings': original})
    assert response.status_code == 200, response.get_json()
    settings = response.get_json()['settings']
    assert token in settings['native_config']
    assert token in settings['template_base_config']
    assert token in settings['legacy_native_config']
    assert '[REDACTED]' not in settings['native_config']
    preview = client.post('/api/profiles/office/oxidns/template-preview', json={
        'settings': settings, 'template_settings': {**settings['template_settings'], 'worker_threads': 7},
    })
    assert preview.status_code == 200, preview.get_json()
    merged = preview.get_json()['settings']
    assert token in merged['native_config'] and token in merged['template_base_config']
    saved = client.post('/api/profiles/office/oxidns/settings', json=merged)
    assert saved.status_code == 200, saved.get_json()
    for payload in [saved.get_json()['settings'], client.get('/api/profiles/office/oxidns/settings').get_json()]:
        assert token in payload['native_config'] and token in payload['template_base_config']
        assert token in payload['legacy_native_config']
    exported = redact_oxidns_settings(merged)
    assert exported['native_config'] == exported['template_base_config'] == exported['legacy_native_config'] == ''
    assert token not in repr(exported)


def test_template_migration_keeps_complete_zip_and_agent_bundle_resources_byte_exact(oxidns_app):
    import base64
    import io
    import tarfile
    import zipfile
    from backend.agents.deployment_bundle import build_deployment_bundle
    from backend.utils.oxidns_archive import build_oxidns_zip

    _, repository = oxidns_app
    config = repository.get_compat_config('office')
    config['oxidns'].update(
        custom_hosts='full:internal.test 192.0.2.15\n',
        adguard={'enabled': True, 'content': '||ad.test^\n', 'urls': [], 'mode': 'nxdomain'},
        additional_files=[{'path': 'assets/native.dat', 'encoding': 'base64',
                           'content': base64.b64encode(b'\x00\xff\x80native\r\n').decode()}],
    )
    original = deepcopy(config)
    unified = deepcopy(config)
    unified['oxidns'] = prepare_editor(config['oxidns'], config, 'http://config.test')

    def packaged(value):
        generated = generate_oxidns_config(value, 'http://config.test')
        downloads = get_oxidns_ruleset_downloads(value, 'http://config.test')
        custom = get_oxidns_custom_files(value)
        with zipfile.ZipFile(build_oxidns_zip(value, generated, downloads, custom,
                                             base_url='http://config.test')) as archive:
            zipped = {name: archive.read(name) for name in archive.namelist() if not name.endswith('/')}
        bundle = build_deployment_bundle({'id': 'migration-test', 'service_type': 'oxidns', 'profile_id': 'office'},
                                         value, generated, ruleset_downloads=downloads, custom_files=custom,
                                         base_url='http://config.test')
        with tarfile.open(fileobj=io.BytesIO(bundle.archive)) as archive:
            deployed = {member.name.removeprefix('files/'): archive.extractfile(member).read()
                        for member in archive.getmembers() if member.isfile() and member.name.startswith('files/')}
        return zipped, deployed

    before_zip, before_agent = packaged(config)
    after_zip, after_agent = packaged(unified)
    for before, after in [(before_zip, after_zip), (before_agent, after_agent)]:
        assert before.keys() == after.keys()
        assert load_native_yaml(before['config.yaml'].decode()) == load_native_yaml(after['config.yaml'].decode())
        assert {key: value for key, value in before.items() if key != 'config.yaml'} == {
            key: value for key, value in after.items() if key != 'config.yaml'}
        assert after['assets/native.dat'] == b'\x00\xff\x80native\r\n'
        assert after['rules/custom_hosts.txt'] == b'full:internal.test 192.0.2.15\n'
        assert after['rules/adguard-custom.txt'] == b'||ad.test^\n'
        assert any(b'example.test' in data for path, data in after.items() if path.startswith('rules/rule_'))
    assert config == original


# Per-rule DNS choices across template <-> native conversion.

BLACKHOLE = {'action': 'blackhole'}
NXDOMAIN = {'action': 'nxdomain'}


def rule_space():
    rule = lambda identity, **extra: {'id': identity, 'itemType': 'rule', 'rule_type': 'DOMAIN',
                                      'value': f'{identity}.test', 'policy': 'Proxy', **extra}
    ruleset = lambda identity, **extra: {'id': identity, 'itemType': 'ruleset', 'behavior': 'domain',
                                         'url': f'https://example.test/{identity}.txt', 'policy': 'Proxy', **extra}
    return {'system_config': {'server_domain': 'http://config.test', 'rule_proxy_token': 'test-only'},
            'rule_configs': [rule('a', policy='DIRECT'), rule('b'), rule('off', enabled=False),
                             rule('follow_off', enabled=False), ruleset('rs'),
                             ruleset('rs_off', library_enabled=False), ruleset('rs_off2', enabled=False)]}


def routing(settings, config):
    from backend.common.oxidns_common_routing import read_routing
    return read_routing(document(settings), settings, config)['values']


def test_prepare_editor_parks_inactive_actions():
    config = rule_space()
    template = {'direct_rules': ['a'], 'proxy_rules': ['follow_off'], 'proxy_rulesets': ['rs_off'],
                'rule_actions': {'b': BLACKHOLE, 'off': NXDOMAIN, 'deleted': NXDOMAIN},
                'rule_dns_follow': ['follow_off']}
    result = prepare_editor(template, config)
    assert result['rule_actions'] == {'b': BLACKHOLE}
    assert result['rule_dns_parked'] == {'off': NXDOMAIN, 'rs_off': {'action': 'proxy'}}
    assert result['rule_dns_follow'] == ['follow_off']
    # Lists stay a retained catalog; the YAML is exactly the template's.
    assert result['proxy_rulesets'] == ['rs_off'] and result['proxy_rules'] == ['follow_off']
    assert document(result) == yaml.safe_load(generate_oxidns_config({**config, 'oxidns': template}))
    assert routing(result, config)['rule_actions'] == {'b': BLACKHOLE}
    assert normalize_oxidns_settings(result, validate_native_arguments=False)['rule_dns_parked'] == result['rule_dns_parked']


def test_template_inputs_restores_parked():
    from backend.common.oxidns_editor import template_inputs
    config = rule_space()
    native = {'config_mode': 'native', 'native_config': 'plugins: []', 'direct_rules': ['a', 'b'], 'proxy_rules': ['b'],
              'direct_rulesets': ['rs_off'], 'rule_actions': {'b': {'action': 'refused'}},
              'rule_dns_parked': {'off': NXDOMAIN, 'rs_off': {'action': 'proxy'}, 'rs_off2': {'action': 'direct'}},
              'rule_dns_follow': ['follow_off']}
    inputs = template_inputs(native, config)
    assert inputs['rule_actions'] == {'b': {'action': 'refused'}, 'off': NXDOMAIN}
    assert inputs['rule_dns_parked'] == {}
    assert inputs['direct_rules'] == ['a'] and inputs['proxy_rules'] == []
    assert inputs['direct_rulesets'] == ['rs_off2'] and inputs['proxy_rulesets'] == ['rs_off']
    assert inputs['rule_dns_follow'] == ['follow_off']
    assert 'native_config' not in inputs and native['rule_dns_parked']['off'] == NXDOMAIN
    # Re-enabled in the template, every parked choice routes again.
    enabled = {**config, 'rule_configs': [{key: value for key, value in item.items()
                                           if key not in ('enabled', 'library_enabled')} for item in config['rule_configs']]}
    main = plugin(yaml.safe_load(generate_oxidns_config({**enabled, 'oxidns': {**inputs, 'config_mode': 'template'}})),
                  'sequence_main')['args']
    assert [entry['exec'] for entry in main if entry.get('matches', [''])[0].startswith('qname $rules_')] == [
        'goto china_dns', 'reject', 'reject 3', 'goto proxy_dns_seq', 'goto china_dns']
    # Without the space, an unlisted parked direct id is taken for a single rule.
    assert template_inputs({'rule_actions': {}, 'rule_dns_parked': {'x': {'action': 'direct'}}})['direct_rules'] == ['x']


def test_native_template_round_trip_keeps_disabled_choices():
    from backend.common.oxidns_editor import template_inputs
    config = rule_space()
    template = {'direct_rules': ['a'], 'rule_actions': {'b': BLACKHOLE, 'off': NXDOMAIN}, 'proxy_rulesets': ['rs_off']}
    inputs = template_inputs(prepare_editor(template, config), config)
    assert inputs['rule_actions'] == template['rule_actions']
    assert inputs['proxy_rulesets'] == ['rs_off'] and inputs['rule_dns_parked'] == {}


def test_preview_template_keeps_current_rule_actions():
    from backend.common.oxidns_common_routing import apply_routing
    config = rule_space()
    start = prepare_editor({'direct_rules': ['a'], 'rule_actions': {'b': BLACKHOLE}}, config)
    updated, current = apply_routing(document(start), start, config, {'rule_actions': {'b': NXDOMAIN}})
    current['native_config'] = yaml.safe_dump(updated, allow_unicode=True, sort_keys=False)
    result = preview_template(current, {**start['template_settings'], 'worker_threads': 3}, config)['settings']
    assert result['rule_actions'] == {'b': NXDOMAIN}
    assert routing(result, config)['rule_actions'] == {'b': NXDOMAIN}
    assert document(result)['runtime']['worker_threads'] == 3
    assert not any(item['tag'] == 'cf_dns_blackhole' for item in document(result)['plugins'])


def test_preview_template_with_union_lists_and_actions():
    config = rule_space()
    start = prepare_editor({'direct_rules': ['a'], 'proxy_rules': ['b'], 'rule_dns_follow': ['a', 'b']}, config)
    # The template now blocks b and a disabled rule; the union catalog still lists b as proxy.
    inputs = {**start['template_settings'], 'rule_actions': {'b': BLACKHOLE, 'off': NXDOMAIN},
              'rule_dns_follow': ['a']}
    result = preview_template(start, inputs, config)['settings']
    assert result['proxy_rules'] == ['b']
    assert result['rule_actions'] == {'b': BLACKHOLE}
    assert result['rule_dns_parked'] == {'off': NXDOMAIN}
    assert result['rule_dns_follow'] == ['a']
    assert routing(result, config)['rule_actions'] == {'b': BLACKHOLE}
    assert 'b' not in result['template_settings']['proxy_rules']
