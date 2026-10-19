package main

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"sort"
	"sync"
	"time"
)

// Mihomo 实时流向：在 Agent 端对 /connections 做差分并按「来源 → 规则 → 策略 → 出口」聚合，
// 只把聚合后的少量路径交给服务端，避免面板每次拉取几千条连接明细。
// 速率来自相邻两次快照的字节差；首次请求或间隔过久时只建立基线，不编造速率。

const (
	mihomoFlowMaxPaths = 120
	// 两次采样相隔过久时差分会把长时间的累计量摊成速率，此时重新建立基线
	mihomoFlowMaxGap = 30 * time.Second
	// 多个页面同时轮询时复用最近一次结果，避免采样间隔过短导致速率抖动
	mihomoFlowReuse = time.Second
)

type mihomoFlowConn struct {
	ID       string `json:"id"`
	Metadata struct {
		SourceIP string `json:"sourceIP"`
	} `json:"metadata"`
	Upload      int64     `json:"upload"`
	Download    int64     `json:"download"`
	Start       time.Time `json:"start"`
	Chains      []string  `json:"chains"`
	Rule        string    `json:"rule"`
	RulePayload string    `json:"rulePayload"`
}

type mihomoFlowPath struct {
	Source      string `json:"source"`
	Rule        string `json:"rule"`
	Policy      string `json:"policy"`
	Outlet      string `json:"outlet"`
	Up          int64  `json:"up"`
	Down        int64  `json:"down"`
	Connections int    `json:"connections"`
}

type mihomoFlowReply struct {
	// 本次速率对应的采样间隔（秒）；为 0 表示刚建立基线，还没有速率
	Interval    float64          `json:"interval"`
	Connections int              `json:"connections"`
	Active      int              `json:"active"`
	Up          int64            `json:"up"`
	Down        int64            `json:"down"`
	Paths       []mihomoFlowPath `json:"paths"`
	Omitted     int              `json:"omitted"`
}

type mihomoFlowBytes struct{ up, down int64 }

type mihomoFlowSampler struct {
	mu      sync.Mutex
	prev    map[string]mihomoFlowBytes
	prevAt  time.Time
	last    *mihomoFlowReply
	lastAt  time.Time
	now     func() time.Time
	maxPath int
}

func newMihomoFlowSampler() *mihomoFlowSampler {
	return &mihomoFlowSampler{now: time.Now, maxPath: mihomoFlowMaxPaths}
}

func (c mihomoFlowConn) ruleLabel() string {
	if c.RulePayload != "" {
		return c.Rule + ": " + c.RulePayload
	}
	return c.Rule
}

// observe 用新快照与上次快照做差分。快照中途关闭的连接最后一段字节无法计入，速率可能略低于实际。
func (s *mihomoFlowSampler) observe(conns []mihomoFlowConn, at time.Time) *mihomoFlowReply {
	next := make(map[string]mihomoFlowBytes, len(conns))
	for _, conn := range conns {
		next[conn.ID] = mihomoFlowBytes{conn.Upload, conn.Download}
	}
	gap := at.Sub(s.prevAt)
	baseline := s.prev == nil || gap <= 0 || gap > mihomoFlowMaxGap
	prev, prevAt := s.prev, s.prevAt
	s.prev, s.prevAt = next, at

	reply := &mihomoFlowReply{Connections: len(conns), Paths: []mihomoFlowPath{}}
	if baseline {
		return reply
	}
	reply.Interval = gap.Seconds()
	type key struct{ source, rule, policy, outlet string }
	groups := map[key]*mihomoFlowPath{}
	for _, conn := range conns {
		var up, down int64
		if old, ok := prev[conn.ID]; ok {
			up, down = conn.Upload-old.up, conn.Download-old.down
		} else if conn.Start.After(prevAt) {
			// 两次快照之间新建的连接，全部字节都发生在这个间隔里
			up, down = conn.Upload, conn.Download
		}
		up, down = max(up, 0), max(down, 0)
		if up+down == 0 {
			continue
		}
		outlet, policy := "", ""
		if n := len(conn.Chains); n > 0 {
			outlet, policy = conn.Chains[0], conn.Chains[n-1]
		}
		k := key{conn.Metadata.SourceIP, conn.ruleLabel(), policy, outlet}
		path := groups[k]
		if path == nil {
			path = &mihomoFlowPath{Source: k.source, Rule: k.rule, Policy: k.policy, Outlet: k.outlet}
			groups[k] = path
		}
		path.Up += up
		path.Down += down
		path.Connections++
		reply.Active++
	}
	seconds := gap.Seconds()
	for _, path := range groups {
		path.Up = int64(float64(path.Up) / seconds)
		path.Down = int64(float64(path.Down) / seconds)
		reply.Up += path.Up
		reply.Down += path.Down
		reply.Paths = append(reply.Paths, *path)
	}
	sort.Slice(reply.Paths, func(i, j int) bool {
		a, b := reply.Paths[i], reply.Paths[j]
		if a.Up+a.Down != b.Up+b.Down {
			return a.Up+a.Down > b.Up+b.Down
		}
		return a.Source+a.Rule+a.Policy+a.Outlet < b.Source+b.Rule+b.Policy+b.Outlet
	})
	if len(reply.Paths) > s.maxPath {
		reply.Omitted = len(reply.Paths) - s.maxPath
		reply.Paths = reply.Paths[:s.maxPath]
	}
	return reply
}

func (s *mihomoFlowSampler) serve(ctx context.Context, w http.ResponseWriter, client *http.Client, controller *mihomoController) {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.now()
	if s.last != nil && now.Sub(s.lastAt) < mihomoFlowReuse {
		writeMihomoFlow(w, s.last)
		return
	}
	ctx, cancel := context.WithTimeout(ctx, mihomoPanelTimeout)
	defer cancel()
	req, err := controller.request(ctx, http.MethodGet, "/connections")
	if err != nil {
		mihomoPanelFail(w, http.StatusBadRequest, "INVALID_REQUEST", "invalid panel request")
		return
	}
	resp, err := client.Do(req)
	if err != nil {
		mihomoPanelFail(w, http.StatusBadGateway, "CORE_UNREACHABLE", "mihomo controller is unreachable")
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden {
		mihomoPanelFail(w, http.StatusBadGateway, "CORE_AUTH_FAILED", "mihomo controller rejected the secret")
		return
	}
	var body struct {
		Connections []mihomoFlowConn `json:"connections"`
	}
	if resp.StatusCode != http.StatusOK || json.NewDecoder(io.LimitReader(resp.Body, 64<<20)).Decode(&body) != nil {
		mihomoPanelFail(w, http.StatusBadGateway, "INVALID_RESPONSE", "mihomo controller returned an invalid connections snapshot")
		return
	}
	reply := s.observe(body.Connections, now)
	s.last, s.lastAt = reply, now
	writeMihomoFlow(w, reply)
}

func writeMihomoFlow(w http.ResponseWriter, reply *mihomoFlowReply) {
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(reply)
}
