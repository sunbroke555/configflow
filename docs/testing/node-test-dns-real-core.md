# Managed node-test DNS: real-core acceptance

The 2026-10-27 run used the real OxiDNS 1.6.1 binary, a Go Agent built from this
branch, the real preview APIs, and the compiled node-probe. The HTTP node was
downloaded from an isolated YAML subscription through the normal subscription
refresh API. No production subscription, DNS server, gateway or VM was involved.

Machine-readable evidence: [2026-10-27 report](node-test-dns-real-core-2026-10-27.json).

## Isolation and prerequisites

Run only on test server `192.168.0.32`. The runner checks that address and requires
the marker in `~/previews/configflow-node-dns-lab`. Its PID records include the
Linux process start time and command, so stop operations do not target reused
PIDs. All configuration, transaction journals, backups and logs stay in that
private directory. It never runs an installer, Docker daemon or systemctl.
The separate preview backend/web server uses the task-specific transient units
`cf-node-dns-preview-backend` and `cf-node-dns-preview-web`, running as
`sunbroke`. No existing system service was modified and no OxiDNS/Agent unit was
installed by this harness.

The Go Agent uses its existing `command` service backend to start/stop a real
OxiDNS process. `deployment_method=docker` is metadata used to skip native Agent
migration; this is a process harness, not Docker acceptance. The OxiDNS binary is
the static amd64 1.6.1 full build, SHA256
`f3b8eab909047570fd3b068df8f0ed1cae8b323e920941ae3345c801ba5604e6`.

Start an isolated ConfigFlow preview beforehand, with an empty database and no
production credentials, at backend `127.0.0.1:15801` (browser preview
`http://192.168.0.32:15800`). Its environment must include:

```text
CONFIGFLOW_NODE_TEST_URL=http://127.0.0.1:18083/generate_204
```

Use the branch's compiled node-probe. The preview's private
`~/previews/configflow-node-test-dns/credentials.json` must contain its
`registration_key`; the runner consumes it in memory and does not print it.
This laboratory preview has authentication disabled and contains only synthetic
data. This setup is not suitable for a public deployment.

| Port | Listener |
| --- | --- |
| 18081 | Go Agent, test server LAN address |
| 15354 | Original OxiDNS entry, Fake-IP fixture upstream |
| 15353 | Managed real DNS entry, created by the feature |
| 15355 / 15356 | Loopback real / fake DNS fixtures |
| 18082 | HTTP proxy, allows only the local target |
| 18083 | Loopback YAML subscription and HTTP 204 target |
| 19199 | Loopback OxiDNS API |

The real fixture answers only `example.com` and `proxy.lab.invalid`; it never
forwards queries to the Internet. The HTTP proxy accepts only
`127.0.0.1:18083`. Actual latency here measures local integration, not Internet
proxy performance.

## Reproduce

From the task worktree, build on `.32` and copy the runner. The OxiDNS source
path below is the existing test-only binary discovered on that server; verify
its hash first or supply an equivalent verified 1.6.1 binary.

```sh
rt -- sh -c 'mkdir -p /home/sunbroke/previews/configflow-node-dns-lab/bin && chmod 700 /home/sunbroke/previews/configflow-node-dns-lab && cp /tmp/configflow-entry-flow-e2e-runtime/oxidns-v1.6.1/oxidns /home/sunbroke/previews/configflow-node-dns-lab/bin/oxidns && cd backend/agents/go-agent && go build -trimpath -o /home/sunbroke/previews/configflow-node-dns-lab/bin/configflow-agent .'
scp scripts/tests/node_test_dns_lab.py sunbroke@192.168.0.32:/home/sunbroke/previews/configflow-node-dns-lab/runner.py
```

On `.32`:

```sh
touch ~/previews/configflow-node-dns-lab/.lab-marker
chmod 600 ~/previews/configflow-node-dns-lab/.lab-marker
~/cf-test/.venv/bin/python ~/previews/configflow-node-dns-lab/runner.py prepare
~/cf-test/.venv/bin/python ~/previews/configflow-node-dns-lab/runner.py test
```

`prepare` refuses to overwrite an existing identity and confirms every lab port
is free before creating resources. `test` can be repeated: it removes only
endpoint IDs previously recorded by this runner and requires port 15353 to be
absent before the first creation. It leaves a fresh ready endpoint selected for
the user to review. Do not modify the lab Agent/settings concurrently with a run.

## Assertions

The eight steps exercise normal publication, actual Fake-IP failure, managed
creation without changing the global selection, explicit binding selection and
successful subscription-node probing, ordinary republication, rejection of
in-use deletion, explicit unbinding and deletion, and recreation for preview.

Each successful probe must add an actual proxy connection and at least two
origin `HEAD` requests that return HTTP 204. The checks do not accept a non-null
latency alone. Republication retains all four managed plugins and byte-identical
rule files. Deletion removes those plugins, closes both TCP and UDP, and leaves
the original Fake-IP entry and its rule file unchanged.

The final run recorded three successful 1 ms local probes, each with one proxy
connection and two origin HEAD 204 responses. The original entry returned
`198.18.0.99`, producing `dns_fake_ip`; the managed UDP and TCP entries both
returned the fixture's real address `192.168.0.32`.

Afterward, only the independent preview backend was restarted. The global DNS
selection remained unchanged, its endpoint still resolved as ready, and the
same subscription node again completed a 1 ms real worker probe. The lab Agent,
OxiDNS and fixtures were left running. See the
[restart evidence](node-test-dns-preview-restart-2026-10-27.json).

