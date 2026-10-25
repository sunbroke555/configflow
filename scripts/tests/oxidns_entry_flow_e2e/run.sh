#!/usr/bin/env bash
# Invoke from a task worktree via rt; every process and database is disposable.
set -euo pipefail
repo="$(pwd)"
qa_runtime="${E2E_RUNTIME:-/tmp/configflow-entry-flow-e2e-runtime}"
qa_hash="$(sha256sum frontend/package-lock.json | cut -c1-16)"
qa_cache="$HOME/cf-test/node_modules/$qa_hash/node_modules"
if [ ! -d "$qa_cache" ]; then
  echo 'Run cf-test typecheck once to prepare the shared lockfile dependency cache.' >&2
  exit 2
fi
# rt can copy the local editor-only symlink; restore the Linux cache link.
ln -sfn "$qa_cache" frontend/node_modules
mkdir -p "$qa_runtime"
if [ ! -f "$qa_runtime/node_modules/playwright/index.mjs" ]; then
  npm install --prefix "$qa_runtime" --no-save playwright@1.64.1
fi
node "$qa_runtime/node_modules/playwright/cli.js" install chromium
export PLAYWRIGHT_RUNTIME="$qa_runtime"
exec "$HOME/cf-test/.venv/bin/python" "$repo/scripts/tests/oxidns_entry_flow_e2e/run.py" --cache "$qa_runtime" "$@"
