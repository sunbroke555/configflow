// SPDX-License-Identifier: GPL-3.0-only
package main

import (
	"context"
	"errors"
	"net"
	"net/netip"
	"strings"

	"github.com/metacubex/mihomo/component/resolver"
	coreDNS "github.com/metacubex/mihomo/dns"
)

var (
	errProxyDNSFakeIP = errors.New("proxy server resolved to a fake IP")
	errProxyDNSConfig = errors.New("invalid proxy DNS configuration")
	commonFakeIPRange = netip.MustParsePrefix("198.18.0.0/15")
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

func proxyDNSNameServers(addresses []string) ([]coreDNS.NameServer, error) {
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

// Call once in main, before starting any concurrent measurements. The short-lived
// worker shares DNS caches across its batch but never changes the running core.
func configureProxyDNS(addresses []string) error {
	servers, err := proxyDNSNameServers(addresses)
	if err != nil {
		return err
	}
	base := resolver.SystemResolver
	if len(servers) != 0 {
		// Nameserver addresses are literal IPv4 addresses, so this needs no
		// bootstrap DNS and does not change the worker's existing IPv6 policy.
		// Core DNS also retries truncated UDP replies over TCP.
		base = coreDNS.NewResolver(coreDNS.Config{Main: servers, IPv6: true}).Resolver
	}
	// L3 protocols (e.g. WireGuard) must resolve the final target locally.
	// Explicitly retain their system resolver; a nil DefaultResolver would
	// make their dialer fall back to the proxy endpoint resolver below.
	resolver.DefaultResolver = resolver.SystemResolver
	resolver.ProxyServerHostResolver = &proxyDNSResolver{Resolver: base}
	return nil
}
