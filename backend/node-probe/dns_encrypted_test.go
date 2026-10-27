// SPDX-License-Identifier: GPL-3.0-only
package main

import (
	"bytes"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"io"
	"log"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"

	"github.com/metacubex/mihomo/component/resolver"
	D "github.com/miekg/dns"
)

func TestEncryptedDNSAddresses(t *testing.T) {
	for _, tc := range []struct{ input, network, address string }{
		{"https://DNS.Example", "https", "https://dns.example:443/dns-query"},
		{"https://127.0.0.1:8443/custom", "https", "https://127.0.0.1:8443/custom"},
		{"tls://DNS.Example", "tls", "dns.example:853"},
		{"tls://127.0.0.1:8853", "tls", "127.0.0.1:8853"},
		{"https://例子.测试", "https", "https://xn--fsqu00a.xn--0zwm56d:443/dns-query"},
	} {
		servers, err := proxyDNSNameServers([]string{tc.input})
		if err != nil || len(servers) != 1 || servers[0].Net != tc.network || servers[0].Addr != tc.address {
			t.Fatalf("parse %q: %+v, %v", tc.input, servers, err)
		}
	}
	for _, raw := range []string{
		"http://dns.example/query", "quic://dns.example", "udp://127.0.0.1",
		"https://user:private-password@dns.example/query", "tls://user@dns.example",
		"https://dns.example?private-token", "https://dns.example?", "https://dns.example#", "tls://dns.example#skip-cert-verify=true",
		"tls://dns.example/", "tls://dns.example/dns-query", "tls://dns.example?insecure=true",
		"https://dns.example:0/query", "https://dns.example:65536/query", "https://dns.example:/query", "https://dns.example:abc/query",
		"https://", "https://0.0.0.0", "tls://224.0.0.1", "https://[::]", "https://[fe80::1%25eth0]",
		"https://[::1]/dns-query", "tls://[::1]", "https://dns.example/has space", "https://dns.example/back\\slash",
		"https://.example", "https://bad_host.example", "https://-bad.example", "https://a..example",
	} {
		if _, err := proxyDNSNameServers([]string{raw}); !errors.Is(err, errProxyDNSConfig) {
			t.Fatalf("accepted invalid encrypted endpoint %q", raw)
		}
	}
	if _, err := plainDNSNameServers([]string{"https://127.0.0.1"}); err == nil {
		t.Fatal("accepted encrypted bootstrap endpoint")
	}
}

// Both UDP and TCP are bound to the same port so tests cover truncated responses
// without using the host resolver or any external DNS service.
func localDNS(t *testing.T, handler D.Handler) (string, *atomic.Int32) {
	t.Helper()
	var udp net.PacketConn
	var tcp net.Listener
	var err error
	for range 10 {
		tcp, err = net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		udp, err = net.ListenPacket("udp", tcp.Addr().String())
		if err == nil {
			break
		}
		_ = tcp.Close()
	}
	if err != nil {
		t.Fatal(err)
	}
	tcpCalls := &atomic.Int32{}
	udpServer := &D.Server{PacketConn: udp, Handler: handler}
	tcpServer := &D.Server{Listener: tcp, Handler: D.HandlerFunc(func(w D.ResponseWriter, req *D.Msg) {
		tcpCalls.Add(1)
		handler.ServeDNS(w, req)
	})}
	go func() { _ = udpServer.ActivateAndServe() }()
	go func() { _ = tcpServer.ActivateAndServe() }()
	t.Cleanup(func() { _ = udpServer.Shutdown(); _ = tcpServer.Shutdown() })
	return udp.LocalAddr().String(), tcpCalls
}

