"""系统设置 → 定时任务: one row per background task with a persisted last result."""
from datetime import timedelta
from unittest.mock import Mock

import pytest
import requests
import yaml

from backend.common import config as config_module
from backend.common.config_repository import ProfileRepository
from backend.routes import tasks as tasks_route
from backend.test_qa_integrity import make_app
from backend.utils import scheduled_refresh, sub_store_client, task_status
from backend.utils.rule_utils import read_validated_rule_cache, write_validated_rule_cache
from backend.utils.subscription_cache import load_subscription_cache, save_subscription_nodes

SUB_URL = 'https://airport.test/sub?token=private'
RULE_URL = 'https://rules.test/list.txt?key=private'
PROXIES = [{'name': 'Remote', 'type': 'ss', 'server': 'example.test', 'port': 443,
            'cipher': 'aes-128-gcm', 'password': 'p'}]
RULE = {'id': 'rule', 'name': 'Streaming', 'source_type': 'url', 'url': RULE_URL,
        'behavior': 'domain', 'format': 'text', 'enabled': True}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    app, repo = make_app(tmp_path)
    repo.save_shared({'subscriptions': [{'id': 's', 'name': 'Feed', 'url': SUB_URL, 'enabled': True, 'interval': 86400}],
                      'nodes': [], 'subscription_aggregations': [], 'rule_library': [dict(RULE)]})
    monkeypatch.setattr(sub_store_client, '_fetch_results', {})
    monkeypatch.setattr(sub_store_client, '_fetch_failures', {})
    monkeypatch.setattr('backend.utils.rule_utils.request_rule', Mock(side_effect=AssertionError('no request')))
    yield app, repo
    config_module.reset_config_context()


def rows(app):
    response = app.test_client().get('/api/tasks')
    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'no-store'
    return {row['key']: row for row in response.get_json()['tasks']}


def wait_background():
    import threading
    for thread in threading.enumerate():
        if thread.name.startswith('task-'):
            thread.join(timeout=10)


def test_record_counts_runs_failures_and_skips(setup):
    task_status.record('x', ok=False, error='boom', cache_retained=True, cache_updated_at='2026-01-01T00:00:00Z')
    task_status.record('x', ok=False, error='boom again', cache_retained=True)
    task_status.record('x', ok=False, skipped=True, error='nothing to do')
    entry = task_status.get('x')
    assert (entry['run_count'], entry['failure_count'], entry['consecutive_failures']) == (2, 2, 2)
    assert (entry['last_status'], entry['last_error']) == ('skipped', 'nothing to do')
    task_status.record('x', ok=True, duration_ms=12.6)
    entry = task_status.get('x')
    assert (entry['run_count'], entry['failure_count'], entry['consecutive_failures']) == (3, 2, 0)
    assert entry['last_error'] == '' and 'cache_retained' not in entry and entry['last_duration_ms'] == 12
    task_status.forget('x', 'missing')
    assert task_status.get('x') == {}


def test_next_run_backs_off_after_failures_but_never_beyond_the_interval():
    base = {'last_run_at': '2026-10-24T00:00:00Z'}
    start = task_status.parse_time(base['last_run_at'])
    assert scheduled_refresh.next_run_at({**base, 'last_status': 'success'}, 3600) == start + timedelta(hours=1)
    assert scheduled_refresh.next_run_at({**base, 'last_status': 'failed', 'consecutive_failures': 1}, 86400) == start + timedelta(minutes=5)
    assert scheduled_refresh.next_run_at({**base, 'last_status': 'failed', 'consecutive_failures': 3}, 86400) == start + timedelta(minutes=20)
    assert scheduled_refresh.next_run_at({**base, 'last_status': 'failed', 'consecutive_failures': 30}, 3600) == start + timedelta(hours=1)
    assert scheduled_refresh.next_run_at(base, 0) is None
    assert scheduled_refresh.next_run_at({}, 3600, '2026-10-24T01:00:00Z') == start + timedelta(hours=2)


def test_failed_rule_refresh_survives_a_reload_with_reason_and_retained_cache(setup, monkeypatch):
    app, repo = setup
    write_validated_rule_cache(RULE, b'+.old.test\n')
    with pytest.raises(Exception):
        scheduled_refresh.refresh_rule_source(RULE, fetch=Mock(side_effect=requests.ConnectTimeout('https://rules.test?key=private')))
    config_module.set_repository(ProfileRepository(repo.data_dir))  # a restart or page reload
    row = rows(app)['rule_source:rule']
    assert row['category'] == 'rule_source' and row['name'] == 'Streaming' and row['detail'] == 'rules.test'
    assert (row['last_status'], row['last_error'], row['cache_retained']) == ('failed', '连接上游超时', True)
    assert row['cache_updated_at'] and row['failure_count'] == 1 and row['last_trigger'] == 'schedule'
    assert row['interval_seconds'] == 86400 and row['enabled'] and row['editable'] and row['can_run']
    assert task_status.parse_time(row['next_run_at']) - task_status.parse_time(row['last_run_at']) == timedelta(minutes=5)
    assert 'private' not in str(rows(app))
    assert read_validated_rule_cache(RULE) == b'+.old.test\n'


