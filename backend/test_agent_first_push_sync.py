"""The first push of a subscription without a node cache is 已同步 right away.

A push downloads a subscription whose node cache is missing (or belongs to an
earlier URL). The content check after it reads the node cache the push stored.
Both must render the same files: the stored nodes and their hosts.
"""
from copy import deepcopy
import io
import tarfile

import pytest
import yaml
from flask import Flask

from backend.agents.content_sync import ContentSyncChecker
from backend.agents.manager import AgentManager
from backend.routes import agents, register_blueprints
from backend.test_agent_content_api import DeferredExecutor, listing, push
from backend.test_agent_content_preparation import content_env  # noqa: F401
from backend.utils.provider_delivery import commit_provider_bundle as real_commit_provider_bundle
from backend.utils.proxy_server_dns import POLICY_KEY

# Keys in an order the node cache does not keep: it renders them sorted.
FEED = """\
hosts:
  node.example.net: entry.example.org
proxies:
  - {name: Remote, type: socks5, server: node.example.net, port: 1080, username: user, password: secret, udp: true}
"""
OTHER_FEED = 'proxies: [{name: Other, type: socks5, server: 192.0.2.30, port: 1080}]\n'
FAKE_IP = """\
mode: rule
log-level: info
dns:
  enable: true
  listen: 0.0.0.0:1053
  enhanced-mode: fake-ip
  fake-ip-range: 198.18.0.1/16
  nameserver: [223.5.5.5]
  proxy-server-nameserver: [https://223.5.5.5/dns-query]
"""
LISTENER = 'udp://127.0.0.1:1053'


@pytest.fixture
def env(content_env, monkeypatch):
    config, _remote, _fetches, repository = content_env
    config['mihomo']['custom_config'] = FAKE_IP
    feeds = {'selected-sub': FEED, 'other-sub': OTHER_FEED}
    failing, fetches = set(), []

    def fetch(sub_id, url, **_kwargs):
        fetches.append(sub_id)
        if sub_id in failing:
            raise ConnectionError('upstream unreachable')
        return feeds[sub_id], 'direct_yaml'

    for target in ('backend.routes.subscriptions', 'backend.routes.aggregations', 'backend.utils.sub_store_client'):
        monkeypatch.setattr(f'{target}.get_subscription_proxies_yaml', fetch)
    # A real push stores caches and aggregation files; no capture here.
    monkeypatch.setattr('backend.utils.provider_delivery.commit_provider_bundle', real_commit_provider_bundle)
    manager = AgentManager(repository)
    monkeypatch.setattr(agents, 'get_agent_manager', lambda: manager)
    monkeypatch.setattr(agents, 'get_config', lambda *_: deepcopy(config))
    monkeypatch.setattr(repository, 'get_compat_config', lambda *_: deepcopy(config))
    executor = DeferredExecutor()
    monkeypatch.setattr('backend.agents.content_sync._checker', ContentSyncChecker(executor=executor))
    archives = []

    def transport(agent, method, path, **kwargs):
        if path == 'capabilities':
            return {'deployment_protocols': [1], 'service_type': agent['service_type'], 'config_path': 'config.yaml'}
        archives.append(kwargs['data'])
        return {'success': True, 'status': 'succeeded', 'config_version': 'agent-hash'}

    monkeypatch.setattr(manager, '_deployment_request', transport)
    app = Flask(__name__)
    register_blueprints(app)
    agent_id = manager.register_agent({'name': 'mihomo', 'host': 'mihomo.test', 'service_type': 'mihomo'})['id']
    return {'client': app.test_client(), 'config': config, 'repository': repository, 'agent_id': agent_id,
            'executor': executor, 'fetches': fetches, 'failing': failing, 'archives': archives}


def deployed_files(archive):
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as bundle:
        manifest = yaml.safe_load(bundle.extractfile('manifest.json').read())
        return {item['path']: bundle.extractfile('files/' + item['path']).read().decode()
                for item in manifest['files']}


def content_status(env):
    client, executor = env['client'], env['executor']
    status = listing(client)['mihomo']['content_sync']
    if status['status'] == 'checking':
        executor.run()
        status = listing(client)['mihomo']['content_sync']
    return status['status']


def use_aggregation(config):
    config['subscription_aggregations'] = [{'id': 'aggregate', 'name': 'Aggregate', 'enabled': True,
                                            'subscriptions': ['selected-sub'], 'nodes': []}]
    config['proxy_groups'][0].update(subscriptions=[], aggregations=['aggregate'])


def store_cache(sub, body):
    """The node cache a manual update (or the scheduled task) stores."""
    from backend.test_subscription_hosts import store
    store(sub, body)


def cache_path(repository, sub_id):
    return repository.shared_path(f'subscribes/{sub_id}.json')


@pytest.mark.parametrize('aggregation', [False, True], ids=['subscription', 'aggregation'])
def test_first_push_without_a_node_cache_is_synced_right_away(env, aggregation):
    if aggregation:
        use_aggregation(env['config'])
    assert not cache_path(env['repository'], 'selected-sub').exists()
    assert push(env['client'], env['agent_id']).status_code == 200
    # Downloaded once; the content check reads the node cache the push stored.
    assert env['fetches'] == ['selected-sub']
    assert cache_path(env['repository'], 'selected-sub').exists()
    assert content_status(env) == 'synced'
    assert env['fetches'] == ['selected-sub']