func dnsAnswer(req *D.Msg, ip string) *D.Msg {
	resp := new(D.Msg)
	resp.SetReply(req)
	if len(req.Question) != 1 {
		return resp
	}
	q := req.Question[0]
	parsed := net.ParseIP(ip)
	if q.Qtype == D.TypeA && parsed.To4() != nil {
		resp.Answer = []D.RR{&D.A{Hdr: D.RR_Header{Name: q.Name, Rrtype: D.TypeA, Class: D.ClassINET, Ttl: 30}, A: parsed}}
	} else if q.Qtype == D.TypeAAAA && parsed != nil && parsed.To4() == nil {
		resp.Answer = []D.RR{&D.AAAA{Hdr: D.RR_Header{Name: q.Name, Rrtype: D.TypeAAAA, Class: D.ClassINET, Ttl: 30}, AAAA: parsed}}
	}
	return resp
}

func dnsTestCertificate(t *testing.T) (tls.Certificate, string) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	template := &x509.Certificate{
		SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "encrypted.test"},
		DNSNames: []string{"encrypted.test"}, IPAddresses: []net.IP{net.ParseIP("127.0.0.1")},
		NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour),
		KeyUsage:    x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign,
		ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}, IsCA: true, BasicConstraintsValid: true,
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	caFile := filepath.Join(t.TempDir(), "ca.pem")
	if err := os.WriteFile(caFile, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), 0600); err != nil {
		t.Fatal(err)
	}
	return tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key}, caFile
}

func encryptedTestDNS(t *testing.T, protocol, ip string) (string, string, *atomic.Int32) {
	t.Helper()
	cert, caFile := dnsTestCertificate(t)
	calls := &atomic.Int32{}
	tlsConfig := &tls.Config{Certificates: []tls.Certificate{cert}, GetConfigForClient: func(hello *tls.ClientHelloInfo) (*tls.Config, error) {
		if hello.ServerName != "encrypted.test" && hello.ServerName != "" && hello.ServerName != "wrong.test" {
			t.Errorf("unexpected encrypted DNS SNI %q", hello.ServerName)
		}
		return nil, nil
	}}
	if protocol == "https" {
		server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			calls.Add(1)
			if r.Method != http.MethodPost && r.Method != http.MethodGet || r.URL.Path != "/dns-query" {
				t.Errorf("unexpected DoH request %s %s", r.Method, r.URL.Path)
			}
			var packet []byte
			var err error
			if r.Method == http.MethodGet {
				packet, err = base64.RawURLEncoding.DecodeString(r.URL.Query().Get("dns"))
			} else {
				packet, err = io.ReadAll(io.LimitReader(r.Body, 65536))
			}
			if err != nil {
				t.Error(err)
				return
			}
			var req D.Msg
			if err := req.Unpack(packet); err != nil {
				t.Error(err)
				return
			}
			resp, _ := dnsAnswer(&req, ip).Pack()
			w.Header().Set("Content-Type", "application/dns-message")
			_, _ = w.Write(resp)
		}))
		server.Config.ErrorLog = log.New(io.Discard, "", 0)
		server.TLS = tlsConfig
		server.StartTLS()
		t.Cleanup(server.Close)
		return server.URL + "/dns-query", caFile, calls
	}
	listener, err := tls.Listen("tcp", "127.0.0.1:0", tlsConfig)
	if err != nil {
		t.Fatal(err)
	}
	server := &D.Server{Listener: listener, Handler: D.HandlerFunc(func(w D.ResponseWriter, req *D.Msg) {
		calls.Add(1)
		_ = w.WriteMsg(dnsAnswer(req, ip))
	})}
	go func() { _ = server.ActivateAndServe() }()
	t.Cleanup(func() { _ = server.Shutdown() })
	return "tls://" + listener.Addr().String(), caFile, calls
}

