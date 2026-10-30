"""Native runtime rule URLs must follow the current ConfigFlow service address."""
from copy import deepcopy
from urllib.parse import parse_qsl, urlencode, urlsplit

import pytest
import yaml

from backend.common.oxidns_editor import prepare_editor
from backend.common.oxidns_native import native_document
from backend.converters.oxidns import generate_oxidns_config, get_oxidns_ruleset_downloads


OLD_BASE = 'http://old.config.test:8080/old-prefix'
NEW_BASE = 'https://new.config.test/new-prefix'


def plugin(document, tag):
    return next(item for item in document['plugins'] if item['tag'] == tag)


def fixture_config(source='https://rules.test/domains.txt', profile='office'):
    config = {
        'system_config': {'server_domain': OLD_BASE, 'rule_proxy_token': 'known-capability'},
        'rule_configs': [{'id': 'domains', 'itemType': 'ruleset', 'behavior': 'domain', 'url': source}],
    }
    if profile:
        config['profile_id'] = profile
    config['oxidns'] = prepare_editor({'direct_rulesets': ['domains']}, config)
    return config


def save_document(config, document):
    config['oxidns']['native_config'] = yaml.safe_dump(document, sort_keys=False)


def rendered(config):
    return yaml.safe_load(generate_oxidns_config(config, 'http://browser.config.test'))


def download(document):
    return plugin(document, 'rules_download')['args']['downloads'][0]


@pytest.mark.parametrize('profile', ['office', None])
@pytest.mark.parametrize('source', ['https://rules.test/domains.txt', '/api/rule-library/local/content'])
@pytest.mark.parametrize('rotated', [False, True])
def test_republication_refreshes_outer_and_internal_source_urls_without_changing_draft(profile, source, rotated):
    config = fixture_config(source, profile)
    before = native_document(config['oxidns'])
    plugin(before, 'rules_download')['args']['timeout'] = '17s'
    download(before)['future_option'] = {'zero': 0, 'off': False}
    save_document(config, before)
    config['system_config']['server_domain'] = NEW_BASE
    if rotated:
        config['system_config'].update(rule_proxy_token='new-capability',
                                       retired_rule_proxy_tokens=['known-capability'])
    original = deepcopy(config)
    actual = rendered(config)
    current = get_oxidns_ruleset_downloads(config)[0]['url']
    assert download(actual) == {**download(before), 'url': current}
    assert plugin(actual, 'rules_download')['args']['timeout'] == '17s'
    parsed = urlsplit(current)
    assert current.startswith(NEW_BASE + '/api/')
    expected_source = NEW_BASE + source if source.startswith('/') else source
    assert dict(parse_qsl(parsed.query))['url'] == expected_source
    assert config == original
    # Rendering twice neither persists nor reintroduces the previous URL.
    assert rendered(config) == actual


@pytest.mark.parametrize('override', [
    'external', 'wrong-token', 'wrong-profile', 'wrong-source', 'wrong-part',
    'extra-query', 'duplicate-token', 'fragment', 'username', 'wrong-path',
    'unrecognized-source-origin',
])
def test_native_url_overrides_are_not_reinterpreted_as_owned_urls(override):
    config = fixture_config('/api/rule-library/local/content')
    document = native_document(config['oxidns'])
    item = download(document)
    parts = urlsplit(item['url'])
    query = dict(parse_qsl(parts.query))
    if override == 'external':
        item['url'] = 'https://manual.test/custom-domains.txt'
    elif override == 'wrong-token':
        item['url'] = parts._replace(query=urlencode({**query, 'token': 'unknown-capability'})).geturl()
    elif override == 'wrong-profile':
        item['url'] = item['url'].replace('/profiles/office/', '/profiles/other/')
    elif override == 'wrong-source':
        item['url'] = parts._replace(query=urlencode({**query, 'url': 'https://rules.test/other.txt'})).geturl()
    elif override == 'wrong-part':
        item['url'] = parts._replace(query=urlencode({**query, 'part': 'ip'})).geturl()
    elif override == 'extra-query':
        item['url'] += '&custom=true'
    elif override == 'duplicate-token':
        item['url'] += '&token=known-capability'
    elif override == 'fragment':
        item['url'] += '#manual'
    elif override == 'username':
        item['url'] = parts._replace(netloc='user@' + parts.netloc).geturl()
    elif override == 'wrong-path':
        item['dir'] = 'manual'
    elif override == 'unrecognized-source-origin':
        item['url'] = parts._replace(query=urlencode({**query, 'url': 'https://other.test/api/rule-library/local/content'})).geturl()
    save_document(config, document)
    config['system_config']['server_domain'] = NEW_BASE
    assert download(rendered(config)) == item


@pytest.mark.parametrize('alias', ['args', 'list'])
def test_refresh_detaches_owned_download_from_custom_yaml_alias_consumers(alias):
    config = fixture_config()
    document = native_document(config['oxidns'])
    args = plugin(document, 'rules_download')['args']
    download(document)['future_option'] = {'keep': False}
    shared = args if alias == 'args' else {'downloads': args['downloads'], 'timeout': '19s'}
    document['plugins'].extend([
        {'tag': tag, 'type': 'download', 'args': shared}
        for tag in ('user_download', 'other_user_download')
    ])
    save_document(config, document)
    config['system_config']['server_domain'] = NEW_BASE
    original = deepcopy(config)
    actual = rendered(config)
    assert download(actual)['url'].startswith(NEW_BASE)
    custom = plugin(actual, 'user_download')
    assert custom == plugin(document, 'user_download')
    assert custom['args'] is plugin(actual, 'other_user_download')['args']
    assert plugin(actual, 'rules_download')['args'] is not custom['args']
    assert download(actual)['future_option'] == {'keep': False}
    assert config == original


def test_refresh_updates_expanded_managed_include_without_rewriting_attachments():
    config = fixture_config()
    document = native_document(config['oxidns'])
    downloader = plugin(document, 'rules_download')
    document['plugins'].remove(downloader)
    document['include'] = ['updates.yaml']
    save_document(config, document)
    config['oxidns']['additional_files'] = [{'path': 'updates.yaml', 'encoding': 'text',
        'content': yaml.safe_dump({'plugins': [downloader]}, sort_keys=False)}]
    config['system_config']['server_domain'] = NEW_BASE
    original = deepcopy(config)
    actual = rendered(config)
    assert download(actual)['url'] == get_oxidns_ruleset_downloads(config)[0]['url']
    assert config == original


def test_custom_download_chain_with_renamed_tags_is_preserved():
    config = fixture_config()
    config['oxidns']['native_config'] = config['oxidns']['native_config'].replace('rules_download', 'custom_download')
    config['system_config']['server_domain'] = NEW_BASE
    before = native_document(config['oxidns'])
    assert rendered(config) == before


def test_refresh_preserves_unrelated_adguard_downloads_and_custom_steps():
    config = fixture_config()
    document = native_document(config['oxidns'])
    unrelated = {'url': 'https://ads.test/filters.txt', 'dir': 'manual', 'filename': 'adguard.txt'}
    plugin(document, 'rules_download')['args']['downloads'].append(unrelated)
    plugin(document, 'rules_refresh')['args'].append({'exec': 'accept'})
    save_document(config, document)
    config['system_config']['server_domain'] = NEW_BASE
    actual = rendered(config)
    assert download(actual)['url'].startswith(NEW_BASE)
    assert plugin(actual, 'rules_download')['args']['downloads'][1] == unrelated
    assert plugin(actual, 'rules_refresh') == plugin(document, 'rules_refresh')
