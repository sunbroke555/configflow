package routes

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"gopkg.in/yaml.v3"
)

type oxRuleUpdateTask struct {
	ID            string `json:"id"`
	Status        string `json:"status"`
	Phase         string `json:"phase"`
	Code          string `json:"code,omitempty"`
	Downloaded    int    `json:"downloaded"`
	DownloadTotal int    `json:"download_total"`
	Reloaded      int    `json:"reloaded"`
	ReloadTotal   int    `json:"reload_total"`
}

type oxRuleUpdatePlan struct {
	api           *oxMonitorConfig
	downloadTag   string
	downloadTotal int
	providers     []string
}

type oxRuleUpdater struct {
	mu             sync.Mutex
	task           oxRuleUpdateTask
	requestTimeout time.Duration
	roundTimeout   time.Duration
}

func oxRuleNotConfigured() *oxMonitorError {
	return oxMonitorFail(http.StatusConflict, "RULE_UPDATE_NOT_CONFIGURED", "Configure downloadable rules and republish the OxiDNS configuration, then retry")
}

// Discover an unconditional, two-step download -> reload_provider sequence.
// Never execute arbitrary cron jobs, sequences, or user-supplied plugin tags.
func oxRuleUpdateConfiguration(cfg *Config) (*oxRuleUpdatePlan, *oxMonitorError) {
	api, apiErr := loadOxiDNSMonitorConfig(cfg)
	if apiErr != nil {
		return nil, apiErr
	}
	data, err := os.ReadFile(cfg.ConfigPath)
	if err != nil || len(data) > oxMonitorJSONLimit || sha256Hex(data) != api.sourceRevision {
		return nil, oxMonitorFail(http.StatusConflict, "SNAPSHOT_CHANGED", "The active configuration changed; try again")
	}
	var doc map[string]interface{}
	if yaml.Unmarshal(data, &doc) != nil {
		return nil, oxMonitorConfigError()
	}
	items, _ := doc["plugins"].([]interface{})
	plugins := make(map[string]map[string]interface{})
	for _, item := range items {
		plugin, _ := item.(map[string]interface{})
		tag, _ := plugin["tag"].(string)
		if tag == "" {
			continue
		}
		if !oxMonitorSafeTag(tag) || plugins[tag] != nil {
			return nil, oxMonitorConfigError()
		}
		plugins[tag] = plugin
	}
	var candidates []*oxRuleUpdatePlan
	for _, plugin := range plugins {
		if plugin["type"] != "sequence" {
			continue
		}
		steps, _ := plugin["args"].([]interface{})
		if len(steps) != 2 {
			continue
		}
		tags := make([]string, 2)
		for i, value := range steps {
			step, _ := value.(map[string]interface{})
			exec, _ := step["exec"].(string)
			if len(step) == 1 && strings.HasPrefix(exec, "$") && oxMonitorSafeTag(exec[1:]) {
				tags[i] = exec[1:]
			}
		}
		download, reload := plugins[tags[0]], plugins[tags[1]]
		if download["type"] != "download" || reload["type"] != "reload_provider" {
			continue
		}
		args, _ := download["args"].(map[string]interface{})
		downloads, _ := args["downloads"].([]interface{})
		if len(downloads) == 0 || len(downloads) > 1000 {
			continue
		}
		var references []interface{}
		switch value := reload["args"].(type) {
		case string:
			references = []interface{}{value}
		case []interface{}:
			references = value
		}
		if len(references) == 0 || len(references) > 1000 {
			continue
		}
		plan := &oxRuleUpdatePlan{api: api, downloadTag: tags[0], downloadTotal: len(downloads)}
		seen := make(map[string]bool)
		for _, value := range references {
			ref, _ := value.(string)
			if !strings.HasPrefix(ref, "$") || !oxMonitorSafeTag(ref[1:]) {
				break
			}
			tag := ref[1:]
			switch plugins[tag]["type"] {
			case "domain_set", "ip_set", "geosite", "geoip", "adguard_rule":
				if !seen[tag] {
					plan.providers = append(plan.providers, tag)
					seen[tag] = true
				}
			default:
				seen = nil
			}
			if seen == nil {
				break
			}
		}
		// Duplicated/malformed provider references are rejected rather than guessed.
		if len(plan.providers) != len(references) || seen == nil {
			continue
		}
		candidates = append(candidates, plan)
	}
	if len(candidates) > 1 {
		return nil, oxMonitorFail(http.StatusConflict, "RULE_UPDATE_AMBIGUOUS", "Multiple rule update chains are configured")
	}
	if len(candidates) == 0 {
		return nil, oxRuleNotConfigured()
	}
	return candidates[0], nil
}

