// SPDX-License-Identifier: GPL-3.0-only
package main

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"os"
	"os/exec"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/metacubex/mihomo/component/dialer"
	"github.com/metacubex/mihomo/component/resolver"
	D "github.com/miekg/dns"
)

type fixedProxyDNS struct {
	resolver.Resolver
	addresses []netip.Addr
	err       error
}

func (*fixedProxyDNS) Invalid() bool { return true }

func (r *fixedProxyDNS) LookupIPv4(context.Context, string) ([]netip.Addr, error) {
	return r.addresses, r.err
}

func (r *fixedProxyDNS) LookupIP(context.Context, string) ([]netip.Addr, error) {
	return r.addresses, r.err
}

func (r *fixedProxyDNS) LookupIPv6(context.Context, string) ([]netip.Addr, error) {
	return r.addresses, r.err
}

func TestProxyDNSNameServers(t *testing.T) {
	for _, raw := range [][]string{nil, {}, {"127.0.0.1"}, {"127.0.0.1:5353"}, {"127.0.0.1", " 192.168.0.1:53 "}} {
		servers, err := proxyDNSNameServers(raw)
		if err != nil {
			t.Fatalf("%q: %v", raw, err)
		}
		for _, server := range servers {
			if server.Net != "udp" {
				t.Fatalf("unexpected DNS transport: %+v", server)
			}
			if _, err := netip.ParseAddrPort(server.Addr); err != nil {
				t.Fatal(err)
			}
		}
	}
	for _, raw := range []string{"", " ", "127.0.0.1,127.0.0.2", "dns.example.com", "127.0.0.1:0", "127.0.0.1:65536", "127.0.0.1,", ",127.0.0.1", "secret@127.0.0.1", "::1", "[::1]:5353", "[fe80::1%eth0]:53", "0.0.0.0", "0.0.0.0:53", "224.0.0.1", "239.1.1.1:53"} {
		if _, err := proxyDNSNameServers([]string{raw}); !errors.Is(err, errProxyDNSConfig) {
			t.Fatalf("accepted invalid setting %q", raw)
		}
	}
	if _, err := proxyDNSNameServers([]string{"127.0.0.1", "127.0.0.2", "127.0.0.3", "127.0.0.4", "127.0.0.5"}); !errors.Is(err, errProxyDNSConfig) {
		t.Fatal("more than four DNS servers accepted")
	}
}

func TestProxyDNSGuard(t *testing.T) {
	for _, address := range []string{"198.18.0.1", "198.19.255.254", "::ffff:198.18.0.1"} {
		r := &proxyDNSResolver{Resolver: &fixedProxyDNS{addresses: []netip.Addr{netip.MustParseAddr(address)}}}
		for _, lookup := range []func(context.Context, string) ([]netip.Addr, error){r.LookupIP, r.LookupIPv4, r.LookupIPv6} {
			if _, err := lookup(context.Background(), "private-node.invalid"); !errors.Is(err, errProxyDNSFakeIP) {
				t.Fatalf("Fake-IP not rejected: %v", err)
			}
		}
	}
	for _, address := range []string{"127.0.0.1", "192.168.0.1", "10.0.0.1", "198.17.255.255", "198.20.0.1", "fd00::1"} {
		if isCommonFakeIP(address) {
			t.Fatalf("real/private address rejected: %s", address)
		}
		if _, err := checkProxyDNS([]netip.Addr{netip.MustParseAddr(address)}, nil); err != nil {
			t.Fatal(err)
		}
	}
	for _, err := range []error{errors.New("private-password-and-host.invalid"), context.DeadlineExceeded} {
		_, wrapped := checkProxyDNS(nil, err)
		out := connectionFailure("test", wrapped)
		if out.ErrorCode != "dns" || strings.Contains(out.Error, "private-") {
			t.Fatalf("unsafe DNS error: %+v", out)
		}
	}
	if _, err := checkProxyDNS(nil, nil); err == nil {
		t.Fatal("empty DNS result accepted")
	}
}

