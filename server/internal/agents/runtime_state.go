package agents

// Port of backend/agents/runtime_state.py: Agent heartbeat state kept in
// process memory.
//
// A heartbeat's liveness, service status and metrics are only worth showing
// while they are fresh, so they live here and are laid over the stored Agent
// on read; a restart forgets them and each Agent reappears with its next
// heartbeat. The reported version is the exception: the manager writes it
// into the configuration document whenever it changes.
//
// Whichever side holds the newer heartbeat wins: registration writes the
// document, so a re-registered Agent shows its new version until its next
// heartbeat. Every Manager for the same data directory shares one state.

import (
	"context"
	"database/sql"
	"errors"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"

	_ "modernc.org/sqlite"

	"configflow/internal/jsonx"
	"configflow/internal/logx"
	"configflow/internal/repo"
)

// RuntimeFields are RUNTIME_FIELDS: what a heartbeat may lay over a record.
var RuntimeFields = []string{"version", "service_status", "system_metrics", "cpu", "memory", "disk", "network"}

var metricFields = []string{"cpu", "memory", "disk", "network"}

// LegacyDatabase is LEGACY_DATABASE, written by #369; read once, then removed.
const LegacyDatabase = "runtime.sqlite3"

var runtimeLogger = logx.Get("backend.agents.runtime_state")

var (
	statesMu      sync.Mutex
	states        = map[string]*RuntimeState{}
	legacyChecked = map[string]bool{}
)

// timestampOf is _timestamp(value): the POSIX time of an ISO text, -Inf
// for anything unparsable or ahead of the clock.
func timestampOf(value any) float64 {
	text, ok := value.(string)
	if !ok {
		return math.Inf(-1)
	}
	moment, _, ok := fromISO(text)
	if !ok {
		return math.Inf(-1)
	}
	// A registration time ahead of the clock (time zone changed, clock stepped
	// back) must not outrank every later heartbeat.
	if ts := pyTimestamp(moment); ts <= nowTimestamp() {
		return ts
	}
	return math.Inf(-1)
}

// RuntimeEntry is one remembered heartbeat.
type RuntimeEntry struct {
	HeartbeatAt   float64
	LastHeartbeat string
	Fields        *jsonx.Object
}

// RuntimeState is AgentRuntimeState.
type RuntimeState struct {
	mu      sync.Mutex
	entries map[string]RuntimeEntry
	// Bumped by each delete, so a heartbeat that looked the Agent up before its
	// deletion cannot put the entry back afterwards.
	deletions int64
	deleted   map[string]int64
}

// NewRuntimeState returns an empty state (one process-wide per data
// directory: use RuntimeFor).
func NewRuntimeState() *RuntimeState {
	return &RuntimeState{entries: map[string]RuntimeEntry{}, deleted: map[string]int64{}}
}

func (s *RuntimeState) newer(agent *jsonx.Object) (RuntimeEntry, bool) {
	id, _ := agent.Value("id").(string)
	entry, ok := s.entries[id]
	if ok && entry.HeartbeatAt > timestampOf(agent.Value("last_heartbeat")) {
		return entry, true
	}
	return RuntimeEntry{}, false
}

// Overlay lays newer heartbeat state over the given (copied) Agents in place.
func (s *RuntimeState) Overlay(agents []*jsonx.Object) []*jsonx.Object {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, agent := range agents {
		if entry, ok := s.newer(agent); ok {
			agent.Update(entry.Fields.DeepCopy())
			agent.Set("last_heartbeat", entry.LastHeartbeat)
			agent.Set("status", "online")
		}
	}
	return agents
}

// Mark is taken before looking the Agent up; pass it to RecordHeartbeat.
func (s *RuntimeState) Mark() int64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.deletions
}

// RecordHeartbeat merges one heartbeat into the newer of the remembered and
// registered state. It returns false, recording nothing, when the Agent was
// deleted after mark.
func (s *RuntimeState) RecordHeartbeat(agent, payload *jsonx.Object, mark int64) bool {
	now := nowTimestamp()
	s.mu.Lock()
	defer s.mu.Unlock()
	id, _ := agent.Value("id").(string)
	if s.deleted[id] > mark {
		return false
	}
	var fields *jsonx.Object
	if entry, ok := s.newer(agent); ok {
		fields = entry.Fields.DeepCopy()
	} else {
		fields = jsonx.NewObject()
		for _, key := range RuntimeFields {
			if value, ok := agent.Get(key); ok {
				fields.Set(key, jsonx.DeepCopy(value))
			}
		}
	}
	// Fields a heartbeat leaves out keep their last value.
	for _, key := range []string{"version", "service_status"} {
		if value, ok := payload.Get(key); ok {
			fields.Set(key, value)
		}
	}
	if value, ok := payload.Get("system_metrics"); ok {
		metrics := jsonx.DeepCopy(value)
		fields.Set("system_metrics", metrics)
		if object, ok := metrics.(*jsonx.Object); ok {
			for _, key := range metricFields {
				if item, ok := object.Get(key); ok {
					fields.Set(key, item)
				}
			}
		}
	}
	s.entries[id] = RuntimeEntry{HeartbeatAt: now, LastHeartbeat: isoformat(fromTimestamp(now), false), Fields: fields}
	return true
}

