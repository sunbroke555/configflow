# OxiDNS entry-flow acceptance test

This is a full-application test: the real Vue application talks to an isolated
Flask HTTP server and SQLite database. Browser actions apply and save native
configuration through the normal authenticated APIs, reopen the editor, and
verify the committed value. The same saved draft is rendered by the API and run
unchanged by the real OxiDNS core. Local DNS stubs return distinct addresses so
routing, multiple listeners, shared flows, and rule order are observable.

Run only on the test server, from a task worktree:

```sh
cf-test typecheck  # prepares the normal lockfile dependency cache
rt --back .ci-results/entry-flow-e2e -- \
  bash scripts/tests/oxidns_entry_flow_e2e/run.sh \
  --output .ci-results/entry-flow-e2e
```

Use a fresh output path for every run. The runner refuses an existing database.
`--bootstrap-only` checks the complete app and real-core fixture baselines without
editing through the flow UI; it does not count as the full acceptance test.

The runner downloads the repository-pinned OxiDNS release into a temporary cache,
checks the published SHA-256 recorded in `gateway_install_script.py`, and installs
Playwright 1.64.1 in that cache without changing package or lock files. Existing
Chromium downloads are reused. Python uses the same 3.11 environment as `cf-test`.
All fixture DNS listeners and upstreams use dynamically selected high loopback
ports. The temporary frontend uses a high port on 0.0.0.0; its backend binds only
to loopback and requires generated credentials. No existing services, production
configuration, Agent, RouterOS, PVE, firewall, or system DNS settings are touched.

Coverage includes independent listener flows, shared UDP/TCP listeners,
handwritten `jump` and overlapping conditions, plugins declared in include files,
a listener targeting a `forward` directly, one HTTP listener with two separately
routed paths queried with actual DNS wire POST bodies, unknown nested fields with false/0/null,
invalid YAML cancellation, real API-backed insert/delete and up/down round trips,
explicit apply versus save, ETag-protected persistence,
and reload. Desktop/375px and dark/light × clay/kraft/sky use the complete settings
page; controls are measured with nonempty count assertions. Final native documents
must differ in exactly the intended field, retaining order, value types and other
plugins/files. Actual HTTP negative checks cover stale node snapshots, tag/type
changes, missing references and dependency cycles; invalid executable drafts must
fail both final rule validation/rendering and the real core's configuration check.

`report.json` combines core query results and browser evidence;
`browser-report.json` records actual HTTP saves, layouts and screenshot names.
Screenshot counts come from files, not expected constants. Core configs and logs
remain under each fixture directory for diagnosis. Owned services are terminated
on success or failure; transient browser credentials are removed before reports
are copied back. The SQLite database and fixture files contain synthetic data only.
