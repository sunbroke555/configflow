package preflight

import "testing"

func TestIPv6SysctlAssignmentsLoaderAndInterfaceNames(t *testing.T) {
	fixture := healthySystemFixture()
	fixture.useLoader("systemd")
	fixture.files = map[string][]byte{
		"/usr/lib/sysctl.d/10-v6.conf": []byte("net.ipv6.conf.all.forwarding=0"),
		"/etc/sysctl.d/10-v6.conf":     []byte("# owned by user\n-net.ipv6.conf.all.forwarding = 1\nnet/ipv6/conf/eth0.100/accept_ra=2 # VLAN\n"),
		"/run/sysctl.d/90-v6.conf":     []byte("net.ipv6.conf.default.accept_ra=2"),
		"/etc/sysctl.conf":             []byte("net.ipv6.conf.all.forwarding=0"),
	}
	got, unknown := ipv6SysctlAssignmentsWith(fixture.probe(), nil)
	if unknown || len(got) != 3 || got[0].Key != "net/ipv6/conf/all/forwarding" || got[0].Value != "1" || got[0].Line != 2 || !got[0].Persistent || got[1].Key != "net/ipv6/conf/eth0.100/accept_ra" || got[2].Persistent {
		t.Fatalf("wrong loader evidence: %+v unknown=%v", got, unknown)
	}
	// Simulating a new file must affect only the returned evidence.
	pending := map[string][]byte{"/etc/sysctl.d/99-configflow-ipv6.conf": []byte("net/ipv6/conf/eth0/accept_ra=2\n")}
	got, unknown = ipv6SysctlAssignmentsWith(fixture.probe(), pending)
	if unknown || len(got) != 4 || got[3].Value != "2" {
		t.Fatalf("pending not included: %+v", got)
	}
	if _, exists := fixture.files["/etc/sysctl.d/99-configflow-ipv6.conf"]; exists {
		t.Fatal("simulation wrote a file")
	}
}
