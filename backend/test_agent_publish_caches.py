"""Publication parses each rule source once, with results identical to a cold parse."""
from copy import deepcopy
import io
import os
import random
import tarfile

import pytest
import yaml

from backend.agents import deployment_bundle
from backend.agents.deployment_bundle import convert_mosdns_rules, deployment_content_fingerprint
from backend.common import config as config_module
from backend.common.config_repository import ProfileRepository
from backend.routes import agents
from backend.utils import ruleset_content
from backend.utils.ruleset_content import (
    RemoteRuleValidationError, clear_rule_content_caches, inspect_domain_content, normalize_mihomo_ruleset,
    validate_remote_rule_content,
)

BASE_URL = 'https://config.test'


def nodes(node):
    """Everything callers read from a composed node, including marks."""
    if node is None:
        return None
    marks = (node.start_mark.index, node.start_mark.line, node.start_mark.column,
             node.end_mark.index, node.end_mark.line, node.end_mark.column)
    if isinstance(node, yaml.ScalarNode):
        return ('scalar', node.tag, node.value, node.style or None, marks)
    children = ([(nodes(key), nodes(value)) for key, value in node.value] if isinstance(node, yaml.MappingNode)
                else [nodes(child) for child in node.value])
    return (type(node).__name__, node.tag, node.flow_style, marks, children)


def tokens(content, loader):
    return [(type(token).__name__, token.start_mark.index, token.start_mark.line, token.start_mark.column,
             token.end_mark.index, getattr(token, 'value', None))
            for token in yaml.scan(content, Loader=loader) if not isinstance(token, yaml.tokens.StreamEndToken)]


def outcome(function):
    try:
        return 'ok', function()
    except Exception as error:  # noqa: BLE001 - compare any outcome
        return 'error', type(error).__name__, str(error)


CHARS = list('abz09.-_+/,=:@~^$()|?*\\#"\' \t[]{}&!%`<>;') + ['中', '😀', '\xa0', '\x85', ' ', '﻿', '\r']
WORDS = ['a.com', '+.b.com', 'DOMAIN,x.com', 'IP-CIDR,1.2.3.0/24,no-resolve', 'IP-CIDR6,2001:db8::/32', '1',
         'true', 'null', '~', '2020-01-01', '0x1F', '.inf', '=', '<<', 'DOMAIN-REGEX,^.*\\.g\\.com$', 'x:', 'x:y',
         'a|b', '(a)', '*.a.com', 'NaN', '12:30:00', '-a', 'bad domain', 'DOMAIN,', 'MATCH']


def random_document(rng):
    def text(count):
        return ''.join(rng.choice(CHARS) for _ in range(rng.randint(0, count)))

    def scalar():
        value = rng.choice(WORDS) if rng.random() < .6 else text(8) or 'a'
        quote = rng.random()
        return f'"{value}"' if quote < .15 else f"'{value}'" if quote < .3 else value

    eol = '\r\n' if rng.random() < .2 else '\n'
    lines = [rng.choice(['', '  ', '#' + text(5), ' # c']) for _ in range(rng.randint(0, 2))]
    lines.append('payload:' + rng.choice(['', ' ', ' # ' + text(4)]))
    indent = ' ' * rng.choice([0, 1, 2, 2, 4])
    for _ in range(rng.randint(0, 6)):
        kind = rng.random()
        if kind < .75:
            lines.append(indent + '-' + ' ' * rng.randint(1, 2) + scalar() + rng.choice(['', ' ', ' #' + text(4)]))
        elif kind < .9:
            lines.append(rng.choice(['', '   ', '#' + text(4)]))
        else:
            lines.append(' ' * rng.randint(0, 6) + rng.choice(['-', '- ', 'x: 1', '...', '---', '- [a, b]']))
    document = eol.join(lines) + (eol if rng.random() < .95 else '')
    if rng.random() < .1:
        position = rng.randrange(len(document) + 1)
        document = document[:position] + rng.choice(CHARS) + document[position:]
    return document