func dnsCheckWorker(t *testing.T, input request, caFile, system string) (dnsCheckResult, []result, []byte) {
	t.Helper()
	cmd := exec.Command(os.Args[0], "-test.run=^TestDNSCheckWorkerProcess$")
	cmd.Env = append(os.Environ(), "CONFIGFLOW_DNS_CHECK_CHILD=1", "CONFIGFLOW_DNS_CHECK_SYSTEM="+system)
	if caFile != "" {
		cmd.Env = append(cmd.Env, "CONFIGFLOW_DNS_CHECK_CA="+caFile, "GODEBUG=x509usefallbackroots=1")
	}
	packet, _ := json.Marshal(input)
	cmd.Stdin = bytes.NewReader(packet)
	output, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("worker failed: %v: %s", err, output)
	}
	var decoded struct {
		Version int            `json:"version"`
		Check   dnsCheckResult `json:"dns_check"`
		Results []result       `json:"results"`
	}
	if err := json.Unmarshal(output, &decoded); err != nil || decoded.Version != responseVersion {
		t.Fatalf("invalid response %s: %v", output, err)
	}
	for _, secret := range []string{"private-", "encrypted.test", "wrong.test", "proxy.test"} {
		if bytes.Contains(output, []byte(secret)) {
			t.Fatalf("private details escaped: %s", output)
		}
	}
	return decoded.Check, decoded.Results, output
}