// Seed restores a heartbeat remembered elsewhere unless a newer one arrived.
func (s *RuntimeState) Seed(agentID string, heartbeatAt float64, lastHeartbeat string, fields *jsonx.Object) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if entry, ok := s.entries[agentID]; !ok || entry.HeartbeatAt < heartbeatAt {
		s.entries[agentID] = RuntimeEntry{HeartbeatAt: heartbeatAt, LastHeartbeat: lastHeartbeat, Fields: fields.DeepCopy()}
	}
}

// Delete forgets an Agent and blocks heartbeats looked up before it.
func (s *RuntimeState) Delete(agentID string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.entries, agentID)
	s.deletions++
	s.deleted[agentID] = s.deletions
}

// Entries returns a copy of the remembered heartbeats (Python's _entries,
// for tests and diagnostics).
func (s *RuntimeState) Entries() map[string]RuntimeEntry {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make(map[string]RuntimeEntry, len(s.entries))
	for id, entry := range s.entries {
		out[id] = entry
	}
	return out
}

// SetEntry replaces one remembered heartbeat (tests).
func (s *RuntimeState) SetEntry(agentID string, entry RuntimeEntry) {
	s.mu.Lock()
	s.entries[agentID] = entry
	s.mu.Unlock()
}

func resolvedDir(dataDir string) string {
	abs, err := filepath.Abs(dataDir)
	if err != nil {
		abs = dataDir
	}
	if resolved, err := filepath.EvalSymlinks(abs); err == nil {
		return resolved
	}
	return abs
}

// RuntimeFor is for_data_dir(data_dir): the process-wide state of a data
// directory.
func RuntimeFor(dataDir string) *RuntimeState {
	key := resolvedDir(dataDir)
	statesMu.Lock()
	defer statesMu.Unlock()
	state, ok := states[key]
	if !ok {
		state = NewRuntimeState()
		states[key] = state
	}
	return state
}

// ResetRuntime forgets every Agent's heartbeat state, as a process restart
// does (tests).
func ResetRuntime() {
	statesMu.Lock()
	states = map[string]*RuntimeState{}
	legacyChecked = map[string]bool{}
	statesMu.Unlock()
}

type legacyRow struct {
	heartbeatAt   float64
	lastHeartbeat string
	fields        *jsonx.Object
}

func legacyRows(path string) map[string]legacyRow {
	result := map[string]legacyRow{}
	db, err := sql.Open("sqlite", "file:"+path+"?_pragma=busy_timeout(5000)")
	if err == nil {
		defer db.Close()
		var rows *sql.Rows
		rows, err = db.Query("SELECT agent_id, heartbeat_at, last_heartbeat, fields FROM agent_runtime")
		if err == nil {
			defer rows.Close()
			for rows.Next() {
				var agentID, heartbeatAt, lastHeartbeat, raw any
				if err = rows.Scan(&agentID, &heartbeatAt, &lastHeartbeat, &raw); err != nil {
					break
				}
				text, ok := raw.(string)
				if bytes, isBytes := raw.([]byte); isBytes {
					text, ok = string(bytes), true
				}
				if !ok {
					continue
				}
				parsed, parseErr := jsonx.ParseString(text)
				if parseErr != nil {
					continue
				}
				id, idOK := agentID.(string)
				last, lastOK := lastHeartbeat.(string)
				fields, fieldsOK := parsed.(*jsonx.Object)
				var at float64
				atOK := true
				switch value := heartbeatAt.(type) {
				case int64:
					at = float64(value)
				case float64:
					at = value
				default:
					atOK = false
				}
				if idOK && atOK && lastOK && fieldsOK {
					result[id] = legacyRow{at, last, fields}
				}
			}
			if err == nil {
				err = rows.Err()
			}
		}
	}
	if err != nil {
		runtimeLogger.Warningf("旧版 Agent 心跳状态库无法读取，已忽略：%s", err)
		return map[string]legacyRow{}
	}
	return result
}

