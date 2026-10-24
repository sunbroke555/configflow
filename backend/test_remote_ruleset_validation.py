"""Remote responses are checked before caching, never repaired or transcoded."""
import base64
from copy import deepcopy
import struct

import pytest
import zstandard

from backend.utils.ruleset_content import RemoteRuleValidationError, validate_remote_rule_content


def rule(behavior='domain', rule_format='text'):
    return {'name': '测试订阅', 'behavior': behavior, 'format': rule_format, 'source_type': 'url'}


@pytest.mark.parametrize(('behavior', 'lines'), [
    ('domain', ['example.com', '+.example.com', '.example.com', '*.example.com',
                '*.*.microsoft.com', 'xbox.*.example.com', '*', '+.*', '.*', '_srv.example.com']),
    ('ipcidr', ['192.0.2.0/24', '192.0.2.3/32', '2001:db8::/32', '2001:db8::1/128']),
    ('classical', ['DOMAIN,example.com', 'DOMAIN-SUFFIX,example.com', 'NETWORK,tcp',
                   'DOMAIN-REGEX,^(foo|bar)\\.example\\.com$', 'PROCESS-NAME,app.exe',
                   'AND,((DOMAIN,example.com),(NETWORK,tcp))',
                   'IP-CIDR,192.0.2.0/24,no-resolve', 'IP-CIDR6,2001:db8::/32,no-resolve',
                   '+.example.com', 'example.com', '192.0.2.3', '2001:db8::1', '192.0.2.0/24']),
])
@pytest.mark.parametrize('rule_format', ['text', 'yaml'])
@pytest.mark.parametrize('as_bytes', [False, True])
def test_valid_sources_preserve_content_and_metadata(behavior, lines, rule_format, as_bytes):
    import yaml
    source = ('# source\r\n\r\n' + '\r\n'.join(lines) + '\r\n' if rule_format == 'text'
              else yaml.safe_dump({'payload': lines}, sort_keys=False))
    source = '\ufeff' + source
    content = source.encode() if as_bytes else source
    item = rule(behavior, rule_format)
    original = deepcopy(item)
    assert validate_remote_rule_content(content, item) is None
    assert item == original and content == (source.encode() if as_bytes else source)


@pytest.mark.parametrize(('behavior', 'invalid', 'reason'), [
    ('domain', 'DOMAIN-SUFFIX,example.com', 'Classical'),
    ('domain', '192.0.2.0/24', '域名'),
    ('domain', 'full:example.com', '域名'),
    ('domain', 'foo..example.com', '域名'),
    ('domain', '*suffix.example.com', '域名'),
    ('ipcidr', '192.0.2.1', '/32'),
    ('ipcidr', '2001:db8::1', '/128'),
    ('ipcidr', 'IP-CIDR,192.0.2.0/24', '网段'),
    ('ipcidr', '+.example.com', 'CIDR'),
    ('ipcidr', '192.0.2.0/33', '前缀'),
    ('classical', '.example.com', '语义'),
    ('classical', '*.example.com', '语义'),
    ('classical', 'full:example.com', '语义'),
    ('classical', 'DOMAIN-SHUFFIX,example.com', 'DOMAIN-SUFFIX'),
    ('classical', 'DOMAIN,', '为空'),
    ('classical', 'IP-CIDR,192.0.2.0/33', '前缀'),
    ('classical', 'RULE-SET,nested', '不支持'),
    ('classical', 'MATCH', '不支持'),
])
@pytest.mark.parametrize('rule_format', ['text', 'yaml'])
def test_type_errors_expose_original_line_and_reason(behavior, invalid, reason, rule_format):
    import json
    source = '# source\n' + (invalid if rule_format == 'text' else 'payload:\n  - ' + json.dumps(invalid))
    expected_line = 2 if rule_format == 'text' else 3
    with pytest.raises(RemoteRuleValidationError) as captured:
        validate_remote_rule_content(source.encode(), rule(behavior, rule_format))
    error = captured.value
    assert error.errors[0]['line'] == expected_line
    assert error.errors[0]['before'] == invalid
    assert reason in error.errors[0]['message']
    assert '测试订阅' in str(error) and f'{behavior}/{rule_format}' in str(error)
    assert f'第 {expected_line} 行' in str(error) and invalid in str(error)


def test_reports_every_invalid_entry_without_accepting_partial_set():
    source = 'valid.test\nDOMAIN-SUFFIX,mixed.test\n192.0.2.0/24\nlast.test\n'
    with pytest.raises(RemoteRuleValidationError) as captured:
        validate_remote_rule_content(source, rule())
    assert [item['line'] for item in captured.value.errors] == [2, 3]
    assert '共 2 处错误' in str(captured.value)


@pytest.mark.parametrize(('source', 'rule_format', 'line', 'reason'), [
    ('payload:\n  - example.com\n', 'text', 1, 'YAML'),
    ('example.com\n', 'yaml', 1, 'payload'),
    ('payload:\n  - true\n', 'yaml', 2, '单行文本'),
    ('payload:\n  - 123\n', 'yaml', 2, '单行文本'),
    ('payload:\n  - [example.com]\n', 'yaml', 2, '单行文本'),
    ('payload:\n  - |\n    example.com\n', 'yaml', 2, '单行文本'),
    ('payload: ["example.com\\nother.com"]\n', 'yaml', 1, '单行文本'),
    ('payload: []\npayload: []\n', 'yaml', 2, '唯一'),
    ('payload:\n  - &x example.com\n  - *x\n', 'yaml', 2, '锚点'),
    ('payload: example.com\n', 'yaml', 1, '列表'),
    ('payload: [example.com\n', 'yaml', 2, 'YAML 格式无效'),
])
def test_format_errors_have_physical_source_locations(source, rule_format, line, reason):
    with pytest.raises(RemoteRuleValidationError) as captured:
        validate_remote_rule_content(source, rule(rule_format=rule_format))
    first = captured.value.errors[0]
    assert first['line'] == line and reason in first['message']


