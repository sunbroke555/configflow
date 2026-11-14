#!/usr/bin/env python3
"""Generate corpus.jsonl: inputs plus what CPython and Flask make of them.

The Go tests in this package assert byte equality with every recorded
output, so the corpus must come from the interpreter production runs
(CPython 3.11) with the production Flask (3.0):

    ~/cf-test/.venv/bin/python server/internal/jsonx/testdata/gen.py

The output is deterministic (fixed seed). Each line is one JSON object,
written with ensure_ascii=True so the file itself is plain ASCII:

    kind=value   input (raw JSON text) and the outputs of every encoder,
                 repr(), str() and bool() for json.loads(input)
    kind=error   input that json.loads rejects, with str(exception)
    kind=floats  [bits as 16 hex digits, json.dumps(float)] pairs
    kind=equal   raw values and the matrix of Python's == between them
"""

import json
import math
import random
import struct
import sys
from pathlib import Path

import flask

assert sys.version_info[:2] == (3, 11), sys.version
APP = flask.Flask(__name__)
R = random.Random(20261114)

STRINGS = [
    '', 'a', 'id', 'name', 'port', 'z', 'A', '_revision', 'with space', 'k.e/y',
    '例子', '节点 🚀', 'ключ', 'é', 'ǅ', ' nbsp', 'line sep', 'para sep', 'zw​sp',
    'tab\there', 'new\nline', 'cr\rlf', 'back\\slash', 'quote"double', "quote'single", 'both\'"',
    'bell\x07', 'nul\x00', 'esc\x1b', 'us\x1f', 'del\x7f', 'c1\x85', 'bs\x08ff\x0c', '</script>', 'a&b<c>d',
    'http://例子.test/路径?q=1', '𝄞 clef', ' private', '￾ nonchar', '� repl',
    '🇨🇳', 'é combining', '\U0010ffff', 'ÿ', 'Ā', '~', '😀😀',
]
INTS = [0, 1, -1, 2, 7, 80, 443, 65535, -32768, 2 ** 31, 2 ** 53, 2 ** 53 + 1, 2 ** 63 - 1, 2 ** 63, 2 ** 64,
        -(2 ** 63) - 1, 10 ** 22, 10 ** 40 + 7, -(10 ** 30), 123456789012345678901234567890]
FLOATS = [0.0, -0.0, 1.0, -1.0, 1.5, 0.1, 0.2, 0.1 + 0.2, 1 / 3, 2.5e-5, 1e-4, 1e-5, 0.00012345, 1e15, 1e16, 1e17,
          123456789012345680.0, 9999999999999998.0, 1e22, 1e23, 1.7976931348623157e308, 5e-324, 2.2250738585072014e-308,
          3.141592653589793, -2.718281828459045, 100.0, 1e100, 1.5e300, 0.30000000000000004, 4.35, 2.675, 1e21,
          float('inf'), float('-inf'), float('nan')]


def spell_float(value):
    if value != value:
        return 'NaN'
    if value in (float('inf'), float('-inf')):
        return R.choice(['Infinity', '1e999', '1E+400']) if value > 0 else R.choice(['-Infinity', '-1e999'])
    text = R.choice([repr(value), '%.17g' % value, '%.17e' % value, '%.20E' % value,
                     repr(value) if 'e' in repr(value) else repr(value) + '0'])
    if 'e' not in text.lower() and '.' not in text:
        text += '.0'
    if 'e' in text.lower() and R.random() < 0.3:
        text = text.replace('e+', 'e').replace('E+', 'E')
    # json.loads rejects "1.e5" and "inf"; the spellings above never produce them.
    assert float(text) == value, (text, value)
    return text


def spell_string(text):
    out = ['"']
    for char in text:
        code = ord(char)
        roll = R.random()
        if char in '"\\' or code < 0x20:
            short = {'"': '\\"', '\\': '\\\\', '\n': '\\n', '\r': '\\r', '\t': '\\t', '\b': '\\b', '\f': '\\f'}
            out.append(short[char] if char in short and roll < 0.8 else '\\u%04x' % code)
        elif char == '/' and roll < 0.5:
            out.append('\\/')
        elif roll < 0.15:
            if code > 0xffff:
                code -= 0x10000
                out.append('\\u%04x\\u%04X' % (0xd800 + (code >> 10), 0xdc00 + (code & 0x3ff)))
            else:
                out.append(('\\u%04x' if roll < 0.07 else '\\u%04X') % code)
        else:
            out.append(char)
    out.append('"')
    return ''.join(out)


