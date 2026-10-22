#!/usr/bin/env sh
# Run the backend suite in parallel (pytest-xdist); extra arguments go to pytest.
# Single tests or debugging: python -m pytest backend/test_x.py -s
set -eu
cd "$(dirname "$0")/.."
PYTHON=${PYTHON:-python}
if [ -x .venv/bin/python ] && [ "$PYTHON" = python ]; then PYTHON=.venv/bin/python; fi
if "$PYTHON" -c 'import xdist' 2>/dev/null; then
    exec "$PYTHON" -m pytest backend -q -n auto --dist=worksteal "$@"
fi
echo 'pytest-xdist 未安装，改为串行运行：pip install pytest-xdist 可大幅提速。' >&2
exec "$PYTHON" -m pytest backend -q "$@"
