// SPDX-License-Identifier: GPL-3.0-only
package main

import (
	"bytes"
	"context"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/metacubex/mihomo/adapter"
)

// URLTest has a global option; only the reference implementation in these
// differential tests changes it, under a lock. Production probes use no globals.
var coreTestMu sync.Mutex

func coreLatency(t *testing.T, node map[string]any, target string, unified bool, timeout time.Duration) (uint16, error) {
	t.Helper()
	coreTestMu.Lock()
	defer coreTestMu.Unlock()
	previous := adapter.UnifiedDelay.Load()
	adapter.UnifiedDelay.Store(unified)
	defer adapter.UnifiedDelay.Store(previous)
	proxy, err := adapter.ParseProxy(node)
	if err != nil {
		t.Fatal(err)
	}
	defer proxy.Close()
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	delay, err := proxy.URLTest(ctx, target, nil)
	// The controller checks the deadline even if URLTest falls back to its
	// first response after the second request runs out of time.
	if ctx.Err() != nil {
		return 0, ctx.Err()
	}
	return delay, err
}

func proxyServer(t *testing.T, destination string, delay time.Duration) (map[string]any, *atomic.Int32) {
	t.Helper()
	connections := &atomic.Int32{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodConnect {
			t.Error("expected proxy CONNECT")
			w.WriteHeader(400)
			return
		}
		if r.Header.Get("Proxy-Authorization") != "Basic "+base64.StdEncoding.EncodeToString([]byte("user:secret")) {
			w.WriteHeader(407)
			return
		}
		connections.Add(1)
		upstream, err := net.Dial("tcp", destination)
		if err != nil {
			w.WriteHeader(502)
			return
		}
		defer upstream.Close()
		time.Sleep(delay)
		conn, rw, err := w.(http.Hijacker).Hijack()
		if err != nil {
			return
		}
		defer conn.Close()
		_, _ = rw.WriteString("HTTP/1.1 200 Connection Established\r\n\r\n")
		_ = rw.Flush()
		go func() { _, _ = io.Copy(upstream, rw); _ = upstream.Close() }()
		_, _ = io.Copy(conn, upstream)
	}))
	t.Cleanup(server.Close)
	u, _ := url.Parse(server.URL)
	host, port, _ := net.SplitHostPort(u.Host)
	portNumber, _ := strconv.Atoi(port)
	return map[string]any{"name": "test", "type": "http", "server": host, "port": portNumber,
		"username": "user", "password": "secret"}, connections
}

func targetServer(t *testing.T, status int) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodHead {
			t.Error("expected HEAD")
		}
		time.Sleep(20 * time.Millisecond)
		w.Header().Set("Location", "http://must-not-follow.invalid/")
		w.WriteHeader(status)
	}))
	t.Cleanup(server.Close)
	return server
}

func TestProxyProtocolAndUnifiedDelay(t *testing.T) {
	target := targetServer(t, 204)
	node, connections := proxyServer(t, target.Listener.Addr().String(), 120*time.Millisecond)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	// This host cannot resolve locally: success proves the request used the proxy.
	result := measure(ctx, node, "http://only-via-proxy.invalid/generate_204", true)
	if result.Error != "" || result.Latency == nil {
		t.Fatalf("%+v", result)
	}
	if *result.Latency < 15 || *result.FirstLatency < *result.Latency+90 {
		t.Fatalf("expected target response time and separate warm-up time: %+v", result)
	}
	if connections.Load() != 1 {
		t.Fatalf("HEAD requests did not reuse the proxy connection: %d", connections.Load())
	}
}