@pytest.mark.parametrize('aggregation', [False, True], ids=['subscription', 'aggregation'])
def test_first_push_delivers_the_subscription_hosts_with_policy_and_filter(env, aggregation):
    if aggregation:
        use_aggregation(env['config'])
    assert push(env['client'], env['agent_id']).status_code == 200
    files = deployed_files(env['archives'][-1])
    main = yaml.safe_load(files['config.yaml'])
    assert main['hosts']['node.example.net'] == 'entry.example.org'
    assert main['dns'][POLICY_KEY] == {'node.example.net': LISTENER}
    assert 'entry.example.org' in main['dns']['fake-ip-filter']
    # The provider file is rendered from the stored nodes, like every later check.
    provider = next(text for path, text in files.items() if path.startswith('providers/'))
    proxy = yaml.safe_load(provider)['proxies'][0]
    assert proxy['server'] == 'node.example.net' and list(proxy) == sorted(proxy)


def test_a_push_with_a_current_node_cache_is_unchanged(env, monkeypatch):
    feed = env['config']['subscriptions'][0]
    store_cache(feed, FEED)
    path = cache_path(env['repository'], 'selected-sub')
    stored = path.read_bytes()
    generated = []
    real_generate = agents.generate_mihomo_config

    def counting_generate(*args, **kwargs):
        generated.append(1)
        return real_generate(*args, **kwargs)

    monkeypatch.setattr(agents, 'generate_mihomo_config', counting_generate)
    assert push(env['client'], env['agent_id']).status_code == 200
    assert env['fetches'] == []          # no download
    assert len(generated) == 1           # the main file is generated once
    assert path.read_bytes() == stored   # the cache is not rewritten
    main = yaml.safe_load(deployed_files(env['archives'][-1])['config.yaml'])
    assert main['hosts']['node.example.net'] == 'entry.example.org'
    assert content_status(env) == 'synced'


def test_a_failed_download_without_a_cache_keeps_the_existing_error(env):
    env['failing'].add('selected-sub')
    response = push(env['client'], env['agent_id'])
    assert response.status_code == 400
    assert '订阅没有可用缓存或远程代理' in response.json['message'] + response.json.get('error_details', '')
    assert env['fetches'] == ['selected-sub']
    assert not cache_path(env['repository'], 'selected-sub').exists()
    assert env['archives'] == []


def test_a_failed_download_keeps_the_earlier_cache_and_is_not_retried(env):
    """One subscription is new, another moved to a URL that is unreachable now.

    The new one is stored and the files are rendered again from the caches; the
    unreachable one keeps its earlier nodes, as before, without a second download.
    """
    config = env['config']
    moved = {'id': 'other-sub', 'name': 'Other', 'url': 'https://subscription.test/old', 'enabled': True}
    store_cache(moved, OTHER_FEED)
    config['subscriptions'][1] = {**moved, 'url': 'https://subscription.test/moved'}
    config['proxy_groups'][0]['subscriptions'] = ['selected-sub', 'other-sub']
    env['failing'].add('other-sub')
    assert push(env['client'], env['agent_id']).status_code == 200
    assert sorted(env['fetches']) == ['other-sub', 'selected-sub']
    files = deployed_files(env['archives'][-1])
    other = files['providers/Other.yaml']
    assert yaml.safe_load(other)['proxies'][0]['name'] == 'Other'
    assert yaml.safe_load(files['config.yaml'])['hosts']['node.example.net'] == 'entry.example.org'
    assert content_status(env) == 'synced'


def test_rendering_again_asks_sub_store_once_per_uri_node(env, monkeypatch):
    """Rendering again after saving reuses the first rendering's Sub-Store conversions."""
    config = env['config']
    config['nodes'] = [
        {'id': 'inline-node', 'name': 'Inline', 'proxy_string': 'ss://inline@192.0.2.41:8388', 'enabled': True},
        {'id': 'member-node', 'name': 'Member', 'proxy_string': 'ss://member@192.0.2.42:8388', 'enabled': True},
    ]
    use_aggregation(config)
    config['subscription_aggregations'][0]['nodes'] = ['member-node']
    config['proxy_groups'][0]['manual_nodes'] = ['inline-node']
    conversions = []

    def convert(proxy_string, target='ClashMeta'):
        conversions.append(proxy_string)
        return {'type': 'ss', 'server': proxy_string.split('@')[1].split(':')[0], 'port': 8388,
                'cipher': 'aes-128-gcm', 'password': 'secret'}

    monkeypatch.setattr('backend.utils.sub_store_client.convert_proxy_string', convert)
    assert push(env['client'], env['agent_id']).status_code == 200
    assert sorted(conversions) == ['ss://inline@192.0.2.41:8388', 'ss://member@192.0.2.42:8388']
    files = deployed_files(env['archives'][-1])
    assert [proxy['name'] for proxy in yaml.safe_load(files['config.yaml'])['proxies']] == ['Inline']
    names = {proxy['name'] for proxy in yaml.safe_load(files['providers/Aggregate.yaml'])['proxies']}
    assert names == {'Remote', 'Member'}
    assert content_status(env) == 'synced'