func TestDNSCheckWorkerProcess(t *testing.T) {
	if os.Getenv("CONFIGFLOW_DNS_CHECK_CHILD") != "1" {
		return
	}
	if file := os.Getenv("CONFIGFLOW_DNS_CHECK_CA"); file != "" {
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
	switch os.Getenv("CONFIGFLOW_DNS_CHECK_SYSTEM") {
	case "real":
		resolver.SystemResolver = &fixedProxyDNS{addresses: []netip.Addr{netip.MustParseAddr("192.0.2.1")}}
	case "fake":
		resolver.SystemResolver = &fixedProxyDNS{addresses: []netip.Addr{netip.MustParseAddr("198.18.0.1")}}
	default:
		resolver.SystemResolver = &fixedProxyDNS{err: errors.New("private-system-DNS-must-not-be-used")}
	}
	main()
	os.Exit(0)
}

func TestDNSCheckPlainAndSystem(t *testing.T) {
	for _, mode := range []string{"real", "fake"} {
		got, _, _ := dnsCheckWorker(t, request{Operation: "dns_check", TimeoutMS: 500}, "", mode)
		if mode == "real" && (!got.Success || len(got.Addresses) != 1) || mode == "fake" && (got.Success || got.ErrorCode != "dns_fake_ip") {
			t.Fatalf("%s: %+v", mode, got)
		}
	}
	for _, mode := range []string{"TCP retry", "fake", "empty", "timeout"} {
		t.Run(mode, func(t *testing.T) {
			address, tcpCalls := localDNS(t, D.HandlerFunc(func(w D.ResponseWriter, req *D.Msg) {
				if req.Question[0].Name != "example.com." {
					t.Errorf("unexpected check target %s", req.Question[0].Name)
				}
				ip := "192.0.2.1"
				if mode == "fake" {
					ip = "198.19.0.1"
				}
				if mode == "empty" {
					ip = ""
				}
				if mode == "timeout" {
					time.Sleep(time.Second)
					return
				}
				resp := dnsAnswer(req, ip)
				if mode == "TCP retry" && w.LocalAddr().Network() == "udp" {
					resp.Answer = nil
					resp.Truncated = true
				}
				_ = w.WriteMsg(resp)
			}))
			got, _, _ := dnsCheckWorker(t, request{Operation: "dns_check", TimeoutMS: 500, DNSServers: []string{address}}, "", "")
			expected := map[string]string{"fake": "dns_fake_ip", "empty": "dns", "timeout": "timeout"}[mode]
			if expected != "" && (got.Success || got.ErrorCode != expected) || expected == "" && (!got.Success || tcpCalls.Load() == 0) {
				t.Fatalf("%s: %+v (TCP %d)", mode, got, tcpCalls.Load())
			}
		})
	}
}

func TestEncryptedDNSWorker(t *testing.T) {
	for _, protocol := range []string{"https", "tls"} {
		for _, mode := range []string{"literal IP", "hostname", "untrusted certificate", "wrong certificate name", "missing bootstrap", "fake bootstrap", "failed bootstrap", "fake answer", "node SNI"} {
			t.Run(protocol+"/"+mode, func(t *testing.T) {
				answer := "192.0.2.1"
				if mode == "fake answer" {
					answer = "198.18.0.1"
				}
				if mode == "node SNI" {
					answer = "127.0.0.1"
				}
				endpoint, caFile, calls := encryptedTestDNS(t, protocol, answer)
				u, _ := url.Parse(endpoint)
				host := "encrypted.test"
				if mode == "wrong certificate name" {
					host = "wrong.test"
				}
				bootstrapCalls := &atomic.Int32{}
				bootstrap, _ := localDNS(t, D.HandlerFunc(func(w D.ResponseWriter, req *D.Msg) {
					bootstrapCalls.Add(1)
					if req.Question[0].Name != host+"." {
						t.Errorf("bootstrap resolved a node/target instead: %s", req.Question[0].Name)
					}
					ip := "127.0.0.1"
					if mode == "fake bootstrap" {
						ip = "198.18.0.1"
					}
					if mode == "failed bootstrap" {
						ip = ""
					}
					_ = w.WriteMsg(dnsAnswer(req, ip))
				}))
				input := request{Operation: "dns_check", TimeoutMS: 1500, DNSBootstrap: []string{bootstrap}}
				if mode == "literal IP" {
					input.DNSBootstrap = nil
				} else {
					u.Host = net.JoinHostPort(host, u.Port())
				}
				input.DNSServers = []string{u.String()}
				if mode == "missing bootstrap" {
					input.DNSBootstrap = nil
				}
				if mode == "untrusted certificate" {
					caFile = ""
				}
				var hellos *atomic.Int32
				if mode == "node SNI" {
					node, count := dnsTLSProxy(t)
					hellos = count
					input.Operation, input.URL, input.Nodes = "", "http://only-via-proxy.invalid/", []map[string]any{node}
				}
				got, results, output := dnsCheckWorker(t, input, caFile, "")
				if mode == "node SNI" {
					if len(results) != 1 || results[0].Latency == nil || hellos.Load() != 1 {
						t.Fatalf("node SNI regression: %s", output)
					}
					return
				}
				expected := map[string]string{"untrusted certificate": "tls", "wrong certificate name": "tls", "missing bootstrap": "dns_bootstrap", "fake bootstrap": "dns_fake_ip", "failed bootstrap": "dns", "fake answer": "dns_fake_ip"}[mode]
				if expected != "" {
					if got.Success || got.ErrorCode != expected {
						t.Fatalf("expected %s: %+v", expected, got)
					}
				} else if !got.Success || len(got.Addresses) != 1 || got.Addresses[0] != answer {
					t.Fatalf("encrypted DNS failed: %+v", got)
				}
				if (mode == "fake bootstrap" || mode == "failed bootstrap" || mode == "missing bootstrap") && calls.Load() != 0 {
					t.Fatal("connected to encrypted upstream after bootstrap failure")
				}
				if mode == "literal IP" && bootstrapCalls.Load() != 0 {
					t.Fatal("literal IP unexpectedly used bootstrap")
				}
			})
		}
	}
}

func TestDNSCheckRejectsUnsafeSettings(t *testing.T) {
	for _, input := range []request{
		{DNSServers: []string{"https://private-user:private-password@encrypted.test/dns-query"}},
		{DNSServers: []string{"https://127.0.0.1/dns-query#skip-cert-verify=true"}},
		{DNSServers: []string{"https://127.0.0.1/dns-query?private-token"}},
		{DNSServers: []string{"https://encrypted.test/dns-query"}, DNSBootstrap: []string{"tls://private-bootstrap"}},
		{DNSBootstrap: []string{"127.0.0.1"}},
		{DNSServers: []string{"127.0.0.1"}, DNSBootstrap: []string{"127.0.0.1", "127.0.0.2", "127.0.0.3", "127.0.0.4", "127.0.0.5"}},
	} {
		input.Operation, input.TimeoutMS = "dns_check", 500
		got, _, _ := dnsCheckWorker(t, input, "", "")
		if got.Success || got.ErrorCode != "config" {
			t.Fatalf("unsafe configuration accepted: %+v", got)
		}
	}
}

func TestDoHRejectsAllRedirects(t *testing.T) {
	for _, encryptedTarget := range []bool{false, true} {
		t.Run(map[bool]string{false: "plaintext downgrade", true: "different HTTPS endpoint"}[encryptedTarget], func(t *testing.T) {
			cert, caFile := dnsTestCertificate(t)
			calls := &atomic.Int32{}
			target := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				w.WriteHeader(http.StatusInternalServerError)
			}))
			if encryptedTarget {
				target.TLS = &tls.Config{Certificates: []tls.Certificate{cert}}
				target.StartTLS()
			} else {
				target.Start()
			}
			defer target.Close()
			source := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				http.Redirect(w, r, target.URL+"/private-redirect", http.StatusTemporaryRedirect)
			}))
			source.TLS = &tls.Config{Certificates: []tls.Certificate{cert}}
			source.StartTLS()
			defer source.Close()
			got, _, _ := dnsCheckWorker(t, request{Operation: "dns_check", TimeoutMS: 1500, DNSServers: []string{source.URL + "/dns-query"}}, caFile, "")
			if got.Success || got.ErrorCode != "dns" || calls.Load() != 0 {
				t.Fatalf("DoH followed redirect: check=%+v redirected requests=%d", got, calls.Load())
			}
		})
	}
}