func legacyFiles(path string) []string {
	files := []string{path, path + "-journal", path + "-wal", path + "-shm"}
	corrupt, _ := filepath.Glob(escapeGlob(path) + ".corrupt-*")
	sort.Strings(corrupt)
	return append(files, corrupt...)
}

func escapeGlob(path string) string {
	replacer := strings.NewReplacer("*", `\*`, "?", `\?`, "[", `\[`, `\`, `\\`)
	return replacer.Replace(path)
}

func removeFiles(paths []string) {
	for _, path := range paths {
		if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
			runtimeLogger.Warningf("旧版 Agent 心跳状态文件 %s 删除失败：%s", filepath.Base(path), pyOSError(err))
		}
	}
}

// pyOSError renders an *os.PathError roughly as Python's OSError str.
func pyOSError(err error) string {
	var pathErr *os.PathError
	if errors.As(err, &pathErr) {
		return pathErr.Err.Error() + ": '" + pathErr.Path + "'"
	}
	return err.Error()
}

func exists(path string) bool {
	_, err := os.Lstat(path)
	return err == nil
}

// removeLegacyFile is the indirection tests use to refuse a removal.
var removeLegacyFile = removeFiles

// ImportLegacyDatabase is import_legacy_database(repository, state,
// persist_versions): carry #369's on-disk heartbeat state into memory once,
// then remove it. Agents newer there than in the document are seeded, and a
// version that only reached that database is handed to persist. Nothing is
// applied unless the file could be removed; a failed version write is left to
// the Agent's next heartbeat.
func ImportLegacyDatabase(ctx context.Context, repository *repo.Repository, state *RuntimeState,
	persist func(ctx context.Context, changes map[string]*jsonx.Object) (map[string]bool, error)) error {
	path := filepath.Join(repository.DataDir(), LegacyDatabase)
	key := filepath.Join(resolvedDir(repository.DataDir()), LegacyDatabase)
	statesMu.Lock()
	if legacyChecked[key] {
		statesMu.Unlock()
		return nil
	}
	legacyChecked[key] = true
	statesMu.Unlock()
	found := false
	for _, item := range legacyFiles(path) {
		if exists(item) {
			found = true
			break
		}
	}
	if !found {
		return nil
	}
	rows := map[string]legacyRow{}
	if info, err := os.Stat(path); err == nil && info.Mode().IsRegular() {
		rows = legacyRows(path)
	}
	// Listed after reading, which may leave a journal behind.
	removeLegacyFile(legacyFiles(path))
	if exists(path) {
		runtimeLogger.Warningf("旧版 %s 无法删除，未导入其中的 Agent 心跳状态", LegacyDatabase)
		return nil
	}
	system, err := repository.GetSystem(ctx)
	if err != nil {
		return err
	}
	agents := map[string]*jsonx.Object{}
	for _, item := range system.GetList("agents") {
		if agent, ok := item.(*jsonx.Object); ok {
			agents[jsonx.Str(agent.Value("id"))] = agent
		}
	}
	current := map[string]legacyRow{}
	var ids []string
	for id, row := range rows {
		if agent, ok := agents[id]; ok && row.heartbeatAt > timestampOf(agent.Value("last_heartbeat")) {
			current[id] = row
			ids = append(ids, id)
		}
	}
	sort.Strings(ids)
	versions := map[string]*jsonx.Object{}
	for _, id := range ids {
		row := current[id]
		if version, ok := row.fields.Value("version").(string); ok && !jsonx.Equal(version, agents[id].Value("version")) {
			versions[id] = jsonx.Obj("version", version)
		}
	}
	for _, id := range ids {
		row := current[id]
		state.Seed(id, row.heartbeatAt, row.lastHeartbeat, row.fields)
	}
	if len(versions) > 0 {
		if _, err := persist(ctx, versions); err != nil {
			runtimeLogger.Warningf("旧版 Agent 心跳状态里的版本号写入配置失败，将在 Agent 下次心跳时写入：%s", err)
			versions = map[string]*jsonx.Object{}
		}
	}
	runtimeLogger.Infof("Agent 心跳状态改为只保存在内存：已从旧版 %s 恢复 %d 个 Agent 的状态（%d 个版本号写入配置），并删除该文件",
		LegacyDatabase, len(current), len(versions))
	return nil
}
