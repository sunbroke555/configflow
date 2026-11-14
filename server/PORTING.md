# Porting the ConfigFlow backend from Python to Go

This directory holds the Go rewrite of `backend/` (Flask). The Vue frontend, the Go Agent
(`backend/agents/go-agent`) and the on-disk data format stay as they are. The Python code remains in
the tree as the reference until the Go server has full parity; it is never run next to the Go server.

**Feature parity is a hard requirement.** Nothing is dropped silently. If something looks dead or
not worth porting, list it in your hand-back under "Not ported" with evidence; do not decide alone.

## Layout

```
server/
  go.mod                      module configflow (Go 1.26)
  cmd/configflow/             main: flags/env, wiring, HTTP listener, background loops
  internal/jsonx/             ordered JSON values with Python-compatible encoding (see below)
  internal/store/             SQLite document store (port of common/config_store.py)
  internal/repo/              ProfileRepository (port of common/config_repository.py, common/config.py)
  internal/httpx/             router, request context, auth hooks, JSON responses, redaction
  internal/<area>/            one package per work package (rules, subs, mihomo, oxidns, agents, routers, ...)
  internal/api/<blueprint>/   HTTP handlers, one package per Flask blueprint file
  parity/routes.tsv           every method+path of the Python app; fill in "go package" and "status"
  testdata/                   goldens (copied from backend/testdata or generated from Python)
```

## Rules for every work package

1. **Port behaviour, not style.** Read the Python module and its tests fully before writing. Keep
   function boundaries and names recognisable (`snake_case` → `CamelCase`), so a reviewer can put the
   two files side by side. Keep the Chinese user-facing messages byte-identical.
2. **Dynamic documents.** Configuration documents are `jsonx` values, not typed structs: Python
   `dict` → `*jsonx.Object` (insertion-ordered), `list` → `[]any`, `str` → `string`,
   `int`/`float` → `jsonx.Number` (keeps the original text), `bool`, `None` → `nil`. Unknown fields must
   survive a read-modify-write untouched. Use typed structs only for values that never reach storage
   or the wire as free-form JSON.
3. **Stored bytes must match Python.** Anything written to `configflow.sqlite3` or to files under
   `DATA_DIR` must be byte-identical to what Python writes for the same value (key order, separators,
   escaping, number text, trailing newline). Use the `jsonx` encoders; never `encoding/json` for
   stored data.
4. **Tests are the acceptance criterion.** For every Python test file listed for your package,
   port the cases to Go tests with the same inputs and expected outputs (table-driven where the
   Python is parametrized). Keep the test names traceable (`test_foo_bar` → `TestFooBar`). Tests that
   only exercise Python mechanics (monkeypatched internals, import-time behaviour) may be replaced by
   an equivalent Go test of the same observable behaviour; say so in the hand-back. Golden files:
   reuse `backend/testdata/*`; when a Python test builds its expectation in code, generate a golden
   by running the Python once and commit it under `server/testdata/<package>/`.
5. **Device and network I/O goes through interfaces** declared in your package (HTTP client,
   clock, command runner), so tests use fakes instead of patching globals. No test may contact a
   real host; never run anything against production addresses.
6. **No new dependencies** beyond those already in `server/go.mod` without saying so in the
   hand-back. Standard library first.
7. **Concurrency.** Python used threads plus `flock`; the Go server is one process. Keep the same
   lock files where another process could interfere (installers, Agents do not touch DATA_DIR, but
   two server instances on one DATA_DIR must still exclude each other). Background loops take a
   `context.Context` and stop on cancel.
8. **HTTP handlers** live in `internal/api/<blueprint>` and only translate HTTP ↔ package calls.
   Register routes in that package's `Register(*httpx.Router)`; never edit another package's
   registration. Update `parity/routes.tsv` for the routes you implemented (`go package`, status
   `done`). Response bodies, status codes, headers and the token redaction described in
   `internal/httpx` must match the Flask app.
9. **Where tests run.** Never run tests, builds or `go vet` on the Mac. Use
   `rt -- bash -lc 'cd server && go test ./internal/<pkg>/...'` from your worktree (it syncs the tree
   to the test server and runs there). Python reference runs:
   `rt -- bash -lc '~/cf-test/.venv/bin/python -m pytest backend/test_x.py -q -p no:cacheprovider'`.
   Only run your own packages; the server is shared.
10. **Git.** Work only inside `server/` (plus `parity/` rows for your routes). Commit on your own
    branch with a message `go(<package>): <what>`; do not push, do not touch `backend/`, `frontend/`,
    `docker/`.

## Hand-back format

- Files added, with line counts.
- Python files covered and, per file, anything not ported or deliberately different (with reason).
- Python test files → Go test files, number of cases ported / skipped (with reason).
- Result of the test run on the test server (command and tail of the output).
- Open questions and assumptions about other packages' APIs.

## Reference: stored JSON encoding

Python writes store values with `json.dumps(v, ensure_ascii=False, separators=(',', ':'))`, artifact
files with `indent=2, ensure_ascii=False` plus a trailing `\n`, and content digests with
`sort_keys=True` and compact separators. `jsonx` provides `Compact`, `Indent2` and `Sorted` encoders
that reproduce these byte for byte (no HTML escaping, control characters as `\u00xx`, `\n \r \t \b \f`
short escapes, non-ASCII verbatim, floats and big integers kept as their original text).
