package routes

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	"gopkg.in/yaml.v3"
)

const oxRankingTestPlugins = `- {tag: records, type: query_recorder, args: {retention_days: 3}}
- {tag: adguard_rules, type: adguard_rule, args: {files: [ads.txt]}}
- {tag: adguard_block, type: black_hole, args: {mode: nxdomain}}
- tag: adguard_reject
  type: sequence
  args:
    - exec: $adguard_block
    - exec: accept
- tag: sequence_main
  type: sequence
  args:
    - exec: metrics_collector metrics
    - exec: $records
    - exec: prefer_ipv4
    - matches: [question $adguard_rules]
      exec: goto adguard_reject
- tag: forward_local
  type: forward
  args:
    upstreams:
      - {tag: local, addr: 'udp://192.0.2.53'}
      - {tag: local, addr: 'tls://dns.example:853'}
      - {addr: 'https://user:secret@dns.example/dns-query?token=secret#token'}
- {tag: server, type: udp_server, args: {entry: sequence_main, listen: ':53'}}
`

func oxRankingConfigForTest(t *testing.T, yamlText string) oxRankingConfig {
	t.Helper()
	var plugins []interface{}
	if err := yaml.Unmarshal([]byte(yamlText), &plugins); err != nil {
		t.Fatal(err)
	}
	return oxMonitorRankingConfig(plugins)
}

