"""Bounded timelines contain observed progress, never credentials or raw payloads."""
from datetime import datetime, timedelta, timezone

from backend.agents.operation_events import append_event, clean_events, event_time, merge_events
from backend.agents.deployment_messages import public_deployment_messages
from backend.agents.upgrade_messages import public_upgrade_state


def test_event_history_is_bounded_and_adjacent_unchanged_progress_does_not_duplicate():
    events = []
    start = datetime(2026, 10, 18, tzinfo=timezone.utc)
    for index in range(120):
        at = (start + timedelta(seconds=index)).isoformat()
        events = append_event(events, 'preparing', f'规则文件 {index}/120', at=at)
        events = append_event(events, 'preparing', f'规则文件 {index}/120', at=at)
    assert len(events) == 80
    assert events[0]['message'] == '规则文件 40/120'
    assert events[-1]['message'] == '规则文件 119/120'


def test_external_events_reject_invalid_shapes_times_and_status_and_strip_secrets():
    events = clean_events([
        None, {}, {'at': 'not-time', 'status': 'checking'},
        {'at': '0001-01-01T00:00:00+14:00', 'status': 'checking'},
        {'at': '9999-12-31T23:59:59-14:00', 'status': 'checking'},
        {'at': '2026-10-18T00:00:00Z', 'status': 'alien'},
        {'at': '2026-10-18T00:00:00Z', 'status': 'checking',
         'message': '下载 https://user:password@example.test/secret-path?token=hidden AgentToken',
         'token': 'extra-secret', 'config': 'private'},
    ], ('AgentToken',), {'checking'})
    assert len(events) == 1
    assert set(events[0]) == {'at', 'status', 'message'}
    assert events[0]['at'] == '2026-10-18T00:00:00.000000Z'
    for secret in ('password', 'secret-path', 'hidden', 'AgentToken', 'extra-secret', 'private'):
        assert secret not in str(events)


def test_repeated_worker_journal_merges_without_replaying_stages():
    events = append_event([], 'queued', at='2026-10-18T00:00:00Z')
    incoming = [{'at': '2026-10-18T00:00:01Z', 'status': 'queued'},
                {'at': '2026-10-18T00:00:02Z', 'status': 'downloading', 'message': '下载 Agent v1'},
                {'at': '2026-10-18T00:00:03Z', 'status': 'verifying', 'message': '检查候选版本'}]
    merged = merge_events(events, incoming)
    assert [item['status'] for item in merged] == ['queued', 'downloading', 'verifying']
    assert merge_events(merged, incoming) == merged


def test_event_times_sort_whole_seconds_before_fractional_seconds():
    whole = event_time('2026-10-18T00:00:00Z')
    fraction = event_time('2026-10-18T00:00:00.1Z')
    assert whole < fraction < event_time('2026-10-18T00:00:01Z')


def test_public_history_localizes_errors_without_mutating_saved_events():
    raw = {'events': [{'at': '2026-10-18T00:00:00Z', 'status': 'failed',
                       'message': 'Agent download size or SHA-256 mismatch'}]}
    public = public_upgrade_state(raw)
    assert '校验失败' in public['events'][0]['message']
    assert raw['events'][0]['message'] == 'Agent download size or SHA-256 mismatch'
    deployment = public_deployment_messages({'events': [{'at': event_time(), 'status': 'failed',
                                                         'message': 'service start failed'}]})
    assert '服务启动失败' in deployment['events'][0]['message']
