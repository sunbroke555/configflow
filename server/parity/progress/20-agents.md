# Package #20 agent registry — progress

## Brief
Port `backend/agents/manager.py` (AgentManager), `common/agent_manager.py`, `runtime_state`, `diagnostics`,
`operation_events`, `idle_rounds`, `presence_log`, `agents/version`, `deployment_messages`, `upgrade_messages`
to `server/internal/agents`; routes in `server/internal/api/agents`. Later packages (#21 metrics, #24/#26
publish, #27 rule sync, #28 DNS/address follow, #29 DNS HA, #30 gateway install, #31 IPv6) plug in through
`agents.RegisterHooks`, `OnAgentDeleted`, `OnManagerInit`, `SetRuleSyncRow`/`SetDNSFollowRow`. Agent HTTP
calls go through `agents.Client` (fake in tests). Register only pure-registry routes whose full behaviour
works now; update `parity/routes.tsv`. Wire via `agents.Wire` in `wirePackages` + `apiagents.Register` in
`routePackages` (one line each in `internal/server/server.go`). No sub-agents; tests only via `rt`.

## Done
- ee00305e core modules; 1dd4e700 routes + wiring + manager tests; next commit: route tests, routes.tsv.
- `internal/agents` tests (91 pass, 1 partial skip: weak-ref GC check of idle rounds): runtime state incl.
  legacy import + presence log, registration/profiles/bindings/light reads (manager), deployments
  (begin/record/poll/activate/rollback/recover, busy retry), failure details, DNS reports, events, messages,
  version, idle rounds.
- `internal/api/agents`: POST register, POST heartbeat, POST /api/agents, PUT/DELETE /api/agents/<id>; tests
  port registration/heartbeat security + item update/delete. routes.tsv: 5 rows done; other agent rows carry
  the package they wait for.
- server.go: `apiagents.Register` in routePackages, `agents.Wire` in wirePackages.

## Next
1. Difftest `internal/api/agents/difftest` (Flask vs Go, loopback; pattern internal/httpx/difftest).
2. `-race` run of agents, api/agents, server; `go test ./internal/server/...`.
3. Hand-back.

## Decisions and findings
- Transport error texts (`Connection error: ...`, `error_details`) carry Go net/http texts, not requests'.
- FailureText: RE2 `\b` is ASCII (Python's is Unicode).
- Hooks default: ReceiptMatches nil → fail closed (OxiDNS activation refused); HA pair paths and
  PublishDeployment → ErrNotPorted; installation registration → InstallationError 501.
- `rebind_profile_agents` route lives in routes/profiles (#17); manager method ported.

## Commands
- `rt -- bash -lc 'cd server && go vet ./internal/agents/... ./internal/api/agents/... && go test -count=1 ./internal/agents/... ./internal/api/agents/...'`
- Regenerate tables: `~/program/configflow/.venv/bin/python -I server/internal/agents/testdata/gen_changelog.py $PWD > server/internal/agents/version_changelog.go` and gen_messages.py → messages_data.go (see
  headers of version_changelog.go / messages_data.go; dict → Go literal).