def space():
    return R.choice(['', '', '', ' ', '  ', '\n', '\t', '\r\n', ' \n  '])


def spell(value, depth=0):
    """Raw JSON text for a random value, with random layout and spellings."""
    kind = R.random()
    if depth > 4 or kind < 0.5:
        leaf = R.random()
        if leaf < 0.3:
            return spell_string(R.choice(STRINGS) + (R.choice(STRINGS) if R.random() < 0.3 else ''))
        if leaf < 0.5:
            number = R.choice(INTS) if R.random() < 0.5 else R.randint(-10 ** 6, 10 ** 6)
            return '-0' if number == 0 and R.random() < 0.3 else str(number)
        if leaf < 0.75:
            number = R.choice(FLOATS) if R.random() < 0.6 else struct.unpack('<d', R.randbytes(8))[0]
            if R.random() < 0.3:
                number = round(R.uniform(-1000, 1000), R.randint(0, 6))
            return spell_float(number)
        return R.choice(['true', 'false', 'null', '[]', '{}', '[ ]', '{ }'])
    count = R.choice([0, 1, 1, 2, 3, 3, 5, 8])
    if kind < 0.75:
        items = [space() + spell(None, depth + 1) + space() for _ in range(count)]
        return '[' + ','.join(items) + (space() if not items else '') + ']'
    keys = [R.choice(STRINGS) for _ in range(count)]  # repeats on purpose: duplicate keys
    items = [space() + spell_string(key) + space() + ':' + space() + spell(None, depth + 1) + space() for key in keys]
    return '{' + ','.join(items) + (space() if not items else '') + '}'


def has_lone_surrogate(value):
    try:
        json.dumps(value, ensure_ascii=False).encode('utf-8')
        return False
    except UnicodeEncodeError:
        return True


def value_case(name, raw, only=None):
    value = json.loads(raw)
    with APP.app_context():
        response = flask.jsonify(value).get_data(as_text=True)
    assert response == json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':')) + '\n'
    case = {
        'kind': 'value', 'name': name, 'input': raw, 'lone': has_lone_surrogate(value),
        'compact': json.dumps(value, ensure_ascii=False, separators=(',', ':')),
        'indent2': json.dumps(value, indent=2, ensure_ascii=False),
        'sorted': json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')),
        'response': response,
        'default': json.dumps(value),
        'sorted_spaced': json.dumps(value, sort_keys=True, ensure_ascii=False),
        'indent1': json.dumps(value, indent=1, sort_keys=True, separators=(',', ':')),
        'repr': repr(value), 'str': str(value), 'truthy': bool(value),
    }
    if only:
        case = {key: item for key, item in case.items() if key in ('kind', 'name', 'input', 'lone') or key in only}
    return case


def error_case(raw):
    try:
        json.loads(raw)
    except ValueError as error:  # JSONDecodeError is a ValueError
        return {'kind': 'error', 'input': raw, 'error': str(error), 'type': type(error).__name__}
    return None


