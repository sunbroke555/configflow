package main

import (
	"context"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"
)

func TestHACheckCLIOnlyQueriesLocalOxiDNS(t *testing.T) {
	answering, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer answering.Close()
	go func() {
		buffer := make([]byte, 1500)
		for {
			n, addr, err := answering.ReadFrom(buffer)
			if err != nil {
				return
			}
			reply := append([]byte(nil), buffer[:n]...)
			reply[2] |= 0x80
			answering.WriteTo(reply, addr)
		}
	}()
	silent, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer silent.Close()
	for _, test := range []struct {
		name string
		addr net.Addr
		code int
	}{{"answering", answering.LocalAddr(), 0}, {"silent", silent.LocalAddr(), 1}} {
		t.Run(test.name, func(t *testing.T) {
			root := t.TempDir()
			_, port, _ := net.SplitHostPort(test.addr.String())
			yaml := "plugins:\n  - tag: udp\n    type: udp_server\n    args:\n      listen: 0.0.0.0:" + port + "\n      entry: main\n"
			if err := os.WriteFile(filepath.Join(root, "oxidns.yaml"), []byte(yaml), 0600); err != nil {
				t.Fatal(err)
			}
			configPath := filepath.Join(root, "agent.json")
			config := []byte(`{"service_type":"oxidns","config_path":"` + filepath.Join(root, "oxidns.yaml") + `","service_manager":"systemd"}`)
			if err := os.WriteFile(configPath, config, 0600); err != nil {
				t.Fatal(err)
			}
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			cmd := exec.CommandContext(ctx, os.Args[0], "-test.run=^TestAgentStartupProcess$", "--", "-config", configPath, "-ha-check")
			cmd.Env = append(os.Environ(), "CONFIGFLOW_STARTUP_TEST_HELPER=1")
			output, err := cmd.CombinedOutput()
			code := 0
			if exit, ok := err.(*exec.ExitError); ok {
				code = exit.ExitCode()
			} else if err != nil {
				t.Fatal(err)
			}
			if code != test.code || len(output) != 0 {
				t.Fatalf("exit %d, output %q", code, output)
			}
			if entries, err := os.ReadDir(root); err != nil || len(entries) != 2 {
				t.Fatalf("health check wrote state: %v %v", entries, err)
			}
		})
	}
}

func TestRegistrationAddressSkipsVirtualIP(t *testing.T) {
	original, originalExcept := detectLocalIPv4, detectLocalIPv4Except
	t.Cleanup(func() { detectLocalIPv4, detectLocalIPv4Except = original, originalExcept })
	detectLocalIPv4 = func() (string, error) { return "192.168.0.2", nil }
	var excluded []string
	detectLocalIPv4Except = func(values []string) (string, error) { excluded = values; return "192.168.0.20", nil }
	if ip, err := getLocalIP(); err != nil || ip != "192.168.0.2" || excluded != nil {
		t.Fatalf("detection without HA changed: %q %v %v", ip, err, excluded)
	}
	if ip, err := getLocalIP("192.168.0.2"); err != nil || ip != "192.168.0.20" || len(excluded) != 1 {
		t.Fatalf("virtual IP not excluded: %q %v %v", ip, err, excluded)
	}
}
