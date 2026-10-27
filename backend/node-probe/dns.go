// SPDX-License-Identifier: GPL-3.0-only
package main

import (
	"context"
	"errors"
	"net"
	"net/netip"
	"net/url"
	"strconv"
	"strings"
	"time"
	"unicode"

	"github.com/metacubex/mihomo/adapter/outbound"
	"github.com/metacubex/mihomo/component/dialer"
	"github.com/metacubex/mihomo/component/resolver"
	C "github.com/metacubex/mihomo/constant"
	coreDNS "github.com/metacubex/mihomo/dns"
	"golang.org/x/net/idna"
)

var (
	errProxyDNSFakeIP       = errors.New("proxy server resolved to a fake IP")
	errProxyDNSConfig       = errors.New("invalid proxy DNS configuration")
	errDNSBootstrapRequired = errors.New("encrypted DNS hostname requires explicit bootstrap DNS")
	commonFakeIPRange       = netip.MustParsePrefix("198.18.0.0/15")
)

// Keep the underlying error available for errors.Is/As, never for display: DNS
// libraries may include the queried server name or other private node details.
type proxyDNSError struct{ cause error }

const proxyDNSErrorMessage = "proxy server DNS lookup failed"

func (*proxyDNSError) Error() string   { return proxyDNSErrorMessage }
func (e *proxyDNSError) Unwrap() error { return e.cause }

func isCommonFakeIP(host string) bool {
	ip, err := netip.ParseAddr(host)
	return err == nil && commonFakeIPRange.Contains(ip.Unmap())
}

// Only the proxy endpoint resolver is replaced. In particular, do not rewrite
// node["server"]: TLS SNI and plugin Host defaults must retain the original name.
// The target URL's host still travels inside the proxy protocol as before.
type proxyDNSResolver struct{ resolver.Resolver }

func (*proxyDNSResolver) Invalid() bool { return true } // Mihomo's name means usable.

func checkProxyDNS(ips []netip.Addr, err error) ([]netip.Addr, error) {
	if err != nil {
		return nil, &proxyDNSError{cause: err}
	}
	if len(ips) == 0 {
		return nil, &proxyDNSError{cause: resolver.ErrIPNotFound}
	}
	for _, ip := range ips {
		if commonFakeIPRange.Contains(ip.Unmap()) {
			return nil, errProxyDNSFakeIP
		}
	}
	return ips, nil
}

func (r *proxyDNSResolver) LookupIP(ctx context.Context, host string) ([]netip.Addr, error) {
	return checkProxyDNS(r.Resolver.LookupIP(ctx, host))
}

func (r *proxyDNSResolver) LookupIPv4(ctx context.Context, host string) ([]netip.Addr, error) {
	return checkProxyDNS(r.Resolver.LookupIPv4(ctx, host))
}

func (r *proxyDNSResolver) LookupIPv6(ctx context.Context, host string) ([]netip.Addr, error) {
	return checkProxyDNS(r.Resolver.LookupIPv6(ctx, host))
}

func plainDNSNameServers(addresses []string) ([]coreDNS.NameServer, error) {
	if len(addresses) > 4 {
		return nil, errProxyDNSConfig
	}
	var servers []coreDNS.NameServer
	for _, item := range addresses {
		item = strings.TrimSpace(item)
		var address string
		if ip, err := netip.ParseAddr(item); err == nil && ip.Is4() {
			address = net.JoinHostPort(ip.String(), "53")
		} else if endpoint, err := netip.ParseAddrPort(item); err == nil && endpoint.Port() != 0 && endpoint.Addr().Is4() {
			address = endpoint.String()
		} else {
			return nil, errProxyDNSConfig
		}
		endpoint, _ := netip.ParseAddrPort(address)
		if endpoint.Addr().IsUnspecified() || endpoint.Addr().IsMulticast() {
			return nil, errProxyDNSConfig
		}
		servers = append(servers, coreDNS.NameServer{Net: "udp", Addr: address})
	}
	return servers, nil
}

