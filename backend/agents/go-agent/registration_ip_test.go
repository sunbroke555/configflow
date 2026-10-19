package main

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"
)

func TestRegistrationAddressDetectionAndFallback(t *testing.T) {
	for _, test := range []struct {
		name, explicit, listen, detected, want string
		detectionErr                           error
	}{
		{name: "explicit", explicit: " 192.168.8.3 ", listen: "0.0.0.0", want: "192.168.8.3"},
		{name: "automatic", listen: "0.0.0.0", detected: "10.8.0.2", want: "10.8.0.2"},
		{name: "ipv4-wildcard", listen: "0.0.0.0", detectionErr: errors.New("no addresses")},
		{name: "ipv6-wildcard", listen: "::", detectionErr: errors.New("no addresses")},
		{name: "mapped-wildcard", listen: "::ffff:0.0.0.0", detectionErr: errors.New("no addresses")},
		{name: "loopback", listen: "127.0.0.1", detectionErr: errors.New("no addresses")},
		{name: "empty-listen", detectionErr: errors.New("no addresses")},
		{name: "specific-ipv4", listen: " 192.168.8.2 ", want: "192.168.8.2", detectionErr: errors.New("interface lookup failed")},
		{name: "specific-ipv6", listen: "2001:db8::2", want: "2001:db8::2", detectionErr: errors.New("interface lookup failed")},
	} {
		t.Run(test.name, func(t *testing.T) {
			original := detectLocalIPv4
			t.Cleanup(func() { detectLocalIPv4 = original })
			detectLocalIPv4 = func() (string, error) {
				if test.explicit != "" {
					t.Error("explicit agent_ip triggered detection")
				}
				return test.detected, test.detectionErr
			}
			requests := make(chan RegisterRequest, 1)
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != http.MethodPost || r.URL.Path != "/api/agents/register" {
					t.Errorf("unexpected request: %s %s", r.Method, r.URL.Path)
				}
				var request RegisterRequest
				if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
					t.Error(err)
				}
				requests <- request
				w.Header().Set("Content-Type", "application/json")
				_, _ = w.Write([]byte(`{"success":true,"id":"registered-agent","token":"registration-token"}`))
			}))
			defer server.Close()
			cfg := &Config{
				ServerURL: server.URL, AgentName: "registration-test", ServiceType: "mihomo",
				AgentIP: test.explicit, AgentHost: test.listen, AgentPort: 8080,
				filePath: filepath.Join(t.TempDir(), "config.json"),
			}
			if err := cfg.RegisterAgent(); err != nil {
				t.Fatal(err)
			}
			request := <-requests
			if request.Host != test.want {
				t.Fatalf("registered host = %q, want %q", request.Host, test.want)
			}
			if cfg.AgentIP != test.explicit {
				t.Fatalf("registration persisted the detected address: %q", cfg.AgentIP)
			}
		})
	}
}