@pytest.fixture
def pure_python(monkeypatch):
    def disable():
        monkeypatch.setattr(ruleset_content, '_LIBYAML', None)
    return disable


@pytest.mark.skipif(ruleset_content._LIBYAML is None, reason='PyYAML built without libyaml')
def test_libyaml_is_used_only_where_it_matches_pyyaml_exactly():
    rng = random.Random(20261025)
    eligible = 0
    for _ in range(4000):
        document = random_document(rng)
        if not ruleset_content._libyaml(document):
            continue
        eligible += 1
        for read in (lambda loader: nodes(yaml.compose(document, Loader=loader)),
                     lambda loader: tokens(document, loader),
                     lambda loader: yaml.load(document, Loader=loader)):
            assert outcome(lambda: read(yaml.CSafeLoader)) == outcome(lambda: read(yaml.SafeLoader)), document
    assert eligible > 300
    # Shapes where libyaml's marks or acceptance differ always use PyYAML.
    for document in ('﻿payload:\n  - a.com\n', 'payload:\n  - a.com', 'payload:\n- a.com\n',
                     'payload:\n\t- a.com\n', 'payload:\n  - a\tb\n', 'payload: [a.com]\n', '{payload: [a]}\n',
                     'payload:\n  - !x a\n', 'payload:\n  - &a b\n', 'payload:\n  - a﻿b\n'):
        assert not ruleset_content._libyaml(document), document


LARGE = 'payload:\n' + ''.join(f"  - '+.d{index}.example.com'\n" for index in range(300)) + '  - bad domain\n'
CORPUS = [
    'payload:\n  - DOMAIN,a.com\n  - +.b.com # note\n  - "c.com"\n  - bare.example\n  - 1.2.3.0/24\n',
    "# header\npayload:\n  - 'DOMAIN-SUFFIX,x.com'\n  - 'MATCH'\n  - 'DOMAIN,'\n  - IP-CIDR,300.1.1.1/8\n",
    'payload:\r\n  - a.com\r\n  - b..com\r\n',
    'payload:\n  - a.com\n  - b.com', 'payload:\n- a.com\n- bad domain\n', '﻿payload:\n  - a.com\n',
    'payload: [a.com, +.b.com, "DOMAIN,c.com"]\n', '{payload: [a.com, bad domain]}  # tail\n',
    'x: &h a.com\npayload:\n  - *h\n', 'payload:\n  - [nested]\n', 'payload:\n  - |\n    a\n', 'payload: {a: 1}\n',
    'payload:\n  - a.com\npayload:\n  - b.com\n', 'payload:\n  - a\n  b\n', 'payload:\n\t- a.com\n',
    '+.a.com\nDOMAIN,b.com\nbad domain\n1.2.3.4\n', LARGE,
]


def rule_results(content):
    results = []
    for behavior in ('domain', 'ipcidr', 'classical'):
        for rule_format in ('yaml', 'text'):
            rule = {'name': 'R', 'behavior': behavior, 'format': rule_format}
            results.append(outcome(lambda: inspect_domain_content(content, behavior, rule_format)))
            results.append(outcome(lambda: normalize_mihomo_ruleset(content, behavior, rule_format)))
            for value in (content, content.encode('utf-8')):
                error = outcome(lambda: validate_remote_rule_content(value, rule, rule_format=rule_format))
                try:
                    validate_remote_rule_content(value, rule, rule_format=rule_format)
                except RemoteRuleValidationError as failure:
                    error += (failure.errors,)
                results.append(error)
        for rule_type in ('domain_set', 'ip_set'):
            for mixed in (False, True):
                results.append(outcome(lambda: convert_mosdns_rules(content.encode('utf-8'), rule_type, mixed=mixed)))
    return results