def test_failure_reasons_are_readable_and_never_render_the_address():
    describe = scheduled_refresh.describe_failure
    assert describe(requests.exceptions.MissingSchema('Invalid URL subscribe?token=private')) == '地址无效，请检查是否为完整的 http(s) 地址'
    assert describe(requests.ConnectionError('https://rules.test?key=private')) == '无法连接上游'
    response = Mock(status_code=403)
    assert describe(requests.HTTPError('403 for https://rules.test?key=private', response=response)) == '上游返回 HTTP 403'
    assert describe(RuntimeError('https://rules.test?key=private')) == '更新失败（RuntimeError）'
    from backend.utils.sub_store_client import SubscriptionFetchError
    assert describe(SubscriptionFetchError(RuntimeError('x'), requests.Timeout('y'))) == '连接上游超时'


def test_rule_without_valid_cache_reports_that_nothing_is_retained(setup):
    with pytest.raises(Exception):
        scheduled_refresh.refresh_rule_source(RULE, fetch=lambda _url: b'DOMAIN,bad.test\n')
    entry = task_status.get('rule_source:rule')
    assert entry['cache_retained'] is False and '第 1 行' in entry['last_error']


def test_device_reads_use_the_cache_and_only_a_missing_cache_downloads_now(setup):
    fetch = Mock(return_value=b'+.new.test\n')
    content, error = scheduled_refresh.read_rule_source(dict(RULE), fetch=fetch)
    assert (content, error) == (b'+.new.test\n', None)
    assert task_status.get('rule_source:rule')['last_trigger'] == 'pull'
    content, error = scheduled_refresh.read_rule_source(dict(RULE), fetch=Mock(side_effect=AssertionError('cached')))
    assert (content, error) == (b'+.new.test\n', None)
    # A declaration outside the library has no task and refreshes on each read.
    outside = {**RULE, 'id': 'profile-only', 'url': 'https://other.test/list'}
    fetch = Mock(return_value=b'+.other.test\n')
    scheduled_refresh.read_rule_source(outside, fetch=fetch)
    scheduled_refresh.read_rule_source(outside, fetch=fetch)
    assert fetch.call_count == 2
    assert task_status.get('rule_source:profile-only') == {}


def test_run_now_refreshes_in_background_and_rejects_unknown_sources(setup, monkeypatch):
    app, _ = setup
    monkeypatch.setattr(sub_store_client, 'get_subscription_proxies_yaml',
                        Mock(return_value=(yaml.safe_dump({'proxies': PROXIES}), 'direct_yaml')))
    response = app.test_client().post('/api/tasks/run', json={'key': 'subscription:s'})
    assert response.status_code == 202
    wait_background()
    row = rows(app)['subscription:s']
    assert (row['last_status'], row['last_trigger'], row['run_count'], row['running']) == ('success', 'manual', 1, False)
    assert load_subscription_cache('s')['count'] == 1
    assert app.test_client().post('/api/tasks/run', json={'key': 'subscription:gone'}).status_code == 404
    assert app.test_client().post('/api/tasks/run', json={'key': 'agent_rule_sync:a'}).status_code == 400
    assert app.test_client().post('/api/tasks/run', json={}).status_code == 400


def test_interval_edit_validates_and_rule_source_edits_keep_it(setup, monkeypatch):
    app, repo = setup
    client = app.test_client()
    assert client.put('/api/tasks/interval', json={'key': 'rule_source:rule', 'interval_seconds': 30}).status_code == 400
    assert client.put('/api/tasks/interval', json={'key': 'rule_source:rule', 'interval_seconds': 3600, 'x': 1}).status_code == 400
    assert client.put('/api/tasks/interval', json={'key': 'router_rule_sync:p:r', 'interval_seconds': 3600}).status_code == 400
    response = client.put('/api/tasks/interval', json={'key': 'rule_source:rule', 'interval_seconds': 0})
    assert response.status_code == 200
    task = response.get_json()['task']
    assert (task['interval_seconds'], task['enabled'], task['next_run_at']) == (0, False, None)
    assert client.put('/api/tasks/interval', json={'key': 'subscription:s', 'interval_seconds': 3600}).status_code == 200
    assert repo.get_shared()['subscriptions'][0]['interval'] == 3600
    # The rule library editor does not send the interval; renaming keeps it.
    write_validated_rule_cache(RULE, b'+.old.test\n')
    edited = {key: value for key, value in RULE.items()}
    edited['name'] = 'Renamed'
    assert client.put('/api/rule-library/rule', json=edited).status_code == 200
    assert repo.get_shared()['rule_library'][0]['update_interval'] == 0


