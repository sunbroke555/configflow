from datetime import datetime

import pytest

from backend.agents.traffic_store import route_key
from backend.utils.domain_discovery import is_device_group_route, summarize


def data(*routes):
    return {'days': {'2026-10-24': {'hosts': {'www.example.com': {
        'first_seen': '2026-10-24T10:00:00', 'last_seen': '2026-10-24T11:00:00',
        'by_route': {route_key(rule, payload, 'direct', 'DIRECT'): {
            'conns': conns, 'fails': fails, 'fail_kinds': {}, 'up': 0, 'down': 0,
        } for rule, payload, conns, fails in routes},
    }}}}}


def summarize_routes(*routes, view='all'):
    return summarize([({'id': 'a'}, data(*routes))], days=1, view=view,
                     now=datetime(2026, 10, 24, 12))


def test_group_failures_remain_visible_but_never_create_global_suggestions():
    group = ('SubRules', '(RULE-SET,cf_dg_set_1234abcd,src)', 2, 9)
    item = summarize_routes(group)['items'][0]
    assert item['conns'] == 2 and item['fails'] == 9
    assert item['tags'] == []
    assert summarize_routes(group, view='failing')['items'] == []
    assert summarize_routes(group, view='uncovered')['items'] == []


def test_group_successes_do_not_dilute_default_group_failures_on_the_same_host():
    item = summarize_routes(('SubRules', '(RULE-SET,cf_dg_set_1234abcd,src)', 100, 0),
                            ('Match', '', 0, 3))['items'][0]
    assert item['tags'] == ['uncovered', 'failing']
    assert item['hosts'][0]['direct_conns'] == 0
    assert item['hosts'][0]['direct_fails'] == 3


def test_group_failures_do_not_inflate_default_group_failure_ratio():
    item = summarize_routes(('SubRules', '(RULE-SET,cf_dg_set_1234abcd,src)', 0, 100),
                            ('Match', '', 10, 1))['items'][0]
    assert item['tags'] == ['uncovered']


@pytest.mark.parametrize('rule,payload', [
    ('SubRules', '(RULE-SET,lan_devices,src)'),
    ('SubRules', '(DOMAIN,cf_dg_set_1234abcd,src)'),
    ('SubRules', '(RULE-SET,cf_dg_set_1234abcd,src),other'),
    ('RuleSet', '(RULE-SET,cf_dg_set_1234abcd,src)'),
])
def test_user_subrules_or_similar_text_are_not_ignored(rule, payload):
    assert not is_device_group_route({'rule': rule, 'rule_payload': payload})
    assert summarize_routes((rule, payload, 0, 3))['items'][0]['tags'] == ['failing']