func TestDoHValidatesResponse(t *testing.T) {
	for _, mode := range []string{"wrong MIME", "oversized", "invalid packet", "wrong ID", "wrong question", "HTTP error"} {
		t.Run(mode, func(t *testing.T) {
			cert, caFile := dnsTestCertificate(t)
			server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/dns-message")
				body, _ := io.ReadAll(io.LimitReader(r.Body, 65536))
				var query D.Msg
				if err := query.Unpack(body); err != nil {
					t.Error(err)
					return
				}
				answer := dnsAnswer(&query, "192.0.2.1")
				switch mode {
				case "wrong MIME":
					w.Header().Set("Content-Type", "text/html")
				case "oversized":
					_, _ = w.Write(make([]byte, 65536))
					return
				case "invalid packet":
					_, _ = w.Write([]byte("private-invalid-DNS-response"))
					return
				case "wrong ID":
					answer.Id++
				case "wrong question":
					answer.Question[0].Name = "private-other.test."
				case "HTTP error":
					w.WriteHeader(http.StatusForbidden)
					return
				}
				packet, _ := answer.Pack()
				_, _ = w.Write(packet)
			}))
			server.TLS = &tls.Config{Certificates: []tls.Certificate{cert}}
			server.StartTLS()
			defer server.Close()
			got, _, _ := dnsCheckWorker(t, request{Operation: "dns_check", TimeoutMS: 1500, DNSServers: []string{server.URL + "/dns-query"}}, caFile, "")
			if got.Success || got.ErrorCode != "dns" {
				t.Fatalf("invalid DoH response accepted: %+v", got)
			}
		})
	}
}

func TestMixedDNSUpstreams(t *testing.T) {
	for _, healthy := range []string{"https", "udp"} {
		t.Run(healthy+" survives other upstream failure", func(t *testing.T) {
			answer := "192.0.2.1"
			if healthy == "udp" {
				answer = ""
			}
			doh, caFile, _ := encryptedTestDNS(t, "https", answer)
			udp, _ := localDNS(t, D.HandlerFunc(func(w D.ResponseWriter, query *D.Msg) {
				answer := dnsAnswer(query, "192.0.2.1")
				if healthy == "https" {
					answer.Answer = nil
					answer.Rcode = D.RcodeServerFailure
				}
				_ = w.WriteMsg(answer)
			}))
			if healthy == "udp" {
				caFile = ""
			} // the DoH endpoint's certificate is untrusted
			got, _, _ := dnsCheckWorker(t, request{Operation: "dns_check", TimeoutMS: 1500, DNSServers: []string{doh, udp}}, caFile, "")
			if !got.Success || len(got.Addresses) != 1 || got.Addresses[0] != "192.0.2.1" {
				t.Fatalf("mixed DNS failed: %+v", got)
			}
		})
	}
}