func TestOxiDNSRankingsUseCoreAggregatesAndConfirmedBlockPath(t *testing.T) {
	var cfg *Config
	var calls []string
	var mu sync.Mutex
	core := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			t.Error("statistics must never mutate the core")
		}
		username, password, ok := r.BasicAuth()
		if !ok || username != "monitor-user" || password != "core-secret" || r.Header.Get("Cookie") != "" {
			t.Error("invalid authentication boundary")
		}
		mu.Lock()
		calls = append(calls, r.URL.RequestURI())
		mu.Unlock()
		if r.URL.Path == "/api/health" {
			fmt.Fprint(w, oxMonitorTestHealth)
			return
		}
		if r.URL.Path == "/api/reload/status" {
			oxRankingTestRuntime(t, w, cfg)
			return
		}
		if r.URL.Query().Get("limit") != "10" || r.URL.Query().Get("since_ms") != "100" || r.URL.Query().Get("until_ms") != "200" || r.URL.Query().Has("recorder") {
			t.Errorf("incorrect aggregate range: %s", r.URL)
		}
		switch r.URL.Path {
		case "/api/plugins/records/stats/top_clients":
			fmt.Fprint(w, `{"ok":true,"sample_size":10000,"rows":[{"key":"192.0.2.1","count":4000,"share":0.4}]}`)
		case "/api/plugins/records/stats/top_qnames":
			if matcher := r.URL.Query().Get("matcher_tag"); matcher != "" {
				if matcher != "qs.match.sequence_main.3.0.question" || r.URL.Query().Get("status") != "has_response" {
					t.Errorf("unsafe block inference: %s", r.URL)
				}
				fmt.Fprint(w, `{"ok":true,"sample_size":20,"rows":[{"key":"ads.example.","count":20,"share":1}]}`)
			} else {
				fmt.Fprint(w, `{"ok":true,"sample_size":10000,"rows":[{"key":"example.com.","count":1000,"share":0.1}]}`)
			}
		default:
			t.Errorf("rankings used unexpected API: %s", r.URL)
			w.WriteHeader(500)
		}
	}))
	defer core.Close()
	cfg = oxMonitorTestConfig(t, core.URL, oxRankingTestPlugins)
	before, _ := os.ReadFile(cfg.ConfigPath)
	response := oxMonitorTestRequest(cfg, "GET", "/api/oxidns/rankings?since_ms=100&until_ms=200")
	var result struct {
		Success         bool           `json:"success"`
		Recorder        string         `json:"recorder"`
		SourceRevision  string         `json:"source_revision"`
		RuntimeRevision string         `json:"runtime_revision"`
		InstanceID      string         `json:"instance_id"`
		CollectedAt     int64          `json:"collected_at_ms"`
		Retention       int            `json:"retention_days"`
		SampleLimit     int            `json:"sample_limit"`
		Clients         oxRankingGroup `json:"clients"`
		Domains         oxRankingGroup `json:"domains"`
		Blocked         struct {
			Available bool   `json:"available"`
			Matcher   string `json:"matcher_tag"`
			oxRankingGroup
		} `json:"blocked"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &result); err != nil || response.Code != 200 || !result.Success || result.Recorder != "records" || result.SourceRevision == "" || len(result.RuntimeRevision) != 64 || result.InstanceID != "test-instance" || result.CollectedAt <= 0 || result.Retention != 3 || result.SampleLimit != 10000 {
		t.Fatalf("invalid metadata: %d %s (%v)", response.Code, response.Body.String(), err)
	}
	if !result.Clients.AtLimit || !result.Domains.AtLimit || !result.Blocked.Available || result.Blocked.AtLimit || result.Blocked.SampleSize != 20 || result.Blocked.Matcher != "qs.match.sequence_main.3.0.question" {
		t.Fatalf("incorrect sample/block semantics: %s", response.Body.String())
	}
	after, _ := os.ReadFile(cfg.ConfigPath)
	if string(before) != string(after) {
		t.Fatal("statistics changed configuration")
	}
	mu.Lock()
	defer mu.Unlock()
	if len(calls) != 7 || calls[0] != "/api/health" || calls[6] != "/api/health" {
		t.Fatalf("snapshot must be bracketed by health checks: %v", calls)
	}
}

func TestOxiDNSRankingsDoNotGuessCustomOrNegativeResponseBlocks(t *testing.T) {
	for _, test := range []struct{ name, text string }{
		{"additional condition", strings.Replace(oxRankingTestPlugins, "matches: [question $adguard_rules]", "matches: [question $adguard_rules, qtype A]", 1)},
		{"different action", strings.Replace(oxRankingTestPlugins, "exec: goto adguard_reject", "exec: goto remote_dns", 1)},
		{"overwritten response", strings.Replace(oxRankingTestPlugins, "- exec: accept", "- exec: $forward_remote", 1)},
		{"provider changed", strings.Replace(oxRankingTestPlugins, "type: adguard_rule", "type: domain_set", 1)},
		{"blackhole changed", strings.Replace(oxRankingTestPlugins, "type: black_hole", "type: forward", 1)},
		{"blackhole custom address", strings.Replace(oxRankingTestPlugins, "args: {mode: nxdomain}", "args: {mode: custom, ips: [192.0.2.1]}", 1)},
		{"blackhole unknown arguments", strings.Replace(oxRankingTestPlugins, "args: {mode: nxdomain}", "args: {mode: nxdomain, unknown: true}", 1)},
		{"unknown wrapper", strings.Replace(oxRankingTestPlugins, "exec: prefer_ipv4", "exec: $custom_wrapper", 1)},
		{"late recorder", strings.Replace(oxRankingTestPlugins, "- exec: $records\n", "- exec: $another\n", 1)},
		{"duplicate sequence", oxRankingTestPlugins + "- {tag: sequence_main, type: sequence, args: [{exec: accept}]}\n"},
		{"outer wrapper", strings.Replace(oxRankingTestPlugins, "entry: sequence_main", "entry: wrapper", 1) + "- {tag: wrapper, type: sequence, args: [{exec: 'jump sequence_main'}, {exec: '$forward_local'}]}\n"},
		{"alternate wrapped entry", oxRankingTestPlugins + "- {tag: wrapper, type: sequence, args: [{exec: 'jump sequence_main'}, {exec: '$forward_local'}]}\n"},
		{"unknown caller", oxRankingTestPlugins + "- {tag: custom, type: custom, args: {child: sequence_main}}\n"},
		{"no direct DNS listener", strings.Replace(oxRankingTestPlugins, "entry: sequence_main", "entry: other", 1)},
	} {
		t.Run(test.name, func(t *testing.T) {
			if result := oxRankingConfigForTest(t, test.text); len(result.blocked) != 0 {
				t.Fatalf("guessed custom block: %#v", result.blocked)
			}
		})
	}
	result := oxRankingConfigForTest(t, oxRankingTestPlugins)
	if result.blocked["records"] != "qs.match.sequence_main.3.0.question" {
		t.Fatal("generated AdGuard path was not recognized")
	}
	// Blocked query types precede AdGuard; current and earlier drafts differ in action.
	for _, action := range []string{"black_hole nodata short_circuit=true", "reject 0"} {
		text := strings.Replace(oxRankingTestPlugins, "    - exec: prefer_ipv4\n",
			"    - exec: prefer_ipv4\n    - matches: [qtype 65 255]\n      exec: "+action+"\n", 1)
		if result := oxRankingConfigForTest(t, text); result.blocked["records"] != "qs.match.sequence_main.4.0.question" {
			t.Fatalf("%s blocked qtypes hid the AdGuard path: %#v", action, result.blocked)
		}
	}
}

func TestOxiDNSRankingsUpstreamIdentityAndAddressRedaction(t *testing.T) {
	result := oxRankingConfigForTest(t, oxRankingTestPlugins)
	want := []oxUpstreamLabel{
		{"forward_local", "local", "udp://192.0.2.53"},
		{"forward_local", "local#1", "tls://dns.example:853"},
		{"forward_local", "https://user:secret@dns.example/dns-query?token=secret#token", "https://dns.example/dns-query"},
	}
	if !reflect.DeepEqual(result.upstreams, want) {
		t.Fatalf("incorrect metric identity/display mapping: %#v", result.upstreams)
	}
	duplicateAddress := `- tag: f
  type: forward
  args:
    upstreams:
      - {addr: 1.1.1.1}
      - {addr: 1.1.1.1}
      - {addr: 1.1.1.1}
`
	labels := oxRankingConfigForTest(t, duplicateAddress).upstreams
	if len(labels) != 3 || labels[1].Upstream != "1.1.1.1#1" || labels[2].Upstream != "1.1.1.1#2" {
		t.Fatalf("duplicate unnamed upstream mapping: %#v", labels)
	}
}

func TestOxiDNSRankingsValidationAndEmptyResponses(t *testing.T) {
	for _, body := range []string{
		`{"ok":true,"sample_size":null,"rows":[]}`,
		`{"ok":true,"sample_size":10001,"rows":[]}`,
		`{"ok":true,"sample_size":1,"rows":null}`,
		`{"ok":false,"sample_size":0,"rows":[],"message":"private-database"}`,
		`{"ok":true,"sample_size":0,"rows":[{"key":"x.","count":1}]}`,
		`{"ok":true,"sample_size":1,"rows":[{"key":"x.","count":0}]}`,
		`{"ok":true,"sample_size":1,"rows":[{"key":"x.","count":1},{"key":"x.","count":1}]}`,
		`{"ok":true,"sample_size":1,"rows":[{"key":"x.\nsecret","count":1}]}`,
	} {
		if _, err := oxDecodeRanking([]byte(body), false); err == nil {
			t.Errorf("accepted malformed ranking: %s", body)
		}
	}
	if _, err := oxDecodeRanking([]byte(`{"ok":true,"sample_size":1,"rows":[{"key":"192.0.2.1","count":1},{"key":"192.0.2.2","count":1}]}`), true); err == nil {
		t.Fatal("client total exceeded recorded query count")
	}
	if result, err := oxDecodeRanking([]byte(`{"ok":true,"sample_size":0,"rows":[]}`), true); err != nil || result.Rows == nil || result.AtLimit {
		t.Fatalf("empty ranking: %#v %v", result, err)
	}
	if result, err := oxDecodeRanking([]byte(`{"ok":true,"sample_size":2,"rows":[{"key":"multi.example.","count":3,"share":0}]}`), false); err != nil || result.Rows[0].Share != 1.5 {
		t.Fatalf("multi-question occurrence counts must be preserved: %#v %v", result, err)
	}
}

func TestOxiDNSRankingsRejectInvalidRequestsBeforeCore(t *testing.T) {
	var calls atomic.Int32
	core := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1) }))
	defer core.Close()
	cfg := oxMonitorTestConfig(t, core.URL, oxRankingTestPlugins)
	for _, query := range []string{"", "recorder=records", "since_ms=-1", "since_ms=9223372036854775808", "since_ms=1&since_ms=2", "since_ms=3&until_ms=2", "since_ms=1&limit=10000", "since_ms=1&matcher_tag=ads", "since_ms=1&recorder=../flush", "since_ms=1&url=http://evil.test", "since_ms=1&qname=abc"} {
		oxMonitorTestError(t, oxMonitorTestRequest(cfg, "GET", "/api/oxidns/rankings?"+query), 400, "INVALID_QUERY")
	}
	for _, method := range []string{"POST", "DELETE", "PUT"} {
		oxMonitorTestError(t, oxMonitorTestRequest(cfg, method, "/api/oxidns/rankings?since_ms=1"), 405, "METHOD_NOT_ALLOWED")
	}
	if calls.Load() != 0 {
		t.Fatal("invalid ranking request contacted core")
	}
}

func TestOxiDNSRankingsDefaultRecorderAndUnavailableBlocks(t *testing.T) {
	var cfg *Config
	core := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/health" {
			fmt.Fprint(w, oxMonitorTestHealth)
			return
		}
		if r.URL.Path == "/api/reload/status" {
			oxRankingTestRuntime(t, w, cfg)
			return
		}
		if !strings.HasPrefix(r.URL.Path, "/api/plugins/a/stats/") || r.URL.Query().Get("until_ms") == "" || r.URL.Query().Has("matcher_tag") {
			t.Errorf("unexpected core call: %s", r.URL)
		}
		fmt.Fprint(w, `{"ok":true,"sample_size":0,"rows":[]}`)
	}))
	defer core.Close()
	cfg = oxMonitorTestConfig(t, core.URL, "- {tag: z, type: query_recorder}\n- {tag: a, type: query_recorder}\n")
	response := oxMonitorTestRequest(cfg, "GET", "/api/oxidns/rankings?since_ms=1")
	if response.Code != 200 || !strings.Contains(response.Body.String(), `"recorder":"a"`) || !strings.Contains(response.Body.String(), `"available":false`) || !strings.Contains(response.Body.String(), `"retention_days":7`) {
		t.Fatalf("unexpected defaults: %d %s", response.Code, response.Body.String())
	}
	// Existing query selection still requires the user to choose an instance.
	oxMonitorTestError(t, oxMonitorTestRequest(cfg, "GET", "/api/oxidns/queries"), 400, "INVALID_QUERY")
}

func TestOxiDNSMonitorRejectsSnapshotAcrossRestartOrConfigChange(t *testing.T) {
	for _, endpoint := range []string{"/api/oxidns/overview", "/api/oxidns/rankings?since_ms=1"} {
		for _, configChange := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/config=%v", endpoint, configChange), func(t *testing.T) {
				var health atomic.Int32
				var cfg *Config
				core := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					if r.URL.Path == "/api/reload/status" {
						oxRankingTestRuntime(t, w, cfg)
						return
					}
					if r.URL.Path == "/api/health" {
						if health.Add(1) == 2 {
							if configChange {
								file, _ := os.OpenFile(cfg.ConfigPath, os.O_APPEND|os.O_WRONLY, 0600)
								_, _ = file.WriteString("# changed\n")
								_ = file.Close()
							} else {
								fmt.Fprint(w, strings.ReplaceAll(oxMonitorTestHealth, "test-instance", "restarted"))
								return
							}
						}
						fmt.Fprint(w, oxMonitorTestHealth)
						return
					}
					if r.URL.Path == "/api/metrics" {
						fmt.Fprint(w, "query_total 5\n")
						return
					}
					fmt.Fprint(w, `{"ok":true,"sample_size":0,"rows":[]}`)
				}))
				defer core.Close()
				cfg = oxMonitorTestConfig(t, core.URL, "- {tag: records, type: query_recorder}\n")
				oxMonitorTestError(t, oxMonitorTestRequest(cfg, "GET", endpoint), 409, "SNAPSHOT_CHANGED")
			})
		}
	}
}

func oxRankingTestRuntime(t *testing.T, w http.ResponseWriter, cfg *Config) {
	t.Helper()
	data, err := os.ReadFile(cfg.ConfigPath)
	if err != nil {
		t.Error(err)
	}
	fmt.Fprintf(w, `{"status":"idle","pending":false,"in_progress":false,"running_version":%q,"last_started_ms":null,"last_completed_ms":null,"last_success_ms":null}`, sha256Hex(data))
}

func TestOxiDNSMonitorRequiresAppliedAndStableRunningConfiguration(t *testing.T) {
	for _, endpoint := range []string{"/api/oxidns/overview", "/api/oxidns/rankings?since_ms=1"} {
		for _, scenario := range []string{"unapplied", "pending", "in_progress", "same_file_reload", "missing_runtime_fields"} {
			t.Run(endpoint+"/"+scenario, func(t *testing.T) {
				var cfg *Config
				var statusCalls atomic.Int32
				core := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					if r.Method != http.MethodGet {
						t.Error("monitoring mutated core")
					}
					switch r.URL.Path {
					case "/api/health":
						fmt.Fprint(w, oxMonitorTestHealth)
					case "/api/reload/status":
						count := statusCalls.Add(1)
						data, _ := os.ReadFile(cfg.ConfigPath)
						version := sha256Hex(data)
						state, pending, running, times := "idle", "false", "false", "null"
						switch scenario {
						case "unapplied":
							version = strings.Repeat("0", 64)
						case "pending":
							state, pending = "pending", "true"
						case "in_progress":
							state, running = "in_progress", "true"
						case "same_file_reload":
							if count > 1 {
								state, times = "ok", "123456"
							}
						case "missing_runtime_fields":
							fmt.Fprintf(w, `{"status":"idle","pending":false,"in_progress":false,"running_version":%q}`, version)
							return
						}
						fmt.Fprintf(w, `{"status":%q,"pending":%s,"in_progress":%s,"running_version":%q,"last_started_ms":%s,"last_completed_ms":%s,"last_success_ms":%s}`, state, pending, running, version, times, times, times)
					case "/api/metrics":
						fmt.Fprint(w, "server_request_total{plugin_tag=\"server\",protocol=\"udp\"} 3\n")
					default:
						fmt.Fprint(w, `{"ok":true,"sample_size":0,"rows":[]}`)
					}
				}))
				defer core.Close()
				cfg = oxMonitorTestConfig(t, core.URL, "- {tag: records, type: query_recorder}\n")
				status, code := 409, "SNAPSHOT_CHANGED"
				if scenario == "missing_runtime_fields" {
					status, code = 502, "INVALID_RESPONSE"
				}
				oxMonitorTestError(t, oxMonitorTestRequest(cfg, "GET", endpoint), status, code)
			})
		}
	}
}

func TestOxiDNSMonitorRuntimeRevisionChangesAfterSameFileReload(t *testing.T) {
	var cfg *Config
	var reloaded atomic.Bool
	core := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/health":
			fmt.Fprint(w, oxMonitorTestHealth)
		case "/api/reload/status":
			data, _ := os.ReadFile(cfg.ConfigPath)
			times := "null"
			if reloaded.Load() {
				times = "123456"
			}
			fmt.Fprintf(w, `{"status":"idle","pending":false,"in_progress":false,"running_version":%q,"last_started_ms":%s,"last_completed_ms":%s,"last_success_ms":%s}`, sha256Hex(data), times, times, times)
		case "/api/metrics":
			fmt.Fprint(w, "server_request_total{plugin_tag=\"server\",protocol=\"udp\"} 3\n")
		}
	}))
	defer core.Close()
	cfg = oxMonitorTestConfig(t, core.URL, "- {tag: records, type: query_recorder}\n")
	read := func() map[string]interface{} {
		response := oxMonitorTestRequest(cfg, "GET", "/api/oxidns/overview")
		var body map[string]interface{}
		if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil || response.Code != 200 {
			t.Fatalf("overview: %d %s", response.Code, response.Body.String())
		}
		return body
	}
	before := read()
	reloaded.Store(true)
	after := read()
	if before["source_revision"] != after["source_revision"] || before["runtime_revision"] == after["runtime_revision"] {
		t.Fatal("same-file core reload did not change runtime identity")
	}
}
