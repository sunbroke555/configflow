package preflight

import (
	"context"
	"encoding/binary"
	"io"
	"net"
	"net/netip"
	"strconv"
	"testing"
	"time"
)

func dnsTestAnswer(request []byte) []byte {
	response := append([]byte(nil), request...)
	response[2] = 0x81
	response[3] = 0x80
	response[6] = 0
	response[7] = 1
	return append(response, 0xc0, 0x0c, 0, 1, 0, 1, 0, 0, 0, 60, 0, 4, 203, 0, 113, 1)
}

func TestDNSWireProbeQueriesActualUDPAndTCPListener(t *testing.T) {
	for _, protocol := range []string{"udp", "tcp"} {
		t.Run(protocol, func(t *testing.T) {
			var address string
			done := make(chan error, 1)
			if protocol == "udp" {
				conn, err := net.ListenPacket("udp", "127.0.0.1:0")
				if err != nil {
					t.Fatal(err)
				}
				defer conn.Close()
				address = conn.LocalAddr().String()
				go func() {
					_ = conn.SetDeadline(time.Now().Add(2 * time.Second))
					buf := make([]byte, 4096)
					n, remote, err := conn.ReadFrom(buf)
					if err == nil {
						_, err = conn.WriteTo(dnsTestAnswer(buf[:n]), remote)
					}
					done <- err
				}()
			} else {
				listener, err := net.Listen("tcp", "127.0.0.1:0")
				if err != nil {
					t.Fatal(err)
				}
				defer listener.Close()
				address = listener.Addr().String()
				go func() {
					conn, err := listener.Accept()
					if err != nil {
						done <- err
						return
					}
					defer conn.Close()
					_ = conn.SetDeadline(time.Now().Add(2 * time.Second))
					var length [2]byte
					_, err = io.ReadFull(conn, length[:])
					if err != nil {
						done <- err
						return
					}
					buf := make([]byte, binary.BigEndian.Uint16(length[:]))
					_, err = io.ReadFull(conn, buf)
					if err != nil {
						done <- err
						return
					}
					response := dnsTestAnswer(buf)
					frame := append([]byte{byte(len(response) >> 8), byte(len(response))}, response...)
					_, err = conn.Write(frame)
					done <- err
				}()
			}
			host, portText, _ := net.SplitHostPort(address)
			port, _ := strconv.Atoi(portText)
			err := (wireDNSProbe{}).Query(context.Background(), portBinding{Protocol: protocol, Address: netip.MustParseAddr(host), Port: port}, "example.com")
			if err != nil {
				t.Fatalf("wire DNS failed: %v", err)
			}
			if err := <-done; err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestDNSWireResponseRejectsFailureAndUnrelatedPackets(t *testing.T) {
	question, _ := dnsWireQuestion("example.com")
	request := append([]byte{1, 2, 1, 0, 0, 1, 0, 0, 0, 0, 0, 0}, question...)
	good := dnsTestAnswer(request)
	if err := dnsValidateResponse(good, request, "example.com"); err != nil {
		t.Fatal(err)
	}
	for _, test := range []struct {
		name   string
		mutate func([]byte) []byte
	}{
		{"wrong ID", func(p []byte) []byte { p[0]++; return p }},
		{"SERVFAIL", func(p []byte) []byte { p[3] = 0x82; return p }},
		{"NXDOMAIN", func(p []byte) []byte { p[3] = 0x83; return p }},
		{"truncated", func(p []byte) []byte { p[2] |= 2; return p }},
		{"no answer", func(p []byte) []byte { p[7] = 0; return p }},
		{"not response", func(p []byte) []byte { p[2] &= 0x7f; return p }},
		{"other question", func(p []byte) []byte { p[13] = 'x'; return p }},
		{"pointer loop", func(p []byte) []byte { p[12] = 0xc0; p[13] = 12; return p }},
		{"short packet", func(p []byte) []byte { return p[:8] }},
	} {
		t.Run(test.name, func(t *testing.T) {
			if err := dnsValidateResponse(test.mutate(append([]byte(nil), good...)), request, "example.com"); err == nil {
				t.Fatal("invalid response accepted")
			}
		})
	}
}

func TestDNSWildcardRecognizesActualLocalLANAddress(t *testing.T) {
	addresses, err := net.InterfaceAddrs()
	if err != nil {
		t.Skip("interface enumeration unavailable")
	}
	for _, address := range addresses {
		prefix, err := netip.ParsePrefix(address.String())
		if err != nil {
			continue
		}
		ip := prefix.Addr().Unmap()
		if !ip.Is4() || ip.IsLoopback() || ip.IsUnspecified() {
			continue
		}
		requirements := []portRequirement{{portBinding: portBinding{Protocol: "udp", Address: netip.IPv4Unspecified(), Port: 53}}}
		if !dnsEndpointMatches(dnsParseUpstream(net.JoinHostPort(ip.String(), "53")), requirements) {
			t.Fatal("actual LAN address was not recognized as a local destination")
		}
		if dnsEndpointMatches(dnsParseUpstream("udp://8.8.8.8:53"), requirements) {
			t.Fatal("remote address matched wildcard")
		}
		return
	}
	t.Skip("no IPv4 LAN interface available")
}
