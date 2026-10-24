"""Indexed matching must preserve the original first-rule semantics."""

from concurrent.futures import ThreadPoolExecutor
import random
from threading import Event
from unittest.mock import Mock

import pytest

from backend.utils import rule_matcher
from backend.utils.rule_matcher import RuleConfigMatcher, match_query, parse_rule_line


def _ruleset(rule_id="set", **overrides):
    return {"id": rule_id, "itemType": "ruleset", "name": rule_id,
            "policy": "PROXY", "behavior": "classical", **overrides}


def _linear_match(configs, contents, query):
    """Linear matching with the same behavior-aware source interpretation."""
    for priority, item in enumerate(configs, 1):
        if not item.get("enabled", True) or not item.get("library_enabled", True):
            continue
        item_type = item.get("itemType", "rule")
        if item_type == "rule":
            rule_type, value = item.get("rule_type", ""), item.get("value", "")
            if match_query(query, rule_type, value):
                return {
                    "rule_name": f"{rule_type} 规则", "rule_type": "rule",
                    "matched_line": f"{rule_type},{value}", "policy": item.get("policy", "DIRECT"),
                    "source": "直接配置的规则", "priority": priority, "rule_id": item.get("id"),
                    "item_type": "rule", "behavior": "classical",
                }
        elif item_type == "ruleset":
            for value, line in rule_matcher._iter_ruleset_lines(contents.get(item["id"], ""), item.get('format', 'text')):
                parsed = rule_matcher._parse_ruleset_line(value, item.get('behavior', 'classical'))
                if parsed and match_query(query, *parsed):
                    name = item.get("name", "规则集")
                    return {
                        "rule_name": name, "rule_type": "ruleset", "matched_line": line.strip(),
                        "policy": item.get("policy", "DIRECT"), "source": f"规则集: {name}",
                        "priority": priority, "rule_id": item.get("id"), "item_type": "ruleset",
                        "behavior": item.get("behavior", "classical"),
                    }
    return None


@pytest.mark.parametrize("query", [
    "EXAMPLE.COM", "a.example.com", "a.b.example.com", "plain.example", "a.plain.example",
    "a.direct.example", "b.a.direct.example", "direct.example", "only.example", "a.only.example",
    "ads.other.test", "cdn23.other.test", "unmatched.test", "localhost", "bad host", "",
    "192.0.2.1", "192.0.2.250", "198.51.100.19", "2001:db8::1", "2001:db9::1",
    "fe80::1%eth0", "fe80::1%eth1", "::ffff:192.0.2.1",
])
def test_mixed_yaml_rules_match_linear_reference(query):
    contents = {"mixed": """
# common supported formats, and ignored YAML metadata
payload:
  - 'DOMAIN-KEYWORD,ads'
  - 'REGEX,[invalid'
  - 'regexp:^cdn[0-9]{1,3}\\.other\\.test$'
  - 'DOMAIN-SUFFIX,EXAMPLE.com'
  - 'full:example.com'
  - 'plain.example'
  - '*.direct.example'
  - '.only.example'
  - 'domain:only.example'
  - 'IP-CIDR,192.0.2.9/24,no-resolve'
  - 'IP,192.0.2.1'
  - 'ip:198.51.100.19'
  - '2001:db8::/32'
  - 'fe80::1%eth0'
  - 'IP-CIDR6,::ffff:192.0.2.0/120'
  - 'DOMAIN-REGEX,.*'
  - 'localhost' # preserve original displayed line
"""}
    configs = [_ruleset("disabled", enabled=False), _ruleset("mixed", format='yaml')]
    matcher = RuleConfigMatcher(configs, [], lambda item, _: contents.get(item["id"], ""))
    assert matcher.match(query) == _linear_match(configs, contents, query)


