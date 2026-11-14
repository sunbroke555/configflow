package agents_test

// Route ports of test_agent_registration_security.py,
// test_agent_heartbeat_security.py, test_agent_runtime_state.py (HTTP case)
// and the registry routes of test_agent_deployments.py.

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"configflow/internal/agents"
	apiagents "configflow/internal/api/agents"
	"configflow/internal/auth"
	"configflow/internal/httpx/httpxtest"
	"configflow/internal/jsonx"
	"configflow/internal/logx"
)

func newServer(t *testing.T) *httpxtest.Server {
	t.Helper()
	agents.ResetRuntime()
	agents.ResetHooks()
	t.Cleanup(func() { agents.ResetRuntime(); agents.ResetHooks() })
	t.Setenv("AGENT_REGISTRATION_KEY", "")
	os.Unsetenv("AGENT_REGISTRATION_KEY")
	return httpxtest.New(t, httpxtest.Options{DefaultFactories: true, Register: httpxtest.Routes(apiagents.Register)})
}

func payload(pairs ...any) *jsonx.Object {
	p := jsonx.Obj("name", "secure-agent", "host", "10.0.0.8", "port", 8080, "service_type", "mihomo", "version", "1.2.3")
	p.Update(jsonx.Obj(pairs...))
	return p
}

func storedAgents(t *testing.T, s *httpxtest.Server) []any {
	system, err := s.Repo.GetSystem(s.Context())
	if err != nil {
		t.Fatal(err)
	}
	return system.GetList("agents")
}

func system(t *testing.T, s *httpxtest.Server) *jsonx.Object {
	system, err := s.Repo.GetSystem(s.Context())
	if err != nil {
		t.Fatal(err)
	}
	return system
}

func TestFirstRegistrationReturnsTokenAndPersistsIt(t *testing.T) {
	s := newServer(t)
	body := s.Post("/api/agents/register", httpxtest.JSON(payload())).WantStatus(200).Object()
	if strings.Join(sortedKeys(body), ",") != "id,is_new,status,success,token" || body.Value("success") != true || body.Value("is_new") != true {
		t.Fatalf("body %v", body)
	}
	persisted := storedAgents(t, s)[0].(*jsonx.Object)
	if persisted.Value("id") != body.Value("id") || persisted.Value("token") != body.Value("token") {
		t.Fatal("not persisted")
	}
}

func sortedKeys(o *jsonx.Object) []string {
	keys := o.Keys()
	for i := range keys {
		for j := i + 1; j < len(keys); j++ {
			if keys[j] < keys[i] {
				keys[i], keys[j] = keys[j], keys[i]
			}
		}
	}
	return keys
}

func TestDuplicateRegistrationRequiresExistingBearerAndDoesNotMutate(t *testing.T) {
	for name, authorization := range map[string]string{
		"missing": "", "wrong": "Bearer wrong-token", "trailing-field": "Bearer wrong-token extra",
		"double-space": "Bearer  wrong-token", "tab": "Bearer\twrong-token", "unicode": "Bearer 令牌🔐",
		"oversized": "Bearer " + strings.Repeat("x", auth.MaxAuthTokenLength+1),
	} {
		s := newServer(t)
		created := s.Post("/api/agents/register", httpxtest.JSON(payload())).Object()
		before := storedAgents(t, s)
		options := []httpxtest.Option{httpxtest.JSON(payload("port", 9999, "version", "attacker"))}
		if authorization != "" {
			options = append(options, httpxtest.Header("Authorization", authorization))
		}
		s.Post("/api/agents/register", options...).WantStatus(401).WantJSON(jsonx.Obj("success", false, "message", "Unauthorized"))
		after := storedAgents(t, s)
		if !jsonx.Equal(after, before) || after[0].(*jsonx.Object).Value("token") != created.Value("token") {
			t.Fatalf("%s: mutated", name)
		}
	}
}

