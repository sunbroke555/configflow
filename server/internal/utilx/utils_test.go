package utilx

import (
	"errors"
	"net"
	"strings"
	"testing"
)

type fakeConn struct {
	net.Conn
	addr net.Addr
}

func (c fakeConn) LocalAddr() net.Addr { return c.addr }
func (c fakeConn) Close() error        { return nil }

func TestLocalIP(t *testing.T) {
	var seen string
	ok := func(network, address string) (net.Conn, error) {
		seen = network + " " + address
		return fakeConn{addr: &net.UDPAddr{IP: net.ParseIP("192.0.2.10"), Port: 5555}}, nil
	}
	if got := LocalIP(ok); got != "192.0.2.10" || seen != "udp4 8.8.8.8:80" {
		t.Errorf("got %q after %q", got, seen)
	}
	failing := func(string, string) (net.Conn, error) { return nil, errors.New("unreachable") }
	if got := LocalIP(failing); got != "127.0.0.1" {
		t.Errorf("got %q", got)
	}
	if ip := net.ParseIP(GetLocalIP()); ip == nil {
		t.Error("GetLocalIP is not an address")
	}
}

func TestGenerateRandomToken(t *testing.T) {
	seen := map[string]bool{}
	for i := 0; i < 50; i++ {
		token := GenerateRandomToken(32)
		if len(token) != 32 || strings.Trim(token, tokenAlphabet) != "" {
			t.Fatalf("bad token %q", token)
		}
		seen[token] = true
	}
	if len(seen) != 50 {
		t.Error("tokens repeat")
	}
	if GenerateRandomToken(0) != "" || len(GenerateRandomToken(5)) != 5 {
		t.Error("length")
	}
}

func TestStrToBool(t *testing.T) {
	for v, want := range map[any]bool{"true": true, "TRUE": true, "True": true, "false": false, "": false, "yes": false, "1": false, true: true, false: false, nil: false, 1: false, " true": false} {
		if got := StrToBool(v); got != want {
			t.Errorf("%#v: %v", v, got)
		}
	}
}