func TestConcurrentMeasurementsKeepDelayModeLocal(t *testing.T) {
	var firstCalls, unifiedCalls atomic.Int32
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/first" {
			firstCalls.Add(1)
		} else {
			unifiedCalls.Add(1)
		}
		time.Sleep(20 * time.Millisecond)
		w.WriteHeader(204)
	}))
	defer target.Close()
	node, _ := proxyServer(t, target.Listener.Addr().String(), 120*time.Millisecond)
	var results [2]result
	var wg sync.WaitGroup
	for i, path := range []string{"first", "unified"} {
		wg.Go(func() {
			ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
			defer cancel()
			results[i] = measure(ctx, node, "http://only-via-proxy.invalid/"+path, i == 1)
		})
	}
	wg.Wait()
	for _, result := range results {
		if result.Latency == nil || result.FirstLatency == nil || *result.FirstLatency < 120 {
			t.Fatalf("proxy handshake was not included: %+v", result)
		}
	}
	if *results[0].Latency != *results[0].FirstLatency || *results[1].FirstLatency < *results[1].Latency+90 {
		t.Fatalf("delay modes interfered: first=%+v, unified=%+v", results[0], results[1])
	}
	if firstCalls.Load() != 1 || unifiedCalls.Load() != 2 {
		t.Fatalf("first HEADs=%d, unified HEADs=%d", firstCalls.Load(), unifiedCalls.Load())
	}
}

func TestWrongCredentialsCannotPass(t *testing.T) {
	target := targetServer(t, 204)
	node, connections := proxyServer(t, target.Listener.Addr().String(), 0)
	node["password"] = "wrong-password-that-must-not-leak"
	result := measure(context.Background(), node, "http://only-via-proxy.invalid/", true)
	if result.Latency != nil || result.ErrorCode != "auth" || connections.Load() != 0 {
		t.Fatalf("%+v", result)
	}
	encoded, _ := json.Marshal(result)
	if bytes.Contains(encoded, []byte("wrong-password")) {
		t.Fatal("credential leaked")
	}
}

func TestOpenTCPPortIsNotAWorkingProxy(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	go func() {
		conn, err := listener.Accept()
		if err == nil {
			_ = conn.Close()
		}
	}()
	address := listener.Addr().(*net.TCPAddr)
	node := map[string]any{"name": "TCP only", "type": "ss", "server": "127.0.0.1", "port": address.Port,
		"cipher": "aes-128-gcm", "password": "password"}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	result := measure(ctx, node, "https://only-via-proxy.invalid/", true)
	if result.Latency != nil || result.Error == "" {
		t.Fatalf("TCP-only node passed: %+v", result)
	}
}

func TestHTTPResponsesMatchMihomoLatency(t *testing.T) {
	for _, status := range []int{200, 204, 302, 500} {
		t.Run(strconv.Itoa(status), func(t *testing.T) {
			target := targetServer(t, status)
			node, _ := proxyServer(t, target.Listener.Addr().String(), 0)
			result := measure(context.Background(), node, "http://only-via-proxy.invalid/", true)
			coreDelay, coreErr := coreLatency(t, node, "http://only-via-proxy.invalid/", true, 2*time.Second)
			if coreErr != nil || coreDelay == 0 || result.Latency == nil || result.Error != "" {
				t.Fatalf("worker=%+v; core=%d, %v", result, coreDelay, coreErr)
			}
		})
	}
}

func TestSecondRequestMatchesMihomo(t *testing.T) {
	for _, behavior := range []string{"http_response", "connection_closed", "timeout"} {
		t.Run(behavior, func(t *testing.T) {
			var calls atomic.Int32
			target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				time.Sleep(20 * time.Millisecond)
				if calls.Add(1) == 1 {
					w.WriteHeader(204)
					return
				}
				switch behavior {
				case "http_response":
					w.WriteHeader(503)
				case "connection_closed":
					conn, _, err := w.(http.Hijacker).Hijack()
					if err == nil {
						_ = conn.Close()
					}
				case "timeout":
					<-r.Context().Done()
				}
			}))
			defer target.Close()
			node, _ := proxyServer(t, target.Listener.Addr().String(), 0)
			ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
			defer cancel()
			result := measure(ctx, node, "http://only-via-proxy.invalid/", true)
			calls.Store(0)
			coreDelay, coreErr := coreLatency(t, node, "http://only-via-proxy.invalid/", true, 200*time.Millisecond)
			if behavior == "timeout" {
				if result.ErrorCode != "timeout" || result.Latency != nil || coreErr == nil {
					t.Fatalf("worker=%+v; core=%d, %v", result, coreDelay, coreErr)
				}
				return
			}
			if result.Latency == nil || coreErr != nil || coreDelay == 0 || result.FirstLatency == nil {
				t.Fatalf("worker=%+v; core=%d, %v", result, coreDelay, coreErr)
			}
			if behavior == "connection_closed" && *result.Latency < *result.FirstLatency+15 {
				t.Fatalf("fallback must include the failed second attempt: %+v", result)
			}
			if calls.Load() < 2 {
				t.Fatalf("%+v", result)
			}
		})
	}
}

