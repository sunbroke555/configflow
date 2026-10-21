package routes

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"reflect"
	"strconv"
	"strings"
	"time"
)

const oxRankingLimit = 10

type oxUpstreamLabel struct {
	PluginTag string `json:"plugin_tag"`
	Upstream  string `json:"upstream"`
	Address   string `json:"address"`
}

type oxRankingConfig struct {
	upstreams []oxUpstreamLabel
	retention map[string]*uint64
	blocked   map[string]string
}

// These labels describe configured upstreams, never the winner inferred from a
// query trace. Match OxiDNS 1.6.1 forward::upstream_metric_names exactly.
func oxMonitorRankingConfig(plugins []interface{}) oxRankingConfig {
	result := oxRankingConfig{upstreams: []oxUpstreamLabel{}, retention: map[string]*uint64{}, blocked: map[string]string{}}
	byTag := map[string]map[string]interface{}{}
	duplicate := map[string]bool{}
	for _, item := range plugins {
		plugin, _ := item.(map[string]interface{})
		tag, _ := plugin["tag"].(string)
		if byTag[tag] != nil {
			duplicate[tag] = true
		}
		byTag[tag] = plugin
		args, _ := plugin["args"].(map[string]interface{})
		if plugin["type"] == "query_recorder" {
			if value, exists := args["retention_days"]; !exists {
				days := uint64(7)
				result.retention[tag] = &days
			} else if days, ok := value.(int); ok && days > 0 {
				days := uint64(days)
				result.retention[tag] = &days
			}
		}
		if plugin["type"] != "forward" || !oxMonitorSafeTag(tag) {
			continue
		}
		upstreams, _ := args["upstreams"].([]interface{})
		names := map[string]bool{}
		for index, raw := range upstreams {
			upstream, _ := raw.(map[string]interface{})
			address, _ := upstream["addr"].(string)
			name, named := upstream["tag"].(string)
			if !named {
				name = address
			}
			if names[name] {
				name += "#" + strconv.Itoa(index)
			}
			names[name] = true
			if address == "" || !oxMonitorLiteral(address) || !oxMonitorLiteral(name) || len(address) > 2048 || len(name) > 2048 {
				continue
			}
			// Addresses may contain DoH credentials or query tokens. Only the
			// server location belongs in this display-only metadata.
			display := address
			if parsed, err := url.Parse(address); err == nil && parsed.Host != "" {
				parsed.User, parsed.RawQuery, parsed.Fragment = nil, "", ""
				display = parsed.String()
			}
			result.upstreams = append(result.upstreams, oxUpstreamLabel{tag, name, display})
		}
	}
	for tag := range duplicate {
		delete(byTag, tag)
	}
	main := byTag["sequence_main"]
	rules, _ := main["args"].([]interface{})
	block := byTag["adguard_block"]
	reject := byTag["adguard_reject"]
	provider := byTag["adguard_rules"]
	wantReject := []interface{}{map[string]interface{}{"exec": "$adguard_block"}, map[string]interface{}{"exec": "accept"}}
	if main["type"] != "sequence" || block["type"] != "black_hole" || provider["type"] != "adguard_rule" || reject["type"] != "sequence" || !reflect.DeepEqual(reject["args"], wantReject) {
		return result
	}
	blockArgs, ok := block["args"].(map[string]interface{})
	mode, _ := blockArgs["mode"].(string)
	if !ok || len(blockArgs) != 1 || mode != "nxdomain" && mode != "nodata" && mode != "null" && mode != "refused" || !oxRankingDirectEntry(plugins) {
		return result
	}
	// A quick matcher match proves a block only for the generated terminal
	// branch. Arbitrary matcher matches, NXDOMAIN and IPv4 suppression do not.
	recorders := []string{}
	for index, raw := range rules {
		rule, ok := raw.(map[string]interface{})
		if !ok {
			break
		}
		exec, _ := rule["exec"].(string)
		matches := oxRankingMatches(rule["matches"])
		if len(rule) == 2 && reflect.DeepEqual(matches, []string{"question $adguard_rules"}) && exec == "goto adguard_reject" {
			for _, recorder := range recorders {
				result.blocked[recorder] = fmt.Sprintf("qs.match.sequence_main.%d.0.question", index)
			}
			break
		}
		if len(rule) == 1 && matches == nil {
			if strings.HasPrefix(exec, "$") && byTag[strings.TrimPrefix(exec, "$")]["type"] == "query_recorder" {
				recorders = append(recorders, strings.TrimPrefix(exec, "$"))
				continue
			}
			if exec == "metrics_collector metrics" || exec == "prefer_ipv4" || exec == "$custom_hosts" && byTag["custom_hosts"]["type"] == "hosts" {
				continue
			}
		}
		if len(rule) == 2 && len(matches) == 1 && (matches[0] == "has_resp" && exec == "accept" || strings.HasPrefix(matches[0], "qtype ") && (exec == "reject 0" || exec == "black_hole nodata short_circuit=true")) {
			continue
		}
		// Unknown wrapping executors could replace the block response on the
		// return path. Do not infer a blocked ranking for custom chains.
		break
	}
	return result
}

