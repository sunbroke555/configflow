//go:build linux

package routes

import (
	"context"
	"net"
	"syscall"

	"golang.org/x/sys/unix"
)

// Binding the verified uplink is essential: a bare TCP connect can be accepted
// by Mihomo's TUN before the actual node is reached, producing a false success.
// A bind permission failure is returned, never silently retried through TUN.
func dialIPv6OnInterface(ctx context.Context, iface, address string) (net.Conn, error) {
	dialer := net.Dialer{Control: func(network, address string, raw syscall.RawConn) error {
		var socketErr error
		if err := raw.Control(func(fd uintptr) {
			socketErr = unix.SetsockoptString(int(fd), unix.SOL_SOCKET, unix.SO_BINDTODEVICE, iface)
		}); err != nil {
			return err
		}
		return socketErr
	}}
	return dialer.DialContext(ctx, "tcp6", address)
}
