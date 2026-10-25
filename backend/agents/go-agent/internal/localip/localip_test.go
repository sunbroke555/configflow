package localip

import (
	"errors"
	"net"
	"reflect"
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

func TestDetectAllCollectsLANAddressesWithoutDuplicates(t *testing.T) {
	interfaces := []net.Interface{
		{Name: "docker0", Flags: net.FlagUp},
		{Name: "eth0", Flags: net.FlagUp},
		{Name: "br-lan", Flags: net.FlagUp},
	}
	addresses := func(iface net.Interface) ([]net.Addr, error) {
		switch iface.Name {
		case "eth0":
			return []net.Addr{testAddress("10.0.0.2"), testAddress("10.0.0.3"), testAddress("2001:db8::1")}, nil
		case "br-lan":
			return []net.Addr{testAddress("192.168.8.2"), testAddress("10.0.0.2"), testAddress("192.168.8.2")}, nil
		default:
			t.Fatalf("read excluded interface %q", iface.Name)
			return nil, nil
		}
	}
	got, err := detectAll(func() ([]net.Interface, error) { return interfaces, nil }, addresses)
	want := []string{"10.0.0.2", "10.0.0.3", "192.168.8.2"}
	if err != nil || !reflect.DeepEqual(got, want) {
		t.Fatalf("DetectAll() = %v, %v; want %v", got, err, want)
	}
}

func TestDetectAllowsVPNAndPointToPointRegistrationWithoutLAN(t *testing.T) {
	for _, iface := range []net.Interface{
		{Name: "ppp0", Flags: net.FlagUp | net.FlagPointToPoint},
		{Name: "tap0", Flags: net.FlagUp | net.FlagBroadcast},
		{Name: "wg0", Flags: net.FlagUp | net.FlagPointToPoint},
	} {
		t.Run(iface.Name, func(t *testing.T) {
			list := func() ([]net.Interface, error) { return []net.Interface{iface}, nil }
			addresses := func(net.Interface) ([]net.Addr, error) {
				return []net.Addr{testAddress("10.8.0.2")}, nil
			}
			if ip, err := detect(list, addresses); err != nil || ip != "10.8.0.2" {
				t.Fatalf("registration address = %q, %v", ip, err)
			}
			if ips, err := detectAll(list, addresses); err == nil || len(ips) != 0 {
				t.Fatalf("DNS protection included a non-LAN interface: %v, %v", ips, err)
			}
		})
	}
}

func TestDetectAllRejectsIncompleteInterfaceSnapshot(t *testing.T) {
	list := func() ([]net.Interface, error) {
		return []net.Interface{{Name: "eth0", Flags: net.FlagUp}, {Name: "eth1", Flags: net.FlagUp}}, nil
	}
	addresses := func(iface net.Interface) ([]net.Addr, error) {
		if iface.Name == "eth1" {
			return nil, errors.New("interface changed during detection")
		}
		return []net.Addr{testAddress("192.168.8.2")}, nil
	}
	if ip, err := detect(list, addresses); err != nil || ip != "192.168.8.2" {
		t.Fatalf("registration lost a usable address: %q, %v", ip, err)
	}
	if ips, err := detectAll(list, addresses); len(ips) != 0 || err == nil || !strings.Contains(err.Error(), "interface changed") {
		t.Fatalf("partial DNS protection snapshot accepted: %v, %v", ips, err)
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
		{name: "no-interfaces", want: "no usable local IPv4"},
		{name: "no-addresses", interfaces: []net.Interface{{Name: "eth0", Flags: net.FlagUp}}, want: "no usable local IPv4"},
		{name: "only-loopback", interfaces: []net.Interface{{Name: "eth0", Flags: net.FlagUp}}, addrs: []net.Addr{testAddress("127.0.0.1")}, want: "no usable local IPv4"},
		{name: "address-error", interfaces: []net.Interface{{Name: "eth0", Flags: net.FlagUp}}, addressErr: errors.New("denied"), want: "denied"},
	} {
		t.Run(test.name, func(t *testing.T) {
			ip, err := detect(func() ([]net.Interface, error) { return test.interfaces, test.listErr }, func(net.Interface) ([]net.Addr, error) {
				return test.addrs, test.addressErr
			})
			if ip != "" || err == nil || !strings.Contains(err.Error(), test.want) {
				t.Fatalf("Detect() = %q, %v; want empty address and %q error", ip, err, test.want)
			}
			ips, err := detectAll(func() ([]net.Interface, error) { return test.interfaces, test.listErr }, func(net.Interface) ([]net.Addr, error) {
				return test.addrs, test.addressErr
			})
			if len(ips) != 0 || err == nil || !strings.Contains(err.Error(), test.want) {
				t.Fatalf("DetectAll() = %v, %v; want no addresses and %q error", ips, err, test.want)
			}
		})
	}
}

func TestDetectExceptSkipsFloatingAddresses(t *testing.T) {
	interfaces := []net.Interface{{Name: "eth0", Flags: net.FlagUp}}
	addresses := func(net.Interface) ([]net.Addr, error) {
		return []net.Addr{testAddress("192.168.0.2"), testAddress("192.168.0.20")}, nil
	}
	if ip, err := detect(func() ([]net.Interface, error) { return interfaces, nil }, addresses); err != nil || ip != "192.168.0.2" {
		t.Fatalf("Detect() = %q, %v", ip, err)
	}
	if ip, err := detect(func() ([]net.Interface, error) { return interfaces, nil }, without(addresses, []string{"192.168.0.2"})); err != nil || ip != "192.168.0.20" {
		t.Fatalf("DetectExcept() = %q, %v", ip, err)
	}
	if _, err := detect(func() ([]net.Interface, error) { return interfaces, nil }, without(addresses, []string{"192.168.0.2", "192.168.0.20"})); err == nil {
		t.Fatal("excluded every address but still detected one")
	}
}