@pytest.mark.parametrize('content', [b'example.com\n\xff', b'\xff', b'example.com\r\xff', 'example.com\u2028'.encode() + b'\xff'])
def test_binary_content_is_not_lossily_decoded(content):
    with pytest.raises(RemoteRuleValidationError, match='UTF-8') as captured:
        validate_remote_rule_content(content, rule())
    assert captured.value.errors[0]['line'] == (1 if content == b'\xff' else 2)


def test_legacy_format_inference_and_library_metadata_match_delivery():
    item = {'name': '订阅', 'behavior': 'domain', 'url': 'https://rules.test/list.yaml'}
    assert validate_remote_rule_content('payload: [example.com]', item) is None
    item['url'] = 'https://rules.test/list.txt'
    config = {'rule_library': [{'name': '订阅', 'format': 'yaml'}]}
    assert validate_remote_rule_content('payload: [example.com]', item, config) is None
    with pytest.raises(RemoteRuleValidationError, match='YAML'):
        validate_remote_rule_content('payload: [example.com]', item)


@pytest.mark.parametrize(('field', 'value'), [('behavior', 'domain-shuffix'), ('format', 'json')])
def test_invalid_declared_metadata_is_not_silently_inferred(field, value):
    with pytest.raises(RemoteRuleValidationError):
        validate_remote_rule_content('example.com', {**rule(), field: value})


# Generated by Mihomo v1.19.32 convert-ruleset from +.example.com / 192.0.2.0/24.
NATIVE_MRS = {
    'domain': 'KLUv/QQATQEAlAFNUlMBACgGqqqqAA1tb2MuZWxwbWF4ZS4rBQBgoJ3naGbBMgGu0wItMyBa',
    'ipcidr': 'KLUv/QQALQEAckEFDOBBAT/YP2OX/K7e981Rn6NeRAQQBQBDEQYkWAqsEMDginLvahA=',
}


@pytest.mark.parametrize('behavior', ['domain', 'ipcidr'])
def test_native_mrs_type_matches_and_binary_bytes_are_unchanged(behavior):
    content = base64.b64decode(NATIVE_MRS[behavior])
    assert validate_remote_rule_content(content, rule(behavior, 'mrs')) is None
    other = 'domain' if behavior == 'ipcidr' else 'ipcidr'
    with pytest.raises(RemoteRuleValidationError, match=f'实际类型为 {behavior}'):
        validate_remote_rule_content(content, rule(other, 'mrs'))
    with pytest.raises(RemoteRuleValidationError, match='不支持 classical'):
        validate_remote_rule_content(content, rule('classical', 'mrs'))
    assert base64.b64encode(content).decode() == NATIVE_MRS[behavior]


def mrs_envelope(*, magic=b'MRS\x01', behavior=0, count=1, extra_length=0, body=b'body'):
    return zstandard.ZstdCompressor(write_checksum=True).compress(
        magic + bytes([behavior]) + struct.pack('>qq', count, extra_length) + body)


@pytest.mark.parametrize(('content', 'reason'), [
    ('text', '原始二进制'),
    (b'example.com', '压缩流'),
    (b'', '不完整'),
    (mrs_envelope()[:-1], '不完整'),
    (mrs_envelope() + b'bad', '压缩流'),
    (mrs_envelope(magic=b'BAD!'), '文件头'),
    (mrs_envelope(behavior=2), '未知规则类型'),
    (mrs_envelope(count=-1), '数量'),
    (mrs_envelope(extra_length=-1), '扩展字段'),
    (mrs_envelope(extra_length=1000), '扩展字段'),
    (mrs_envelope(body=b''), '正文缺失'),
])
def test_mrs_envelope_or_compression_failure_rejects_refresh(content, reason):
    with pytest.raises(RemoteRuleValidationError, match=reason):
        validate_remote_rule_content(content, rule('domain', 'mrs'))


def test_mrs_checksum_corruption_is_rejected():
    content = bytearray(base64.b64decode(NATIVE_MRS['domain']))
    content[-1] ^= 1
    with pytest.raises(RemoteRuleValidationError, match='校验失败'):
        validate_remote_rule_content(bytes(content), rule('domain', 'mrs'))


def test_remote_error_supports_source_resolution_message():
    error = RemoteRuleValidationError('来源配置有歧义')
    assert str(error) == '来源配置有歧义'
    assert error.errors == [{'line': 0, 'before': '', 'message': '来源配置有歧义'}]


def test_text_error_keeps_original_indentation_for_diagnostics():
    with pytest.raises(RemoteRuleValidationError) as captured:
        validate_remote_rule_content('valid.example\n  DOMAIN,wrong.example  \n', rule())
    assert captured.value.errors[0]['before'] == '  DOMAIN,wrong.example  '
