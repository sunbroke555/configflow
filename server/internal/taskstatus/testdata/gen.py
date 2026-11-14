#!/usr/bin/env python3
"""Generate corpus.jsonl for task_status.parse_time: strings and the instant (microseconds since the
epoch, UTC) or null where Python rejects them. Run on the test server from the repository root:

    PYTHONPATH=. ~/cf-test/.venv/bin/python server/internal/taskstatus/testdata/gen.py
"""
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

assert sys.version_info[:2] == (3, 11)
from backend.utils.task_status import parse_time  # noqa: E402

R = random.Random(20261114)
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
DATES = ['2026-11-14', '20261114', '2026-W46-5', '2026W465', '2026-W46', '2026W46', '2024-02-29', '2023-02-29', '2026-13-01', '2026-00-10',
         '2026-11-31', '0000-01-01', '9999-12-31', '2026-1-1', '2026/11/14', '2026-11-1', '2026-W53-1', '2020-W53-7', '2026-W00-1', '2026-W46-0',
         '2026-W46-8', '2026-11_14', '2026-1114', '202611-14', '2026W46-5', '2026-W465', 'abcd-ef-gh', '２０２６-11-14', '2026-11-14x']
SEPS = ['T', ' ', 't', '_', '', '-', 'é', ' ']
TIMES = ['', '12', '12:34', '12:34:56', '12:34:56.789', '12:34:56.789012', '12:34:56.7890123', '12:34:56,5', '123456', '1234', '123456.5', '12:3456',
         '1234:56', '24:00:00', '12:60:00', '12:00:60', '12:34:56.', '12:34:56.1a', '12:34:56.1234567x', '1', '12:', '12:34:', 'ab:cd', '12:34:56:78',
         '23:59:59.999999', '00:00:00', '12.5']
ZONES = ['', 'Z', '+00:00', '-00:00', '+08:00', '-05:30', '+0530', '+05', '+5', '+05:30:15', '+05:30:15.5', '+24:00', '+23:59', '+99', 'z', '+', '-', '+0',
         '+05:3', '+05:30:1', '+123', '+12345', '+05:30.5', '+1:30', '-12:00', '+14:00', ' +08:00', '+08:00 ']
FIXED = ['', ' ', 'Z', '2026-11-14T12:34:56Z', '2026-11-14T12:34:56+00:00', '2026-11-14T12:34:56.123456Z', '2026-11-14 12:34:56Z',
         '2026-11-14T08:00:00+08:00', 'garbage', 'yesterday', '2026-11-14T12:34:56ZZ', 'Z2026-11-14', '2026-11-14T12:34:56Z\n']


def outcome(text):
    value = parse_time(text)
    if value is None:
        return None
    return (value - EPOCH) // timedelta(microseconds=1)


def main():
    cases = list(FIXED)
    for date in DATES:
        for sep in SEPS:
            for time in R.sample(TIMES, 4):
                cases.append(date + sep + time + R.choice(ZONES))
        cases.append(date)
    for _ in range(900):
        cases.append(R.choice(DATES[:6]) + R.choice(SEPS[:3]) + R.choice(TIMES) + R.choice(ZONES))
    seen, lines = set(), []
    for text in cases:
        if text in seen:
            continue
        seen.add(text)
        lines.append({'in': text, 'us': outcome(text)})
    with open(Path(__file__).with_name('corpus.jsonl'), 'w', encoding='ascii', newline='\n') as handle:
        for line in lines:
            handle.write(json.dumps(line, ensure_ascii=True, separators=(',', ':')) + '\n')
    print(len(lines), sum(1 for line in lines if line['us'] is not None))


main()
