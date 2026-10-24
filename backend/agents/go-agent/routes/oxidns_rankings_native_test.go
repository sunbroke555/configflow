package routes

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// Optional real-core regression. All DNS/API listeners and query data belong
// to this test's private loopback instance, never an installed system service.
func TestOxiDNSNativeDashboardRankings(t *testing.T) {
	binaryPath := os.Getenv("OXIDNS_TEST_BINARY")
	if binaryPath == "" {
		t.Skip("set OXIDNS_TEST_BINARY to validate the actual OxiDNS stats API")
	}
	root := t.TempDir()
	api, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	apiAddress := api.Addr().String()
	api.Close()
	dns, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	dnsAddress := dns.LocalAddr().String()
	dns.Close()
	upstream, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer upstream.Close()
	var upstreamQueries atomic.Uint64
	upstreamDone := make(chan struct{})
	go func() {
		defer close(upstreamDone)
		packet := make([]byte, 4096)
		for {
			n, peer, err := upstream.ReadFrom(packet)
			if err != nil {
				return
			}
			if n < 12 {
				continue
			}
			upstreamQueries.Add(1)
			packet[2], packet[3] = 0x81, 0x80
			_, _ = upstream.WriteTo(packet[:n], peer)
		}
	}()
	defer func() { _ = upstream.Close(); <-upstreamDone }()
	cfg := &Config{ServiceType: "oxidns", Token: "native-agent", ConfigPath: filepath.Join(root, "config.yaml")}
	content := fmt.Sprintf(`runtime: {worker_threads: 1}
api:
  http:
    listen: %q
plugins:
  - {tag: records, type: query_recorder, args: {path: queries.sqlite, flush_interval_ms: 20, retention_days: '${CONFIGFLOW_TEST_DNS_RETENTION}'}}
  - {tag: adguard_rules, type: adguard_rule, args: {files: [ads.txt]}}
  - {tag: adguard_block, type: black_hole, args: {mode: nxdomain}}
  - {tag: adguard_reject, type: sequence, args: [{exec: '$adguard_block'}, {exec: accept}]}
  - {tag: forward_local, type: forward, args: {upstreams: [{tag: test_upstream, addr: %q}]}}
  - {tag: rules_domain_1, type: domain_set, args: {exps: [domain:refused.example]}}
  - {tag: rules_domain_2, type: domain_set, args: {exps: [domain:hole.example]}}
  - {tag: cf_dns_blackhole, type: black_hole, args: {mode: 'null', short_circuit: true}}
  - tag: sequence_main
    type: sequence
    args:
      - exec: metrics_collector metrics
      - exec: $records
      - matches: [question $adguard_rules]
        exec: goto adguard_reject
      - {matches: [qname $rules_domain_1], exec: reject}
      - {matches: [qname $rules_domain_2], exec: $cf_dns_blackhole}
      - exec: $forward_local
      - exec: accept
  - {tag: server, type: udp_server, args: {listen: %q, entry: sequence_main}}
`, apiAddress, "udp://"+upstream.LocalAddr().String(), dnsAddress)
	if err := os.WriteFile(cfg.ConfigPath, []byte(content), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "ads.txt"), []byte("||ads.example^\n"), 0600); err != nil {
		t.Fatal(err)
	}
	logFile, err := os.Create(filepath.Join(root, "core.log"))
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cmd := exec.CommandContext(ctx, binaryPath, "start", "-c", cfg.ConfigPath, "-d", root)
	cmd.Env = append(os.Environ(), "CONFIGFLOW_TEST_DNS_RETENTION=7")
	cmd.Stdout, cmd.Stderr = logFile, logFile
	if err := cmd.Start(); err != nil {
		cancel()
		logFile.Close()
		t.Fatal(err)
	}
	defer func() { cancel(); _ = cmd.Wait(); _ = logFile.Close() }()
	client := &http.Client{Timeout: time.Second, Transport: &http.Transport{Proxy: nil}}
	defer client.CloseIdleConnections()
	deadline := time.Now().Add(8 * time.Second)
	for {
		resp, err := client.Get("http://" + apiAddress + "/api/health")
		if err == nil {
			resp.Body.Close()
			if resp.StatusCode == 200 {
				break
			}
		}
		if time.Now().After(deadline) {
			data, _ := os.ReadFile(logFile.Name())
			t.Fatalf("private core did not start: %s", data)
		}
		time.Sleep(30 * time.Millisecond)
	}
	start := time.Now().UnixMilli()
	query := func(name string, rcode byte) {
		packet := make([]byte, 12)
		binary.BigEndian.PutUint16(packet, 123)
		packet[2], packet[5] = 1, 1
		for _, label := range strings.Split(name, ".") {
			packet = append(packet, byte(len(label)))
			packet = append(packet, label...)
		}
		packet = append(packet, 0, 0, 1, 0, 1)
		conn, err := net.DialTimeout("udp", dnsAddress, time.Second)
		if err != nil {
			t.Fatal(err)
		}
		defer conn.Close()
		_ = conn.SetDeadline(time.Now().Add(time.Second))
		if _, err := conn.Write(packet); err != nil {
			t.Fatal(err)
		}
		response := make([]byte, 4096)
		n, err := conn.Read(response)
		if err != nil || n < 12 || response[3]&15 != rcode {
			t.Fatalf("DNS response %s: n=%d err=%v", name, n, err)
		}
	}
	for i := 0; i < 60; i++ {
		query("ads.example", 3)
	}
	for i := 0; i < 7; i++ {
		query("allowed.example", 0)
	}
	for i := 0; i < 5; i++ {
		query("refused.example", 5)
	}
	for i := 0; i < 3; i++ {
		query("hole.example", 0)
	}
	deadline = time.Now().Add(8 * time.Second)
	for {
		response := oxMonitorTestRequest(cfg, "GET", fmt.Sprintf("/api/oxidns/rankings?since_ms=%d", start))
		var body struct {
			Retention *uint64        `json:"retention_days"`
			Clients   oxRankingGroup `json:"clients"`
			Domains   oxRankingGroup `json:"domains"`
			Blocked   struct {
				Available bool `json:"available"`
				oxRankingGroup
			} `json:"blocked"`
		}
		if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil || response.Code != 200 {
			t.Fatalf("real ranking API: %d %s (%v)", response.Code, response.Body.String(), err)
		}
		if body.Clients.SampleSize == 75 {
			blocked := []oxRankingRow{{"ads.example.", 60, 60.0 / 68}, {"refused.example.", 5, 5.0 / 68}, {"hole.example.", 3, 3.0 / 68}}
			if body.Retention != nil || len(body.Clients.Rows) != 1 || body.Clients.Rows[0].Count != 75 || len(body.Domains.Rows) != 4 || body.Domains.Rows[0].Key != "ads.example." || body.Domains.Rows[0].Count != 60 || !body.Blocked.Available || body.Blocked.SampleSize != 68 || !reflect.DeepEqual(body.Blocked.Rows, blocked) {
				t.Fatalf("real aggregate semantics: %s", response.Body.String())
			}
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("recorder did not flush all queries: %s", response.Body.String())
		}
		time.Sleep(30 * time.Millisecond)
	}
	overview := oxMonitorTestRequest(cfg, "GET", "/api/oxidns/overview")
	var summary struct {
		Metrics string            `json:"metrics"`
		Runtime string            `json:"runtime_revision"`
		Labels  []oxUpstreamLabel `json:"upstream_labels"`
	}
	if err := json.Unmarshal(overview.Body.Bytes(), &summary); err != nil || overview.Code != 200 || len(summary.Runtime) != 64 || upstreamQueries.Load() != 7 || len(summary.Labels) != 1 || summary.Labels[0].Upstream != "test_upstream" {
		t.Fatalf("actual upstream/runtime contract: %d %s", overview.Code, overview.Body.String())
	}
	for _, metric := range []string{"forward_upstream_query_total", "forward_upstream_latency_count"} {
		if !strings.Contains(summary.Metrics, metric+`{plugin_tag="forward_local",upstream="test_upstream"} 7`) {
			t.Fatalf("missing actual metric %s: %s", metric, summary.Metrics)
		}
	}
	t.Log("real OxiDNS: 75 recorded requests, 60 confirmed AdGuard blocks plus 8 rule reject/blackhole blocks, 7 isolated upstream attempts; nullable retention, aggregate APIs and runtime revisions verified")
}
