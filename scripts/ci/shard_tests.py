"""Plan complete pytest file shards and verify exact partitions of test inventories."""

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import statistics
import subprocess
import sys
from tempfile import TemporaryDirectory


REPO_ROOT = Path(__file__).resolve().parents[2]


def pytest_addoption(parser):
    parser.addoption('--ci-inventory', help='Write collected node IDs for CI shard planning')


def pytest_collection_finish(session):
    output = session.config.getoption('--ci-inventory')
    if output:
        Path(output).write_text(json.dumps([item.nodeid for item in session.items]), encoding='utf-8')


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def collect_nodeids():
    # Separate collection from execution: imported modules and collection hooks
    # must not leak state into the process that runs the selected tests.
    with TemporaryDirectory(prefix='configflow-ci-collection-') as directory:
        output = Path(directory) / 'inventory.json'
        subprocess.run([
            sys.executable, '-m', 'pytest', 'backend', '--collect-only',
            '-p', 'no:terminal', '-p', 'scripts.ci.shard_tests',
            '--ci-inventory', str(output),
        ], cwd=REPO_ROOT, check=True)
        return read_json(output)


def unique_inventory(items, label):
    if not isinstance(items, list) or not items:
        raise ValueError(f'{label} must contain a nonempty list')
    if any(not isinstance(item, str) or not item for item in items):
        raise ValueError(f'{label} must contain nonempty strings')
    duplicates = sorted(item for item, count in Counter(items).items() if count > 1)
    if duplicates:
        raise ValueError(f'{label} contains duplicates: {duplicates[:5]}')
    return set(items)


def validate_partition(expected, shards):
    expected_set = unique_inventory(expected, 'Expected inventory')
    if not shards:
        raise ValueError('At least one shard is required')
    actual = Counter()
    for index, shard in enumerate(shards, 1):
        unique_inventory(shard, f'Shard {index}')
        actual.update(shard)
    overlaps = sorted(item for item, count in actual.items() if count > 1)
    missing = sorted(expected_set - actual.keys())
    extra = sorted(actual.keys() - expected_set)
    if overlaps or missing or extra:
        raise ValueError(f'Invalid partition: overlapping={overlaps[:5]}, missing={missing[:5]}, extra={extra[:5]}')


def plan_shards(nodeids, durations, count):
    unique_inventory(nodeids, 'Pytest inventory')
    if count < 1:
        raise ValueError('Shard count must be positive')
    if not isinstance(durations, dict) or any(
        isinstance(value, bool) or not isinstance(value, (int, float)) or
        not math.isfinite(value) or value < 0 for value in durations.values()
    ):
        raise ValueError('Durations must map file paths to finite nonnegative seconds')
    files = defaultdict(list)
    for nodeid in nodeids:
        file = nodeid.split('::', 1)[0]
        path = Path(file)
        if path.is_absolute() or '..' in path.parts or path.parts[:1] != ('backend',):
            raise ValueError(f'Unexpected pytest path: {file}')
        files[file].append(nodeid)
    if count > len(files):
        raise ValueError('Shard count exceeds the number of collected test files')
    known = [durations[file] for file in files if file in durations and durations[file] > 0]
    fallback = statistics.median(known) if known else 1.0
    weights = {file: max(durations.get(file, fallback), 0.001) for file in files}
    groups = [[] for _ in range(count)]
    totals = [0.0] * count
    # Longest file first; path and shard-index ties are stable across runners.
    for file in sorted(files, key=lambda file: (-weights[file], file)):
        index = min(range(count), key=lambda i: (totals[i], len(groups[i]), i))
        groups[index].append(file)
        totals[index] += weights[file]
    plans = []
    for index, group in enumerate(groups):
        selected = set(group)
        plans.append({
            'all_nodeids': nodeids,
            'selected_nodeids': [nodeid for nodeid in nodeids if nodeid.split('::', 1)[0] in selected],
            'files': sorted(group),
            'shard': index + 1,
            'shards': count,
            'estimated_seconds': round(totals[index], 3),
        })
    validate_partition(nodeids, [plan['selected_nodeids'] for plan in plans])
    return plans