func TestAuthenticatedDuplicateRegistrationNeverReturnsToken(t *testing.T) {
	s := newServer(t)
	created := s.Post("/api/agents/register", httpxtest.JSON(payload())).Object()
	result := s.Post("/api/agents/register", httpxtest.JSON(payload("port", 9090)), httpxtest.Bearer(created.GetString("token"))).WantStatus(200)
	result.WantJSON(jsonx.Obj("success", true, "id", created.Value("id"), "status", "online", "is_new", false))
	if result.Contains("token") {
		t.Fatal("token in body")
	}
	persisted := storedAgents(t, s)[0].(*jsonx.Object)
	if persisted.Value("token") != created.Value("token") || !jsonx.Equal(persisted.Value("port"), 9090) {
		t.Fatal("persisted")
	}
}

func TestRegistrationKeyDoesNotBypassExistingAgentToken(t *testing.T) {
	s := newServer(t)
	t.Setenv("AGENT_REGISTRATION_KEY", "valid-registration-key")
	p := payload("registration_key", "valid-registration-key")
	created := s.Post("/api/agents/register", httpxtest.JSON(p)).WantStatus(200).Object()
	before := storedAgents(t, s)
	s.Post("/api/agents/register", httpxtest.JSON(p)).WantStatus(401)
	if !jsonx.Truthy(created.Value("token")) || !jsonx.Equal(storedAgents(t, s), before) {
		t.Fatal("mutated")
	}
	s.Post("/api/agents/register", httpxtest.JSON(payload("name", "other", "registration_key", "wrong"))).
		WantStatus(403).WantJSON(jsonx.Obj("success", false, "message", "Invalid registration key"))
	if before[0].(*jsonx.Object).Has("registration_key") {
		t.Fatal("key stored")
	}
}

func TestHostMatchingHappensAfterRealClientIPNormalization(t *testing.T) {
	s := newServer(t)
	p := payload("host", "172.18.0.4")
	proxy := httpxtest.Header("X-Forwarded-For", "203.0.113.9, 172.18.0.1")
	created := s.Post("/api/agents/register", httpxtest.JSON(p), proxy).Object()
	s.Post("/api/agents/register", httpxtest.JSON(p), proxy).WantStatus(401)
	accepted := s.Post("/api/agents/register", httpxtest.JSON(p), proxy, httpxtest.Bearer(created.GetString("token"))).WantStatus(200)
	if storedAgents(t, s)[0].(*jsonx.Object).Value("host") != "203.0.113.9" || accepted.Object().Has("token") {
		t.Fatal("host")
	}
}

func TestRegistrationLogsOnlyBoundedControlFreeWhitelistedFields(t *testing.T) {
	s := newServer(t)
	logFile := filepath.Join(t.TempDir(), "app.log")
	root, err := logx.Setup(logx.Config{Level: logx.Info, File: logFile})
	if err != nil {
		t.Fatal(err)
	}
	defer root.Close()
	raw, prefix, nested, control := "raw-secret-8d2f", "prefix-secret-4a6c", "nested-secret-1b9e", "unicode-control-secret-3c7a"
	p := payload("name", "safe-agent-"+strings.Repeat("x", 256)+prefix, "service_type", "mihomo\r\n"+raw,
		"version", "1.2.3 "+control, "raw", raw, "prefix", jsonx.Obj("value", prefix),
		"nested", jsonx.Obj("authorization", jsonx.Obj("token", nested)))
	result := s.Post("/api/agents/register", httpxtest.JSON(p)).WantStatus(200)
	data, _ := os.ReadFile(logFile)
	text := string(data)
	for _, secret := range []string{raw, prefix, nested, control} {
		if strings.Contains(text, secret) || result.Contains(secret) {
			t.Fatalf("%s leaked", secret)
		}
	}
	if !strings.Contains(text, "safe-agent-") || !strings.Contains(text, "mihomo") || strings.Contains(text, "authorization") || strings.Contains(text, "\r") {
		t.Fatalf("log %q", text)
	}
}

func TestManualAgentCreateDoesNotReturnToken(t *testing.T) {
	s := newServer(t)
	result := s.Post("/api/agents", httpxtest.JSON(jsonx.Obj("name", "manual", "host", "10.0.0.9"))).WantStatus(200)
	token := storedAgents(t, s)[0].(*jsonx.Object).GetString("token")
	if result.Contains(token) {
		t.Fatal("token returned")
	}
	s.Post("/api/agents", httpxtest.JSON(jsonx.Obj("name", "manual", "host", "10.0.0.9"))).WantStatus(500).
		WantJSON(jsonx.Obj("success", false, "message", "Unauthorized"))
	s.Post("/api/agents", httpxtest.JSON([]any{1})).WantStatus(500).WantJSON(jsonx.Obj("success", false, "message", "'list' object has no attribute 'get'"))
}

