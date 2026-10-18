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
	"net/url"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/metacubex/http"
	"github.com/metacubex/mihomo/adapter"
	"github.com/metacubex/mihomo/component/ca"
	C "github.com/metacubex/mihomo/constant"
	"github.com/metacubex/mihomo/log"
)

const maxNodes = 20

type request struct {
	URL          string           `json:"url"`
	TimeoutMS    int              `json:"timeout_ms"`
	UnifiedDelay bool             `json:"unified_delay"`
	DNSServers   []string         `json:"dns_servers,omitempty"`
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
	// Some core adapters (VMess/VLESS) flatten the dialer's error with %s.
	// Recognize only our fixed DNS suffixes in that case; never display the
	// adapter error itself, which can include private node details.
	if errors.Is(err, errProxyDNSFakeIP) || strings.HasSuffix(err.Error(), errProxyDNSFakeIP.Error()) {
		return failure(name, "dns_fake_ip", "节点服务器域名被解析为 Fake-IP；请在节点库测速设置中指定返回真实 IP 的 DNS，或将节点域名加入 fake-ip-filter")
	}
	var dnsError *proxyDNSError
	if errors.As(err, &dnsError) || strings.HasSuffix(err.Error(), proxyDNSErrorMessage) {
		return failure(name, "dns", "节点服务器域名解析失败，请检查测速 DNS 设置及网络")
	}
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
	if server, _ := node["server"].(string); isCommonFakeIP(server) {
		return connectionFailure(name, errProxyDNSFakeIP)
	}
	p, err := adapter.ParseProxy(node)
	if err != nil {
		if strings.Contains(err.Error(), "unsupport proxy type") {
			return failure(name, "unsupported", "测速组件暂不支持此代理协议")
		}
		return failure(name, "config", "节点配置无效或缺少必要参数")
	}
	defer p.Close()
	u, err := url.Parse(target)
	if err != nil {
		return failure(name, "config", "测试地址无效")
	}
	port := u.Port()
	if port == "" {
		port = "80"
		if u.Scheme == "https" {
			port = "443"
		}
	}
	var metadata C.Metadata
	if err := metadata.SetRemoteAddress(net.JoinHostPort(u.Hostname(), port)); err != nil {
		return failure(name, "config", "测试地址无效")
	}
	// Mirror Mihomo v1.19.32's Proxy.URLTest without changing its process-global
	// UnifiedDelay switch. Keeping this local also lets us retain first_latency.
	started := time.Now()
	connection, err := p.DialContext(ctx, &metadata)
	if err != nil {
		return connectionFailure(name, err)
	}
	defer connection.Close()
	tlsConfig, err := ca.GetTLSConfig(ca.Option{})
	if err != nil {
		return connectionFailure(name, err)
	}
	transport := &http.Transport{
		// Reuse the exact connection as URLTest does; never use HTTP_PROXY.
		DialContext: func(context.Context, string, string) (net.Conn, error) {
			return connection, nil
		},
		MaxIdleConns:          100,
		IdleConnTimeout:       90 * time.Second,
		TLSHandshakeTimeout:   10 * time.Second,
		ExpectContinueTimeout: time.Second,
		TLSClientConfig:       tlsConfig,
	}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, Timeout: 30 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	}}
	req, err := http.NewRequestWithContext(ctx, http.MethodHead, target, nil)
	if err != nil {
		return failure(name, "config", "测试地址无效")
	}
	probe := func() error {
		resp, err := client.Do(req)
		if err != nil {
			return err
		}
		_ = resp.Body.Close()
		// The panel does not set an expected status: any HTTP response is a
		// completed latency probe, including redirects (which are not followed).
		return nil
	}
	if err = probe(); err != nil {
		return connectionFailure(name, err)
	}
	first := max(1, time.Since(started).Milliseconds())
	elapsed := first
	if unified {
		second := time.Now()
		if err := probe(); err == nil {
			started = second
		}
		// As in URLTest, a failed second request keeps the first connection's
		// start time. A hard overall deadline still fails, as in the core API.
		elapsed = max(1, time.Since(started).Milliseconds())
	}
	if err := ctx.Err(); err != nil {
		return connectionFailure(name, err)
	}
	return result{Name: name, Latency: &elapsed, FirstLatency: &first}
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
	for range min(maxNodes, len(input.Nodes)) {
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
	if err := configureProxyDNS(input.DNSServers); err != nil {
		fmt.Fprintln(os.Stderr, "invalid node test DNS: use IPv4 or IPv4:port")
		os.Exit(3)
	}
	results, err := execute(input)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	if err := json.NewEncoder(os.Stdout).Encode(map[string]any{"version": 3, "results": results}); err != nil {
		os.Exit(1)
	}
}