def test_rule_results_match_pure_pyyaml_cold_and_cached(pure_python):
    fast_cold, fast_cached = [], []
    for content in CORPUS:
        clear_rule_content_caches()
        fast_cold.append(rule_results(content))
        fast_cached.append(rule_results(content))
    pure_python()
    for index, content in enumerate(CORPUS):
        clear_rule_content_caches()
        expected = rule_results(content)
        assert fast_cold[index] == expected, content
        assert fast_cached[index] == expected, content
    # Diagnostics keep their source line numbers.
    with pytest.raises(RemoteRuleValidationError) as failure:
        validate_remote_rule_content(LARGE, {'name': 'R', 'behavior': 'domain', 'format': 'yaml'}, rule_format='yaml')
    assert failure.value.errors[0]['line'] == 302


def test_validation_verdict_is_cached_by_content_and_type(monkeypatch):
    calls = []
    original = ruleset_content._remote_text_entries
    monkeypatch.setattr(ruleset_content, '_remote_text_entries',
                        lambda *args: calls.append(args[1]) or original(*args))
    rule = {'name': 'R', 'behavior': 'domain', 'format': 'yaml'}
    content = b'payload:\n  - a.com\n  - +.b.com\n'
    for _ in range(3):
        validate_remote_rule_content(content, rule)
    validate_remote_rule_content(content.decode(), rule)
    assert len(calls) == 2  # bytes once, text once
    validate_remote_rule_content(content, {**rule, 'behavior': 'classical'})
    assert len(calls) == 3
    broken = b'payload:\n  - bad domain\n'
    messages = []
    for _ in range(2):
        with pytest.raises(RemoteRuleValidationError) as failure:
            validate_remote_rule_content(broken, rule)
        messages.append(str(failure.value))
    # Failures are rebuilt each time, with this rule's own diagnostics.
    assert len(calls) == 5 and messages[0] == messages[1]
    with pytest.raises(RemoteRuleValidationError, match='「Other」'):
        validate_remote_rule_content(broken, {**rule, 'name': 'Other'})


@pytest.fixture
def library_env(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path, default_config_factory=config_module.get_default_config,
                                   initial_config_factory=config_module.get_default_config)
    monkeypatch.setattr(config_module, '_repository', repository)
    config_module.reset_config_context()
    monkeypatch.setattr('requests.sessions.Session.request',
                        lambda *_args, **_kwargs: pytest.fail('unexpected network request'))
    entries = [f'DOMAIN-SUFFIX,a{index}.example' if index % 4 == 0 else f'bare{index}.example' if index % 4 == 1
               else f'IP-CIDR,10.{index % 250}.0.0/16,no-resolve' if index % 4 == 2 else f'+.c{index}.example'
               for index in range(400)]
    content = 'payload:\n' + ''.join(f"  - '{entry}'\n" for entry in entries)
    rule = {'id': 'big', 'name': 'Big', 'source_type': 'url', 'url': 'https://rules.test/big.yaml',
            'behavior': 'classical', 'format': 'yaml', 'enabled': True}
    repository.save_shared({'rule_library': [rule]})
    config = config_module.get_default_config()
    config.update(profile_id='default', rule_library=[rule])
    config['system_config'].update(server_domain=BASE_URL, rule_proxy_token='fixture-rule-token')
    config['mihomo']['custom_config'] = 'mode: rule\nlog-level: info\ndns: {enable: false}\n'
    config['mosdns'].update(local_dns='udp://192.0.2.1:53', remote_dns='udp://192.0.2.2:53',
                            fallback_dns='udp://192.0.2.3:53', direct_rulesets=['big-ref'])
    config['oxidns']['direct_rulesets'] = ['big-ref']
    config['rule_configs'] = [{'id': 'big-ref', 'itemType': 'ruleset', 'name': 'Big', 'library_rule_id': 'big',
                               'policy': 'DIRECT', 'enabled': True, 'behavior': 'classical', 'format': 'yaml',
                               'url': BASE_URL + '/api/profiles/default/rules/local/Big'}]
    from backend.utils.rule_utils import write_validated_rule_cache
    write_validated_rule_cache(rule, content.encode(), config)
    clear_rule_content_caches()  # As after a restart: nothing parsed yet.
    yield config
    config_module.reset_config_context()