// A local DNS server makes these tests independent of the host's DNS and of
// public DNS availability. Any target URL lookup here would be a regression:
// only the proxy server's address should be resolved by the worker.
func proxyTestDNS(t *testing.T, answer string, rcode int) (string, *atomic.Int32) {
	t.Helper()
	pc, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	calls := &atomic.Int32{}
	server := &D.Server{PacketConn: pc, Handler: D.HandlerFunc(func(w D.ResponseWriter, req *D.Msg) {
		calls.Add(1)
		response := new(D.Msg)
		response.SetReply(req)
		response.Rcode = rcode
		if len(req.Question) != 1 || req.Question[0].Name != "proxy.test." {
			t.Errorf("worker resolved the wrong domain: %v", req.Question)
			response.Rcode = D.RcodeNameError
		} else if ip := net.ParseIP(answer); ip != nil && ip.To4() != nil && req.Question[0].Qtype == D.TypeA {
			response.Answer = []D.RR{&D.A{Hdr: D.RR_Header{Name: "proxy.test.", Rrtype: D.TypeA, Class: D.ClassINET, Ttl: 60}, A: net.ParseIP(answer)}}
		} else if ip != nil && ip.To4() == nil && req.Question[0].Qtype == D.TypeAAAA {
			response.Answer = []D.RR{&D.AAAA{Hdr: D.RR_Header{Name: "proxy.test.", Rrtype: D.TypeAAAA, Class: D.ClassINET, Ttl: 60}, AAAA: ip}}
		}
		_ = w.WriteMsg(response)
	})}
	go func() { _ = server.ActivateAndServe() }()
	t.Cleanup(func() { _ = server.Shutdown() })
	return pc.LocalAddr().String(), calls
}

func dnsTLSProxy(t *testing.T) (map[string]any, *atomic.Int32) {
	t.Helper()
	return dnsTLSProxyNetwork(t, "tcp4", "127.0.0.1:0")
}

func dnsTLSProxyNetwork(t *testing.T, network, address string) (map[string]any, *atomic.Int32) {
	t.Helper()
	listener, err := net.Listen(network, address)
	if err != nil {
		t.Fatal(err)
	}
	target := targetServer(t, 204)
	clientHellos := &atomic.Int32{}
	server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		if req.Method != http.MethodConnect || req.Host != "only-via-proxy.invalid:80" {
			t.Errorf("target host changed: %s %s", req.Method, req.Host)
			w.WriteHeader(400)
			return
		}
		upstream, err := net.Dial("tcp", target.Listener.Addr().String())
		if err != nil {
			w.WriteHeader(502)
			return
		}
		defer upstream.Close()
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
	_ = server.Listener.Close()
	server.Listener = listener
	server.TLS = &tls.Config{GetConfigForClient: func(info *tls.ClientHelloInfo) (*tls.Config, error) {
		if info.ServerName != "proxy.test" {
			t.Errorf("DNS override changed TLS SNI: %q", info.ServerName)
		}
		clientHellos.Add(1)
		return nil, nil
	}}
	server.StartTLS()
	t.Cleanup(server.Close)
	return map[string]any{"name": "test", "type": "http", "server": "proxy.test", "port": server.Listener.Addr().(*net.TCPAddr).Port,
		"tls": true, "skip-cert-verify": true, "password": "private-node-password"}, clientHellos
}

func dnsWorker(t *testing.T, node map[string]any, setting []string, systemMode string) (result, []byte, error) {
	t.Helper()
	cmd := exec.Command(os.Args[0], "-test.run=^TestProxyDNSWorkerProcess$")
	cmd.Env = append(os.Environ(), "CONFIGFLOW_PROBE_DNS_TEST_CHILD=1",
		"CONFIGFLOW_PROBE_SYSTEM_DNS_TEST="+systemMode)
	body, _ := json.Marshal(request{URL: "http://only-via-proxy.invalid/", TimeoutMS: 2000, UnifiedDelay: true, DNSServers: setting, Nodes: []map[string]any{node}})
	cmd.Stdin = bytes.NewReader(body)
	output, err := cmd.CombinedOutput()
	if err != nil {
		return result{}, output, err
	}
	var decoded struct {
		Version int      `json:"version"`
		Results []result `json:"results"`
	}
	if err := json.Unmarshal(output, &decoded); err != nil || decoded.Version != responseVersion || len(decoded.Results) != 1 {
		t.Fatalf("invalid worker response: %q, %v", output, err)
	}
	return decoded.Results[0], output, nil
}