func oxRuleRequest(ctx context.Context, client *http.Client, api *oxMonitorConfig, method, path string, result interface{}) *oxMonitorError {
	var body io.Reader
	if method == http.MethodPost {
		body = strings.NewReader("{}")
	}
	req, err := http.NewRequestWithContext(ctx, method, api.endpoint+path, body)
	if err != nil {
		return oxMonitorConfigError()
	}
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if api.basicAuth {
		req.SetBasicAuth(api.username, api.password)
	}
	response, err := client.Do(req)
	if err != nil {
		var networkError net.Error
		// Client.Timeout does not wrap context.DeadlineExceeded on every supported Go version.
		if errors.Is(err, context.DeadlineExceeded) || errors.Is(ctx.Err(), context.DeadlineExceeded) ||
			(errors.As(err, &networkError) && networkError.Timeout()) {
			return oxMonitorFail(http.StatusGatewayTimeout, "RULE_UPDATE_UNKNOWN", "The core request timed out; its outcome could not be confirmed")
		}
		return oxMonitorFail(http.StatusBadGateway, "CORE_UNREACHABLE", "Cannot reach the OxiDNS API")
	}
	defer response.Body.Close()
	switch response.StatusCode {
	case http.StatusOK:
	case http.StatusNotFound, http.StatusMethodNotAllowed:
		return oxMonitorFail(http.StatusConflict, "CORE_UPDATE_UNSUPPORTED", "The running OxiDNS core does not support native rule update APIs")
	case http.StatusUnauthorized, http.StatusForbidden:
		return oxMonitorFail(http.StatusBadGateway, "CORE_AUTH_FAILED", "OxiDNS API authentication failed")
	case http.StatusConflict:
		return oxMonitorFail(http.StatusConflict, "CORE_BUSY", "An OxiDNS rule operation is already running")
	default:
		return oxMonitorFail(http.StatusBadGateway, "RULE_UPDATE_FAILED", "OxiDNS rejected the rule operation")
	}
	data, err := io.ReadAll(io.LimitReader(response.Body, oxMonitorJSONLimit+1))
	if err != nil {
		return oxMonitorFail(http.StatusBadGateway, "RULE_UPDATE_UNKNOWN", "The core response could not be confirmed")
	}
	if len(data) > oxMonitorJSONLimit || json.Unmarshal(data, result) != nil {
		return oxMonitorFail(http.StatusBadGateway, "INVALID_RESPONSE", "OxiDNS returned an invalid rule update response")
	}
	return nil
}

func oxRuleCheckVersion(ctx context.Context, client *http.Client, cfg *Config, plan *oxRuleUpdatePlan) *oxMonitorError {
	var status struct {
		Pending        *bool   `json:"pending"`
		InProgress     *bool   `json:"in_progress"`
		RunningVersion *string `json:"running_version"`
	}
	if err := oxRuleRequest(ctx, client, plan.api, http.MethodGet, "/api/reload/status", &status); err != nil {
		return err
	}
	data, err := os.ReadFile(cfg.ConfigPath)
	if err != nil || status.Pending == nil || status.InProgress == nil || status.RunningVersion == nil ||
		*status.Pending || *status.InProgress || *status.RunningVersion != plan.api.sourceRevision || sha256Hex(data) != plan.api.sourceRevision {
		return oxMonitorFail(http.StatusConflict, "SNAPSHOT_CHANGED", "The running configuration changed or has not been applied")
	}
	return nil
}