type heartbeatEnv struct {
	s     *httpxtest.Server
	id    string
	token string
}

func newHeartbeat(t *testing.T) heartbeatEnv {
	s := newServer(t)
	manager, err := agents.Get(s.Context())
	if err != nil {
		t.Fatal(err)
	}
	registered, err := manager.RegisterAgent(s.Context(), jsonx.Obj("name", "secured", "host", "127.0.0.1"), nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	return heartbeatEnv{s: s, id: registered.GetString("id"), token: registered.GetString("token")}
}

func (h heartbeatEnv) path() string { return "/api/agents/" + h.id + "/heartbeat" }

func (h heartbeatEnv) agent(t *testing.T) *jsonx.Object {
	manager, _ := agents.Get(h.s.Context())
	agent, err := manager.GetAgentByID(h.s.Context(), h.id, true)
	if err != nil {
		t.Fatal(err)
	}
	return agent
}

func TestHeartbeatRejectsUnauthorizedWithoutMutatingSystem(t *testing.T) {
	for _, authorization := range []string{"", "Basic ignored", "Bearer wrong-token", "Bearer 令牌", "Bearer café",
		"Bearer token with spaces", "Bearer  token", "Bearer\twrong", "Bearer " + strings.Repeat("x", auth.MaxAuthTokenLength+1)} {
		h := newHeartbeat(t)
		before := system(t, h.s)
		options := []httpxtest.Option{httpxtest.JSON(jsonx.Obj("version", "attacker", "config_version", "attacker"))}
		if authorization != "" {
			options = append(options, httpxtest.Header("Authorization", authorization))
		}
		h.s.Post(h.path(), options...).WantStatus(401)
		if !jsonx.Equal(system(t, h.s), before) {
			t.Fatalf("%q mutated", authorization)
		}
	}
}

func TestHeartbeatAcceptsMatchingBearerToken(t *testing.T) {
	h := newHeartbeat(t)
	h.s.Post(h.path(), httpxtest.Bearer(h.token), httpxtest.JSON(jsonx.Obj("version", "2.0.0", "config_version", "cfg-2", "service_status", "running"))).
		WantStatus(200).WantJSON(jsonx.Obj("success", true, "domain_discovery_enabled", false, "traffic_history_enabled", false, "reads_host", true))
	agent := h.agent(t)
	if agent.Value("version") != "2.0.0" || agent.Value("config_version") != "cfg-2" || agent.Value("service_status") != "running" {
		t.Fatalf("agent %v", agent)
	}
}

func TestHeartbeatRejectsUnapprovedOrMalformedFieldsWithoutMutation(t *testing.T) {
	for _, body := range []any{
		[]any{}, jsonx.Obj("unexpected", "value"), jsonx.Obj("version", 2), jsonx.Obj("version", strings.Repeat("v", 129)),
		jsonx.Obj("config_version", nil), jsonx.Obj("config_version", strings.Repeat("c", 129)), jsonx.Obj("service_status", false),
		jsonx.Obj("service_status", strings.Repeat("s", 65)), jsonx.Obj("system_metrics", []any{}),
		jsonx.Obj("system_metrics", jsonx.Obj("unexpected", jsonx.NewObject())),
		jsonx.Obj("system_metrics", jsonx.Obj("cpu", jsonx.Obj("unexpected", 1))),
		jsonx.Obj("system_metrics", jsonx.Obj("cpu", jsonx.Obj("usage_percent", "high"))),
		jsonx.Obj("system_metrics", jsonx.Obj("memory", jsonx.Obj("total", -1))),
		jsonx.Obj("system_metrics", jsonx.Obj("collected_at", strings.Repeat("t", 65))),
		jsonx.Obj("system_metrics", jsonx.Obj("memory", jsonx.Obj("total", 1.5))),
		jsonx.Obj("system_metrics", jsonx.Obj("cpu", jsonx.Obj("usage_percent", 101))),
		nil,
	} {
		h := newHeartbeat(t)
		before := system(t, h.s)
		h.s.Post(h.path(), httpxtest.Bearer(h.token), httpxtest.JSON(body)).WantStatus(400).
			WantJSON(jsonx.Obj("success", false, "message", "Invalid heartbeat data"))
		if !jsonx.Equal(system(t, h.s), before) {
			t.Fatalf("%v mutated", body)
		}
	}
}

func TestHeartbeatRejectsMalformedJSONAndOversizedBody(t *testing.T) {
	h := newHeartbeat(t)
	before := system(t, h.s)
	h.s.Post(h.path(), httpxtest.Bearer(h.token), httpxtest.Body([]byte(`{"version":`), "application/json")).WantStatus(400)
	h.s.Post(h.path(), httpxtest.Bearer(h.token), httpxtest.Body([]byte{0xff}, "application/json")).WantStatus(400)
	h.s.Post(h.path(), httpxtest.Bearer(h.token), httpxtest.Body([]byte(`{}`), "text/plain")).WantStatus(400)
	h.s.Post(h.path(), httpxtest.Bearer(h.token), httpxtest.Body(bytes.Repeat([]byte(" "), 32769), "application/json")).
		WantStatus(413).WantJSON(jsonx.Obj("success", false, "message", "Request body too large"))
	if !jsonx.Equal(system(t, h.s), before) {
		t.Fatal("mutated")
	}
}

type countingReader struct {
	r     io.Reader
	count int
}

func (c *countingReader) Read(p []byte) (int, error) {
	n, err := c.r.Read(p)
	c.count += n
	return n, err
}

func streamBody(reader io.Reader) httpxtest.Option {
	return func(r *http.Request) {
		r.Body = io.NopCloser(reader)
		r.ContentLength = -1
		r.Header.Del("Content-Length")
		r.Header.Set("Content-Type", "application/json")
	}
}

func TestHeartbeatWithoutContentLengthReadsOnlyMaxPlusOneBytes(t *testing.T) {
	h := newHeartbeat(t)
	reader := &countingReader{r: bytes.NewReader(bytes.Repeat([]byte(" "), 1024*1024))}
	h.s.Post(h.path(), httpxtest.Bearer(h.token), streamBody(reader)).WantStatus(413)
	if reader.count != 32769 {
		t.Fatalf("read %d", reader.count)
	}
	chunked := &countingReader{r: strings.NewReader(`{"version":"chunked"}`)}
	h.s.Post(h.path(), httpxtest.Bearer(h.token), streamBody(chunked)).WantStatus(200)
	if h.agent(t).Value("version") != "chunked" {
		t.Fatal("chunked heartbeat")
	}
}

func TestHeartbeatAcceptsWhitelistedSystemMetrics(t *testing.T) {
	h := newHeartbeat(t)
	metrics := jsonx.Obj(
		"cpu", jsonx.Obj("usage_percent", 12.5, "core_count", 4),
		"memory", jsonx.Obj("total", 1024, "used", 512, "available", 512, "used_percent", 50.0),
		"disk", jsonx.Obj("total", 2048, "used", 1024, "free", 1024, "used_percent", 50.0),
		"network", jsonx.Obj("bytes_sent", 10, "bytes_recv", 20, "speed_sent", 1, "speed_recv", 2),
		"collected_at", "2026-08-31T00:00:00Z")
	var seen any
	agents.RegisterHooks(agents.Hooks{AddMetrics: func(_ *agents.Manager, _ string, m any) error { seen = m; return nil }})
	h.s.Post(h.path(), httpxtest.Bearer(h.token), httpxtest.JSON(jsonx.Obj("system_metrics", metrics))).WantStatus(200)
	if !jsonx.Equal(h.agent(t).Value("system_metrics"), metrics) || !jsonx.Equal(seen, metrics) {
		t.Fatal("metrics")
	}
}

func TestHeartbeatEndpointRecordsStateAndRejectsUnknownAgents(t *testing.T) {
	h := newHeartbeat(t)
	revision := system(t, h.s).GetInt("_revision")
	h.s.Post(h.path(), httpxtest.Bearer(h.token), httpxtest.JSON(jsonx.Obj("version", "1.0.0", "service_status", "running"))).WantStatus(200)
	if system(t, h.s).GetInt("_revision") != revision || h.agent(t).Value("service_status") != "running" {
		t.Fatal("heartbeat wrote")
	}
	h.s.Post(h.path(), httpxtest.Bearer(h.token), httpxtest.JSON(jsonx.Obj("version", "2.0.0"))).WantStatus(200)
	if storedAgents(t, h.s)[0].(*jsonx.Object).Value("version") != "2.0.0" {
		t.Fatal("version not written")
	}
	h.s.Post("/api/agents/missing/heartbeat", httpxtest.Bearer(h.token), httpxtest.JSON(jsonx.NewObject())).WantStatus(401)
}

func TestHeartbeatReportedHostUsesAddressFollowHooks(t *testing.T) {
	h := newHeartbeat(t)
	h.s.Post(h.path(), httpxtest.Bearer(h.token), httpxtest.JSON(jsonx.Obj("host", "192.0.2.50"))).WantStatus(200).
		WantJSON(jsonx.Obj("success", true, "domain_discovery_enabled", false, "traffic_history_enabled", false, "reads_host", true))
	var followed string
	agents.RegisterHooks(agents.Hooks{
		AddressFollow:    func(_ context.Context, _ *agents.Manager, _ *jsonx.Object, host string) error { followed = host; return nil },
		AddressConfirmed: func(_ *jsonx.Object, host string) string { return host },
	})
	h.s.Post(h.path(), httpxtest.Bearer(h.token), httpxtest.JSON(jsonx.Obj("host", "192.0.2.50"))).WantStatus(200).
		WantJSON(jsonx.Obj("success", true, "domain_discovery_enabled", false, "traffic_history_enabled", false, "reads_host", true, "confirmed_host", "192.0.2.50"))
	if followed != "192.0.2.50" {
		t.Fatal("not followed")
	}
}

func TestAgentItemUpdateAndDelete(t *testing.T) {
	h := newHeartbeat(t)
	manager, _ := agents.Get(h.s.Context())
	if _, err := h.s.Repo.CreateProfile(h.s.Context(), jsonx.Obj("id", "other", "name", "Other"), ""); err != nil {
		t.Fatal(err)
	}
	if _, err := manager.BeginDeployment(h.s.Context(), h.id, "binding-request"); err != nil {
		t.Fatal(err)
	}
	result := h.s.Put("/api/agents/"+h.id, httpxtest.JSON(jsonx.Obj("profile_id", "other"))).WantStatus(409).Object()
	if !strings.Contains(result.GetString("message"), "unfinished deployment") || h.agent(t).Value("profile_id") != "default" {
		t.Fatal("binding changed")
	}
	manager.FinishDeploymentPreparation(h.id, "binding-request")
	h.s.Put("/api/agents/"+h.id, httpxtest.JSON(jsonx.Obj("name", "renamed", "token", "ignored"))).WantStatus(200).WantJSON(jsonx.Obj("success", true, "data", true))
	if h.agent(t).Value("name") != "renamed" || h.agent(t).Value("token") != h.token {
		t.Fatal("edit")
	}
	h.s.Put("/api/agents/missing", httpxtest.JSON(jsonx.NewObject())).WantStatus(404).WantJSON(jsonx.Obj("success", false, "message", "Agent not found"))
	h.s.Put("/api/agents/"+h.id, httpxtest.JSON(jsonx.Obj("profile_id", "bad id"))).WantStatus(500).WantJSON(jsonx.Obj("success", false, "message", "Invalid profile id"))
	h.s.Put("/api/agents/"+h.id, httpxtest.JSON(nil)).WantStatus(500).WantJSON(jsonx.Obj("success", false, "message", "argument of type 'NoneType' is not iterable"))
	var cleared []string
	agents.OnAgentDeleted("test", func(_ context.Context, id string) error { cleared = append(cleared, id); return nil })
	h.s.Delete("/api/agents/"+h.id).WantStatus(200).WantJSON(jsonx.Obj("success", true))
	h.s.Delete("/api/agents/"+h.id).WantStatus(404)
	if len(storedAgents(t, h.s)) != 0 || len(cleared) != 1 {
		t.Fatal("delete")
	}
}
