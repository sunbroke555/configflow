package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func flowConn(id, source, rule, payload string, chains []string, up, down int64, start time.Time) mihomoFlowConn {
	conn := mihomoFlowConn{ID: id, Upload: up, Download: down, Start: start, Chains: chains, Rule: rule, RulePayload: payload}
	conn.Metadata.SourceIP = source
	return conn
}

func TestMihomoFlowFirstSampleOnlySetsBaseline(t *testing.T) {
	s := newMihomoFlowSampler()
	t0 := time.Unix(1000, 0)
	reply := s.observe([]mihomoFlowConn{flowConn("a", "10.0.0.2", "Match", "", []string{"HK-01", "Proxy"}, 500, 9000, t0.Add(-time.Minute))}, t0)
	if reply.Interval != 0 || reply.Up != 0 || reply.Down != 0 || len(reply.Paths) != 0 || reply.Connections != 1 {
		t.Fatalf("baseline must not report rates: %+v", reply)
	}
}

func TestMihomoFlowAggregatesByteDeltasIntoPaths(t *testing.T) {
	s := newMihomoFlowSampler()
	t0 := time.Unix(1000, 0)
	old := t0.Add(-time.Minute)
	proxy := []string{"HK-01", "Auto", "Proxy"}
	s.observe([]mihomoFlowConn{
		flowConn("a", "10.0.0.2", "RuleSet", "proxy", proxy, 100, 1000, old),
		flowConn("b", "10.0.0.2", "RuleSet", "proxy", proxy, 0, 0, old),
		flowConn("idle", "10.0.0.3", "Match", "", []string{"DIRECT"}, 50, 50, old),
		flowConn("closed", "10.0.0.4", "Match", "", []string{"DIRECT"}, 50, 50, old),
	}, t0)

	t1 := t0.Add(2 * time.Second)
	reply := s.observe([]mihomoFlowConn{
		flowConn("a", "10.0.0.2", "RuleSet", "proxy", proxy, 300, 5000, old),
		flowConn("b", "10.0.0.2", "RuleSet", "proxy", proxy, 100, 1000, old),
		flowConn("idle", "10.0.0.3", "Match", "", []string{"DIRECT"}, 50, 50, old),
		// 新连接：起始在上次采样之后，全部字节计入
		flowConn("new", "10.0.0.5", "Match", "", []string{"DIRECT"}, 20, 400, t0.Add(time.Second)),
		// 上次采样时就已存在却没被看到（例如计数回绕），不计入
		flowConn("unseen", "10.0.0.6", "Match", "", []string{"DIRECT"}, 9999, 9999, old),
	}, t1)

	if reply.Interval != 2 || reply.Connections != 5 || reply.Active != 3 {
		t.Fatalf("unexpected summary: %+v", reply)
	}
	want := []mihomoFlowPath{
		{Source: "10.0.0.2", Rule: "RuleSet: proxy", Policy: "Proxy", Outlet: "HK-01", Up: 150, Down: 2500, Connections: 2},
		{Source: "10.0.0.5", Rule: "Match", Policy: "DIRECT", Outlet: "DIRECT", Up: 10, Down: 200, Connections: 1},
	}
	if fmt.Sprint(reply.Paths) != fmt.Sprint(want) {
		t.Fatalf("paths = %+v, want %+v", reply.Paths, want)
	}
	if reply.Up != 160 || reply.Down != 2700 {
		t.Fatalf("totals = %d/%d", reply.Up, reply.Down)
	}
}

func TestMihomoFlowRebaselinesAfterLongGapAndCapsPaths(t *testing.T) {
	s := newMihomoFlowSampler()
	s.maxPath = 2
	t0 := time.Unix(1000, 0)
	old := t0.Add(-time.Minute)
	conns := func(scale int64) []mihomoFlowConn {
		var out []mihomoFlowConn
		for i := int64(1); i <= 4; i++ {
			out = append(out, flowConn(fmt.Sprint(i), fmt.Sprintf("10.0.0.%d", i), "Match", "", []string{"DIRECT"}, 0, i*scale, old))
		}
		return out
	}
	s.observe(conns(1), t0)
	if reply := s.observe(conns(100), t0.Add(mihomoFlowMaxGap+time.Second)); reply.Interval != 0 || len(reply.Paths) != 0 {
		t.Fatalf("long gap must rebaseline: %+v", reply)
	}
	reply := s.observe(conns(200), t0.Add(mihomoFlowMaxGap+2*time.Second))
	if len(reply.Paths) != 2 || reply.Omitted != 2 || reply.Paths[0].Source != "10.0.0.4" || reply.Paths[0].Down != 400 {
		t.Fatalf("paths not capped by rate: %+v", reply)
	}
	if reply.Down != 1000 {
		t.Fatalf("totals must include omitted paths: %d", reply.Down)
	}
}

func TestMihomoPanelFlowEndpointSamplesConnections(t *testing.T) {
	calls := 0
	handler := newMihomoPanelFixture(t, func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.Method != http.MethodGet || r.URL.Path != "/connections" || r.Header.Get("Authorization") != "Bearer s3cret" {
			t.Errorf("unexpected core request %s %s", r.Method, r.URL)
		}
		_, _ = w.Write([]byte(`{"connections":[{"id":"a","metadata":{"sourceIP":"10.0.0.2"},"upload":1,"download":2,"chains":["DIRECT"],"rule":"Match"}]}`))
	})
	recorder := httptest.NewRecorder()
	handler(recorder, httptest.NewRequest(http.MethodGet, "/api/mihomo/flow", nil))
	var reply mihomoFlowReply
	if recorder.Code != http.StatusOK || json.Unmarshal(recorder.Body.Bytes(), &reply) != nil || reply.Connections != 1 || reply.Interval != 0 {
		t.Fatalf("got %d %s", recorder.Code, recorder.Body.String())
	}
	// 一秒内的重复请求复用上次结果，不重新采样
	recorder = httptest.NewRecorder()
	handler(recorder, httptest.NewRequest(http.MethodGet, "/api/mihomo/flow", nil))
	if recorder.Code != http.StatusOK || calls != 1 {
		t.Fatalf("expected cached reply, core called %d times", calls)
	}
	recorder = httptest.NewRecorder()
	handler(recorder, httptest.NewRequest(http.MethodPost, "/api/mihomo/flow", strings.NewReader("{}")))
	if recorder.Code != http.StatusMethodNotAllowed {
		t.Fatalf("POST flow: got %d", recorder.Code)
	}
}
