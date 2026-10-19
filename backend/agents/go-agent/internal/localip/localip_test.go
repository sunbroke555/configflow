package localip

import (
	"errors"
	"net"
	"strings"
	"testing"
)

func testAddress(value string) net.Addr {
	return &net.IPNet{IP: net.ParseIP(value), Mask: net.CIDRMask(24, 32)}
}

func TestDetectFiltersInterfacesBeforeReadingAddresses(t *testing.T) {
	interfaces := []net.Interface{
		{Name: "eth-down"},
		{Name: "lo", Flags: net.FlagUp | net.FlagLoopback},
		{Name: "ppp0", Flags: net.FlagUp | net.FlagPointToPoint},
	}
	for _, name := range []string{"tun0", "utun1", "tap0", "Meta", "mihomo0", "docker0", "veth123", "br-012345abcdef"} {
		interfaces = append(interfaces, net.Interface{Name: name, Flags: net.FlagUp})
	}
	interfaces = append(interfaces, net.Interface{Name: "br-lan", Flags: net.FlagUp})
	ip, err := detect(func() ([]net.Interface, error) { return interfaces, nil }, func(iface net.Interface) ([]net.Addr, error) {
		if iface.Name != "br-lan" {
			t.Fatalf("read excluded interface %q", iface.Name)
		}
		return []net.Addr{testAddress("192.168.8.2")}, nil
	})
	if err != nil || ip != "192.168.8.2" {
		t.Fatalf("Detect() = %q, %v", ip, err)
	}
}

func TestDetectFiltersAddressesAndPreservesEnumerationOrder(t *testing.T) {
	interfaces := []net.Interface{{Name: "eth1", Flags: net.FlagUp}, {Name: "eth0", Flags: net.FlagUp}}
	ip, err := detect(func() ([]net.Interface, error) { return interfaces, nil }, func(iface net.Interface) ([]net.Addr, error) {
		if iface.Name != "eth1" {
			t.Fatal("did not preserve interface enumeration order")
		}
		return []net.Addr{
			(*net.IPNet)(nil), (*net.IPAddr)(nil),
			testAddress("2001:db8::1"), testAddress("127.0.0.1"), testAddress("0.0.0.0"),
			testAddress("169.254.1.2"), testAddress("224.0.0.1"), testAddress("255.255.255.255"),
			&net.IPAddr{IP: net.ParseIP("10.8.0.2")}, testAddress("192.168.8.2"),
		}, nil
	})
	if err != nil || ip != "10.8.0.2" {
		t.Fatalf("Detect() = %q, %v", ip, err)
	}
}

func TestDetectContinuesAfterInterfaceAddressError(t *testing.T) {
	interfaces := []net.Interface{{Name: "eth0", Flags: net.FlagUp}, {Name: "eth1", Flags: net.FlagUp}}
	ip, err := detect(func() ([]net.Interface, error) { return interfaces, nil }, func(iface net.Interface) ([]net.Addr, error) {
		if iface.Name == "eth0" {
			return nil, errors.New("interface disappeared")
		}
		return []net.Addr{testAddress("192.168.8.2")}, nil
	})
	if err != nil || ip != "192.168.8.2" {
		t.Fatalf("Detect() = %q, %v", ip, err)
	}
}

func TestDetectReportsFailuresWithoutLoopbackFallback(t *testing.T) {
	for _, test := range []struct {
		name       string
		interfaces []net.Interface
		listErr    error
		addrs      []net.Addr
		addressErr error
		want       string
	}{
		{name: "list-error", listErr: errors.New("unavailable"), want: "unavailable"},
		{name: "no-interfaces", want: "no usable LAN IPv4"},
		{name: "no-addresses", interfaces: []net.Interface{{Name: "eth0", Flags: net.FlagUp}}, want: "no usable LAN IPv4"},
		{name: "only-loopback", interfaces: []net.Interface{{Name: "eth0", Flags: net.FlagUp}}, addrs: []net.Addr{testAddress("127.0.0.1")}, want: "no usable LAN IPv4"},
		{name: "address-error", interfaces: []net.Interface{{Name: "eth0", Flags: net.FlagUp}}, addressErr: errors.New("denied"), want: "denied"},
	} {
		t.Run(test.name, func(t *testing.T) {
			ip, err := detect(func() ([]net.Interface, error) { return test.interfaces, test.listErr }, func(net.Interface) ([]net.Addr, error) {
				return test.addrs, test.addressErr
			})
			if ip != "" || err == nil || !strings.Contains(err.Error(), test.want) {
				t.Fatalf("Detect() = %q, %v; want empty address and %q error", ip, err, test.want)
			}
		})
	}
}