func TestProxyDNSWorkerProcess(t *testing.T) {
	if os.Getenv("CONFIGFLOW_PROBE_DNS_TEST_CHILD") == "1" {
		switch os.Getenv("CONFIGFLOW_PROBE_SYSTEM_DNS_TEST") {
		case "fake":
			resolver.SystemResolver = &fixedProxyDNS{addresses: []netip.Addr{netip.MustParseAddr("198.18.0.1")}}
		case "failure":
			resolver.SystemResolver = &fixedProxyDNS{err: errors.New("private-system-DNS-password")}
		}
		main()
		os.Exit(0)
	}
	t.Run("custom DNS retains TLS SNI and remote target", func(t *testing.T) {
		node, hellos := dnsTLSProxy(t)
		dnsAddress, calls := proxyTestDNS(t, "127.0.0.1", D.RcodeSuccess)
		before, _ := json.Marshal(node)
		item, output, err := dnsWorker(t, node, []string{dnsAddress}, "failure")
		if err != nil || item.Latency == nil || item.Error != "" || calls.Load() == 0 || hellos.Load() != 1 {
			t.Fatalf("worker=%+v, %v, DNS calls=%d TLS hellos=%d: %s", item, err, calls.Load(), hellos.Load(), output)
		}
		after, _ := json.Marshal(node)
		if !bytes.Equal(before, after) {
			t.Fatal("node was modified")
		}
	})
	t.Run("multiple custom DNS servers", func(t *testing.T) {
		node, _ := dnsTLSProxy(t)
		missing, _ := proxyTestDNS(t, "", D.RcodeServerFailure)
		available, calls := proxyTestDNS(t, "127.0.0.1", D.RcodeSuccess)
		item, output, err := dnsWorker(t, node, []string{missing, available}, "failure")
		if err != nil || item.Latency == nil || calls.Load() == 0 {
			t.Fatalf("worker=%+v, %v: %s", item, err, output)
		}
	})
	for _, mode := range []string{"system fake", "custom fake", "custom second fake subnet", "custom DNS failure", "literal fake", "invalid setting"} {
		t.Run(mode, func(t *testing.T) {
			node := map[string]any{"name": "test", "type": "ss", "server": "proxy.test", "port": 443, "cipher": "aes-128-gcm", "password": "private-node-password"}
			setting, systemMode, expected := "", "fake", "dns_fake_ip"
			switch mode {
			case "custom fake":
				setting, _ = proxyTestDNS(t, "198.18.0.4", D.RcodeSuccess)
			case "custom second fake subnet":
				setting, _ = proxyTestDNS(t, "198.19.0.4", D.RcodeSuccess)
			case "custom DNS failure":
				setting, _ = proxyTestDNS(t, "", D.RcodeNameError)
				expected = "dns"
			case "literal fake":
				node["server"] = "198.18.0.4"
			case "invalid setting":
				setting = "private-invalid-setting"
			}
			item, output, err := dnsWorker(t, node, func() []string {
				if setting == "" {
					return nil
				}
				return []string{setting}
			}(), systemMode)
			if mode == "invalid setting" {
				var exit *exec.ExitError
				if !errors.As(err, &exit) || exit.ExitCode() != 3 {
					t.Fatalf("expected config exit 3: %v", err)
				}
			} else if err != nil || item.ErrorCode != expected || item.Latency != nil {
				t.Fatalf("worker=%+v, %v: %s", item, err, output)
			}
			if bytes.Contains(output, []byte("private-")) || bytes.Contains(output, []byte("proxy.test")) {
				t.Fatalf("private details escaped in result: %s", output)
			}
		})
	}
}

