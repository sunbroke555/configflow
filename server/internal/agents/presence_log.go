package agents

// Port of backend/agents/presence_log.py: log Agent online/offline
// transitions.
//
// Offline is derived from the heartbeat age when Agents are listed, never
// stored, so transitions are detected by comparing successive scans in this
// process. Heartbeats are only kept in memory, so after a restart every Agent
// reads as offline until it next reports. An Agent is therefore followed only
// from the first scan that sees it online.

import (
	"sync"

	"configflow/internal/jsonx"
	"configflow/internal/logx"
)

var presenceLogger = logx.Get("backend.agents.presence_log")

type presence struct {
	status          string
	name, heartbeat any
}

var (
	presenceMu    sync.Mutex
	presenceKnown = map[string]presence{}
)

// ObservePresence is presence_log.observe(agents) (called by the Agent rule
// sync loop with get_all_agents()).
func ObservePresence(agents []*jsonx.Object) {
	presenceMu.Lock()
	defer presenceMu.Unlock()
	current := map[string]presence{}
	var order []string
	for _, agent := range agents {
		status := "offline"
		if agent.Value("status") == "online" {
			status = "online"
		}
		id := jsonx.Str(agent.Value("id"))
		if _, known := presenceKnown[id]; status == "online" || known {
			name := agent.Value("name")
			if !jsonx.Truthy(name) {
				name = agent.Value("id")
			}
			heartbeat := agent.Value("last_heartbeat")
			if !jsonx.Truthy(heartbeat) {
				heartbeat = ""
			}
			if _, seen := current[id]; !seen {
				order = append(order, id)
			}
			current[id] = presence{status, name, heartbeat}
		}
	}
	for _, id := range order {
		item := current[id]
		previous, known := presenceKnown[id]
		if !known || previous.status == item.status {
			continue
		}
		if item.status == "online" {
			presenceLogger.Infof("Agent %s 已恢复在线", logx.LogText(item.name, 120))
		} else {
			presenceLogger.Warningf("Agent %s 已离线：超过 2 分钟没有心跳（最后心跳 %s）", logx.LogText(item.name, 120), logx.LogText(item.heartbeat, 32))
		}
	}
	presenceKnown = current
}

// resetPresence forgets every followed Agent (tests).
func resetPresence() {
	presenceMu.Lock()
	presenceKnown = map[string]presence{}
	presenceMu.Unlock()
}
