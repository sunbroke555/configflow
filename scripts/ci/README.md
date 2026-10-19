# CI test shards

Regression checks run two backend shards and two frontend shards concurrently.
Frontend type checking and the production build have their own concurrent job.
The existing required checks, `backend` and `frontend`, succeed only after all
their jobs pass and the inventories form an exact partition. No tests are
selected by changed paths. The normal full-suite commands remain available:
`python -m pytest backend -q -n 4 --dist=worksteal` and `cd frontend && npm test`.
Reports are retained for seven days. A new commit cancels obsolete runs for the
same pull request; manual and reusable-workflow runs are not canceled.

The backend planner collects **all** tests with pytest in a separate process,
groups collected node IDs by file, and assigns the longest files to the least
loaded shard. The committed duration JSON maps repository-relative file paths
(for example, `backend/test_api.py`) to seconds. New files use the median known
file duration; deleted files are ignored. Timing data affects scheduling only:
new tests are always collected and included. Equal weights use stable path and
shard-index ordering.

The initial timing snapshot comes from the full suite before this optimization.
It records summed per-test elapsed time, not a prediction of wall time with four
workers. Refresh it after major changes to the test distribution by running:

```sh
python -m pytest backend -q -n 4 --dist=worksteal --junitxml=/tmp/backend-timings.xml
python - <<'PY'
import json
from collections import defaultdict
from pathlib import Path
from xml.etree import ElementTree as ET

durations = defaultdict(float)
for case in ET.parse('/tmp/backend-timings.xml').iter('testcase'):
    parts = case.attrib['classname'].split('.')
    for length in range(len(parts), 0, -1):
        path = Path(*parts[:length]).with_suffix('.py')
        if path.is_file():
            durations[path.as_posix()] += float(case.attrib.get('time', 0))
            break
    else:
        raise ValueError(f'Cannot map test to a file: {case.attrib}')
Path('.github/ci/backend-durations.json').write_text(
    json.dumps({path: round(seconds, 3) for path, seconds in sorted(durations.items())}, indent=2) + '\n'
)
PY
```

From the repository root:

```sh
python scripts/ci/shard_tests.py backend --shard 1/2 \
  --durations .github/ci/backend-durations.json --plan .ci-results/backend-1.json \
  -- -q -n 4 --dist=worksteal --durations=15 --junitxml=.ci-results/backend-1.xml
```

Repeat with `2/2` and separate output names. `--plan-only` writes the plan without
running tests. Actual execution uses a fresh pytest process with the selected
file paths and forwards the options following `--`, including the original four
xdist workers. Do not pass test filters, exclusions, or additional test paths.

Check that both plans contain every collected test exactly once, including
parameterized cases:

```sh
python scripts/ci/shard_tests.py validate \
  --expected .ci-results/backend-1.json --expected-key all_nodeids \
  --shard-key selected_nodeids --inventory-key all_nodeids \
  .ci-results/backend-1.json .ci-results/backend-2.json
```

For Vitest, collect the unsplit inventory using `vitest list --filesOnly --json`,
then validate it against the JSON reports from the **actual shard runs**.
Vitest 3.2.6's list command ignores `--shard`, so it cannot prove a partition.

```sh
python scripts/ci/shard_tests.py validate \
  --expected .ci-results/frontend-all.json --expected-key file \
  --shard-key testResults.name --path-prefix frontend/tests \
  .ci-results/frontend-1.json .ci-results/frontend-2.json
```

The validator accepts any JSON string inventory; dotted selectors traverse
arrays. It rejects empty inventories, duplicate entries, overlapping shards,
missing tests, and unexpected tests. The optional path prefix removes differing
checkout roots while requiring each report path to belong to that directory.
`--inventory-key` additionally verifies that every shard collected the same full
inventory, so differing collections cannot hide tests assigned to another shard.
The validator uses only Python's standard library.

Run helper regressions with the backend's pytest dependency installed:

```sh
python -m unittest discover -s scripts/ci -p 'test_*.py'
```
