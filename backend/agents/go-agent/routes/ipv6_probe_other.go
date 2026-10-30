//go:build !linux

package routes

import (
	"context"
	"fmt"
	"net"
)

func dialIPv6OnInterface(context.Context, string, string) (net.Conn, error) {
	return nil, fmt.Errorf("IPv6 上联网卡绑定仅支持 Linux")
}
