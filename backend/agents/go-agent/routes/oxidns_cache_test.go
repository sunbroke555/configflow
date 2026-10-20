package routes

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
)

type oxCacheFixture struct {
	cfg     *Config
	handler http.HandlerFunc
	mu      sync.Mutex
	calls   []string
}

// Renamed caches prove discovery follows plugin types, not template tags.
const oxCacheTestPlugins = `
  - {tag: main_cache, type: cache, args: {size: 1024}}
  - {tag: probe, type: cache}
  - {tag: records, type: query_recorder}
  - {tag: forward_local, type: forward}
`

func newOxCacheFixture(t *testing.T, plugins string, status func(tag string) int) *oxCacheFixture {
	t.Helper()
	fixture := &oxCacheFixture{cfg: &Config{ServiceType: "oxidns", Token: "agent-secret", ConfigPath: filepath.Join(t.TempDir(), "config.yaml")}}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fixture.mu.Lock()
		fixture.calls = append(fixture.calls, r.Method+" "+r.URL.Path)
		fixture.mu.Unlock()
		if user, password, ok := r.BasicAuth(); !ok || user != "tester" || password != "core-secret" || r.Header.Get("Cookie") != "" {
			t.Error("core received incorrect authentication or forwarded cookie")
		}
		tag := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/api/plugins/"), "/flush")
		if r.Method != http.MethodGet || r.URL.Path != "/api/plugins/"+tag+"/flush" {
			t.Errorf("unexpected core operation: %s %s", r.Method, r.URL.Path)
		}
		code := http.StatusOK
		if status != nil {
			code = status(tag)
		}
		w.WriteHeader(code)
		fmt.Fprint(w, `{"ok":true,"secret":"core-detail"}`)
	}))
	t.Cleanup(server.Close)
	text := fmt.Sprintf("api:\n  http:\n    listen: %s\n    auth: {type: basic, username: tester, password: core-secret}\nplugins:\n%s", strings.TrimPrefix(server.URL, "http://"), plugins)
	if err := os.WriteFile(fixture.cfg.ConfigPath, []byte(text), 0600); err != nil {
		t.Fatal(err)
	}
	fixture.handler = oxidnsMonitorAuth(fixture.cfg, OxiDNSCacheHandler(fixture.cfg))
	return fixture
}

func (f *oxCacheFixture) request(method, target, body string) *httptest.ResponseRecorder {
	r := httptest.NewRequest(method, target, strings.NewReader(body))
	r.Header.Set("Authorization", "Bearer "+f.cfg.Token)
	r.Header.Set("Cookie", "do-not-forward=secret")
	w := httptest.NewRecorder()
	f.handler(w, r)
	return w
}

func (f *oxCacheFixture) coreCalls() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.calls...)
}

func TestOxiDNSCacheListsConfiguredCachePluginsByType(t *testing.T) {
	f := newOxCacheFixture(t, oxCacheTestPlugins, nil)
	w := f.request("GET", "/api/oxidns/cache", "")
	var response struct {
		Success bool     `json:"success"`
		Caches  []string `json:"caches"`
	}
	if w.Code != 200 || json.Unmarshal(w.Body.Bytes(), &response) != nil || !response.Success {
		t.Fatalf("cache list failed: %d %s", w.Code, w.Body.String())
	}
	if !reflect.DeepEqual(response.Caches, []string{"main_cache", "probe"}) {
		t.Fatalf("wrong caches: %v", response.Caches)
	}
	if len(f.coreCalls()) != 0 {
		t.Fatal("listing caches contacted the core")
	}
	f = newOxCacheFixture(t, "  - {tag: records, type: query_recorder}\n", nil)
	if w := f.request("GET", "/api/oxidns/cache", ""); w.Code != 200 || !strings.Contains(w.Body.String(), `"caches":[]`) {
		t.Fatalf("configuration without caches: %d %s", w.Code, w.Body.String())
	}
}

