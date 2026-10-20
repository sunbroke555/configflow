"""Both editors withdraw owned downloads without touching shared/user assets."""
from copy import deepcopy
import json

import pytest
import yaml

from backend.common.oxidns_common import apply_common_settings
from backend.common.oxidns_editor import prepare_editor
from backend.common.oxidns_native import native_document
from backend.common.oxidns_native_apply import apply_native_settings
from backend.common.oxidns_validation import validate_settings
from backend.converters.oxidns import generate_oxidns_config, get_oxidns_ruleset_downloads
from backend.test_oxidns_common_routing import fixture_config
from backend.test_oxidns_integration import oxidns_app  # noqa: F401


def plugin(document, tag):
    return next(item for item in document['plugins'] if item['tag'] == tag)


def setup_sources(*, keep=True):
    config = fixture_config()
    ids = [f'source{i}' for i in range(8)]
    config['rule_configs'] = [
        {'id': identity, 'itemType': 'ruleset', 'behavior': 'domain',
         'url': f'https://example.test/{identity}.txt'} for identity in [*ids, 'keep']]
    settings = prepare_editor({'direct_rulesets': ids, 'proxy_rulesets': ['keep'] if keep else [],
                               'rule_update': {'enabled': True}}, config)
    document = yaml.safe_load(settings['native_config'])
    provider = next(p for p in document['plugins'] if p['type'] == 'domain_set' and len(p['args'].get('files', [])) == 8)
    return config, settings, document, provider


def withdraw(settings, config, callback):
    original = deepcopy(settings)
    candidate = yaml.safe_load(settings['native_config'])
    callback(candidate)
    result = apply_native_settings(settings, yaml.safe_dump(candidate, sort_keys=False), config)['settings']
    assert settings == original
    # Reopening uses the same persisted YAML, without repairing it implicitly.
    reopened = prepare_editor(json.loads(json.dumps(result)), config)
    assert reopened['native_config'] == result['native_config']
    assert validate_settings(reopened, config)['valid']
    return reopened, yaml.safe_load(generate_oxidns_config({**config, 'oxidns': reopened}))


def delete_provider(document, tag):
    document['plugins'] = [p for p in document['plugins'] if p['tag'] != tag]
    main = plugin(document, 'sequence_main')
    main['args'] = [step for step in main['args'] if f'qname ${tag}' not in step.get('matches', [])]
    reload = plugin(document, 'rules_reload')
    reload['args'] = [ref for ref in reload['args'] if ref != '$' + tag]


@pytest.mark.parametrize('action', ['delete', 'clear-files', 'remove-file-field'])
def test_native_withdrawal_matches_common_control_and_survives_reopen(action):
    config, settings, document, source = setup_sources()
    expected = apply_common_settings(settings, {'direct_rulesets': []}, config)['settings']
    def change(candidate):
        if action == 'delete': delete_provider(candidate, source['tag'])
        elif action == 'clear-files': plugin(candidate, source['tag'])['args']['files'] = []
        else: plugin(candidate, source['tag'])['args'].pop('files')
    result, rendered = withdraw(settings, config, change)
    assert plugin(rendered, 'rules_download')['args']['downloads'] == plugin(native_document(expected), 'rules_download')['args']['downloads']
    assert len(plugin(rendered, 'rules_download')['args']['downloads']) == 1
    assert plugin(rendered, 'rules_cron') == plugin(document, 'rules_cron')
    assert len(get_oxidns_ruleset_downloads({**config, 'oxidns': result})) == 1


@pytest.mark.parametrize('include', [False, True])
def test_other_provider_or_include_keeps_shared_download_bytes_and_options(include):
    config, settings, document, source = setup_sources()
    shared_path = source['args']['files'][0]
    shared = {'tag': 'user_shared', 'type': 'domain_set', 'args': {'files': [shared_path]}}
    if include:
        document['include'] = ['shared.yaml']
        settings['additional_files'] = [{'path': 'shared.yaml', 'encoding': 'text', 'content': yaml.safe_dump({'plugins': [shared]})}]
    else: document['plugins'].append(shared)
    downloads = plugin(document, 'rules_download')['args']['downloads']
    downloads[0].update(url='https://override.test/shared', custom_option=False)
    original = deepcopy(downloads[0])
    custom = {'url': 'https://user.test/custom', 'dir': 'custom', 'filename': 'my-rule.txt', 'extension': False}
    downloads.append(deepcopy(custom))
    settings['native_config'] = yaml.safe_dump(document, sort_keys=False)
    result, rendered = withdraw(settings, config, lambda d: delete_provider(d, source['tag']))
    remaining = plugin(rendered, 'rules_download')['args']['downloads']
    assert len(remaining) == 3
    assert original in remaining and custom in remaining
    assert plugin(rendered, 'user_shared') == shared


