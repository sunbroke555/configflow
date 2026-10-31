"""Editing a rule-library source must reach native OxiDNS downloads and later domain changes."""
import io
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit

import pytest
import requests
import yaml

from backend.agents.rule_sync_store import bundle_files
from backend.common import config as config_module
from backend.common.config_repository import ProfileRepository
from backend.converters.oxidns import generate_oxidns_config
from backend.routes.agents import _prepare_transactional_content
from backend.test_profile_api import make_app

OLD = 'https://configflow.old.test'
NEW = 'http://192.168.0.105:15800'
SOURCE = 'https://raw.test/Docker.list'
MOVED = 'https://raw.test/Docker2.list'
HEADERS = {'X-ConfigFlow-Profile': 'home'}


@pytest.fixture
def env(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path, default_config_factory=config_module.get_default_config,
                                   initial_config_factory=config_module.get_default_config)
    monkeypatch.setattr(config_module, '_repository', repository)
    config_module.reset_config_context()
    fetched = []

    def fake_request(self, method, url, *args, **kwargs):
        fetched.append(url)
        response = requests.models.Response()
        response.status_code, response.url = 200, url
        response._content = b'+.' + url.rsplit('/', 1)[-1].lower().replace('.', '-').encode() + b'.example\n'
        response.raw = io.BytesIO(response._content)
        response.headers['Content-Type'] = 'text/plain'
        return response
    monkeypatch.setattr('requests.sessions.Session.request', fake_request)
    repository.save_system({'system_config': {'server_domain': OLD}})
    library = lambda identity, url: {'id': identity, 'name': identity, 'source_type': 'url', 'url': url,
                                     'behavior': 'domain', 'format': 'text', 'enabled': True}
    repository.save_shared({'rule_library': [library('lib-docker', SOURCE),
                                             library('lib-other', 'https://raw.test/Other.list')]})
    repository.create_profile({'id': 'home', 'name': 'home'})
    ruleset = lambda identity, library_id: {'id': identity, 'name': identity, 'itemType': 'ruleset', 'enabled': True,
                                            'library_rule_id': library_id, 'behavior': 'domain', 'policy': 'Proxy'}
    repository.save_profile('home', {'rule_configs': [ruleset('docker', 'lib-docker'), ruleset('other', 'lib-other')],
                                     'oxidns': {'proxy_rulesets': ['docker', 'other']}})
    config, _ = repository.get_publish_snapshot('home')
    repository.save_profile('home', {'oxidns': {**config['oxidns'], 'config_mode': 'native',
                                                'native_config': generate_oxidns_config(config)}})
    yield repository, make_app(repository, monkeypatch).test_client(), fetched
    config_module.reset_config_context()


def published(repository):
    """Downloads of the YAML a full OxiDNS publish would deliver, by filename."""
    source, _ = repository.get_publish_snapshot('home')
    prepared = _prepare_transactional_content({'id': 'oxidns-home', 'service_type': 'oxidns', 'profile_id': 'home'},
                                              source, base_url=OLD)
    bundle = prepared['bundle']
    runtime = yaml.safe_load(bundle_files(bundle)[bundle.manifest['config_path']][0])
    downloads = next(p for p in runtime['plugins'] if p['tag'] == 'rules_download')['args']['downloads']
    return {item['filename']: item['url'] for item in downloads}


def nested(url):
    return parse_qs(urlsplit(url).query)['url'][0]


def docker_file(downloads):
    return next(name for name, url in downloads.items() if 'Docker' in nested(url))


def edit_source(client, **changes):
    items = client.get('/api/rule-library', headers=HEADERS).get_json()
    items = items.get('data', items) if isinstance(items, dict) else items
    item = next(entry for entry in items if entry['id'] == 'lib-docker')
    response = client.put('/api/rule-library/lib-docker', json={**item, **changes}, headers=HEADERS)
    assert response.status_code == 200, response.get_json()


def test_source_edit_reaches_publish_proxy_and_later_domain_change(env):
    repository, client, fetched = env
    before = published(repository)
    name = docker_file(before)
    edit_source(client, url=MOVED)
    after = published(repository)
    assert nested(after[name]) == MOVED
    assert after[name].startswith(OLD + '/api/profiles/home/mosdns/rule-proxy?')
    assert {key: value for key, value in after.items() if key != name} == \
        {key: value for key, value in before.items() if key != name}
    # The device downloads through the published URL from the new source.
    parts = urlsplit(after[name])
    fetched.clear()
    response = client.get(parts.path + '?' + parts.query)
    assert response.status_code == 200
    assert 'docker2-list.example' in response.get_data(as_text=True)
    assert SOURCE not in fetched
    # The entry is recognized again, so a later domain change moves it too.
    assert client.post('/api/server-domain', json={'new_domain': NEW}, headers=HEADERS).status_code == 200
    moved = published(repository)
    assert all(url.startswith(NEW + '/api/profiles/home/') for url in moved.values())
    assert nested(moved[name]) == MOVED


def test_switch_to_pasted_content_points_at_the_library_content_endpoint(env):
    repository, client, _ = env
    name = docker_file(published(repository))
    edit_source(client, source_type='content', content='+.docker.example')
    assert nested(published(repository)[name]) == OLD + '/api/profiles/home/rule-library/content/lib-docker'


def test_manual_source_override_survives_a_source_edit(env):
    repository, client, _ = env
    profile = repository.get_profile('home')
    document = yaml.safe_load(profile['oxidns']['native_config'])
    downloads = next(p for p in document['plugins'] if p['tag'] == 'rules_download')['args']['downloads']
    entry = next(item for item in downloads if 'Docker' in nested(item['url']))
    parts = urlsplit(entry['url'])
    entry['url'] = parts._replace(query=urlencode({**dict(parse_qsl(parts.query)),
                                                   'url': 'https://manual.test/mine.list'})).geturl()
    repository.save_profile('home', {'oxidns': {**profile['oxidns'], 'native_config': yaml.safe_dump(document)}})
    edit_source(client, url=MOVED)
    assert nested(published(repository)[entry['filename']]) == 'https://manual.test/mine.list'


def test_metadata_edit_leaves_the_native_draft_untouched(env):
    repository, client, _ = env
    draft = repository.get_profile('home')['oxidns']['native_config']
    edit_source(client, name='Docker 规则')
    assert repository.get_profile('home')['oxidns']['native_config'] == draft