def test_source_changes_and_deletions_forget_the_previous_history(setup, monkeypatch):
    app, repo = setup
    client = app.test_client()
    task_status.record('subscription:s', ok=False, error='old address')
    task_status.record('rule_source:rule', ok=False, error='old address')
    sub = repo.get_shared()['subscriptions'][0]
    assert client.put('/api/subscriptions/s', json={**sub, 'name': 'Renamed'}).status_code == 200
    assert task_status.get('subscription:s')['last_error'] == 'old address'
    assert client.put('/api/subscriptions/s', json={**sub, 'url': SUB_URL + '&v=2'}).status_code == 200
    assert task_status.get('subscription:s') == {}
    monkeypatch.setattr('backend.utils.rule_utils.request_rule', Mock(return_value=Mock(
        content=b'+.new.test\n', raise_for_status=lambda: None)))
    assert client.put('/api/rule-library/rule', json={**RULE, 'url': RULE_URL + '&v=2'}).status_code == 200
    assert task_status.get('rule_source:rule') == {}
    task_status.record('rule_source:rule', ok=True)
    assert client.delete('/api/rule-library/rule').status_code == 200
    assert task_status.get('rule_source:rule') == {}


def test_scheduler_skips_disabled_sources_and_sources_without_automatic_updates(setup, monkeypatch):
    _, repo = setup
    def disable(shared):
        shared['subscriptions'][0]['interval'] = 0
        shared['rule_library'][0]['enabled'] = False
    repo.update_shared_transaction(disable)
    assert scheduled_refresh.run_round() == 0


def test_rule_source_due_on_first_round_without_cache(setup, monkeypatch):
    _, repo = setup
    repo.update_shared_transaction(lambda shared: shared['subscriptions'][0].update(interval=0))
    monkeypatch.setattr('backend.utils.rule_utils.request_rule', Mock(return_value=Mock(
        content=b'+.new.test\n', raise_for_status=lambda: None)))
    assert scheduled_refresh.run_round() == 1
    assert read_validated_rule_cache(RULE) == b'+.new.test\n'
    assert scheduled_refresh.run_round() == 0


def test_router_agent_dns_follow_domain_and_backup_rows(setup, monkeypatch):
    app, repo = setup
    from backend.routers import rule_sync_store
    repo.save_profile('default', {'routers': {'items': [
        {'id': 'ros', 'name': 'ROS', 'type': 'routeros', 'auto_rule_sync': True, 'rule_sync_interval': 30},
        {'id': 'ikuai', 'name': 'iKuai', 'type': 'ikuai'}]},
        'domain_discovery': {'auto_probe': True}})
    rule_sync_store.write('default', 'ros', {'status': 'failed', 'message': '下载失败', 'checked_at': '2026-10-24T00:00:00+00:00',
                                              'baseline': {'rules': {}}})
    repo.update_system_transaction(lambda system: system.update(
        agents=[{'id': 'm', 'name': 'Mihomo', 'service_type': 'mihomo', 'profile_id': 'default',
                 'rule_sync': {'settings': {'library': True, 'rules': False},
                               'state': {'status': 'blocked', 'message': '请先升级 Agent'}}},
                {'id': 'o', 'name': 'OxiDNS', 'service_type': 'oxidns', 'profile_id': 'default', 'dns_follow': {'source_agent_id': 'm'},
                 'dns_follow_state': {'status': 'failed', 'message': 'OxiDNS 同步失败'}}],
        backup={'webdav_url': 'https://dav.test', 'webdav_username': 'u', 'webdav_password': 'p', 'auto_backup': True}))
    task_status.record('webdav_backup', ok=False, error='WebDAV 返回 HTTP 401', trigger='event')
    listed = rows(app)
    router = listed['router_rule_sync:default:ros']
    assert (router['last_status'], router['last_error'], router['interval_seconds'], router['can_run']) == ('failed', '下载失败', 1800, True)
    assert router['next_run_at'] == '2026-10-24T00:30:00Z' and router['profile_name']
    assert not any(key.endswith(':ikuai') for key in listed)
    agent = listed['agent_rule_sync:m']
    assert (agent['last_status'], agent['last_error'], agent['can_run'], agent['schedule_text']) == ('skipped', '请先升级 Agent', False, '规则变化后自动同步')
    assert 'agent_rule_sync:o' not in listed  # automatic sync is off and it never ran
    follow = listed['dns_follow:o']
    assert (follow['last_status'], follow['can_run']) == ('failed', True)
    domain = listed['domain_discovery:default']
    assert (domain['enabled'], domain['interval_seconds'], domain['can_run']) == (True, 600, True)
    backup = listed['webdav_backup']
    assert (backup['last_status'], backup['last_error'], backup['enabled']) == ('failed', 'WebDAV 返回 HTTP 401', True)


def test_domain_discovery_records_a_skipped_round_with_its_reason(setup):
    _, repo = setup
    from backend.utils.domain_discovery_service import run_auto_task
    assert run_auto_task('default') is None
    assert task_status.get('domain_discovery:default') == {}  # automatic probing is off
    repo.save_profile('default', {'domain_discovery': {'auto_probe': True}})
    config_module.reset_config_context()
    assert run_auto_task('default') is None
    entry = task_status.get('domain_discovery:default')
    assert entry['last_status'] == 'skipped' and entry['last_error'] and entry.get('run_count', 0) == 0
