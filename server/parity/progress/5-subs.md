# Package 5: subs (subscription parsing) — progress

## Brief
Port to `server/internal/subs`: `subscription_parser.parse_uri_list` (+ per-scheme parsers, `parse_yaml_object`;
`parse_subscription`/`parse_clash_yaml` not ported, no caller), `subscription_hosts`, `subscription_filter`,
`subscription_dialers`, `proxy_chains`, `mihomo_listeners`, `proxy_server_dns`, and `commented_mihomo`
(ruamel round-trip listener edit, sub-package `subs/commented`, byte-identical output). Subscriptions themselves
go through Sub-Store (#13). Follow-up from the coordinator: the commented editor must not lose features —
reduce refusals (`无法安全保留…`) to zero on realistic configs, then per category (numbers, comment before `---`,
folded/tags, comment on dash line, CRLF incl. ruamel's mixed `\r\r\n`, alias-to-commented-block last);
invariant 0 mismatches (refusing beats different bytes). `\N{name}` filters and the 24 Python-crash anchor
rows stay documented as is.

## Done (all tests pass on the test server)
- Modules ported + corpora (hand-back sent earlier): parser, filter, hosts, dialers, chains, dns, listeners,
  commented; tldextract 5.1.2 snapshot + matcher (`psl.go`, 89c701bc); lazy `ReportedCoreVersion` (d981fb7c);
  compat golden cross-check (0c980414).
- Follow-up:
  - realistic corpus `commented_realistic` (128 repo base configs × listener layouts, 4911 rows): 0 refused,
    0 mismatches (f233dcb6).
  - Category numbers: ruamel resolver (first-char lookup), RT int/float constructors + representers,
    generated anchors (id001) for OctalInt/date, dropped anchors for sexagesimal/inf/null; `commented_scalars`
    corpus 2080 rows (307cca7a). Negative hex/bin `!!int '0x-1F'` reloaded via a decimal substitution because
    yamlx cannot construct it.
  - Category comment before `---`: ruamel drops them, the missing-comment step re-adds them on top (b419883a,
    refined in the folded commit).
  - Category folded `>`: scanner with `\a` fold marks, FoldedScalarString fold_pos, write_folded port.

## Current counts (refused / rows; mismatches 0 everywhere)
- commented (random): 126 / 2411 — CRLF with comments 118, comment on dash line holding a nested block 7,
  load-check 1.
- commented_anchors: 336 / 1884 — alias to a commented block collection 336 (+24 Python crashes).
- commented_realistic: 0 / 4911.
- commented_scalars: 16 / 2080 — yamlx cannot parse source (`1,000` in flow) 8, lone indicator `-` 4,
  comment on null item of nested seq 4.

## In progress
Nothing half-written.

## Next
1. Tags / reserved indicators (`!`, `%`, `@`, backtick): check counts (none in random corpus), decide.
2. Comment on a dash line holding a nested block (7).
3. CRLF documents with comments (118): reproduce ruamel's mixed `\r\r\n` / `\n` output.
4. Alias to commented block (336): estimate; likely report instead of implementing.
5. Hand-back with per-category leftovers (count, minimal example, reason).

## Decisions and findings
- Refusals return Python's failure message; route answers 400; original text kept.
- `compat.go` = private copy of dialer_references (#6 to replace); non-string-key proxy strings and 3 malformed
  profiles differ (documented in compat_golden_test.go).
- `\N{name}` filter patterns: filter not applied (rules engine), documented.
- Debug helper lives outside the repo (/tmp/zz_debug_test.go.keep + temporary `DebugLast` var); never commit.

## Commands
- Tests: `rt -- bash -lc 'cd server && gofmt -l internal/subs; go vet ./internal/subs/... && go test -count=1 ./internal/subs/...'`
  (+ `-race` once at the end).
- Corpus stats: `go test -count=1 -v -run TestCommented ./internal/subs/`.
- Regenerate one corpus: `rt --back server/internal/subs/testdata/<name>.jsonl.gz -- bash -lc 'GEN_ONLY=<name> PYTHONPATH=. ~/cf-test/.venv/bin/python server/internal/subs/testdata/gen.py'`
- Check all corpora reproduce: copy gen.py to /tmp/genchk on the server, run it there, `cmp` with testdata.