@pytest.mark.parametrize("lines,query,expected", [
    (["DOMAIN-SUFFIX,com", "DOMAIN,a.example.com"], "a.example.com", "DOMAIN-SUFFIX,com"),
    (["DOMAIN,a.example.com", "DOMAIN-SUFFIX,com"], "a.example.com", "DOMAIN,a.example.com"),
    (["DOMAIN-KEYWORD,example", "+.example.com"], "a.example.com", "DOMAIN-KEYWORD,example"),
    (["+.example.com", "DOMAIN-KEYWORD,example"], "a.example.com", "+.example.com"),
    (["DOMAIN-REGEX,example", "DOMAIN-SUFFIX,example.com"], "a.example.com", "DOMAIN-REGEX,example"),
    (["DOMAIN-SUFFIX,example.com", "DOMAIN-REGEX,example"], "a.example.com", "DOMAIN-SUFFIX,example.com"),
    (["full:Example.com", "DOMAIN,example.com"], "example.com", "DOMAIN,example.com"),
    (["IP-CIDR,192.0.2.1/24", "IP-CIDR,192.0.2.0/25", "IP,192.0.2.1"],
     "192.0.2.1", "IP-CIDR,192.0.2.1/24"),
    (["192.0.2.1", "IP-CIDR6,192.0.2.0/24"], "192.0.2.1", "192.0.2.1"),
    (["IP-CIDR,::/0", "IP-CIDR6,2001:db8::/32"], "2001:db8::1", "IP-CIDR,::/0"),
    (["IP,invalid", "IP-CIDR,bad", "IP-CIDR6,fe80::/16"], "fe80::1%eth0", "IP-CIDR6,fe80::/16"),
])
def test_earliest_line_wins_over_specificity_or_rule_type(lines, query, expected):
    matcher = RuleConfigMatcher([_ruleset()], [], lambda *_: "\n".join(lines))
    assert matcher.match(query)["matched_line"] == expected


def test_configuration_priority_and_lazy_loading_are_unchanged():
    library = {"id": "lib", "behavior": "domain"}
    configs = [
        {"id": "disabled", "rule_type": "DOMAIN", "value": "example.com", "enabled": False},
        _ruleset("disabled-lib", library_enabled=False),
        {"id": "direct", "rule_type": "DOMAIN", "value": "direct.test"},
        _ruleset("first", library_rule_id="lib", policy="first-policy", behavior="domain"),
        _ruleset("last", policy="last-policy"),
    ]
    contents = {"first": "+.example.com", "last": "example.com"}
    loader = Mock(side_effect=lambda item, _: contents[item["id"]])
    matcher = RuleConfigMatcher(configs, [library], loader)
    assert matcher.match("direct.test") == _linear_match(configs, contents, "direct.test")
    loader.assert_not_called()
    assert matcher.match("example.com") == _linear_match(configs, contents, "example.com")
    loader.assert_called_once_with(configs[3], library)
    assert matcher.match("A.example.com")["priority"] == 4
    assert loader.call_count == 1


@pytest.mark.parametrize("rule_type,value,query", [
    ("IP", "192.0.2.1", "192.0.2.1"),
    ("IP", "fe80::1%eth0", "fe80::1%eth0"),
    ("IP", "fe80::1%eth0", "fe80::1%eth1"),
    ("IP", "bad", "192.0.2.1"),
    ("IP-CIDR", "192.0.2.123/24", "192.0.2.1"),
    ("IP-CIDR", "2001:db8::/32", "2001:db8::1"),
    ("IP-CIDR6", "192.0.2.0/24", "192.0.2.1"),
    ("IP-CIDR6", "bad", "2001:db8::1"),
    ("DOMAIN", "192.0.2.1", "192.0.2.1"),
    ("IP", "192.0.2.1", "www.example.com"),
    ("REGEX", r"^\\D+$", "123"),
    ("REGEX", r"^\\D+$", "abc"),
    ("REGEX", "[", "abc"),
])
def test_direct_rules_retain_query_classification_and_regex_semantics(rule_type, value, query):
    configs = [{"id": "direct", "rule_type": rule_type, "value": value}]
    matcher = RuleConfigMatcher(configs, [], Mock(side_effect=AssertionError("unexpected ruleset load")))
    assert matcher.match(query) == _linear_match(configs, {}, query)


