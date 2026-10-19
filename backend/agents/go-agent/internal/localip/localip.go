// Package localip discovers a LAN IPv4 address from local network interfaces.
package localip

import (
	"encoding/hex"
	"errors"
	"fmt"
	"net"
	"strings"
)

// Detect prefers the first usable LAN IPv4 address for Agent registration.
// If no LAN address exists, it also accepts VPN and point-to-point interfaces.
// It only inspects local interfaces and never makes a network request.
func Detect() (string, error) {
	return detect(net.Interfaces, interfaceAddresses)
}

// DetectAll returns every usable LAN IPv4 address for DNS protection, without
// duplicates. Interface and address enumeration order is preserved.
func DetectAll() ([]string, error) {
	return detectAll(net.Interfaces, interfaceAddresses)
}

func interfaceAddresses(iface net.Interface) ([]net.Addr, error) {
	return iface.Addrs()
}

func detect(list func() ([]net.Interface, error), addresses func(net.Interface) ([]net.Addr, error)) (string, error) {
	interfaces, err := list()
	if err != nil {
		return "", fmt.Errorf("list network interfaces: %w", err)
	}
	preferred, preferredErr := collect(interfaces, addresses, true, true)
	if len(preferred) > 0 {
		return preferred[0], nil
	}
	fallback, fallbackErr := collect(interfaces, addresses, false, true)
	if len(fallback) > 0 {
		return fallback[0], nil
	}
	return "", noAddressError(errors.Join(preferredErr, fallbackErr))
}

func detectAll(list func() ([]net.Interface, error), addresses func(net.Interface) ([]net.Addr, error)) ([]string, error) {
	interfaces, err := list()
	if err != nil {
		return nil, fmt.Errorf("list network interfaces: %w", err)
	}
	result, err := collect(interfaces, addresses, true, false)
	if err != nil {
		// A partial snapshot could silently omit an address that needs DNS
		// protection. Registration can still use its first known address.
		return nil, fmt.Errorf("could not inspect all LAN IPv4 addresses: %w", err)
	}
	if len(result) == 0 {
		return nil, noAddressError(err)
	}
	return result, nil
}

func noAddressError(cause error) error {
	if cause != nil {
		return fmt.Errorf("no usable local IPv4 address found: %w", cause)
	}
	return fmt.Errorf("no usable local IPv4 address found")
}

// preferred selects LAN candidates; false selects the remaining active
// interfaces only, allowing registration to retain VPN/PPP compatibility.
func collect(interfaces []net.Interface, addresses func(net.Interface) ([]net.Addr, error), preferred, firstOnly bool) ([]string, error) {
	var result []string
	seen := make(map[string]bool)
	var addressErr error
	for _, iface := range interfaces {
		if iface.Flags&net.FlagUp == 0 || iface.Flags&net.FlagLoopback != 0 {
			continue
		}
		lan := iface.Flags&net.FlagPointToPoint == 0 && !excludedInterface(iface.Name)
		if lan != preferred {
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
				value := ipv4.String()
				if !seen[value] {
					seen[value] = true
					result = append(result, value)
					if firstOnly {
						return result, nil
					}
				}
			}
		}
	}
	return result, addressErr
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