func proxyDNSNameServers(addresses []string) ([]coreDNS.NameServer, error) {
	if len(addresses) > 4 {
		return nil, errProxyDNSConfig
	}
	var servers []coreDNS.NameServer
	for _, item := range addresses {
		item = strings.TrimSpace(item)
		if len(item) > 2048 || strings.ContainsRune(item, '\\') || strings.IndexFunc(item, unicode.IsSpace) >= 0 {
			return nil, errProxyDNSConfig
		}
		if !strings.Contains(item, "://") {
			plain, err := plainDNSNameServers([]string{item})
			if err != nil {
				return nil, err
			}
			servers = append(servers, plain[0])
			continue
		}
		u, err := url.Parse(item)
		if err != nil || (u.Scheme != "https" && u.Scheme != "tls") || u.User != nil || u.Opaque != "" ||
			strings.ContainsAny(item, "?#") || u.Hostname() == "" {
			return nil, errProxyDNSConfig
		}
		host := u.Hostname()
		if ip, err := netip.ParseAddr(host); err == nil {
			if !ip.Is4() || ip.Zone() != "" || ip.IsUnspecified() || ip.IsMulticast() {
				return nil, errProxyDNSConfig
			}
			host = ip.String()
		} else {
			host, err = idna.Lookup.ToASCII(strings.ToLower(host))
			if err != nil || strings.ContainsAny(host, ":%[]") || len(host) > 253 {
				return nil, errProxyDNSConfig
			}
			for _, label := range strings.Split(strings.TrimSuffix(host, "."), ".") {
				if len(label) == 0 || len(label) > 63 || label[0] == '-' || label[len(label)-1] == '-' {
					return nil, errProxyDNSConfig
				}
				for _, ch := range label {
					if !(ch >= 'a' && ch <= 'z' || ch >= '0' && ch <= '9' || ch == '-') {
						return nil, errProxyDNSConfig
					}
				}
			}
		}
		port := u.Port()
		if port == "" {
			if strings.HasSuffix(u.Host, ":") {
				return nil, errProxyDNSConfig
			}
			port = "443"
			if u.Scheme == "tls" {
				port = "853"
			}
		}
		parsedPort, err := strconv.ParseUint(port, 10, 16)
		if err != nil || parsedPort == 0 {
			return nil, errProxyDNSConfig
		}
		address := net.JoinHostPort(host, strconv.FormatUint(parsedPort, 10))
		if u.Scheme == "tls" {
			if u.Path != "" || u.RawPath != "" {
				return nil, errProxyDNSConfig
			}
		} else {
			u.Host = address
			if u.Path == "" {
				u.Path = "/dns-query"
			}
			address = u.String()
		}
		servers = append(servers, coreDNS.NameServer{Net: u.Scheme, Addr: address})
	}
	return servers, nil
}

// Only encrypted DNS connections use this adapter. Its explicit resolver never
// falls back to the system or node resolver, and rejects Fake-IP bootstrap
// answers before connecting. The core retains the URL/host for TLS verification.
type bootstrapDNSAdapter struct {
	*outbound.Direct
	bootstrap resolver.Resolver
}

func (*bootstrapDNSAdapter) IsL3Protocol(*C.Metadata) bool { return false }

func (d *bootstrapDNSAdapter) DialContext(ctx context.Context, metadata *C.Metadata) (C.Conn, error) {
	conn, err := dialer.DialContext(ctx, "tcp", metadata.RemoteAddress(), dialer.WithResolver(d.bootstrap))
	if err != nil {
		return nil, err
	}
	return outbound.NewConn(conn, d), nil
}

// Call once in main, before starting any concurrent measurements. The short-lived
// worker shares DNS caches across its batch but never changes the running core.
func configureProxyDNS(addresses, bootstrapAddresses []string) error {
	servers, err := proxyDNSNameServers(addresses)
	if err != nil {
		return err
	}
	bootstrapServers, err := plainDNSNameServers(bootstrapAddresses)
	if err != nil || len(servers) == 0 && len(bootstrapServers) != 0 {
		return errProxyDNSConfig
	}
	bootstrap := &proxyDNSResolver{Resolver: coreDNS.NewResolver(coreDNS.Config{Main: bootstrapServers, IPv6: true}).Resolver}
	var clients probeDNSClients
	var coreServers []coreDNS.NameServer
	for i := range servers {
		server := &servers[i]
		if server.Net == "udp" {
			coreServers = append(coreServers, *server)
			continue
		}
		host, _, _ := net.SplitHostPort(server.Addr)
		if server.Net == "https" {
			u, _ := url.Parse(server.Addr)
			host = u.Hostname()
		}
		if _, err := netip.ParseAddr(host); err != nil && len(bootstrapServers) == 0 {
			return errDNSBootstrapRequired
		}
		if server.Net == "https" {
			clients = append(clients, newProbeDoHClient(server.Addr, bootstrap))
			continue
		}
		server.ProxyAdapter = &bootstrapDNSAdapter{Direct: outbound.NewDirect(), bootstrap: bootstrap}
		coreServers = append(coreServers, *server)
	}
	// Standalone workers do not load Mihomo's global config. Its resolver
	// defaults to IPv4-only, which also rejects literal IPv6 endpoints.
	resolver.DisableIPv6 = false
	base := resolver.SystemResolver
	if len(servers) != 0 {
		// Plain DNS uses literal IPv4 endpoints; DoT uses the isolated adapter.
		// Core DNS retries truncated UDP over TCP. DoH forbids HTTP redirects.
		if len(coreServers) != 0 {
			coreResolver := coreDNS.NewResolver(coreDNS.Config{Main: coreServers, IPv6: true}).Resolver
			if len(clients) == 0 {
				base = coreResolver
			} else {
				clients = append(clients, coreResolver)
			}
		}
		if len(clients) != 0 {
			base = coreDNS.NewResolverFromClient(clients)
		}
	}
	// L3 protocols (e.g. WireGuard) must resolve the final target locally.
	// Explicitly retain their system resolver; a nil DefaultResolver would
	// make their dialer fall back to the proxy endpoint resolver below.
	resolver.DefaultResolver = resolver.SystemResolver
	resolver.ProxyServerHostResolver = &proxyDNSResolver{Resolver: base}
	return nil
}