def test_classical_regex_preserves_regex_escapes_and_original_line():
    content = r"DOMAIN-REGEX,^\D+$"
    matcher = RuleConfigMatcher([_ruleset()], [], lambda *_: content)
    assert matcher.match("abc")["matched_line"] == content
    assert matcher.match("123") is None


@pytest.mark.parametrize("seed", range(12))
def test_shuffled_rules_and_queries_match_linear_reference(seed):
    rng = random.Random(seed)
    rules = [
        "DOMAIN,example.com", "DOMAIN-SUFFIX,example.com", "keyword:ample", "REGEXP:example",
        ".example.com", "*.example.com", "example.net", "+.example.net", "regexp:^a\\..*",
        "REGEX,[", "DOMAIN-REGEX,.*", "DOMAIN-KEYWORD,", "REGEX,", "DOMAIN-SUFFIX,",
        "IP-CIDR,192.0.2.19/24", "IP-CIDR6,192.0.2.0/25", "IP,192.0.2.10", "IP-CIDR,bad",
        "IP-CIDR6,2001:db8::/32", "IP-CIDR,::/0", "ip:2001:db8::1", "IP,fe80::1%eth0",
    ]
    rng.shuffle(rules)
    configs = [_ruleset("a"), {"id": "direct", "rule_type": "DOMAIN-SUFFIX", "value": "test"}, _ruleset("b")]
    contents = {"a": "\n".join(rules[:10]), "b": "\n".join(rules[10:])}
    matcher = RuleConfigMatcher(configs, [], lambda item, _: contents[item["id"]])
    queries = [f"{prefix}{domain}" for prefix in ("", "a.", "a.b.")
               for domain in ("EXAMPLE.COM", "example.net", "sample.test", "unknown.org")]
    queries += ["192.0.2.10", "192.0.2.199", "192.0.3.1", "2001:db8::1", "2001:db9::1", "fe80::1%eth0"]
    for query in queries:
        assert matcher.match(query) == _linear_match(configs, contents, query), (seed, query)


def test_large_ruleset_reuses_index_and_classifies_once_per_query(monkeypatch):
    # Representative large remote ruleset, followed by hundreds of reported hosts.
    content = "\n".join(f"DOMAIN-SUFFIX,rule{i}.example" for i in range(100_000))
    loader = Mock(return_value=content)
    matcher = RuleConfigMatcher([_ruleset()], [], loader)
    assert matcher.match("warmup.test") is None
    parse_ip = Mock(wraps=rule_matcher.ipaddress.ip_address)
    validate_domain = Mock(wraps=rule_matcher.is_valid_domain)
    monkeypatch.setattr(rule_matcher.ipaddress, "ip_address", parse_ip)
    monkeypatch.setattr(rule_matcher, "is_valid_domain", validate_domain)
    # The old algorithm performed these operations on every rule of every host.
    monkeypatch.setattr(rule_matcher, "match_query", Mock(side_effect=AssertionError("per-rule query parsing")))
    monkeypatch.setattr(rule_matcher, "match_domain", Mock(side_effect=AssertionError("linear domain scanning")))
    monkeypatch.setattr(rule_matcher, "parse_rule_line", Mock(side_effect=AssertionError("reparsing rules")))
    for i in range(400):
        expected = f"DOMAIN-SUFFIX,rule{99_999 - i}.example"
        assert matcher.match(f"sub.rule{99_999 - i}.example")["matched_line"] == expected
        assert matcher.match(f"unmatched{i}.test") is None
    assert parse_ip.call_count == 800
    assert validate_domain.call_count == 800
    loader.assert_called_once()