def test_last_owned_download_removal_stops_only_owned_pipeline_and_keeps_user_job():
    config, settings, document, source = setup_sources(keep=False)
    job = {'name': 'user-job', 'schedule': '0 * * * *', 'executors': ['$user_executor']}
    document['plugins'].append({'tag': 'user_executor', 'type': 'sequence', 'args': [{'exec': 'accept'}]})
    plugin(document, 'rules_cron')['args']['jobs'].append(deepcopy(job))
    settings['native_config'] = yaml.safe_dump(document, sort_keys=False)
    result, rendered = withdraw(settings, config, lambda d: delete_provider(d, source['tag']))
    assert all(p['tag'] not in ('rules_download', 'rules_reload', 'rules_refresh') for p in rendered['plugins'])
    assert plugin(rendered, 'rules_cron')['args']['jobs'] == [job]
    # Restoring selected rules must recreate only live downloads, once each.
    restored = apply_common_settings(result, {'direct_rulesets': ['source0'], 'rule_update.enabled': True}, config)['settings']
    restored_doc = native_document(restored)
    assert len(plugin(restored_doc, 'rules_download')['args']['downloads']) == 1
    assert job in plugin(restored_doc, 'rules_cron')['args']['jobs']


def test_last_owned_removal_keeps_unreferenced_user_download_and_manual_refresh():
    config, settings, document, source = setup_sources(keep=False)
    entry = {'url': 'https://user.test/manual', 'dir': 'manual', 'filename': 'file.txt'}
    plugin(document, 'rules_download')['args']['downloads'].append(deepcopy(entry))
    settings['native_config'] = yaml.safe_dump(document, sort_keys=False)
    _, rendered = withdraw(settings, config, lambda d: delete_provider(d, source['tag']))
    assert plugin(rendered, 'rules_download')['args']['downloads'] == [entry]
    assert plugin(rendered, 'rules_refresh')['args'] == [{'exec': '$rules_download'}]
    assert all(p['tag'] != 'rules_reload' for p in rendered['plugins'])


def test_noncanonical_download_and_lookalike_path_are_not_owned():
    config, settings, document, source = setup_sources()
    custom = {'tag': 'my-download', 'type': 'download', 'args': {'downloads': [
        {'url': 'https://user.test/keep', 'dir': 'rules', 'filename': source['args']['files'][0].split('/')[-1]}]}}
    unknown = {'url': 'https://user.test/unknown', 'dir': 'rules', 'filename': 'rule_deadbeef_domain.txt'}
    document['plugins'].append(deepcopy(custom))
    plugin(document, 'rules_download')['args']['downloads'].append(deepcopy(unknown))
    settings['native_config'] = yaml.safe_dump(document, sort_keys=False)
    _, rendered = withdraw(settings, config, lambda d: delete_provider(d, source['tag']))
    assert plugin(rendered, 'my-download') == custom
    assert unknown in plugin(rendered, 'rules_download')['args']['downloads']


@pytest.mark.parametrize('editor', ['native', 'common'])
@pytest.mark.parametrize('reference,expected', [
    ('{path}', 2), ('${SHARED_RULE_FILE}', 9), ('/opt/oxidns/rules/*.txt', 9),
])
def test_unknown_plugin_reader_is_preserved_conservatively(editor, reference, expected):
    config, settings, document, source = setup_sources()
    shared_path = source['args']['files'][0]
    reference = reference.replace('{path}', shared_path)
    document['plugins'].append({'tag': 'future-reader', 'type': 'future_plugin', 'args': {'rule_path': reference}})
    settings['native_config'] = yaml.safe_dump(document, sort_keys=False)
    if editor == 'native':
        _, rendered = withdraw(settings, config, lambda d: delete_provider(d, source['tag']))
    else:
        result = apply_common_settings(settings, {'direct_rulesets': []}, config)['settings']
        rendered = yaml.safe_load(generate_oxidns_config({**config, 'oxidns': result}))
    remaining = plugin(rendered, 'rules_download')['args']['downloads']
    assert len(remaining) == expected
    assert any(item['filename'] == shared_path.split('/')[-1] for item in remaining)


