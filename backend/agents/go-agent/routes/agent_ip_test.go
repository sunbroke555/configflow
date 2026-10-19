package routes

import (
	"errors"
	"strings"
	"testing"
)

func stubAgentIPv4(t *testing.T, detect func() (string, error)) {
	t.Helper()
	original := detectAgentIPv4
	detectAgentIPv4 = detect
	t.Cleanup(func() { detectAgentIPv4 = original })
}

func TestAgentLANIPv4UsesAutomaticAddressOnlyWhenUnspecified(t *testing.T) {
	for _, explicit := range []string{"", "  "} {
		t.Run("automatic-"+explicit, func(t *testing.T) {
			stubAgentIPv4(t, func() (string, error) { return "192.168.8.2", nil })
			cfg := &Config{AgentIP: explicit, AgentID: "already-registered"}
			ip, err := agentLANIPv4(cfg)
			if err != nil || ip.String() != "192.168.8.2" || cfg.AgentIP != explicit {
				t.Fatalf("automatic address = %v, err = %v, persisted IP = %q", ip, err, cfg.AgentIP)
			}
		})
	}
	stubAgentIPv4(t, func() (string, error) {
		t.Error("explicit agent_ip must not trigger automatic detection")
		return "192.168.8.2", nil
	})
	ip, err := agentLANIPv4(&Config{AgentIP: " 192.168.8.3 "})
	if err != nil || ip.String() != "192.168.8.3" {
		t.Fatalf("explicit address lost priority: %v, %v", ip, err)
	}
	for _, invalid := range []string{"0.0.0.0", "127.0.0.1", "::1", "2001:db8::1", "169.254.1.2", "224.0.0.1", "example.com"} {
		if _, err := agentLANIPv4(&Config{AgentIP: invalid}); err == nil {
			t.Fatalf("invalid explicit address %q silently fell back", invalid)
		}
	}
}

func TestAgentLANIPv4RejectsFailedOrInvalidAutomaticDetection(t *testing.T) {
	for _, detected := range []string{"", "0.0.0.0", "127.0.0.1", "::1", "169.254.1.2", "224.0.0.1"} {
		t.Run(detected, func(t *testing.T) {
			stubAgentIPv4(t, func() (string, error) { return detected, nil })
			if _, err := agentLANIPv4(&Config{}); err == nil {
				t.Fatalf("invalid automatic address %q accepted", detected)
			}
		})
	}
	stubAgentIPv4(t, func() (string, error) { return "", errors.New("interface enumeration failed") })
	if _, err := agentLANIPv4(&Config{}); err == nil || !strings.Contains(err.Error(), "interface enumeration failed") {
		t.Fatalf("lost detection failure: %v", err)
	}
}
