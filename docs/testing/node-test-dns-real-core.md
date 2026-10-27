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