func TestOxiDNSCacheFlushesOnlyRequestedCaches(t *testing.T) {
	f := newOxCacheFixture(t, oxCacheTestPlugins, nil)
	w := f.request("POST", "/api/oxidns/cache/flush", `{"tags":["probe"]}`)
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"flushed":["probe"]`) {
		t.Fatalf("flush failed: %d %s", w.Code, w.Body.String())
	}
	if got := f.coreCalls(); !reflect.DeepEqual(got, []string{"GET /api/plugins/probe/flush"}) {
		t.Fatalf("flushed other caches: %v", got)
	}
	if strings.Contains(w.Body.String(), "core-detail") {
		t.Fatal("core response leaked")
	}
	w = f.request("POST", "/api/oxidns/cache/flush", `{"tags":["main_cache","probe"]}`)
	if w.Code != 200 || len(f.coreCalls()) != 3 {
		t.Fatalf("flush all failed: %d %v", w.Code, f.coreCalls())
	}
}

func TestOxiDNSCacheRejectsUnknownAndInvalidRequests(t *testing.T) {
	f := newOxCacheFixture(t, oxCacheTestPlugins, nil)
	for _, test := range []struct {
		method, target, body string
		status               int
		code                 string
	}{
		{"POST", "/api/oxidns/cache/flush", `{"tags":["records"]}`, 409, "CACHE_UNKNOWN"},
		{"POST", "/api/oxidns/cache/flush", `{"tags":["lazy_cache"]}`, 409, "CACHE_UNKNOWN"},
		{"POST", "/api/oxidns/cache/flush", `{}`, 400, "INVALID_REQUEST"},
		{"POST", "/api/oxidns/cache/flush", `{"tags":[]}`, 400, "INVALID_REQUEST"},
		{"POST", "/api/oxidns/cache/flush", `{"tags":["../reload"]}`, 400, "INVALID_REQUEST"},
		{"POST", "/api/oxidns/cache/flush", `{"tags":["probe","probe"]}`, 400, "INVALID_REQUEST"},
		{"POST", "/api/oxidns/cache/flush", `{"tags":"probe"}`, 400, "INVALID_REQUEST"},
		{"POST", "/api/oxidns/cache/flush", `{"tags":["probe"],"url":"https://evil.test"}`, 400, "INVALID_REQUEST"},
		{"POST", "/api/oxidns/cache/flush?tag=probe", `{"tags":["probe"]}`, 400, "INVALID_REQUEST"},
		{"GET", "/api/oxidns/cache/flush", ``, 405, "METHOD_NOT_ALLOWED"},
		{"POST", "/api/oxidns/cache", `{}`, 405, "METHOD_NOT_ALLOWED"},
		{"GET", "/api/oxidns/cache", `{}`, 400, "INVALID_REQUEST"},
	} {
		w := f.request(test.method, test.target, test.body)
		if w.Code != test.status || !strings.Contains(w.Body.String(), test.code) {
			t.Errorf("%s %s %s: %d %s", test.method, test.target, test.body, w.Code, w.Body.String())
		}
	}
	if len(f.coreCalls()) != 0 {
		t.Fatalf("invalid request reached core: %v", f.coreCalls())
	}
	mux := http.NewServeMux()
	RegisterRoutes(mux, f.cfg)
	for _, test := range []struct{ method, target string }{{"GET", "/api/oxidns/cache"}, {"POST", "/api/oxidns/cache/flush"}} {
		w := httptest.NewRecorder()
		mux.ServeHTTP(w, httptest.NewRequest(test.method, test.target, strings.NewReader(`{"tags":["probe"]}`)))
		if w.Code != 401 {
			t.Errorf("unauthenticated %s %s: %d", test.method, test.target, w.Code)
		}
	}
}

func TestOxiDNSCacheFlushRespectsServiceOperations(t *testing.T) {
	f := newOxCacheFixture(t, oxCacheTestPlugins, nil)
	release, err := AcquireServiceOperation(f.cfg)
	if err != nil {
		t.Fatal(err)
	}
	w := f.request("POST", "/api/oxidns/cache/flush", `{"tags":["probe"]}`)
	release()
	if w.Code != 409 || !strings.Contains(w.Body.String(), "SERVICE_BUSY") || len(f.coreCalls()) != 0 {
		t.Fatalf("flushed during another operation: %d %s", w.Code, w.Body.String())
	}
	if w := f.request("POST", "/api/oxidns/cache/flush", `{"tags":["probe"]}`); w.Code != 200 {
		t.Fatalf("operation lock was not released: %d %s", w.Code, w.Body.String())
	}
}

func TestOxiDNSCacheFlushReportsPartialFailure(t *testing.T) {
	f := newOxCacheFixture(t, oxCacheTestPlugins, func(tag string) int {
		if tag == "probe" {
			return http.StatusInternalServerError
		}
		return http.StatusOK
	})
	w := f.request("POST", "/api/oxidns/cache/flush", `{"tags":["main_cache","probe"]}`)
	if w.Code != 502 || !strings.Contains(w.Body.String(), "CACHE_FLUSH_FAILED") || !strings.Contains(w.Body.String(), `"flushed":["main_cache"]`) {
		t.Fatalf("partial failure misreported: %d %s", w.Code, w.Body.String())
	}
	f = newOxCacheFixture(t, oxCacheTestPlugins, func(string) int { return http.StatusNotFound })
	if w := f.request("POST", "/api/oxidns/cache/flush", `{"tags":["probe"]}`); w.Code != 409 || !strings.Contains(w.Body.String(), "CACHE_FLUSH_UNSUPPORTED") {
		t.Fatalf("unsupported core misreported: %d %s", w.Code, w.Body.String())
	}
}