// An outer sequence/executor can replace the response after the main chain
// returns. Recognize only direct built-in server entries and conservatively
// decline custom references rather than interpreting arbitrary plugin config.
func oxRankingDirectEntry(plugins []interface{}) bool {
	servers := 0
	for _, item := range plugins {
		plugin, _ := item.(map[string]interface{})
		if plugin["tag"] == "sequence_main" {
			continue
		}
		args, _ := plugin["args"].(map[string]interface{})
		kind, _ := plugin["type"].(string)
		server := kind == "udp_server" || kind == "tcp_server" || kind == "http_server" || kind == "quic_server"
		if server && args["entry"] == "sequence_main" {
			servers++
			for key, value := range args {
				if key != "entry" && oxRankingMentionsMain(value) {
					return false
				}
			}
		} else if oxRankingMentionsMain(plugin["args"]) {
			return false
		}
	}
	return servers > 0
}

func oxRankingMentionsMain(value interface{}) bool {
	switch value := value.(type) {
	case string:
		return strings.Contains(value, "sequence_main")
	case []interface{}:
		for _, item := range value {
			if oxRankingMentionsMain(item) {
				return true
			}
		}
	case map[string]interface{}:
		for _, item := range value {
			if oxRankingMentionsMain(item) {
				return true
			}
		}
	}
	return false
}

func oxRankingMatches(raw interface{}) []string {
	if value, ok := raw.(string); ok {
		return []string{value}
	}
	if values, ok := raw.([]interface{}); ok {
		result := []string{}
		for _, value := range values {
			text, ok := value.(string)
			if !ok {
				return nil
			}
			result = append(result, text)
		}
		return result
	}
	return nil
}

func oxMonitorParseRankings(raw string) (string, url.Values, *oxMonitorError) {
	values, err := url.ParseQuery(raw)
	if err != nil || len(raw) > 8192 || !values.Has("since_ms") {
		return "", nil, oxMonitorBadQuery()
	}
	for key, items := range values {
		if len(items) != 1 || key != "recorder" && key != "since_ms" && key != "until_ms" {
			return "", nil, oxMonitorBadQuery()
		}
		if key != "recorder" {
			if _, err := strconv.ParseInt(items[0], 10, 64); err != nil || strings.HasPrefix(items[0], "-") {
				return "", nil, oxMonitorBadQuery()
			}
		}
	}
	recorder, query, queryErr := oxMonitorParseQuery(raw, false)
	if queryErr != nil {
		return "", nil, queryErr
	}
	query.Set("limit", strconv.Itoa(oxRankingLimit))
	return recorder, query, nil
}

type oxRankingRow struct {
	Key   string  `json:"key"`
	Count uint64  `json:"count"`
	Share float64 `json:"share"`
}