type dnsCheckResult struct {
	Success   bool     `json:"success"`
	Addresses []string `json:"addresses"`
	ErrorCode string   `json:"error_code,omitempty"`
	Error     string   `json:"error,omitempty"`
	LatencyMS int64    `json:"latency_ms"`
}

func checkDNS(input request) dnsCheckResult {
	started := time.Now()
	result := dnsCheckResult{Addresses: []string{}}
	if input.TimeoutMS < 500 || input.TimeoutMS > 10000 {
		result.ErrorCode, result.Error = "config", "DNS 检查超时须为 500–10000 毫秒"
		return result
	}
	if err := configureProxyDNS(input.DNSServers, input.DNSBootstrap); err != nil {
		result.ErrorCode, result.Error = "config", "DNS 设置无效，请检查地址、协议及引导 DNS"
		if errors.Is(err, errDNSBootstrapRequired) {
			result.ErrorCode, result.Error = "dns_bootstrap", "域名形式的 DoH/DoT 地址需要指定引导 DNS"
		}
		return result
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(input.TimeoutMS)*time.Millisecond)
	defer cancel()
	ips, err := checkDNSAddresses(ctx, resolver.ProxyServerHostResolver)
	result.LatencyMS = max(1, time.Since(started).Milliseconds())
	if err != nil {
		result.ErrorCode, result.Error = "dns", "DNS 解析失败，请检查上游地址、引导 DNS 及网络"
		if errors.Is(err, errProxyDNSFakeIP) || dnsErrorContains(err, errProxyDNSFakeIP.Error()) {
			result.ErrorCode, result.Error = "dns_fake_ip", "DNS 返回了 Fake-IP，请使用返回真实 IP 的 DNS"
		} else if errors.Is(err, context.DeadlineExceeded) || ctx.Err() != nil {
			result.ErrorCode, result.Error = "timeout", "DNS 检查超时"
		} else if dnsErrorContains(err, "certificate", "x509", "tls") {
			result.ErrorCode, result.Error = "tls", "DNS 的 TLS 握手或证书校验失败"
		}
		return result
	}
	seen := map[string]bool{}
	for _, ip := range ips {
		address := ip.Unmap().String()
		if !seen[address] {
			result.Addresses = append(result.Addresses, address)
			seen[address] = true
		}
	}
	result.Success = true
	return result
}

// The core's combined LookupIP replaces failures from both families with
// ErrIPNotFound. Check each family explicitly so TLS/bootstrap failures remain
// distinguishable, while a normal empty AAAA response still allows IPv4.
func checkDNSAddresses(ctx context.Context, r resolver.Resolver) ([]netip.Addr, error) {
	type lookupResult struct {
		ips []netip.Addr
		err error
	}
	answers := make(chan lookupResult, 2)
	for _, lookup := range []func(context.Context, string) ([]netip.Addr, error){r.LookupIPv4, r.LookupIPv6} {
		go func() {
			ips, err := lookup(ctx, "example.com")
			answers <- lookupResult{ips, err}
		}()
	}
	var ips []netip.Addr
	var errs []error
	for range 2 {
		select {
		case answer := <-answers:
			if errors.Is(answer.err, errProxyDNSFakeIP) || dnsErrorContains(answer.err, errProxyDNSFakeIP.Error()) {
				return nil, errProxyDNSFakeIP
			}
			ips = append(ips, answer.ips...)
			if answer.err != nil {
				errs = append(errs, answer.err)
			}
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	if len(ips) != 0 {
		return ips, nil
	}
	return nil, errors.Join(errs...)
}

// Inspect causes only to select a fixed public message. Never return their text.
func dnsErrorContains(err error, terms ...string) bool {
	if err == nil {
		return false
	}
	message := strings.ToLower(err.Error())
	for _, term := range terms {
		if strings.Contains(message, term) {
			return true
		}
	}
	if joined, ok := err.(interface{ Unwrap() []error }); ok {
		for _, cause := range joined.Unwrap() {
			if dnsErrorContains(cause, terms...) {
				return true
			}
		}
	}
	return dnsErrorContains(errors.Unwrap(err), terms...)
}