The existing Go Agent real-core lifecycle regression also passed separately:

```sh
cd backend/agents/go-agent
OXIDNS_TEST_BINARY=/tmp/configflow-entry-flow-e2e-runtime/oxidns-v1.6.1/oxidns GOMAXPROCS=4 go test ./routes -run '^TestOxiDNSNativeTransactionalDeployment$' -count=1
```

That test covers actual core publication, health-failure rollback and manual
rollback in temporary directories with loopback ports.

## Existing published upstream acceptance

The later [existing-upstream report](node-test-dns-existing-upstream-2026-10-27.json)
uses a second empty backend at `127.0.0.1:15901` and a separate laboratory.
The original public preview already contained user draft edits, so this run
did not load, publish, clear or copy its database, credentials or configuration.
Only source code and test binaries were reused. The secondary backend, Agent,
OxiDNS and fixtures are ordinary private processes, with no system units.

Set `CONFIGFLOW_NODE_DNS_LAB_VARIANT=upstreams`; the only permitted alternate
paths are `~/previews/configflow-node-dns-upstreams-check` (backend) and
`~/previews/configflow-node-dns-upstreams-lab` (Agent/core/fixtures). Ports are:

| Port | Listener |
| --- | --- |
| 15901 | Independent backend, loopback only |
| 18091 | Go Agent |
| 15364 / 15363 | Original Fake-IP entry / managed entry |
| 15365 / 15366 | Loopback real / fake fixture DNS |
| 18092 / 18093 | Restricted HTTP proxy / loopback subscription and 204 target |
| 19209 | Loopback OxiDNS API |

The published baseline contains an unused `lab_real_forward` alongside its
active Fake-IP path. Creation sends only its opaque upstream ID. The runner
asserts that the deployed managed forward preserves its complete `concurrent`,
`timeout`, `idle_timeout` and address parameters. A separate unpublished
`lab_pending_draft` forward remains in the draft, is absent from candidates and
is not deployed by managed creation. Only that runner-created sentinel is
explicitly removed before the ordinary full publication test.

All nine steps passed: Fake-IP failure was reproduced; managed UDP/TCP returned
the real fixture address; explicit binding selection, ordinary republication
and recreation each produced a 1 ms probe with one proxy connection and two
actual HEAD 204 responses. Deletion closed UDP/TCP and retained the original
entry and rule bytes. The second backend and all its lab processes were then
stopped; all nine port numbers were confirmed closed. The public preview was
left running. These local timings establish integration, not Internet latency.

To reproduce in **new empty secondary directories**, first synchronize source
and build the probe on the test server from the task worktree:

```sh
rt -- sh -c 'cd backend/node-probe && go build -trimpath -o /tmp/configflow-node-test-dns-modes-probe .'
```

Then run these commands on `.32` (the initial `mkdir` deliberately refuses to
reuse an existing experiment):

```sh
set -eu
task_source="$HOME/rt/program__configflow-node-test-dns-modes"
lab_root="$HOME/previews/configflow-node-dns-upstreams-lab"
backend_root="$HOME/previews/configflow-node-dns-upstreams-check"
mkdir -m 700 "$lab_root" "$backend_root"
mkdir "$lab_root/bin" "$backend_root/bin" "$backend_root/source"
rsync -a --exclude=.git --exclude=node_modules --exclude=.venv --exclude=__pycache__ --exclude=.ci-results --exclude=frontend/dist "$task_source/" "$backend_root/source/"
cp "$task_source/scripts/tests/node_test_dns_lab.py" "$lab_root/runner.py"
cp /tmp/configflow-node-test-dns-modes-probe "$backend_root/bin/node-probe"
cp /tmp/configflow-entry-flow-e2e-runtime/oxidns-v1.6.1/oxidns "$lab_root/bin/oxidns"
cd "$task_source/backend/agents/go-agent"
go build -trimpath -o "$lab_root/bin/configflow-agent" .
touch "$lab_root/.lab-marker"
chmod 600 "$lab_root/.lab-marker"
export CONFIGFLOW_NODE_DNS_LAB_VARIANT=upstreams
~/cf-test/.venv/bin/python "$lab_root/runner.py" backend-start
~/cf-test/.venv/bin/python "$lab_root/runner.py" prepare
~/cf-test/.venv/bin/python "$lab_root/runner.py" test
~/cf-test/.venv/bin/python "$lab_root/runner.py" stop
```

The helper generates fresh mode-0600 registration/JWT secrets and initializes
an empty repository before importing the app. Its source copy is independent
of future `rt` synchronizations. The original default runner also now rejects
any non-fixture draft before changing selection, deleting an endpoint or
publishing; do not run it against the public preview after user edits.

## Evidence and cleanup

`report.json` contains bounded public step evidence. `network-events.jsonl`
contains only synthetic DNS names and local proxy/HTTP events. Do not commit
`agent.json`, `identity.json`, preview credentials, environment files or logs;
Agent tokens are confined to private runtime files.

To stop only the lab's Agent, core and fixture processes:

```sh
~/cf-test/.venv/bin/python ~/previews/configflow-node-dns-lab/runner.py stop
```

This leaves private evidence and the independent preview server intact. Before
deleting runtime data, switch the preview away from the managed binding and
remove its endpoint through the UI/API while the lab processes are still alive.