func oxRuleCheckDownload(ctx context.Context, client *http.Client, plan *oxRuleUpdatePlan) *oxMonitorError {
	var status struct {
		OK        bool              `json:"ok"`
		Running   *bool             `json:"running"`
		Downloads []json.RawMessage `json:"downloads"`
	}
	if err := oxRuleRequest(ctx, client, plan.api, http.MethodGet, "/api/plugins/"+plan.downloadTag+"/downloads", &status); err != nil {
		return err
	}
	if !status.OK || status.Running == nil || len(status.Downloads) != plan.downloadTotal {
		return oxMonitorFail(http.StatusBadGateway, "INVALID_RESPONSE", "OxiDNS returned an invalid download status")
	}
	if *status.Running {
		return oxMonitorFail(http.StatusConflict, "CORE_BUSY", "An OxiDNS download is already running")
	}
	return nil
}

func (updater *oxRuleUpdater) snapshot() oxRuleUpdateTask {
	updater.mu.Lock()
	defer updater.mu.Unlock()
	return updater.task
}

func (updater *oxRuleUpdater) change(edit func(*oxRuleUpdateTask)) {
	updater.mu.Lock()
	defer updater.mu.Unlock()
	edit(&updater.task)
}

func (updater *oxRuleUpdater) run(cfg *Config, plan *oxRuleUpdatePlan, release func()) {
	finalStatus, finalCode := "failed", "RULE_UPDATE_UNKNOWN"
	defer func() {
		// A terminal observation also means the Agent service lock was released.
		updater.mu.Lock()
		defer updater.mu.Unlock()
		release()
		updater.task.Status, updater.task.Phase, updater.task.Code = finalStatus, "done", finalCode
	}()
	ctx, cancel := context.WithTimeout(context.Background(), updater.roundTimeout)
	defer cancel()
	client := oxMonitorClient()
	client.Timeout = updater.requestTimeout
	fail := func(code string) {
		finalCode = code
	}
	var download struct {
		OK        bool `json:"ok"`
		Total     *int `json:"total"`
		Succeeded *int `json:"succeeded"`
		Failed    *int `json:"failed"`
	}
	if err := oxRuleRequest(ctx, client, plan.api, http.MethodPost, "/api/plugins/"+plan.downloadTag+"/download", &download); err != nil {
		fail(err.code)
		return
	}
	if download.Total == nil || download.Succeeded == nil || download.Failed == nil || *download.Total != plan.downloadTotal ||
		*download.Succeeded < 0 || *download.Succeeded > plan.downloadTotal || *download.Failed < 0 || *download.Failed > plan.downloadTotal ||
		*download.Succeeded+*download.Failed != plan.downloadTotal {
		fail("INVALID_RESPONSE")
		return
	}
	updater.change(func(task *oxRuleUpdateTask) { task.Downloaded = *download.Succeeded })
	if !download.OK || *download.Failed != 0 {
		fail("DOWNLOAD_FAILED")
		return
	}
	// External core reloads are outside the Agent lock. Recheck before touching
	// providers so a stale plan cannot target a replacement plugin instance.
	if err := oxRuleCheckVersion(ctx, client, cfg, plan); err != nil {
		fail(err.code)
		return
	}
	updater.change(func(task *oxRuleUpdateTask) { task.Phase = "reloading" })
	for _, tag := range plan.providers {
		var result struct {
			OK bool `json:"ok"`
		}
		if err := oxRuleRequest(ctx, client, plan.api, http.MethodPost, "/api/plugins/"+tag+"/reload", &result); err != nil {
			if err.code == "RULE_UPDATE_FAILED" {
				fail("RELOAD_FAILED")
			} else {
				fail(err.code)
			}
			return
		}
		if !result.OK {
			fail("RELOAD_FAILED")
			return
		}
		updater.change(func(task *oxRuleUpdateTask) { task.Reloaded++ })
	}
	if ctx.Err() != nil {
		fail("RULE_UPDATE_UNKNOWN")
		return
	}
	if err := oxRuleCheckVersion(ctx, client, cfg, plan); err != nil {
		fail(err.code)
		return
	}
	finalStatus, finalCode = "succeeded", ""
}

