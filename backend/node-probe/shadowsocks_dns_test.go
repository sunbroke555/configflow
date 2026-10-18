// SPDX-License-Identifier: GPL-3.0-only
package main

import (
	"context"
	"fmt"
	"io"
	"net"
	"testing"

	"github.com/metacubex/sing-shadowsocks/shadowaead"
	M "github.com/metacubex/sing/common/metadata"
	N "github.com/metacubex/sing/common/network"
	D "github.com/miekg/dns"
)

type proxySSHandler struct{ target string }

func (h *proxySSHandler) NewConnection(ctx context.Context, conn net.Conn, metadata M.Metadata) error {
	defer conn.Close()
	if metadata.Destination.String() != "only-via-proxy.invalid:80" {
		return fmt.Errorf("proxy target host was changed")
	}
	upstream, err := net.Dial("tcp", h.target)
	if err != nil {
		return err
	}
	defer upstream.Close()
	go func() { _, _ = io.Copy(upstream, conn); _ = upstream.Close() }()
	_, err = io.Copy(conn, upstream)
	return err
}

func (*proxySSHandler) NewPacketConnection(context.Context, N.PacketConn, M.Metadata) error {
	return nil
}

func (*proxySSHandler) NewError(context.Context, error) {}

// Exercise the real encrypted SS path that originally failed when endpoint DNS
// returned Fake-IP. The test server uses sing-shadowsocks; the worker's Mihomo
// adapter uses sing-shadowsocks2, so this is also a protocol interoperability test.
func TestShadowsocksDNSWorkerRoundTrip(t *testing.T) {
	for _, cipher := range []string{"aes-128-gcm", "aes-256-gcm", "chacha20-ietf-poly1305"} {
		t.Run(cipher, func(t *testing.T) {
			target := targetServer(t, 204)
			service, err := shadowaead.NewService(cipher, nil, "private-node-password", 60, &proxySSHandler{target: target.Listener.Addr().String()})
			if err != nil {
				t.Fatal(err)
			}
			listener, err := net.Listen("tcp", "127.0.0.1:0")
			if err != nil {
				t.Fatal(err)
			}
			defer listener.Close()
			go func() {
				for {
					conn, err := listener.Accept()
					if err != nil {
						return
					}
					go func() {
						defer conn.Close()
						_ = service.NewConnection(context.Background(), conn, M.Metadata{})
					}()
				}
			}()
			node := map[string]any{"name": "test", "type": "ss", "server": "proxy.test", "port": listener.Addr().(*net.TCPAddr).Port,
				"cipher": cipher, "password": "private-node-password"}
			address, calls := proxyTestDNS(t, "127.0.0.1", D.RcodeSuccess)
			item, output, err := dnsWorker(t, node, []string{address}, "fake")
			if err != nil || item.Latency == nil || item.Error != "" || calls.Load() == 0 {
				t.Fatalf("worker=%+v, %v: %s", item, err, output)
			}
		})
	}
}