type oxRankingGroup struct {
	SampleSize uint64         `json:"sample_size"`
	Rows       []oxRankingRow `json:"rows"`
	AtLimit    bool           `json:"at_limit"`
}

type oxMonitorRuntime struct {
	RunningVersion string  `json:"running_version"`
	LastStarted    *uint64 `json:"last_started_ms"`
	LastCompleted  *uint64 `json:"last_completed_ms"`
	LastSuccess    *uint64 `json:"last_success_ms"`
}

func (state oxMonitorRuntime) revision() string {
	data, _ := json.Marshal(state)
	digest := sha256.Sum256(data)
	return hex.EncodeToString(digest[:])
}

func (config *oxMonitorConfig) runtimeSnapshot(ctx context.Context, client *http.Client) (*oxMonitorRuntime, *oxMonitorError) {
	data, err := config.fetch(ctx, client, "/api/reload/status", nil, 64<<10, false)
	if err != nil {
		return nil, err
	}
	var status struct {
		Status     string `json:"status"`
		Pending    *bool  `json:"pending"`
		InProgress *bool  `json:"in_progress"`
		oxMonitorRuntime
	}
	var fields map[string]json.RawMessage
	if json.Unmarshal(data, &status) != nil || json.Unmarshal(data, &fields) != nil || status.Pending == nil || status.InProgress == nil || fields["last_started_ms"] == nil || fields["last_completed_ms"] == nil || fields["last_success_ms"] == nil {
		return nil, oxMonitorFail(http.StatusBadGateway, "INVALID_RESPONSE", "OxiDNS running configuration status is unavailable")
	}
	if *status.Pending || *status.InProgress || status.RunningVersion != config.sourceRevision || status.Status != "idle" && status.Status != "ok" && status.Status != "failed" {
		return nil, oxMonitorFail(http.StatusConflict, "SNAPSHOT_CHANGED", "The running OxiDNS configuration is changing or differs from the configuration file")
	}
	return &status.oxMonitorRuntime, nil
}

func oxDecodeRanking(data []byte, clients bool) (oxRankingGroup, *oxMonitorError) {
	bad := oxMonitorFail(http.StatusBadGateway, "INVALID_RESPONSE", "OxiDNS API returned invalid ranking statistics")
	var core struct {
		OK         bool            `json:"ok"`
		SampleSize *uint64         `json:"sample_size"`
		Rows       *[]oxRankingRow `json:"rows"`
	}
	if json.Unmarshal(data, &core) != nil || !core.OK || core.SampleSize == nil || core.Rows == nil || *core.SampleSize > oxMonitorClientSampleLimit || len(*core.Rows) > oxRankingLimit {
		return oxRankingGroup{}, bad
	}
	result := oxRankingGroup{*core.SampleSize, *core.Rows, *core.SampleSize == oxMonitorClientSampleLimit}
	seen := map[string]bool{}
	var clientTotal uint64
	for index := range result.Rows {
		row := &result.Rows[index]
		// DNS can contain multiple questions; domain counts count question
		// occurrences and can exceed the number of recorded requests.
		max := result.SampleSize * 65535
		if clients {
			max = result.SampleSize
		}
		if row.Key == "" || len(row.Key) > 1024 || !oxMonitorLiteral(row.Key) || seen[row.Key] || row.Count == 0 || row.Count > max || clients && net.ParseIP(row.Key) == nil {
			return oxRankingGroup{}, bad
		}
		seen[row.Key] = true
		if clients {
			clientTotal += row.Count
		}
		row.Share = float64(row.Count) / float64(result.SampleSize)
	}
	if clients && clientTotal > result.SampleSize {
		return oxRankingGroup{}, bad
	}
	return result, nil
}