func (updater *oxRuleUpdater) handler(cfg *Config) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		if r.Method != http.MethodGet && r.Method != http.MethodPost {
			w.Header().Set("Allow", "GET, POST")
			oxMonitorWriteError(w, oxMonitorFail(http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "Use GET or POST"))
			return
		}
		if r.URL.Path != "/api/oxidns/rules/update" || r.URL.EscapedPath() != r.URL.Path || r.URL.RawQuery != "" {
			oxMonitorWriteError(w, oxMonitorFail(http.StatusBadRequest, "INVALID_REQUEST", "Rule updates do not accept parameters"))
			return
		}
		body, err := io.ReadAll(io.LimitReader(r.Body, 1025))
		var object map[string]json.RawMessage
		if err != nil || len(body) > 1024 || (r.Method == http.MethodGet && len(bytes.TrimSpace(body)) != 0) ||
			(r.Method == http.MethodPost && (json.Unmarshal(body, &object) != nil || object == nil || len(object) != 0)) {
			oxMonitorWriteError(w, oxMonitorFail(http.StatusBadRequest, "INVALID_REQUEST", "POST requires an empty JSON object"))
			return
		}
		if r.Method == http.MethodGet {
			JsonResponse(w, http.StatusOK, map[string]interface{}{"success": true, "task": updater.snapshot()})
			return
		}
		release, err := AcquireServiceOperation(cfg)
		if err != nil {
			code := "SERVICE_BUSY"
			if updater.snapshot().Status == "running" {
				code = "RULE_UPDATE_BUSY"
			}
			oxMonitorWriteError(w, oxMonitorFail(http.StatusConflict, code, "A service operation is already running"))
			return
		}
		accepted := false
		defer func() {
			if !accepted {
				release()
			}
		}()
		if deploymentBlocked(cfg) != nil {
			oxMonitorWriteError(w, oxMonitorFail(http.StatusConflict, "SERVICE_BUSY", "Complete deployment recovery before updating rules"))
			return
		}
		plan, planErr := oxRuleUpdateConfiguration(cfg)
		if planErr != nil {
			oxMonitorWriteError(w, planErr)
			return
		}
		client := oxMonitorClient()
		if checkErr := oxRuleCheckVersion(r.Context(), client, cfg, plan); checkErr != nil {
			oxMonitorWriteError(w, checkErr)
			return
		}
		if checkErr := oxRuleCheckDownload(r.Context(), client, plan); checkErr != nil {
			oxMonitorWriteError(w, checkErr)
			return
		}
		var id [16]byte
		if _, err := rand.Read(id[:]); err != nil {
			oxMonitorWriteError(w, oxMonitorFail(http.StatusInternalServerError, "RULE_UPDATE_FAILED", "Cannot create a rule update task"))
			return
		}
		task := oxRuleUpdateTask{ID: hex.EncodeToString(id[:]), Status: "running", Phase: "downloading", DownloadTotal: plan.downloadTotal, ReloadTotal: len(plan.providers)}
		updater.change(func(current *oxRuleUpdateTask) { *current = task })
		accepted = true
		go updater.run(cfg, plan, release)
		JsonResponse(w, http.StatusAccepted, map[string]interface{}{"success": true, "task": task})
	}
}

// State is kept across requests, not across an Agent process restart.
func OxiDNSRuleUpdateHandler(cfg *Config) http.HandlerFunc {
	updater := &oxRuleUpdater{task: oxRuleUpdateTask{Status: "idle", Phase: "idle"}, requestTimeout: 180 * time.Second, roundTimeout: 600 * time.Second}
	return updater.handler(cfg)
}