def test_concurrent_first_queries_publish_one_complete_index():
    loading, release = Event(), Event()

    def load(*_):
        loading.set()
        assert release.wait(5)
        return "+.example.com\nDOMAIN,other.test"

    loader = Mock(side_effect=load)
    matcher = RuleConfigMatcher([_ruleset()], [], loader)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(matcher.match, "a.example.com") for _ in range(8)]
        assert loading.wait(5)
        release.set()
        results = [future.result(timeout=5) for future in futures]
    assert all(result["matched_line"] == "+.example.com" for result in results)
    loader.assert_called_once()


def test_failed_ruleset_load_is_cached_and_later_rules_can_match():
    loader = Mock(side_effect=OSError("missing cache"))
    matcher = RuleConfigMatcher([
        _ruleset(), {"id": "fallback", "rule_type": "DOMAIN", "value": "example.com"},
    ], [], loader)
    assert matcher.match("example.com")["rule_id"] == "fallback"
    assert matcher.match("example.com")["rule_id"] == "fallback"
    loader.assert_called_once()


def _library_key(_item, library):
    return library['id'] if library else None


def test_compact_index_keeps_first_source_line_across_duplicate_and_ignored_rules():
    lines = [
        'DOMAIN-REGEX,ignored', 'IP,bad', 'REGEX,[invalid',
        '  full:Example.com  ', 'DOMAIN,example.com',
        'IP-CIDR,192.0.2.9/24,no-resolve', 'IP-CIDR,192.0.2.0/24',
        'IP,192.0.2.1', 'IP,192.0.2.1',
        'DOMAIN-KEYWORD,example', 'REGEX,example',
    ]
    contents = {'set': '\n'.join(lines)}
    configs = [_ruleset()]
    matcher = RuleConfigMatcher(configs, [], lambda *_: contents['set'])
    for query in ('example.com', 'www.example.com', '192.0.2.1', '192.0.2.250', 'absent.test'):
        assert matcher.match(query) == _linear_match(configs, contents, query)


def test_shared_library_does_not_reuse_item_dependent_loader_by_default():
    library = {'id': 'shared', 'name': 'same-name'}
    configs = [_ruleset('first', library_rule_id='shared'), _ruleset('second', library_rule_id='shared')]
    contents = {'first': 'first.example', 'second': 'second.example'}
    loader = Mock(side_effect=lambda item, _: contents[item['id']])
    matcher = RuleConfigMatcher(configs, [library], loader)
    assert matcher.match('second.example') == _linear_match(configs, contents, 'second.example')
    assert matcher.match('first.example') == _linear_match(configs, contents, 'first.example')
    assert loader.call_count == 2


@pytest.mark.parametrize('reverse', [False, True])
def test_opt_in_reuse_separates_behavior_and_preserves_reference_metadata(reverse):
    library = {'id': 'shared', 'name': 'shared-source'}
    configs = [
        _ruleset('first', name='first display', library_rule_id='shared', policy='DIRECT', behavior='domain'),
        _ruleset('second', name='second display', library_rule_id='shared', policy='PROXY', behavior='classical'),
    ]
    if reverse:
        configs.reverse()
    contents = {item['id']: '+.example.com\nIP-CIDR,192.0.2.0/24' for item in configs}
    loader = Mock(return_value=contents['first'])
    matcher = RuleConfigMatcher(configs, [library], loader, index_key=_library_key)
    # Different behavior parses the same source into distinct indexes.
    assert matcher.match('absent.test') is None
    for query in ('example.com', 'sub.example.com', '192.0.2.99'):
        assert matcher.match(query) == _linear_match(configs, contents, query)
    assert loader.call_count == 2


