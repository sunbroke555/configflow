"""Freeze complete pre-device-group output before changing any generator.

Fixtures were recorded with the real converters on upstream/main c602993.
They are deliberately static: these tests never regenerate expected output.
Mihomo provider delivery is disabled because this checks serialization, not I/O.
"""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from backend.converters.loon import generate_loon_config
from backend.converters.mihomo import generate_mihomo_config
from backend.converters.mosdns import generate_mosdns_config
from backend.converters.oxidns import generate_oxidns_config
from backend.converters.surge import generate_surge_config


FIXTURES = Path(__file__).parent / 'fixtures' / 'device_group_compat_golden'
CASES = json.loads((FIXTURES / 'manifest.json').read_text(encoding='utf-8'))['cases']
CONVERTERS = {
    'oxidns': generate_oxidns_config,
    'mihomo': lambda config: generate_mihomo_config(config, preflight_providers=False),
    'mosdns': generate_mosdns_config,
    'surge': generate_surge_config,
    'loon': generate_loon_config,
}


def _cases():
    for case in CASES:
        states = ['absent', 'null']
        # A legacy policy space must explicitly migrate before owning groups.
        if case['no_op_groups']:
            states += ['default_only', 'follow_only']
        for state in states:
            yield pytest.param(case, state, id=f"{case['name']}-{state}")


@pytest.mark.parametrize(('case', 'group_state'), list(_cases()))
def test_no_device_dispatch_preserves_complete_generated_bytes(case, group_state):
    config = json.loads((FIXTURES / case['input']).read_text(encoding='utf-8'))
    if group_state == 'null':
        config.update(device_groups=None, policy_routing_sources=None)
    elif group_state in ('default_only', 'follow_only'):
        groups = [{'id': 'default', 'name': '默认组', 'members': []}]
        if group_state == 'follow_only':
            groups.insert(0, {
                'id': 'dg_1234567890abcdef', 'name': '跟随全局',
                'members': ['192.168.60.0/24'], 'mode': 'follow',
            })
        config['device_groups'] = {'version': 1, 'groups': groups}

    expected = (FIXTURES / case['output']).read_bytes()
    actual = CONVERTERS[case['engine']](deepcopy(config)).encode('utf-8')
    assert actual == expected