@pytest.mark.parametrize('reference', ['${SHARED_RULE_FILE}', './rules/*.txt', '/opt/oxidns/rules/{filename}'])
def test_unresolved_glob_and_absolute_readers_prevent_unsafe_cleanup(reference):
    config, settings, document, source = setup_sources()
    entry = source['args']['files'][0]
    reference = reference.format(filename=entry.split('/')[-1]) if '{filename}' in reference else reference
    document['plugins'].append({'tag': 'user-shared', 'type': 'domain_set', 'args': {'files': [reference]}})
    settings['native_config'] = yaml.safe_dump(document, sort_keys=False)
    candidate = deepcopy(document)
    delete_provider(candidate, source['tag'])
    result = apply_native_settings(settings, yaml.safe_dump(candidate), config)['settings']
    remaining = plugin(yaml.safe_load(result['native_config']), 'rules_download')['args']['downloads']
    assert any(item['filename'] == entry.split('/')[-1] for item in remaining)


def test_explicit_empty_download_list_still_serializes_pipeline_cleanup():
    config, settings, document, source = setup_sources(keep=False)
    delete_provider(document, source['tag'])
    plugin(document, 'rules_download')['args']['downloads'] = []
    result = apply_native_settings(settings, yaml.safe_dump(document), config)['settings']
    assert validate_settings(result, config)['valid']
    assert all(p['tag'] not in ('rules_download', 'rules_reload', 'rules_refresh', 'rules_cron')
               for p in native_document(result)['plugins'])


def test_publish_bundle_downloads_only_remaining_rules_and_keeps_a_valid_runtime(monkeypatch):
    import io
    import tarfile
    import requests
    from backend.agents.deployment_bundle import build_deployment_bundle
    config, settings, _, source = setup_sources()
    withdrawn, _ = withdraw(settings, config, lambda d: delete_provider(d, source['tag']))
    config['oxidns'] = withdrawn
    downloads = get_oxidns_ruleset_downloads(config)
    fetched = []
    def fetch(url, **kwargs):
        fetched.append(url)
        response = requests.Response()
        response.status_code = 200
        response._content = b'example.test\n'
        response._content_consumed = True
        return response
    monkeypatch.setattr('backend.agents.deployment_bundle.request_rule', fetch)
    bundle = build_deployment_bundle({'id': 'test-oxidns', 'service_type': 'oxidns'}, config,
                                    generate_oxidns_config(config), ruleset_downloads=downloads)
    assert fetched == ['https://example.test/keep.txt']
    with tarfile.open(fileobj=io.BytesIO(bundle.archive), mode='r:gz') as archive:
        content = yaml.safe_load(archive.extractfile('files/config.yaml').read())
        assert len(plugin(content, 'rules_download')['args']['downloads']) == 1


def test_http_save_render_and_reopen_do_not_reintroduce_withdrawn_downloads(oxidns_app, monkeypatch):
    app, repository = oxidns_app
    config = repository.get_compat_config('office')
    settings = prepare_editor({'direct_rulesets': ['rs'], 'rule_update': {'enabled': True}}, config)
    document = yaml.safe_load(settings['native_config'])
    provider = next(p for p in document['plugins'] if p['type'] == 'domain_set')
    delete_provider(document, provider['tag'])
    client = app.test_client()
    def forbidden(*args, **kwargs): pytest.fail('draft editing must not use the network')
    monkeypatch.setattr('requests.sessions.Session.request', forbidden)
    other = repository.get_profile('default')
    response = client.post('/api/profiles/office/oxidns/native-settings/apply', json={
        'settings': settings, 'native_config': yaml.safe_dump(document)})
    assert response.status_code == 200, response.json
    saved = client.post('/api/profiles/office/oxidns/settings', json=response.json['settings'])
    assert saved.status_code == 200, saved.json
    reopened = client.get('/api/profiles/office/oxidns/settings').json
    rendered = client.post('/api/profiles/office/oxidns/render', json={'settings': reopened})
    assert rendered.status_code == 200, rendered.json
    assert all(p['type'] not in ('download', 'reload_provider', 'cron') for p in yaml.safe_load(rendered.json['config'])['plugins'])
    assert repository.get_profile('default') == other