def select_values(data, selector):
    """Read a dotted JSON field, traversing arrays (e.g. testResults.name)."""
    def visit(value, parts):
        if isinstance(value, list):
            return [item for child in value for item in visit(child, parts)]
        if parts:
            if not isinstance(value, dict) or parts[0] not in value:
                raise ValueError(f'Missing JSON selector: {selector}')
            return visit(value[parts[0]], parts[1:])
        if not isinstance(value, str) or not value:
            raise ValueError(f'JSON selector {selector!r} must select nonempty strings')
        return [value]

    return visit(data, selector.split('.') if selector else [])


def normalize_paths(items, prefix):
    """Compare test report paths independently of the runner's checkout root."""
    anchor = Path(prefix).parts
    if not anchor or Path(prefix).is_absolute() or '..' in anchor:
        raise ValueError('Path prefix must be a nonempty relative directory')
    result = []
    for item in items:
        parts = Path(item).parts
        matches = [index for index in range(len(parts) - len(anchor) + 1)
                   if parts[index:index + len(anchor)] == anchor]
        if len(matches) != 1 or '..' in parts:
            raise ValueError(f'Report path does not uniquely match {prefix}: {item}')
        result.append(Path(*parts[matches[0]:]).as_posix())
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    backend = commands.add_parser('backend', help='Collect every backend test and run one file shard')
    backend.add_argument('--shard', required=True, help='One-based index/count, e.g. 1/2')
    backend.add_argument('--durations', required=True)
    backend.add_argument('--plan', required=True)
    backend.add_argument('--plan-only', action='store_true')
    backend.add_argument('pytest_args', nargs=argparse.REMAINDER)
    validate = commands.add_parser('validate', help='Require a disjoint, complete partition')
    validate.add_argument('--expected', required=True)
    validate.add_argument('--expected-key', default='')
    validate.add_argument('--shard-key', default='')
    validate.add_argument('--inventory-key', help='Also require each shard document to contain the same full inventory')
    validate.add_argument('--path-prefix', help='Normalize report paths at this relative directory')
    validate.add_argument('shards', nargs='+')
    args = parser.parse_args(argv)
    try:
        if args.command == 'validate':
            expected = select_values(read_json(args.expected), args.expected_key)
            documents = [read_json(path) for path in args.shards]
            shards = [select_values(document, args.shard_key) for document in documents]
            if args.path_prefix:
                expected = normalize_paths(expected, args.path_prefix)
                shards = [normalize_paths(shard, args.path_prefix) for shard in shards]
            if args.inventory_key:
                for path, document in zip(args.shards, documents):
                    inventory = select_values(document, args.inventory_key)
                    if args.path_prefix:
                        inventory = normalize_paths(inventory, args.path_prefix)
                    try:
                        validate_partition(expected, [inventory])
                    except ValueError as error:
                        raise ValueError(f'Shard inventory differs in {path}: {error}') from error
            validate_partition(expected, shards)
            print(f'Validated {len(expected)} unique entries across {len(shards)} disjoint shards')
            return 0
        parts = args.shard.split('/')
        if len(parts) != 2:
            raise ValueError('--shard must be index/count')
        index, count = map(int, parts)
        if not 1 <= index <= count:
            raise ValueError('--shard index must be between 1 and count')
        plans = plan_shards(collect_nodeids(), read_json(args.durations), count)
        plan = plans[index - 1]
        output = Path(args.plan)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(plan, indent=2) + '\n', encoding='utf-8')
        print(f'Shard {index}/{count}: {len(plan["files"])} files, {len(plan["selected_nodeids"])} tests, '
              f'{plan["estimated_seconds"]:.3f} estimated seconds', flush=True)
        if args.plan_only:
            return 0
        pytest_args = args.pytest_args
        if pytest_args[:1] == ['--']:
            pytest_args = pytest_args[1:]
        return subprocess.run([sys.executable, '-m', 'pytest', *plan['files'], *pytest_args], cwd=REPO_ROOT).returncode
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f'CI shard error: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