def test_reuse_is_scoped_to_matcher_and_source_identity_not_display_name():
    configs = [_ruleset('one', library_rule_id='one'), _ruleset('two', library_rule_id='two')]
    libraries = [{'id': 'one', 'name': 'same'}, {'id': 'two', 'name': 'same'}]
    contents = {'one': 'one.example', 'two': 'two.example'}
    loader = Mock(side_effect=lambda _, library: contents[library['id']])
    first = RuleConfigMatcher(configs, libraries, loader, index_key=_library_key)
    assert first.match('two.example')['rule_id'] == 'two'
    assert loader.call_count == 2
    # A different repository/snapshot with the same IDs must build its own index.
    second_loader = Mock(return_value='new.example')
    second = RuleConfigMatcher(configs, libraries, second_loader, index_key=_library_key)
    assert second.match('new.example')['rule_id'] == 'one'
    assert first.match('new.example') is None
    second_loader.assert_called_once()


def test_none_reuse_key_falls_back_to_individual_item_without_key_collision():
    configs = [_ruleset('one'), _ruleset('two'), _ruleset('three')]
    contents = {'one': 'one.example', 'two': 'two.example', 'three': 'three.example'}
    loader = Mock(side_effect=lambda item, _: contents[item['id']])
    matcher = RuleConfigMatcher(configs, [], loader,
                                index_key=lambda item, _: 1 if item['id'] == 'three' else None)
    for query in ('one.example', 'two.example', 'three.example'):
        assert matcher.match(query) == _linear_match(configs, contents, query)
    assert loader.call_count == 3


def test_invalidating_shared_failed_source_retries_all_references_but_keeps_healthy_index():
    libraries = [{'id': 'failed', 'name': 'failed'}, {'id': 'healthy', 'name': 'healthy'}]
    configs = [_ruleset('failed-first', library_rule_id='failed'),
               _ruleset('healthy', library_rule_id='healthy'),
               _ruleset('failed-second', library_rule_id='failed')]
    unavailable = [True]

    def load(_item, library):
        if library['id'] == 'healthy':
            return 'healthy.example'
        if unavailable[0]:
            raise OSError('temporary source read failure')
        return 'recovered.example'

    loader = Mock(side_effect=load)
    matcher = RuleConfigMatcher(configs, libraries, loader, index_key=_library_key)
    assert matcher.match('absent.test') is None
    assert loader.call_count == 2
    unavailable[0] = False
    # Selecting just the later reference still invalidates the one shared source.
    assert matcher.invalidate_rulesets(lambda item, _: item['id'] == 'failed-second') == 1
    assert matcher.match('recovered.example')['rule_id'] == 'failed-first'
    assert matcher.match('healthy.example')['rule_id'] == 'healthy'
    assert loader.call_count == 3
    assert matcher.invalidate_rulesets(lambda *_: False) == 0


def test_invalidation_without_reuse_only_reloads_selected_item():
    configs = [_ruleset('one'), _ruleset('two')]
    contents = {'one': 'one.example', 'two': 'two.example'}
    loader = Mock(side_effect=lambda item, _: contents[item['id']])
    matcher = RuleConfigMatcher(configs, [], loader)
    assert matcher.match('absent.test') is None
    contents['one'] = 'changed.example'
    assert matcher.invalidate_rulesets(lambda item, _: item['id'] == 'one') == 1
    assert matcher.match('two.example')['rule_id'] == 'two'
    assert matcher.match('changed.example')['rule_id'] == 'one'
    assert matcher.match('one.example') is None
    assert loader.call_count == 3


@pytest.mark.parametrize('separator', ['\n', '\r', '\r\n', '\v', '\f', '\x1c', '\x1d', '\x1e', '\x85', '\u2028', '\u2029'])
@pytest.mark.parametrize('trailing', [False, True])
def test_streamed_lines_preserve_splitlines_and_original_match(separator, trailing):
    content = separator.join(['', '  full:Example.com  ', '# comment', '+.second.example', ''])
    if trailing:
        content += separator
    assert list(rule_matcher._iter_rule_lines(content)) == content.splitlines()
    configs = [_ruleset()]
    matcher = RuleConfigMatcher(configs, [], lambda *_: content)
    for query in ('example.com', 'sub.second.example', 'absent.test'):
        assert matcher.match(query) == _linear_match(configs, {'set': content}, query)


