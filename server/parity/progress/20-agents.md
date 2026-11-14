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
- First commit (see git log `go(agents):`): all modules written, `go vet ./internal/agents/` clean. No tests yet.
  Files: version.go (+generated version_changelog.go), pytime.go, diagnostics.go, operation_events.go,
  messages.go (+generated messages_data.go), idle_rounds.go, presence_log.go, runtime_state.go (incl. legacy
  runtime.sqlite3 import), client.go, hooks.go, compat.go (#29 HA helpers), manager.go, remote.go,
  deployments.go, singleton.go (Get/Init/Wire, task rows).

## In progress / Next
1. `internal/api/agents`: POST /api/agents/register, POST /api/agents/<id>/heartbeat, POST /api/agents
   (manual add), PUT+DELETE /api/agents/<id>. GET /api/agents and GET /api/agents/<id> stay todo (#24
   content_sync, #26 upgrade artifacts).
2. Wire into server.go; routes.tsv rows.
3. Go tests: registration security, heartbeat security, runtime state, light reads (manager part), agent
   profiles, profile bindings (manager), failure details / dns reports (manager parts), deployments (manager
   parts), operation events, deployment/upgrade messages, version sync, idle rounds, presence log.
4. Difftest under `internal/api/agents/difftest` (pattern of internal/httpx/difftest).
5. `-race` run, hand-back.

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