func (config *oxMonitorConfig) verifySnapshot(ctx context.Context, client *http.Client, cfg *Config, before []byte, running *oxMonitorRuntime) *oxMonitorError {
	afterRuntime, err := config.runtimeSnapshot(ctx, client)
	if err != nil {
		return err
	}
	changed := oxMonitorFail(http.StatusConflict, "SNAPSHOT_CHANGED", "OxiDNS changed while statistics were being collected; refresh the dashboard")
	if !reflect.DeepEqual(running, afterRuntime) {
		return changed
	}
	after, err := config.fetch(ctx, client, "/api/health", nil, oxMonitorJSONLimit, true)
	if err != nil {
		return err
	}
	if oxSourceInstance(before) == "" || oxSourceInstance(before) != oxSourceInstance(after) {
		return changed
	}
	file, openErr := os.Open(cfg.ConfigPath)
	if openErr != nil {
		return changed
	}
	defer file.Close()
	data, readErr := io.ReadAll(io.LimitReader(file, oxMonitorJSONLimit+1))
	if readErr != nil || len(data) > oxMonitorJSONLimit {
		return changed
	}
	revision := sha256.Sum256(data)
	if hex.EncodeToString(revision[:]) != config.sourceRevision {
		return changed
	}
	return nil
}

func (config *oxMonitorConfig) writeRankings(w http.ResponseWriter, ctx context.Context, client *http.Client, cfg *Config, recorder string, query url.Values) {
	if recorder == "" && len(config.capabilities.QueryRecorders) > 0 {
		recorder = config.capabilities.QueryRecorders[0]
	}
	tag, err := config.recorder(recorder)
	if err != nil {
		oxMonitorWriteError(w, err)
		return
	}
	health, err := config.fetch(ctx, client, "/api/health", nil, oxMonitorJSONLimit, true)
	if err != nil {
		oxMonitorWriteError(w, err)
		return
	}
	running, err := config.runtimeSnapshot(ctx, client)
	if err != nil {
		oxMonitorWriteError(w, err)
		return
	}
	collectedAt := time.Now().UnixMilli()
	if !query.Has("until_ms") {
		query.Set("until_ms", strconv.FormatInt(collectedAt, 10))
	}
	fetch := func(kind string, params url.Values) (oxRankingGroup, *oxMonitorError) {
		// Validate the bounded top response independently: the existing client
		// picker requests every bucket and requires their sum to equal sample.
		data, fetchErr := config.fetch(ctx, client, "/api/plugins/"+tag+"/stats/"+kind, params, oxMonitorJSONLimit, false)
		if fetchErr != nil {
			return oxRankingGroup{}, fetchErr
		}
		return oxDecodeRanking(data, kind == "top_clients")
	}
	clients, err := fetch("top_clients", query)
	if err != nil {
		oxMonitorWriteError(w, err)
		return
	}
	domains, err := fetch("top_qnames", query)
	if err != nil {
		oxMonitorWriteError(w, err)
		return
	}
	blocked := map[string]interface{}{"available": false, "reason": "当前配置未包含可确认的标准 AdGuard 拦截路径", "sample_size": 0, "rows": []oxRankingRow{}, "at_limit": false}
	if matcher := config.rankings.blocked[tag]; matcher != "" {
		filtered := url.Values{}
		for key, items := range query {
			filtered[key] = append([]string{}, items...)
		}
		filtered.Set("matcher_tag", matcher)
		filtered.Set("status", "has_response")
		group, fetchErr := fetch("top_qnames", filtered)
		if fetchErr != nil {
			oxMonitorWriteError(w, fetchErr)
			return
		}
		blocked = map[string]interface{}{"available": true, "reason": "standard_adguard", "matcher_tag": matcher, "sample_size": group.SampleSize, "rows": group.Rows, "at_limit": group.AtLimit}
	}
	if err := config.verifySnapshot(ctx, client, cfg, health, running); err != nil {
		oxMonitorWriteError(w, err)
		return
	}
	JsonResponse(w, http.StatusOK, map[string]interface{}{"success": true, "recorder": tag, "collected_at_ms": collectedAt, "source_revision": config.sourceRevision, "runtime_revision": running.revision(), "instance_id": oxSourceInstance(health), "retention_days": config.rankings.retention[tag], "sample_limit": oxMonitorClientSampleLimit, "clients": clients, "domains": domains, "blocked": blocked})
}