def bundle_files(prepared):
    bundle = prepared['bundle']
    with tarfile.open(fileobj=io.BytesIO(bundle.archive), mode='r:gz') as archive:
        return {item['path']: archive.extractfile('files/' + item['path']).read() for item in bundle.manifest['files']}


def prepare(config, service, agent_id):
    agent = {'id': agent_id, 'profile_id': 'default', 'service_type': service}
    return agents._prepare_transactional_content(agent, deepcopy(config), base_url=BASE_URL,
                                                  deployment_id='release-' + agent_id)


@pytest.mark.parametrize('service', ['mihomo', 'oxidns', 'mosdns'])
def test_two_agents_parse_a_shared_rule_source_once(library_env, monkeypatch, service):
    parses = []
    for name in ('_remote_text_entries', 'inspect_domain_content'):
        original = getattr(ruleset_content, name)
        monkeypatch.setattr(ruleset_content, name, lambda *args, _o=original, _n=name: parses.append(_n) or _o(*args))
    original_convert = deployment_bundle._convert_mosdns_rules
    monkeypatch.setattr(deployment_bundle, '_convert_mosdns_rules',
                        lambda *args: parses.append('convert') or original_convert(*args))
    first, second = prepare(library_env, service, 'agent-a'), prepare(library_env, service, 'agent-b')
    per_agent = len(parses)
    # Before: every Agent re-validated the cache read, normalized and
    # validated the normalized bytes (Mihomo), or converted again (DNS cores).
    assert per_agent == 3, parses
    assert deployment_content_fingerprint(first['bundle'].manifest) == \
        deployment_content_fingerprint(second['bundle'].manifest)
    cached = bundle_files(second)
    monkeypatch.setattr(ruleset_content, '_LIBYAML', None)
    clear_rule_content_caches()
    assert bundle_files(prepare(library_env, service, 'agent-c')) == cached


def test_unchanged_rule_cache_is_touched_not_rewritten(library_env, monkeypatch):
    from backend.utils.rule_utils import rule_cache_filename, write_validated_rule_cache
    repository = config_module._repository
    rule = library_env['rule_library'][0]
    path = repository.shared_path(os.path.join('rules', rule_cache_filename(rule)))
    content = path.read_bytes()
    writes = []
    original = repository.write_shared_text
    monkeypatch.setattr(repository, 'write_shared_text', lambda *args: writes.append(args) or original(*args))
    write_validated_rule_cache(rule, content, library_env)  # No .bak yet: a real write creates it.
    assert len(writes) == 1 and path.with_name(path.name + '.bak').read_bytes() == content
    os.utime(path, (1, 1))
    write_validated_rule_cache(rule, content, library_env)
    write_validated_rule_cache(rule, content.decode(), library_env)
    assert len(writes) == 1 and path.read_bytes() == content
    assert path.stat().st_mtime > 1  # Still reported as freshly refreshed.
    changed = content.replace(b'bare1.example', b'renamed1.example')
    assert changed != content
    write_validated_rule_cache(rule, changed, library_env)
    assert len(writes) == 2 and path.read_bytes() == changed
    assert path.with_name(path.name + '.bak').read_bytes() == content


def test_gateway_preparation_generates_mihomo_once(monkeypatch):
    from backend.agents.linux_gateway import MIN_AGENT_VERSION
    from backend.converters import mihomo
    from backend.test_linux_gateway import snapshot
    config = snapshot()
    expected = mihomo.get_mihomo_ruleset_downloads(deepcopy(config))
    calls = []
    original = mihomo.generate_mihomo_config
    counted = lambda *args, **kwargs: calls.append(1) or original(*args, **kwargs)  # noqa: E731
    monkeypatch.setattr(mihomo, 'generate_mihomo_config', counted)
    monkeypatch.setattr(agents, 'generate_mihomo_config', counted)
    prepared = agents._prepare_transactional_content(
        {'id': 'gateway', 'service_type': 'mihomo', 'version': MIN_AGENT_VERSION}, deepcopy(config))
    assert len(calls) == 1
    assert prepared['ruleset_downloads'] == expected and expected