func TestProxyDNSWorkerIPv6(t *testing.T) {
	for _, mode := range []string{"literal/system DNS", "literal/custom DNS", "AAAA/custom DNS"} {
		t.Run(mode, func(t *testing.T) {
			node, hellos := dnsTLSProxyNetwork(t, "tcp6", "[::1]:0")
			literal := strings.HasPrefix(mode, "literal/")
			if literal {
				node["server"] = "::1"
				node["sni"] = "proxy.test"
			}
			var servers []string
			var calls *atomic.Int32
			if strings.HasSuffix(mode, "/custom DNS") {
				address, count := proxyTestDNS(t, "::1", D.RcodeSuccess)
				servers, calls = []string{address}, count
			}
			item, output, err := dnsWorker(t, node, servers, "")
			if err != nil || item.Latency == nil || item.Error != "" || hellos.Load() != 1 {
				t.Fatalf("IPv6 worker=%+v, err=%v, TLS hellos=%d: %s", item, err, hellos.Load(), output)
			}
			if calls != nil && ((literal && calls.Load() != 0) || (!literal && calls.Load() == 0)) {
				t.Fatalf("unexpected proxy DNS queries for %s: %d", mode, calls.Load())
			}
		})
	}
}

func TestProxyDNSGuardKeepsCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, err := checkProxyDNS(nil, ctx.Err())
	if !errors.Is(err, context.Canceled) {
		t.Fatal("DNS wrapper lost cancellation")
	}
	ctx, cancel = context.WithTimeout(context.Background(), time.Nanosecond)
	defer cancel()
	<-ctx.Done()
	_, err = checkProxyDNS(nil, ctx.Err())
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal("DNS wrapper lost deadline")
	}
}

func TestProxyDNSWorkerFlattenedErrors(t *testing.T) {
	for _, protocol := range []string{"vmess", "vless"} {
		for _, mode := range []string{"fake", "failure"} {
			t.Run(protocol+"/"+mode, func(t *testing.T) {
				node := map[string]any{"name": "test", "type": protocol, "server": "proxy.test", "port": 443,
					"uuid": "00000000-0000-4000-8000-000000000001", "cipher": "auto", "alterId": 0}
				item, output, err := dnsWorker(t, node, nil, mode)
				expected := "dns"
				if mode == "fake" {
					expected = "dns_fake_ip"
				}
				if err != nil || item.ErrorCode != expected || item.Latency != nil {
					t.Fatalf("flattened core error lost DNS cause: %+v, %v: %s", item, err, output)
				}
				if bytes.Contains(output, []byte("private-")) || bytes.Contains(output, []byte("proxy.test")) {
					t.Fatalf("private details escaped in result: %s", output)
				}
			})
		}
	}
}

func TestProxyDNSTargetResolverScope(t *testing.T) {
	if os.Getenv("CONFIGFLOW_PROBE_DNS_SCOPE_CHILD") == "1" {
		resolver.SystemResolver = &fixedProxyDNS{addresses: []netip.Addr{netip.MustParseAddr("127.0.0.1")}}
		if err := configureProxyDNS([]string{os.Getenv("CONFIGFLOW_PROBE_DNS_SCOPE_SERVER")}, nil); err != nil {
			t.Fatal(err)
		}
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		defer cancel()
		// WireGuard/OpenVPN and other L3 adapters pass DefaultResolver for
		// target lookup. It must not fall back to the endpoint DNS override.
		conn, err := dialer.DialContext(ctx, "tcp", os.Getenv("CONFIGFLOW_PROBE_DNS_SCOPE_TARGET"), dialer.WithResolver(resolver.DefaultResolver))
		if err != nil {
			t.Fatal(err)
		}
		_ = conn.Close()
		return
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	address, calls := proxyTestDNS(t, "198.18.0.1", D.RcodeSuccess)
	_, port, _ := net.SplitHostPort(listener.Addr().String())
	cmd := exec.Command(os.Args[0], "-test.run=^TestProxyDNSTargetResolverScope$")
	cmd.Env = append(os.Environ(), "CONFIGFLOW_PROBE_DNS_SCOPE_CHILD=1", "CONFIGFLOW_PROBE_DNS_SCOPE_SERVER="+address,
		"CONFIGFLOW_PROBE_DNS_SCOPE_TARGET="+net.JoinHostPort("only-via-proxy.invalid", port))
	output, err := cmd.CombinedOutput()
	if err != nil || calls.Load() != 0 {
		t.Fatalf("target resolution used endpoint DNS: calls=%d, err=%v: %s", calls.Load(), err, output)
	}
}
