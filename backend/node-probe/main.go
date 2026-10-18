// SPDX-License-Identifier: GPL-3.0-only
// configflow-node-probe is an on-demand JSON-in/JSON-out proxy latency worker.
// It opens outbound connections only: no controller, listeners, DNS server or TUN.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/metacubex/mihomo/adapter"
	C "github.com/metacubex/mihomo/constant"
	"github.com/metacubex/mihomo/log"
)

const maxNodes = 20

type request struct {
	URL          string           `json:"url"`
	TimeoutMS    int              `json:"timeout_ms"`
	UnifiedDelay bool             `json:"unified_delay"`
	Nodes        []map[string]any `json:"nodes"`
}

type result struct {
	Name         string `json:"name"`
	Latency      *int64 `json:"latency"`
	FirstLatency *int64 `json:"first_latency,omitempty"`
	ErrorCode    string `json:"error_code,omitempty"`
	Error        string `json:"error,omitempty"`
}

func failure(name, code, message string) result {
	return result{Name: name, ErrorCode: code, Error: message}
}

// Never return core error strings: protocol errors can contain node credentials.
func connectionFailure(name string, err error) result {
	var timeout net.Error
	if errors.Is(err, context.DeadlineExceeded) || (errors.As(err, &timeout) && timeout.Timeout()) {
		return failure(name, "timeout", "连接或请求超时")
	}
	message := strings.ToLower(err.Error())
	if strings.Contains(message, "certificate") || strings.Contains(message, "tls") || strings.Contains(message, "x509") {
		return failure(name, "tls", "TLS 握手或证书校验失败")
	}
	if strings.Contains(message, "auth") || strings.Contains(message, "password") || strings.Contains(message, "407") {
		return failure(name, "auth", "代理认证失败，请检查节点凭据")
	}
	return failure(name, "connection", "代理连接或请求失败，请检查节点配置及网络")
}

func measure(ctx context.Context, node map[string]any, target string, unified bool) (out result) {
	name, _ := node["name"].(string)
	// Invalid protocol input must fail this node, not crash the rest of the batch.
	defer func() {
		if recover() != nil {
			out = failure(name, "config", "节点配置无效或协议初始化失败")
		}
	}()
	typ, _ := node["type"].(string)
	switch typ {
	case "", "direct", "reject", "dns", "rematch", "pass", "compatible":
		return failure(name, "unsupported", "该类型不是可测试的代理节点")
	}
	if chain, ok := node["dialer-proxy"]; ok && chain != nil && chain != "" {
		return failure(name, "chain", "此节点依赖代理链，请在已配置该链的 Mihomo 中测试")
	}
	p, err := adapter.ParseProxy(node)
	if err != nil {
		if strings.Contains(err.Error(), "unsupport proxy type") {
			return failure(name, "unsupported", "测速组件暂不支持此代理协议")
		}
		return failure(name, "config", "节点配置无效或缺少必要参数")
	}
	defer p.Close()
	transport := &http.Transport{
		// Explicit node dialer: do not use HTTP_PROXY or a system proxy.
		DialContext: func(ctx context.Context, network, address string) (net.Conn, error) {
			var metadata C.Metadata
			if err := metadata.SetRemoteAddress(address); err != nil {
				return nil, err
			}
			return p.DialContext(ctx, &metadata)
		},
		MaxIdleConns:        1,
		MaxIdleConnsPerHost: 1,
		TLSHandshakeTimeout: 5 * time.Second,
	}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, CheckRedirect: func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	}}
	probe := func() (int64, error) {
		req, err := http.NewRequestWithContext(ctx, http.MethodHead, target, nil)
		if err != nil {
			return 0, err
		}
		started := time.Now()
		resp, err := client.Do(req)
		if err != nil {
			return 0, err
		}
		defer resp.Body.Close()
		if resp.StatusCode != http.StatusNoContent {
			return 0, statusError(resp.StatusCode)
		}
		return max(1, time.Since(started).Milliseconds()), nil
	}
	first, err := probe()
	if err != nil {
		return probeFailure(name, err)
	}
	elapsed := first
	if unified {
		// Like unified-delay, measure a second HEAD on a reusable connection.
		// A failed second request is a failure, never a successful warm-up result.
		elapsed, err = probe()
		if err != nil {
			return probeFailure(name, err)
		}
	}
	return result{Name: name, Latency: &elapsed, FirstLatency: &first}
}

type statusError int

func (e statusError) Error() string { return fmt.Sprintf("HTTP %d", int(e)) }

func probeFailure(name string, err error) result {
	var status statusError
	if errors.As(err, &status) {
		return failure(name, "http_status", fmt.Sprintf("测试地址返回 HTTP %d，预期 204", status))
	}
	return connectionFailure(name, err)
}

func execute(input request) ([]result, error) {
	u, err := url.Parse(input.URL)
	if err != nil || u.Hostname() == "" || u.User != nil || (u.Scheme != "http" && u.Scheme != "https") {
		return nil, errors.New("invalid test URL")
	}
	if len(input.Nodes) > maxNodes || input.TimeoutMS < 500 || input.TimeoutMS > 10000 {
		return nil, errors.New("invalid batch size or timeout")
	}
	results := make([]result, len(input.Nodes))
	jobs := make(chan int)
	var wg sync.WaitGroup
	for range min(5, len(input.Nodes)) {
		wg.Go(func() {
			for i := range jobs {
				ctx, cancel := context.WithTimeout(context.Background(), time.Duration(input.TimeoutMS)*time.Millisecond)
				results[i] = measure(ctx, input.Nodes[i], input.URL, input.UnifiedDelay)
				cancel()
			}
		})
	}
	for i := range input.Nodes {
		jobs <- i
	}
	close(jobs)
	wg.Wait()
	return results, nil
}

func main() {
	// Core logging must not corrupt stdout's JSON or disclose credentials.
	log.SetLevel(log.SILENT)
	var input request
	if err := json.NewDecoder(io.LimitReader(os.Stdin, 8<<20)).Decode(&input); err != nil {
		fmt.Fprintln(os.Stderr, "invalid input")
		os.Exit(2)
	}
	results, err := execute(input)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	if err := json.NewEncoder(os.Stdout).Encode(map[string]any{"version": 1, "results": results}); err != nil {
		os.Exit(1)
	}
}