func TestFirstRequestFailureCannotPass(t *testing.T) {
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, _, err := w.(http.Hijacker).Hijack()
		if err == nil {
			_ = conn.Close()
		}
	}))
	defer target.Close()
	node, _ := proxyServer(t, target.Listener.Addr().String(), 0)
	result := measure(context.Background(), node, "http://only-via-proxy.invalid/", true)
	_, coreErr := coreLatency(t, node, "http://only-via-proxy.invalid/", true, time.Second)
	if result.Latency != nil || result.ErrorCode != "connection" || coreErr == nil {
		t.Fatalf("%+v", result)
	}
}

func TestTimeoutCoversProxyHandshake(t *testing.T) {
	target := targetServer(t, 204)
	node, _ := proxyServer(t, target.Listener.Addr().String(), 250*time.Millisecond)
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	result := measure(ctx, node, "http://only-via-proxy.invalid/", true)
	if result.Latency != nil || result.ErrorCode != "timeout" {
		t.Fatalf("%+v", result)
	}
}

func TestUnsupportedAndChainFailClosed(t *testing.T) {
	for _, node := range []map[string]any{
		{"name": "direct", "type": "direct"},
		{"name": "future", "type": "future-protocol"},
		{"name": "chain", "type": "ss", "dialer-proxy": "another-node"},
	} {
		result := measure(context.Background(), node, "https://example.invalid/", true)
		if result.Latency != nil || result.Error == "" {
			t.Fatalf("%+v", result)
		}
	}
}

func TestBatchBoundedAndIsolatesFailures(t *testing.T) {
	input := request{URL: "https://example.invalid/", TimeoutMS: 500, Nodes: []map[string]any{
		{"name": "bad", "type": "ss"}, {"name": "unknown", "type": "unknown"},
	}}
	results, err := execute(input)
	if err != nil || len(results) != 2 || results[0].Error == "" || results[1].Error == "" {
		t.Fatalf("%+v, %v", results, err)
	}
	input.Nodes = make([]map[string]any, 21)
	if _, err := execute(input); err == nil {
		t.Fatal("unbounded batch accepted")
	}
}

func TestBatchRunsTwentyConcurrentlyAndPreservesFirstConnectionMode(t *testing.T) {
	var active, peak, calls atomic.Int32
	started := make(chan struct{}, maxNodes)
	release := make(chan struct{})
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		now := active.Add(1)
		defer active.Add(-1)
		for old := peak.Load(); now > old; old = peak.Load() {
			if peak.CompareAndSwap(old, now) {
				break
			}
		}
		calls.Add(1)
		started <- struct{}{}
		select {
		case <-release:
		case <-r.Context().Done():
		}
		w.WriteHeader(204)
	}))
	defer target.Close()
	node, _ := proxyServer(t, target.Listener.Addr().String(), 0)
	nodes := make([]map[string]any, maxNodes)
	for i := range nodes {
		nodes[i] = node
	}
	finished := make(chan []result, 1)
	go func() {
		results, _ := execute(request{URL: "http://only-via-proxy.invalid/", TimeoutMS: 2000, Nodes: nodes})
		finished <- results
	}()
	deadline := time.NewTimer(time.Second)
	defer deadline.Stop()
	for range maxNodes {
		select {
		case <-started:
		case <-deadline.C:
			close(release)
			<-finished
			t.Fatal("the whole batch did not start together")
		}
	}
	close(release)
	results := <-finished
	if len(results) != maxNodes {
		t.Fatalf("results=%d", len(results))
	}
	for _, result := range results {
		if result.Latency == nil || *result.Latency != *result.FirstLatency {
			t.Fatalf("%+v", result)
		}
	}
	if calls.Load() != maxNodes || peak.Load() != maxNodes {
		t.Fatalf("calls=%d, peak=%d", calls.Load(), peak.Load())
	}
}

