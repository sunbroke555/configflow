package main

import (
	"bytes"
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestEnvironmentCheckRejectsInvalidPeerWithoutMigrationOrRegistration(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "candidate.json")
	peer := filepath.Join(root, "peer.json")
	config := []byte(`{"server_url":"http://127.0.0.1:1","service_type":"mihomo","config_path":"/etc/mihomo/config.yaml","agent_port":8080,"native_aio":true}`)
	badPeer := []byte(`{"token":"PRIVATE-TOKEN",`)
	for name, data := range map[string][]byte{path: config, peer: badPeer} {
		if err := os.WriteFile(name, data, 0600); err != nil {
			t.Fatal(err)
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, os.Args[0], "-test.run=^TestAgentStartupProcess$", "--", "-config", path, "-environment-peer-config", peer, "-check-environment")
	cmd.Env = append(os.Environ(), "CONFIGFLOW_STARTUP_TEST_HELPER=1")
	output, err := cmd.CombinedOutput()
	if exit, ok := err.(*exec.ExitError); !ok || exit.ExitCode() != 1 {
		t.Fatalf("invalid peer must stop installation: %v\n%s", err, output)
	}
	if !strings.Contains(string(output), "环境检查未通过") || !strings.Contains(string(output), "处理方法") || strings.Contains(string(output), "PRIVATE-TOKEN") {
		t.Fatalf("missing or unsafe diagnostics: %s", output)
	}
	for name, data := range map[string][]byte{path: config, peer: badPeer} {
		got, err := os.ReadFile(name)
		if err != nil || !bytes.Equal(got, data) {
			t.Fatalf("preflight changed candidate: %v", err)
		}
	}
	entries, err := os.ReadDir(root)
	if err != nil || len(entries) != 2 {
		t.Fatalf("preflight unexpectedly wrote migration or deployment state: %v", entries)
	}
}
