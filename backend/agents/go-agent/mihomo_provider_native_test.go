package main

import (
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// MIHOMO_BINARY enables an isolated native-core regression test. All listeners,
// proxies and test destinations use loopback; no subscription or DNS is fetched.
func TestMihomoPanelNativeProviderNodeDelay(t *testing.T) {
	binary := os.Getenv("MIHOMO_BINARY")
	if binary == "" {
		t.Skip("set MIHOMO_BINARY to run the native Mihomo integration test")
	}
	binary, err := filepath.Abs(binary)
	if err != nil {
		t.Fatal(err)
	}

	destination := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/slow":
			select {
			case <-r.Context().Done():
				return
			case <-time.After(400 * time.Millisecond):
			}
		case "/disconnect":
			conn, _, hijackErr := w.(http.Hijacker).Hijack()
			if hijackErr == nil {
				conn.Close()
			}
			return
		default:
			time.Sleep(5 * time.Millisecond)
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	defer destination.Close()
	destinationHost := strings.TrimPrefix(destination.URL, "http://")
	var proxyRequests atomic.Int32
	transport := &http.Transport{}
	defer transport.CloseIdleConnections()
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Host != destinationHost {
			http.Error(w, "only the local test destination is allowed", http.StatusBadGateway)
			return
		}
		proxyRequests.Add(1)
		if r.Method == http.MethodConnect {
			upstream, dialErr := net.DialTimeout("tcp", destinationHost, time.Second)
			if dialErr != nil {
				http.Error(w, dialErr.Error(), http.StatusBadGateway)
				return
			}
			defer upstream.Close()
			client, buffered, hijackErr := w.(http.Hijacker).Hijack()
			if hijackErr != nil {
				return
			}
			defer client.Close()
			_, _ = client.Write([]byte("HTTP/1.1 200 Connection Established\r\n\r\n"))
			done := make(chan struct{})
			go func() {
				_, _ = io.Copy(upstream, buffered)
				upstream.Close()
				close(done)
			}()
			_, _ = io.Copy(client, upstream)
			client.Close()
			<-done
			return
		}
		r.RequestURI = ""
		response, requestErr := transport.RoundTrip(r)
		if requestErr != nil {
			http.Error(w, requestErr.Error(), http.StatusBadGateway)
			return
		}
		defer response.Body.Close()
		w.WriteHeader(response.StatusCode)
		_, _ = io.Copy(w, response.Body)
	}))
	defer proxy.Close()
	_, proxyPort, err := net.SplitHostPort(strings.TrimPrefix(proxy.URL, "http://"))
	if err != nil {
		t.Fatal(err)
	}
	node := func(name string) map[string]any {
		return map[string]any{"name": name, "type": "http", "server": "127.0.0.1", "port": proxyPort}
	}
	const ordinary = "普通 🇯🇵/节点 50%?name#1"
	const fileProvider = "文件订阅 🇯🇵/50%?source#1"
	const fileNode = "🇯🇵 JP-05 · AWS2/节点%?name#2"
	const inlineProvider = "内联订阅 🇺🇸/100%?source#2"
	const inlineNode = "美国 🇺🇸/节点 100%?name#3"
	const duplicateName = "同名节点"
	failedProxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "test proxy unavailable", http.StatusServiceUnavailable)
	}))
	defer failedProxy.Close()
	_, failedPort, err := net.SplitHostPort(strings.TrimPrefix(failedProxy.URL, "http://"))
	if err != nil {
		t.Fatal(err)
	}
	failedNode := node(duplicateName)
	failedNode["port"] = failedPort
	dir := t.TempDir()
	writeJSON := func(name string, value any) string {
		t.Helper()
		data, marshalErr := json.Marshal(value)
		if marshalErr != nil {
			t.Fatal(marshalErr)
		}
		path := filepath.Join(dir, name)
		if writeErr := os.WriteFile(path, data, 0600); writeErr != nil {
			t.Fatal(writeErr)
		}
		return path
	}
	writeJSON("provider.yaml", map[string]any{"proxies": []any{node(fileNode), failedNode}})
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	controller := listener.Addr().String()
	listener.Close()
	configPath := writeJSON("config.yaml", map[string]any{
		"external-controller": controller, "secret": "local-test-only", "log-level": "warning",
		"mode": "rule", "dns": map[string]any{"enable": false}, "tun": map[string]any{"enable": false},
		"geo-auto-update": false, "profile": map[string]any{"store-selected": false, "store-fake-ip": false},
		"proxies": []any{node(ordinary), node(duplicateName)},
		"proxy-providers": map[string]any{
			fileProvider:   map[string]any{"type": "file", "path": "provider.yaml", "health-check": map[string]any{"enable": false}},
			inlineProvider: map[string]any{"type": "inline", "payload": []any{node(inlineNode), node(duplicateName)}, "health-check": map[string]any{"enable": false}},
		},
		"proxy-groups": []any{
			map[string]any{"name": "TestGroup", "type": "select", "proxies": []string{ordinary}, "use": []string{fileProvider, inlineProvider}},
			map[string]any{"name": "SecondGroup", "type": "select", "proxies": []string{ordinary, duplicateName}},
		},
		"rules": []string{"MATCH,DIRECT"},
	})
	logPath := filepath.Join(dir, "mihomo.log")
	log, err := os.Create(logPath)
	if err != nil {
		t.Fatal(err)
	}
	defer log.Close()
	command := exec.Command(binary, "-d", dir, "-f", configPath)
	command.Stdout, command.Stderr = log, log
	if err := command.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() {
		_ = command.Process.Kill()
		_ = command.Wait()
		if t.Failed() {
			data, _ := os.ReadFile(logPath)
			t.Logf("native Mihomo output:\n%s", data)
		}
	}()
	handler := mihomoPanelHandler(&Config{ServiceType: "mihomo", ConfigPath: configPath})
	request := func(path string) *httptest.ResponseRecorder {
		result := httptest.NewRecorder()
		handler(result, httptest.NewRequest(http.MethodGet, "/api/mihomo"+path, nil))
		return result
	}
	deadline := time.Now().Add(5 * time.Second)
	for request("/version").Code != http.StatusOK {
		if time.Now().After(deadline) {
			t.Fatal("native Mihomo controller did not become ready")
		}
		time.Sleep(20 * time.Millisecond)
	}
	providerPath := func(provider, name string) string {
		return "/providers/proxies/" + url.PathEscape(provider) + "/" + url.PathEscape(name) + "/healthcheck"
	}
	t.Run("compatible providers repeat the same core nodes", func(t *testing.T) {
		type proxyMetadata struct {
			Name         string `json:"name"`
			ID           string `json:"id"`
			ProviderName string `json:"provider-name"`
		}
		var core struct {
			Proxies map[string]proxyMetadata `json:"proxies"`
		}
		var providers struct {
			Providers map[string]struct {
				VehicleType string          `json:"vehicleType"`
				Proxies     []proxyMetadata `json:"proxies"`
			} `json:"providers"`
		}
		if err := json.Unmarshal(request("/proxies").Body.Bytes(), &core); err != nil {
			t.Fatal(err)
		}
		if err := json.Unmarshal(request("/providers/proxies").Body.Bytes(), &providers); err != nil {
			t.Fatal(err)
		}
		if core.Proxies[ordinary].ID == "" || core.Proxies[fileNode].ID != "" || core.Proxies[inlineNode].ID != "" {
			t.Fatal("core proxy lookup must distinguish configured and provider-only nodes")
		}
		for _, name := range []string{"default", "TestGroup", "SecondGroup"} {
			provider := providers.Providers[name]
			found := false
			for _, proxy := range provider.Proxies {
				if proxy.Name == ordinary {
					found = true
					if provider.VehicleType != "Compatible" || proxy.ID != core.Proxies[ordinary].ID || proxy.ProviderName != "" {
						t.Fatalf("%s must wrap the same ordinary proxy: %+v", name, proxy)
					}
				}
			}
			if !found {
				t.Fatalf("%s omitted the shared ordinary proxy", name)
			}
		}
		for _, name := range []string{fileProvider, inlineProvider} {
			for _, proxy := range providers.Providers[name].Proxies {
				if proxy.Name == duplicateName && (proxy.ProviderName != name || proxy.ID == core.Proxies[duplicateName].ID) {
					t.Fatalf("%s must contain its own duplicate-name proxy: %+v", name, proxy)
				}
			}
		}
	})
	query := func(path string, timeout int) string {
		return "?" + url.Values{"url": {destination.URL + path}, "timeout": {fmt.Sprint(timeout)}}.Encode()
	}
	cases := []struct{ name, path string }{
		{"ordinary", "/proxies/" + url.PathEscape(ordinary) + "/delay"},
		{"file provider", providerPath(fileProvider, fileNode)},
		{"inline provider", providerPath(inlineProvider, inlineNode)},
		{"ordinary with duplicate name", "/proxies/" + url.PathEscape(duplicateName) + "/delay"},
		{"inline with duplicate name", providerPath(inlineProvider, duplicateName)},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			before := proxyRequests.Load()
			result := request(tc.path + query("/generate_204", 1000))
			var body map[string]json.RawMessage
			if result.Code != http.StatusOK || json.Unmarshal(result.Body.Bytes(), &body) != nil || body["delay"] == nil {
				t.Fatalf("delay request returned %d %s", result.Code, result.Body.String())
			}
			if proxyRequests.Load() <= before {
				t.Fatal("delay test bypassed the selected proxy")
			}
		})
	}
	t.Run("old provider lookup reproduces 404", func(t *testing.T) {
		result := request("/proxies/" + url.PathEscape(fileNode) + "/delay" + query("/generate_204", 1000))
		if result.Code != http.StatusNotFound || !strings.Contains(result.Body.String(), "Resource not found") {
			t.Fatalf("old lookup returned %d %s", result.Code, result.Body.String())
		}
	})
	t.Run("provider node does not fall back to global duplicate", func(t *testing.T) {
		result := request(providerPath(fileProvider, duplicateName) + query("/generate_204", 1000))
		if result.Code != http.StatusServiceUnavailable || !strings.Contains(result.Body.String(), "An error occurred") {
			t.Fatalf("unavailable provider node returned %d %s", result.Code, result.Body.String())
		}
	})
	t.Run("group delay still works", func(t *testing.T) {
		result := request("/group/TestGroup/delay" + query("/generate_204", 1000))
		var body map[string]int
		if result.Code != http.StatusOK || json.Unmarshal(result.Body.Bytes(), &body) != nil {
			t.Fatalf("group delay returned %d %s", result.Code, result.Body.String())
		}
		for _, name := range []string{ordinary, fileNode, inlineNode} {
			if _, ok := body[name]; !ok {
				t.Errorf("group delay omitted %q: %s", name, result.Body.String())
			}
		}
	})
	for _, tc := range []struct {
		path, message string
		status        int
	}{
		{"/slow", "Timeout", http.StatusGatewayTimeout},
		{"/disconnect", "An error occurred", http.StatusServiceUnavailable},
	} {
		t.Run("failed probe "+tc.path, func(t *testing.T) {
			result := request(providerPath(fileProvider, fileNode) + query(tc.path, 30))
			if result.Code != tc.status || !strings.Contains(result.Body.String(), tc.message) {
				t.Fatalf("failed probe returned %d %s", result.Code, result.Body.String())
			}
		})
	}
}