HAND_VALUES = [
    '0', '-0', '-0.0', '0.0', '1', '1.0', '1.50', '1E2', '1e2', '1e+2', '1E-2', '0e0', '-0e0', '0.1e-6', '1.0e16',
    '1e15', '1e16', '9999999999999999', '9999999999999999.0', '1e400', '-1e400', '1e-400', '-1e-400',
    '123456789012345678901234567890', '-123456789012345678901234567890', '18446744073709551616',
    '9223372036854775807', '9223372036854775808', '-9223372036854775808', '-9223372036854775809',
    '0.000001', '0.0001', '0.00001', '123456789.123456789123456789', '4.9e-324', '2.5e-324', '2.4e-324',
    '1.7976931348623157e308', '1.7976931348623159e308', '1' + '0' * 4299, '-' + '9' * 4300, '0.' + '0' * 400 + '1',
    'NaN', 'Infinity', '-Infinity', '[NaN,Infinity,-Infinity]', '{"n":NaN}',
    'true', 'false', 'null', '""', '"a"', '[]', '{}', '[[]]', '[{}]', '{"a":{}}', '{"a":[]}', '{"":""}', '{"":{"":[]}}',
    ' \t\r\n[ \t\r\n1 \t\r\n, \t\r\n2 \t\r\n] \t\r\n', '{ "a" : 1 , "b" : [ ] }',
    '{"a":1,"b":2,"a":3}', '{"a":1,"b":2,"a":3,"c":4,"b":5}', '{"a":{"x":1},"a":{"y":2}}', '{"k":1,"k":1.0,"k":true}',
    '{"b":1,"a":2,"C":3,"é":4,"z":5,"例":6,"🚀":7,"\\uffff":8,"":9,"aa":10,"B":11}',
    '"\\u0000\\u0001\\u001f\\u007f\\u0080\\u009f\\u00a0\\u00ff"', '"\\b\\f\\n\\r\\t\\"\\\\\\/"',
    '"\\ud83d\\ude00"', '"\\uD83D\\uDE00"', '"  "', '"\\u2028\\u2029"', '"</script><!--&amp;-->"',
    '"\x7f"', '"\x80\x9f"', '"/"', '"\\u00e9\\u4f8b"', '"\U0010ffff\\udbff\\udfff"', '"\\u0041\\u0062"',
    '["\\u20ac", "€", "\\ud834\\udd1e", "𝄞"]', '[1,2.0,"3",true,null,[4,[5,[6]]],{"7":8}]',
    '{"schema_version":5,"system":{"system_config":{"server_domain":"https://例子.test","z":1,"a":2},"backup":{},'
    '"agents":[],"_revision":3},"shared":{"nodes":[{"id":"n","name":"节点 🚀","port":1.5}],"_revision":7},'
    '"profiles":{"zeta":{"id":"zeta","extra":{"b":[1,{"c":null}],"a":true}},"default":{"name":"Default"}},'
    '"future_root_key":{"kept":true}}',
]
LONE_VALUES = ['"\\ud800"', '"\\udc00"', '"a\\ud83dz"', '"\\ud83d\\u0041"', '"\\ude00\\ud83d"', '["\\udfff",1]',
               '{"\\ud800":1}', '"\\ud83d\\ud83d\\ude00"']
HAND_ERRORS = [
    '', ' ', '\n', '﻿{}', '{', '[', '}', ']', '{"a"', '{"a":', '{"a":1', '{"a":1,', '{"a":1,}', '[1,]', '[1,,2]',
    '[,1]', '{,}', '{"a" 1}', '{"a":1 "b":2}', '[1 2]', '{a:1}', "{'a':1}", "'a'", '{"a":1}}', '[] []', '1 2', '01',
    '-01', '1.', '.5', '-.5', '1.e5', '1e', '1e+', '1E-', '-', '--1', '+1', '0x10', '1_000', 'nan', 'inf', '-inf',
    'Nan', 'NaNa', 'Infinit', '-Infinit', 'infinity', 'tru', 'True', 'nul', 'None', 'falsey', 'truefalse',
    '"', '"abc', '"abc\\', '"\\', '"\\x41"', '"\\a"', '"\\u"', '"\\u12"', '"\\u123"', '"\\u1234', '"\\u12g4"',
    '"\\uzzzz"', '"\\ud83d\\u12"', '"\\ud83d\\uzzzz"', '"\\ud83d\\ude0"', '"\\ud83d\\ude00', '"\\u12é4"',
    '"tab\tinside"', '"new\nline"', '"nul\x00"', '"\x1f"', '{"a\nb":1}', '["例子", "\x01"]', '\n\n  {"例":\n "值" "x"}',
    '[1,\n2,\n 3 4]', '{"é😀": tru}', '["😀", ]', '[1] x', '{"a":[1,2,{"b":}]}', '\x00', '\x0b1', '\xa01', '　1',
    '[1\x0c]', '１', '[１]', '1.５', '"\\u１234"', '{"a":1,"a"}', '[[[[[[', ']]]]', '{"a":{"b":{"c":',
    '1' * 4301, '-' + '1' * 4301, '[' + '2' * 5000 + ']',
]


def mutate(raw):
    position = R.randrange(len(raw) + 1)
    piece = R.choice(['{', '}', '[', ']', ',', ':', '"', '\\', ' ', '\n', '\t', 'u', '0', 'e', 'E', '.', '-', '+',
                      'N', 'a', 'I', 'n', 't', 'f', "'", 'é', '😀', '\x01', '\\u', '\\ud83d', '1', 'null', '/'])
    action = R.random()
    if action < 0.3:
        return raw[:position]
    if action < 0.55:
        return raw[:position] + raw[position + 1:]
    if action < 0.8:
        return raw[:position] + piece + raw[position:]
    return raw[:position] + piece + raw[position + 1:]


