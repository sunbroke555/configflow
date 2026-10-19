// Package localip discovers a LAN IPv4 address from local network interfaces.
package localip

import (
	"encoding/hex"
	"fmt"
	"net"
	"strings"
)

// Detect returns the first usable IPv4 address on an active LAN interface.
// It only inspects local interfaces and never makes a network request.
func Detect() (string, error) {
	return detect(net.Interfaces, func(iface net.Interface) ([]net.Addr, error) {
		return iface.Addrs()
	})
}

func detect(list func() ([]net.Interface, error), addresses func(net.Interface) ([]net.Addr, error)) (string, error) {
	interfaces, err := list()
	if err != nil {
		return "", fmt.Errorf("list network interfaces: %w", err)
	}
	var addressErr error
	for _, iface := range interfaces {
		if iface.Flags&net.FlagUp == 0 || iface.Flags&(net.FlagLoopback|net.FlagPointToPoint) != 0 || excludedInterface(iface.Name) {
			continue
		}
		addrs, err := addresses(iface)
		if err != nil {
			addressErr = fmt.Errorf("read addresses for %s: %w", iface.Name, err)
			continue
		}
		for _, addr := range addrs {
			var ip net.IP
			switch value := addr.(type) {
			case *net.IPNet:
				if value != nil {
					ip = value.IP
				}
			case *net.IPAddr:
				if value != nil {
					ip = value.IP
				}
			}
			if ipv4 := ip.To4(); ipv4 != nil && ipv4.IsGlobalUnicast() && !ipv4.IsLoopback() {
				return ipv4.String(), nil
			}
		}
	}
	if addressErr != nil {
		return "", fmt.Errorf("no usable LAN IPv4 address found: %w", addressErr)
	}
	return "", fmt.Errorf("no usable LAN IPv4 address found")
}

func excludedInterface(name string) bool {
	name = strings.ToLower(name)
	// Docker user-defined bridges use br-<12 hex digits>. Keep ordinary
	// LAN bridge names such as br-lan eligible for automatic detection.
	if len(name) == 15 && strings.HasPrefix(name, "br-") {
		if _, err := hex.DecodeString(name[3:]); err == nil {
			return true
		}
	}
	for _, prefix := range []string{"tun", "utun", "tap", "meta", "mihomo", "docker", "veth"} {
		if strings.HasPrefix(name, prefix) {
			return true
		}
	}
	return false
}
