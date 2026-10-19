from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
from tempfile import TemporaryDirectory

from scripts.ci.shard_tests import REPO_ROOT, main, normalize_paths, plan_shards, select_values, validate_partition


class ShardTests(unittest.TestCase):
    def setUp(self):
        self.nodeids = [
            'backend/test_slow.py::test_one[a]',
            'backend/test_slow.py::test_one[b]',
            'backend/test_middle.py::TestCase::test_two',
            'backend/test_fast.py::test_three',
            'backend/test_new.py::test_new',
        ]
        self.durations = {'backend/test_slow.py': 10, 'backend/test_middle.py': 6, 'backend/test_fast.py': 2}

    def test_complete_deterministic_file_shards_include_new_parameterized_tests(self):
        plans = plan_shards(self.nodeids, self.durations, 2)
        validate_partition(self.nodeids, [plan['selected_nodeids'] for plan in plans])
        reversed_plans = plan_shards(list(reversed(self.nodeids)), dict(reversed(list(self.durations.items()))), 2)
        self.assertEqual([plan['files'] for plan in plans], [plan['files'] for plan in reversed_plans])
        self.assertEqual([plan['estimated_seconds'] for plan in plans], [12, 12])
        slow = [plan for plan in plans if 'backend/test_slow.py' in plan['files']][0]
        self.assertIn(self.nodeids[0], slow['selected_nodeids'])
        self.assertIn(self.nodeids[1], slow['selected_nodeids'])
        self.assertEqual(plans, plan_shards(self.nodeids, {**self.durations, 'backend/deleted.py': 999}, 2))

    def test_empty_and_zero_duration_caches_include_every_file(self):
        for durations in ({}, {file.split('::')[0]: 0 for file in self.nodeids}):
            plans = plan_shards(self.nodeids, durations, 2)
            validate_partition(self.nodeids, [plan['selected_nodeids'] for plan in plans])

    def test_invalid_plans_are_rejected(self):
        for durations in ([], {'test': -1}, {'test': float('nan')}, {'test': float('inf')}, {'test': True}):
            with self.subTest(durations=durations), self.assertRaises(ValueError):
                plan_shards(self.nodeids, durations, 2)
        for nodeids, count in (([], 2), (self.nodeids, 0), (self.nodeids, 8), (self.nodeids * 2, 2), (['../secret::test'], 1)):
            with self.subTest(nodeids=nodeids, count=count), self.assertRaises(ValueError):
                plan_shards(nodeids, {}, count)

    def test_partition_rejects_missing_extra_overlap_and_duplicates(self):
        for expected, shards in (
            (['a', 'b'], [['a']]),
            (['a'], [['a'], ['extra']]),
            (['a', 'b'], [['a', 'b'], ['b']]),
            (['a'], [['a', 'a']]),
            (['a', 'a'], [['a']]),
            (['a'], [['a'], []]),
            ([], []),
        ):
            with self.subTest(expected=expected, shards=shards), self.assertRaises(ValueError):
                validate_partition(expected, shards)

    def test_vitest_json_selectors_and_validation_cli(self):
        expected = [{'file': '/tests/a.test.ts'}, {'file': '/tests/b.test.ts'}]
        reports = [{'testResults': [{'name': item['file']}]} for item in expected]
        self.assertEqual(select_values(expected, 'file'), ['/tests/a.test.ts', '/tests/b.test.ts'])
        with TemporaryDirectory() as directory:
            paths = [Path(directory) / name for name in ('all.json', '1.json', '2.json')]
            for path, content in zip(paths, [expected, *reports]):
                path.write_text(json.dumps(content))
            self.assertEqual(main(['validate', '--expected', str(paths[0]), '--expected-key', 'file',
                                   '--shard-key', 'testResults.name', *map(str, paths[1:])]), 0)
        for data, selector in (({'wrong': []}, 'testResults.name'), ([{'file': None}], 'file')):
            with self.assertRaises(ValueError):
                select_values(data, selector)

    def test_backend_plan_runs_real_pytest_in_separate_process_with_original_flags(self):
        with TemporaryDirectory() as directory:
            durations = Path(directory) / 'durations.json'
            output = Path(directory) / 'plan.json'
            durations.write_text(json.dumps(self.durations))
            with patch('scripts.ci.shard_tests.collect_nodeids', return_value=self.nodeids), \
                 patch('scripts.ci.shard_tests.subprocess.run') as run:
                run.return_value.returncode = 3
                self.assertEqual(main(['backend', '--shard', '1/2', '--durations', str(durations),
                                       '--plan', str(output), '--', '-q', '-n', '4', '--dist=worksteal']), 3)
                plan = json.loads(output.read_text())
                run.assert_called_once_with([sys.executable, '-m', 'pytest', *plan['files'],
                                             '-q', '-n', '4', '--dist=worksteal'], cwd=REPO_ROOT)

    def test_validation_rejects_different_full_inventories_even_when_selected_union_matches(self):
        with TemporaryDirectory() as directory:
            paths = [Path(directory) / f'{index}.json' for index in (1, 2)]
            for path, selected in zip(paths, ('a', 'b')):
                path.write_text(json.dumps({'all_nodeids': ['a', 'b'], 'selected_nodeids': [selected]}))
            args = ['validate', '--expected', str(paths[0]), '--expected-key', 'all_nodeids',
                    '--shard-key', 'selected_nodeids', '--inventory-key', 'all_nodeids', *map(str, paths)]
            self.assertEqual(main(args), 0)
            paths[1].write_text(json.dumps({'all_nodeids': ['a', 'b', 'new'], 'selected_nodeids': ['b']}))
            errors = io.StringIO()
            with redirect_stderr(errors):
                self.assertEqual(main(args), 1)
            self.assertIn('Shard inventory differs', errors.getvalue())
            self.assertIn("extra=['new']", errors.getvalue())

    def test_report_paths_normalize_across_checkout_roots_and_reject_unrelated_paths(self):
        expected = normalize_paths(['/local/configflow/frontend/tests/a.test.ts',
                                    '/local/configflow/frontend/tests/b.test.ts'], 'frontend/tests')
        shards = [normalize_paths([f'/runner/{index}/frontend/tests/{name}.test.ts'], 'frontend/tests')
                  for index, name in enumerate(('a', 'b'))]
        validate_partition(expected, shards)
        for path in ('/runner/tests/a.test.ts', '/runner/frontend/tests/../secret',
                     '/frontend/tests/copy/frontend/tests/a.test.ts'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                normalize_paths([path], 'frontend/tests')

    def test_pytest_inventory_plugin_collects_parameterized_nodeids(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            test = root / 'test_inventory.py'
            output = root / 'inventory.json'
            test.write_text('import pytest\n@pytest.mark.parametrize("value", [1, 2])\ndef test_values(value):\n    raise AssertionError("collection must not execute tests")\n')
            result = subprocess.run([sys.executable, '-m', 'pytest', str(test), '--collect-only',
                                     '-p', 'no:terminal', '-p', 'scripts.ci.shard_tests',
                                     '--ci-inventory', str(output)], cwd=REPO_ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual([nodeid.split('::')[-1] for nodeid in json.loads(output.read_text())],
                             ['test_values[1]', 'test_values[2]'])


if __name__ == '__main__':
    unittest.main()