def main():
    cases = []
    for index, raw in enumerate(HAND_VALUES):
        cases.append(value_case(f'hand-{index}', raw))
    for index, raw in enumerate(LONE_VALUES):
        cases.append(value_case(f'lone-{index}', raw))
    deep = 400
    cases.append(value_case('deep-list', '[' * deep + ']' * deep, only=('compact', 'default', 'truthy')))
    cases.append(value_case('deep-object', '{"k":' * deep + '1' + '}' * deep, only=('compact', 'sorted', 'truthy')))
    cases.append(value_case('nested-indent', '[' * 30 + '{"a":[1,{"b":{}}]}' + ']' * 30))
    randoms = []
    while len(randoms) < 420:
        raw = space() + spell(None) + space()
        if len(raw) <= 700:
            randoms.append(raw)
    for index, raw in enumerate(randoms):
        cases.append(value_case(f'random-{index}', raw))

    errors = dict.fromkeys(HAND_ERRORS)
    bases = [raw for raw in HAND_VALUES if len(raw) < 300] + randoms[:150]
    extra_values = {}
    for _ in range(4000):
        if len(errors) >= 700:
            break
        raw = mutate(R.choice(bases))
        if R.random() < 0.2:
            raw = mutate(raw)
        try:
            raw.encode('utf-8')
        except UnicodeEncodeError:
            continue
        if error_case(raw) is not None:
            errors.setdefault(raw)
        elif len(extra_values) < 120:
            extra_values.setdefault(raw)
    for index, raw in enumerate(extra_values):
        cases.append(value_case(f'mutated-{index}', raw))
    for raw in errors:
        case = error_case(raw)
        assert case is not None, raw
        cases.append(case)

    floats = list(FLOATS)
    for exponent in range(-25, 26):
        floats += [10.0 ** exponent, float(f'1e{exponent}'), float(f'9.5e{exponent}'), float(f'1.2345678e{exponent}'),
                   float(f'-9.999999999999999e{exponent}'), 2.0 ** exponent, 2.0 ** (exponent * 20)]
    floats += [float(value) for value in INTS]
    floats += [R.uniform(-1, 1) for _ in range(300)] + [R.uniform(-1e17, 1e17) for _ in range(300)]
    floats += [round(R.uniform(0, 100000), R.randint(0, 5)) for _ in range(300)]
    while len(floats) < 3000:
        value = struct.unpack('<d', R.randbytes(8))[0]
        if not math.isnan(value):
            floats.append(value)
    cases.append({'kind': 'floats', 'items': [[struct.pack('>d', value).hex(), json.dumps(value)] for value in floats]})

    equal = ['1', '1.0', '1.5', '0', '0.0', '-0.0', '-1', 'true', 'false', 'null', '""', '"1"', '"a"', '"true"', '[]',
             '{}', '[1]', '[1.0]', '[true]', '[1,2]', '[2,1]', '[[]]', '[{}]', '[null]', '{"a":1,"b":2}',
             '{"b":2.0,"a":true}', '{"a":1}', '{"a":null}', '{"a":[]}', '{"b":1}', '9007199254740993',
             '9007199254740992.0', '9007199254740992', '1e400', 'Infinity', '-Infinity', 'NaN', '[NaN]',
             '100000000000000000000', '1e20', '100000000000000000001', '2', '2.0000000000000004',
             '123456789012345678901234567890', '1.2345678901234568e+29', '{"a":{"x":[1,{"y":0}]}}',
             '{"a":{"x":[1.0,{"y":false}]}}']
    left = [json.loads(raw) for raw in equal]
    right = [json.loads(raw) for raw in equal]  # separate objects: no identity short-cut
    cases.append({'kind': 'equal', 'values': equal,
                  'matrix': [''.join('1' if a == b else '0' for b in right) for a in left]})

    target = Path(__file__).with_name('corpus.jsonl')
    with open(target, 'w', encoding='ascii', newline='\n') as handle:
        for case in cases:
            handle.write(json.dumps(case, ensure_ascii=True, separators=(',', ':')) + '\n')
    counts = {}
    for case in cases:
        counts[case['kind']] = counts.get(case['kind'], 0) + 1
    print(f'{target.name}: {counts}, {target.stat().st_size} bytes, python {sys.version.split()[0]}, '
          f'flask {__import__("importlib.metadata").metadata.version("flask")}')


if __name__ == '__main__':
    main()