func TestTimeoutBatchFinishesInOneRound(t *testing.T) {
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		<-r.Context().Done()
	}))
	defer target.Close()
	node, _ := proxyServer(t, target.Listener.Addr().String(), 0)
	nodes := make([]map[string]any, maxNodes)
	for i := range nodes {
		nodes[i] = node
	}
	started := time.Now()
	results, err := execute(request{URL: "http://only-via-proxy.invalid/", TimeoutMS: 500, UnifiedDelay: true, Nodes: nodes})
	elapsed := time.Since(started)
	if err != nil {
		t.Fatal(err)
	}
	for _, result := range results {
		if result.ErrorCode != "timeout" || result.Latency != nil {
			t.Fatalf("%+v", result)
		}
	}
	t.Logf("20 timeout nodes completed in %s (previous five-worker schedule requires four 500ms rounds)", elapsed.Round(time.Millisecond))
	if elapsed >= 1500*time.Millisecond {
		t.Fatalf("batch still waited for serial timeout rounds: %s", elapsed)
	}
}

// Run the actual JSON worker in a fresh process so its system trust roots can be
// set to this test CA, without weakening certificate verification in production.
func TestHTTPSWorkerProcess(t *testing.T) {
	if os.Getenv("CONFIGFLOW_PROBE_TEST_CHILD") == "1" {
		if file := os.Getenv("CONFIGFLOW_PROBE_TEST_CA"); file != "" {
			cert, err := os.ReadFile(file)
			if err != nil {
				t.Fatal(err)
			}
			roots := x509.NewCertPool()
			if !roots.AppendCertsFromPEM(cert) {
				t.Fatal("invalid test CA")
			}
			x509.SetFallbackRoots(roots)
		}
		main()
		os.Exit(0)
	}
	target := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodHead {
			t.Error("expected HEAD")
		}
		w.WriteHeader(204)
	}))
	defer target.Close()
	node, connections := proxyServer(t, target.Listener.Addr().String(), 0)
	cert := filepath.Join(t.TempDir(), "ca.pem")
	if err := os.WriteFile(cert, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: target.Certificate().Raw}), 0600); err != nil {
		t.Fatal(err)
	}
	for _, trusted := range []bool{true, false} {
		cmd := exec.Command(os.Args[0], "-test.run=^TestHTTPSWorkerProcess$")
		cmd.Env = append(os.Environ(), "CONFIGFLOW_PROBE_TEST_CHILD=1")
		if trusted {
			cmd.Env = append(cmd.Env, "CONFIGFLOW_PROBE_TEST_CA="+cert, "GODEBUG=x509usefallbackroots=1")
		}
		body, _ := json.Marshal(request{URL: target.URL, TimeoutMS: 2000, UnifiedDelay: true, Nodes: []map[string]any{node}})
		cmd.Stdin = bytes.NewReader(body)
		output, err := cmd.Output()
		if err != nil {
			t.Fatal(err)
		}
		var response struct {
			Version int      `json:"version"`
			Results []result `json:"results"`
		}
		if err := json.Unmarshal(output, &response); err != nil {
			t.Fatalf("%s: %v", output, err)
		}
		if response.Version != 2 || len(response.Results) != 1 {
			t.Fatal(string(output))
		}
		item := response.Results[0]
		if trusted && (item.Latency == nil || item.Error != "") {
			t.Fatalf("%+v", item)
		}
		if !trusted && (item.Latency != nil || item.ErrorCode != "tls") {
			t.Fatalf("%+v", item)
		}
		if strings.Contains(string(output), "secret") {
			t.Fatal("credential leaked")
		}
	}
	if connections.Load() != 2 {
		t.Fatalf("unexpected proxy connections: %d", connections.Load())
	}
}
