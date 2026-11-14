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
   `int`/`float` → `jsonx.Number` (holds the text Python would re-emit: ints keep every digit, floats
   are `repr()` text, so `1.50` read from a file is `1.5` afterwards, exactly as in Python), `bool`,
   `None` → `nil`. Unknown fields must survive a read-modify-write untouched. Use typed structs only
   for values that never reach storage or the wire as free-form JSON.
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

## Reference: packages already available

Read the source for exact signatures; every exported name has a doc comment.

**`configflow/internal/jsonx`** (`doc.go`, `object.go`, `value.go`, `number.go`, `decode.go`, `encode.go`)
- `Object`: `NewObject()`, `Obj("k", v, ...)` literal, `Get/Value/GetOr/Set/SetDefault/Delete/Pop`,
  `Keys/Values/All()` (range in insertion order), `Clone` (shallow), `Update`, `DeepCopy`, typed getters
  `GetString/GetBool/GetObject/GetList/GetNumber/GetInt/GetFloat` (zero value when missing or of another
  type; reads are safe on a nil `*Object`, writes are not).
- Helpers: `AsObject/AsList/AsString/AsBool/AsNumber/AsInt/AsFloat`, `ListOf`, `Strings`, `Path(v, keys...)`,
  `Truthy` (Python `bool(v)`), `Equal` (Python `==`: `1 == 1.0`, dict order ignored), `DeepCopy`,
  `Repr`/`Str` (Python `repr()`/`str()` for messages).
- Numbers: `Int(i)`, `BigInt`, `Float(f)`, `ParseNumber`; `IsInt/IsFloat/Int64/Float64`.
- `Parse([]byte)` is `json.loads`; errors carry Python's message.
- Encoders, byte-identical to CPython 3.11 (verified against a 634-value corpus):
  `Compact` (stored SQLite values), `Indent2` (files; append `"\n"` yourself), `Sorted` (content
  digests), `Default` (`json.dumps(v)`), `Response` (`flask.jsonify` body: sorted keys, ASCII, compact,
  trailing newline), and `Encode(v, Options{EnsureASCII, SortKeys, Compact, Indent})` for the rest.
  `backend/common/json_response.py::jsonify_external` is `Encode(v, Options{EnsureASCII: true, SortKeys: true})`
  without a trailing newline. With `LOG_LEVEL=DEBUG` Flask's `jsonify` indents by 2.
- Not provided: `default=str`, `allow_nan=False`; convert or check before encoding. Request bodies with
  a BOM must be stripped before `Parse`.

**`configflow/internal/store`** (`store.go`, `conn.go`, `rows.go`, `errors.go`) — port of
`common/config_store.py`, interchangeable with it on the same `configflow.sqlite3` (cross-checked both
ways). `New(path, timeout)`, `Load`, `Meta`, `Reading(func(*Conn) error)`, `Commit(base, document, meta)`
→ `(entry, committed, err)`, `Create`, `Check`, `BackupTo`, `PreserveCorrupt`, `Fingerprint`. `Entry.Document`
is shared and must never be mutated: `DeepCopy()` first. Errors are `*store.Error`; use `errors.Is` with
`ErrStore`, `ErrCorrupt`, `ErrUnsupported`, `ErrConflict`, `ErrStorage`, `ErrSQLite`, `ErrDocument`; `Error()`
is Python's `str(exc)`.

**Infra (package #1)** — each has a package doc; read it before use.
- `internal/yamlx`: PyYAML-compatible YAML on the jsonx model. `Load`/`SafeLoad(text)` is
  `yaml.safe_load` (YAML 1.1 typing: `yes/no/on/off`, octal, sexagesimal, timestamps → `yamlx.Timestamp`);
  `LoadWith(text, LoadOptions{Schema: SchemaNative, Key, Name})` is `oxidns_native.NativeLoader`.
  `Dump(v, DumpOptions{AllowUnicode, SortKeys, FlowStyle, Indent, Width, IndentSequences, ExplicitStart,
  ExplicitEnd, NoAliases, OnMarked})` is a port of PyYAML's pure-Python emitter, byte-identical for the
  option sets used in the backend (11 sets × 1374 corpus values). Presets: `SafeDump` (`yaml.safe_dump(v)`,
  sorted), `SafeDumpUnicode` (`allow_unicode=True, sort_keys=False`), `IndentDump` (`IndentDumper`).
  `FlowList`, `FlowMap`, `*SharedString` (anchor/alias), `Marked` + `OnMarked` (for `push_diff`).
  Known limits: a non-string mapping key gives `*NonStringKeyError`; syntax error texts are yaml.v3's;
  CSafeDumper output (used once in `push_diff.py` to measure size) is not reproduced.
- `internal/logx`: `Setup(Config)`, `Get(name)` with `Debug/Info/Warning/Error/Critical` and `…f`
  variants, `Exception(msg, err)`, `LogText(value, limit)` (Python `log_text`, default limit 120),
  `ClientIP(req)`. **Use the Python module path as the logger name** (`backend.routes.agents`): the log
  line format `2026-11-14 12:34:56,789 - name - LEVEL - msg` is what the log page shows.
- `internal/jsonstore`: one JSON file per id under a directory, atomic writes (`utils/json_store.py`).
- `internal/urlx`: `SafeURLForLog`, `SafeExceptionDetails(err)`, `Unquote`, `UnquotePlus`, `QuotePlus`.
- `internal/taskstatus`: `DATA_DIR/task-status.json` store; `Record`, `Get`, `Load`, `Forget`, `NowISO`,
  `ParseTime`, `FromISOFormat` (CPython `datetime.fromisoformat` semantics); `OnRecord` hook.
- `internal/localhttp`: in-process dispatch of requests addressed to this server (`utils/local_http.py`):
  `Register(handler)`, `Do(ctx, Request)` → `(nil, nil)` means "use the network".
- `internal/resource` (bundled files: `FromEnv().Backend(name)`), `internal/utilx` (`GetLocalIP`,
  `GenerateRandomToken`, `StrToBool`), `internal/version` (`Version`, `Info()`), `internal/jsonresp`
  (`External(payload)` = `jsonify_external` body; `PlainTree`; `Sanitized` marker — HTTP glue is #3's).

## Helpers from a package that is not ported yet

If you need a small helper that belongs to another work package still marked `todo`/`wip` in
`parity/packages.md`, put a minimal private copy in your package in a file named `compat.go`, mark it
`// TODO(port): replace with <package>` and list it in the hand-back. Do not create or edit another
package's directory.

## Working next to other agents

Several agents work in this same worktree at once. Touch only your package directories and your rows of
`parity/routes.tsv`. Stage with explicit paths (`git add server/internal/<yours>`), never `git add -A` or
`git commit -a`. If `git` reports an index lock, wait a few seconds and retry. Commit early and often
(after each coherent, compiling step) so nothing is lost if you are interrupted. Run only your own
packages' tests: `rt -- bash -lc 'cd server && go vet ./internal/<yours>/... && go test -count=1 ./internal/<yours>/...'`.
Another agent's half-written package may sit in the tree; never "fix" it, and do not run `go test ./...`.
Do the work yourself: do not spawn sub-agents or forks (parallel helpers exhaust the shared usage
window within an hour and leave half-written files behind).
`rt --back <path>` copies that path from the server over your local files. Use it only for a single
generated file or a directory inside your own package (`--back server/internal/<yours>/testdata`), never
for `server/internal` or `server`: it would overwrite other agents' newer local edits.