@pytest.mark.parametrize('content', ['', 'one', '\r\r\n', 'one\r\ntwo\n\rthree\vfour\u2028'])
def test_streamed_empty_and_mixed_newlines_match_splitlines(content):
    assert list(rule_matcher._iter_rule_lines(content)) == content.splitlines()


@pytest.mark.parametrize('comment', ['中文', '😀', '\ud800', '\udfff', '\ud800\udc00', '\x00'])
def test_compact_sources_preserve_unicode_and_malformed_comment_text(comment):
    lines = [f'  DOMAIN-SUFFIX,item{i}.example # {comment}  ' for i in range(300)]
    # Storage supports arbitrary original source text independently of whether
    # a provider parser accepts inline comments in a particular file format.
    index = rule_matcher._RuleIndex(
        ('DOMAIN-SUFFIX', f'item{i}.example', line.strip()) for i, line in enumerate(lines))
    for i in (0, 149, 299):
        assert index.match_domain(f'sub.item{i}.example') == lines[i].strip()
    assert isinstance(index._source_lines, rule_matcher._PackedSourceLines)
    assert list(index._source_lines) == [line.strip() for line in lines]


def test_plain_domain_sources_keep_one_shared_string_per_lookup_key():
    lines = [f'plain{i}.example' for i in range(2_000)]
    index = rule_matcher._RuleIndex(('DOMAIN', line, line) for line in lines)
    assert isinstance(index._source_lines, list)
    assert list(index._exact) == lines
    for key, line, original in zip(index._exact, index._source_lines, lines):
        assert key is line is original
    assert index.match_domain('plain1999.example') == lines[-1]


def test_mostly_shared_sources_fall_back_without_copying_shared_strings():
    lines = [f'plain{i}.example' for i in range(500)]
    entries = [('DOMAIN', line, line) for line in lines]
    entries.insert(200, ('DOMAIN-SUFFIX', 'suffix.example', 'DOMAIN-SUFFIX,suffix.example'))
    index = rule_matcher._RuleIndex(entries)
    assert isinstance(index._source_lines, list)
    assert index._source_lines == [line for _, _, line in entries]
    for key, position in index._exact.items():
        assert index._source_lines[position] is key
    assert index.match_domain('sub.suffix.example') == 'DOMAIN-SUFFIX,suffix.example'


def test_mixed_compact_sources_keep_order_shared_keys_patterns_and_first_duplicate():
    lines = ['early.example', 'DOMAIN-SUFFIX,early.example',
             'DOMAIN-REGEX,^regex[0-9]+\\.example$',
             'DOMAIN-KEYWORD,keyword',
             'DOMAIN,keyword.example', 'middle.example']
    lines += [f'DOMAIN-SUFFIX,item{i}.example' for i in range(300)]
    lines += ['DOMAIN-SUFFIX,item0.example', 'last.example']
    content = '\n'.join(lines)
    configs = [_ruleset()]
    matcher = RuleConfigMatcher(configs, [], lambda *_: content)
    for query in ('early.example', 'sub.early.example', 'regex42.example',
                  'keyword.example', 'middle.example', 'item0.example', 'last.example'):
        assert matcher.match(query) == _linear_match(configs, {'set': content}, query)
    index = next(iter(matcher._indexes.values()))
    assert isinstance(index._source_lines, rule_matcher._PackedSourceLines)
    assert list(index._source_lines) == lines[:-2] + lines[-1:]
    for key, position in index._exact.items():
        if key in ('early.example', 'middle.example', 'last.example'):
            assert index._source_lines[position] is key


def test_single_huge_source_and_wide_unicode_can_keep_cheaper_strings():
    huge = 'DOMAIN,huge.example # ' + '😀' * (64 * 1024 + 1)
    index = rule_matcher._RuleIndex([('DOMAIN', 'huge.example', huge)])
    assert isinstance(index._source_lines, list)
    assert index.match_domain('huge.example') is huge

    wide = 'DOMAIN,wide.example # ' + '中' * 10_000
    index = rule_matcher._RuleIndex([('DOMAIN', 'wide.example', wide)])
    assert isinstance(index._source_lines, list)
    assert index.match_domain('wide.example') is wide


