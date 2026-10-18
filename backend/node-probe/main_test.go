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
	"sync/atomic"
	"testing"
	"time"
)

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

func TestBadHTTPStatusAndRedirectFail(t *testing.T) {
	for _, status := range []int{200, 302, 500} {
		t.Run(strconv.Itoa(status), func(t *testing.T) {
			target := targetServer(t, status)
			node, _ := proxyServer(t, target.Listener.Addr().String(), 0)
			result := measure(context.Background(), node, "http://only-via-proxy.invalid/", true)
			if result.Latency != nil || result.ErrorCode != "http_status" {
				t.Fatalf("%+v", result)
			}
		})
	}
}

func TestSecondRequestFailureIsNotWarmupSuccess(t *testing.T) {
	var calls atomic.Int32
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if calls.Add(1) == 1 {
			w.WriteHeader(204)
		} else {
			w.WriteHeader(503)
		}
	}))
	defer target.Close()
	node, _ := proxyServer(t, target.Listener.Addr().String(), 0)
	result := measure(context.Background(), node, "http://only-via-proxy.invalid/", true)
	if result.Latency != nil || result.ErrorCode != "http_status" {
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

func TestBatchUsesAtMostFiveRequestsAndFirstConnectionMode(t *testing.T) {
	var active, peak, calls atomic.Int32
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		now := active.Add(1)
		defer active.Add(-1)
		for old := peak.Load(); now > old; old = peak.Load() {
			if peak.CompareAndSwap(old, now) {
				break
			}
		}
		calls.Add(1)
		time.Sleep(30 * time.Millisecond)
		w.WriteHeader(204)
	}))
	defer target.Close()
	node, _ := proxyServer(t, target.Listener.Addr().String(), 0)
	nodes := make([]map[string]any, 11)
	for i := range nodes {
		nodes[i] = node
	}
	results, err := execute(request{URL: "http://only-via-proxy.invalid/", TimeoutMS: 1000, Nodes: nodes})
	if err != nil {
		t.Fatal(err)
	}
	for _, result := range results {
		if result.Latency == nil || *result.Latency != *result.FirstLatency {
			t.Fatalf("%+v", result)
		}
	}
	if calls.Load() != 11 || peak.Load() > 5 {
		t.Fatalf("calls=%d, peak=%d", calls.Load(), peak.Load())
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
			Results []result `json:"results"`
		}
		if err := json.Unmarshal(output, &response); err != nil {
			t.Fatalf("%s: %v", output, err)
		}
		if len(response.Results) != 1 {
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
