package routes

import (
	"errors"
	"strings"
	"testing"
)

func stubAgentIPv4(t *testing.T, detect func() (string, error)) {
	t.Helper()
	stubAgentIPv4s(t, func() ([]string, error) {
		ip, err := detect()
		return []string{ip}, err
	})
}

func stubAgentIPv4s(t *testing.T, detect func() ([]string, error)) {
	t.Helper()
	original := detectAgentIPv4s
	detectAgentIPv4s = detect
	t.Cleanup(func() { detectAgentIPv4s = original })
}

func TestAgentLANIPv4UsesAutomaticAddressOnlyWhenUnspecified(t *testing.T) {
	for _, explicit := range []string{"", "  "} {
		t.Run("automatic-"+explicit, func(t *testing.T) {
			stubAgentIPv4(t, func() (string, error) { return "192.168.8.2", nil })
			cfg := &Config{AgentIP: explicit, AgentID: "already-registered"}
			ips, err := agentLANIPv4s(cfg)
			if err != nil || len(ips) != 1 || ips[0].String() != "192.168.8.2" || cfg.AgentIP != explicit {
				t.Fatalf("automatic addresses = %v, err = %v, persisted IP = %q", ips, err, cfg.AgentIP)
			}
		})
	}
	stubAgentIPv4(t, func() (string, error) {
		t.Error("explicit agent_ip must not trigger automatic detection")
		return "192.168.8.2", nil
	})
	ips, err := agentLANIPv4s(&Config{AgentIP: " 192.168.8.3 "})
	if err != nil || len(ips) != 1 || ips[0].String() != "192.168.8.3" {
		t.Fatalf("explicit address lost priority: %v, %v", ips, err)
	}
	for _, invalid := range []string{"0.0.0.0", "127.0.0.1", "::1", "2001:db8::1", "169.254.1.2", "224.0.0.1", "example.com"} {
		if _, err := agentLANIPv4s(&Config{AgentIP: invalid}); err == nil {
			t.Fatalf("invalid explicit address %q silently fell back", invalid)
		}
	}
}

func TestAgentLANIPv4RejectsFailedOrInvalidAutomaticDetection(t *testing.T) {
	for _, detected := range []string{"", "0.0.0.0", "127.0.0.1", "::1", "169.254.1.2", "224.0.0.1"} {
		t.Run(detected, func(t *testing.T) {
			stubAgentIPv4(t, func() (string, error) { return detected, nil })
			if _, err := agentLANIPv4s(&Config{}); err == nil {
				t.Fatalf("invalid automatic address %q accepted", detected)
			}
		})
	}
	for _, detected := range [][]string{nil, {"192.168.8.2", "127.0.0.1"}} {
		t.Run("invalid-address-list", func(t *testing.T) {
			stubAgentIPv4s(t, func() ([]string, error) { return detected, nil })
			if _, err := agentLANIPv4s(&Config{}); err == nil {
				t.Fatalf("invalid automatic address list accepted: %v", detected)
			}
		})
	}
	stubAgentIPv4(t, func() (string, error) { return "", errors.New("interface enumeration failed") })
	if _, err := agentLANIPv4s(&Config{}); err == nil || !strings.Contains(err.Error(), "interface enumeration failed") {
		t.Fatalf("lost detection failure: %v", err)
	}
}

func TestAgentLANIPv4IncludesEveryAutomaticAddressOnce(t *testing.T) {
	stubAgentIPv4s(t, func() ([]string, error) { return []string{"192.168.8.2", "10.0.0.2", "192.168.8.2"}, nil })
	ips, err := agentLANIPv4s(&Config{})
	if err != nil || len(ips) != 2 || ips[0].String() != "192.168.8.2" || ips[1].String() != "10.0.0.2" {
		t.Fatalf("automatic addresses were lost or duplicated: %v, %v", ips, err)
	}
}