@pytest.mark.parametrize('comment', ['a' * 512, '中' * 256])
def test_larger_source_strings_are_shared_across_repeated_matches(comment):
    line = 'DOMAIN-SUFFIX,shared.example # ' + comment
    assert rule_matcher.getsizeof(line) > rule_matcher._MAX_PACKED_LINE_BYTES
    # The wide Unicode source is short in characters but still exceeds the
    # byte budget, so character-count thresholds would miss this case.
    if comment.startswith('中'):
        assert len(line) < rule_matcher._MAX_PACKED_LINE_BYTES
    entries = [('DOMAIN-SUFFIX', f'item{i}.example', f'DOMAIN-SUFFIX,item{i}.example')
               for i in range(300)]
    entries.insert(120, ('DOMAIN-SUFFIX', 'shared.example', line))
    index = rule_matcher._RuleIndex(entries)
    assert isinstance(index._source_lines, rule_matcher._PackedSourceLines)
    assert index.match_domain('one.shared.example') is line
    assert index.match_domain('two.shared.example') is line


def test_compact_mixed_sources_keep_a_huge_line_without_encoding_it():
    huge = 'DOMAIN,huge.example # ' + '\ud800' * (64 * 1024 + 1)
    entries = [('DOMAIN-SUFFIX', f'item{i}.example', f'DOMAIN-SUFFIX,item{i}.example')
               for i in range(300)]
    entries.insert(120, ('DOMAIN', 'huge.example', huge))
    index = rule_matcher._RuleIndex(entries)
    assert isinstance(index._source_lines, rule_matcher._PackedSourceLines)
    assert index.match_domain('huge.example') is huge
    assert list(index._source_lines) == [line for _, _, line in entries]


def test_large_source_offsets_widen_without_truncating_unicode_or_order(monkeypatch):
    # Exercise the exact overflow branch without allocating a 4 GiB fixture.
    monkeypatch.setattr(rule_matcher, '_SOURCE_OFFSET_MAX', 63)
    monkeypatch.setattr(rule_matcher, '_SOURCE_ORDER_MAX', 2)
    lines = [f'DOMAIN,item{i}.example # 中文 😀 \ud800' for i in range(30)]
    builder = rule_matcher._SourceLinesBuilder()
    for line in lines:
        builder.append(line)
    assert builder._offsets.typecode == 'Q'
    assert builder._order.typecode == 'q'
    packed = builder.finish()
    assert isinstance(packed, rule_matcher._PackedSourceLines)
    assert list(packed) == lines
    assert packed[-1] == lines[-1]
    for position in (len(lines), -len(lines) - 1):
        with pytest.raises(IndexError):
            packed[position]


def test_large_shared_reference_positions_widen_without_losing_identity(monkeypatch):
    monkeypatch.setattr(rule_matcher, '_SOURCE_ORDER_MAX', 1)
    builder = rule_matcher._SourceLinesBuilder()
    lines = [f'plain{i}.example' for i in range(5)]
    for line in lines:
        builder.append(line, shared=True)
    assert builder._order.typecode == 'q'
    result = builder.finish()
    assert isinstance(result, list)
    assert all(actual is original for actual, original in zip(result, lines))


def test_empty_or_invalid_only_sources_do_not_retain_empty_packing_buffers():
    for entries in ([], [('REGEX', '[invalid', 'REGEX,[invalid')],
                    [('IP-CIDR', 'bad network', 'IP-CIDR,bad network')]):
        index = rule_matcher._RuleIndex(entries)
        assert index._source_lines == []
        assert isinstance(index._source_lines, list)
        assert index.match_domain('example.com') is None
