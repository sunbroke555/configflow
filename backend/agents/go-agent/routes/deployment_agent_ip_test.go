package routes

import (
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestReadyActivationRejectsChangedAutomaticLANIPv4BeforeMutatingLive(t *testing.T) {
	for _, change := range []string{"address-changed", "address-unavailable", "agent-restarted"} {
		t.Run(change, func(t *testing.T) {
			cfg := testDeploymentConfig(t)
			cfg.NativeAIO = true
			currentIP := "192.168.8.2"
			var detectionErr error
			stubAgentIPv4(t, func() (string, error) { return currentIP, detectionErr })
			root := filepath.Dir(cfg.ConfigPath)
			cfg.StopCommand = "printf 'stop\\n' >> lifecycle-actions; rm -f running"
			cfg.StartCommand = "printf 'start\\n' >> lifecycle-actions; touch running"
			live := []byte("mode: direct\n")
			writeActivationFixture(t, cfg.ConfigPath, live, 0600)
			writeActivationFixture(t, filepath.Join(root, "running"), nil, 0600)
			const input = "mode: rule\ndns: {enable: true}\ntun: {enable: true, auto-route: true, auto-redirect: true}\n"
			state := submitBundle(t, cfg, "ready-ip-changed", map[string][]byte{"config.yaml": []byte(input)}, false)
			if state.Status != "ready" {
				t.Fatalf("prepare failed: %+v", state)
			}
			stage := filepath.Join(deploymentPath(cfg, state.DeploymentID), "stage", "config.yaml")
			prepared, err := os.ReadFile(stage)
			if err != nil {
				t.Fatal(err)
			}
			currentIP = "192.168.8.3"
			if change == "address-unavailable" {
				detectionErr = errors.New("no active LAN interface")
			}
			if change == "agent-restarted" {
				// A new Agent instance resumes the durable ready record without
				// an in-memory address captured by the upload handler.
				restarted := *cfg
				cfg = &restarted
			}
			if err := loadEffectiveManifest(cfg, state); err != nil {
				t.Fatal(err)
			}
			if err := validateDeploymentAgentIPs(cfg, state); err == nil {
				t.Fatal("stale address check unexpectedly passed")
			}
			// The guard itself is read-only. The normal failed-transaction
			// cleanup may remove stage after activation is rejected.
			data, err := os.ReadFile(stage)
			if err != nil || !bytes.Equal(data, prepared) {
				t.Fatalf("address validation changed prepared bytes: %q, %v", data, err)
			}
			state = activateReadyCandidate(t, cfg, state.DeploymentID)
			if state.Status != "failed" || state.FailedStage != "verifying" || !strings.Contains(state.Error, "重新发布") {
				t.Fatalf("unsafe activation was not rejected: %+v", state)
			}
			data, err = os.ReadFile(cfg.ConfigPath)
			if err != nil || !bytes.Equal(data, live) {
				t.Fatalf("rejected activation changed live configuration: %q, %v", data, err)
			}
			if _, err := os.Stat(filepath.Join(root, "running")); err != nil {
				t.Fatalf("running service changed: %v", err)
			}
			if _, err := os.Stat(filepath.Join(root, "lifecycle-actions")); !os.IsNotExist(err) {
				t.Fatalf("rejected activation ran a lifecycle command: %v", err)
			}
		})
	}
}

func TestReadyActivationAcceptsCurrentLANIPv4Coverage(t *testing.T) {
	for _, change := range []string{"reordered-addresses", "covered-new-address"} {
		t.Run(change, func(t *testing.T) {
			cfg := testDeploymentConfig(t)
			cfg.NativeAIO = true
			addresses := []string{"192.168.8.2", "192.168.8.3"}
			stubAgentIPv4s(t, func() ([]string, error) { return addresses, nil })
			excluded := ""
			if change == "covered-new-address" {
				addresses = []string{"192.168.8.2"}
				excluded = ", route-exclude-address: [192.168.8.0/24]"
			}
			input := "mode: rule\ndns: {enable: true}\ntun: {enable: true, auto-route: true, auto-redirect: true" + excluded + "}\n"
			state := submitBundle(t, cfg, "ready-ip-covered", map[string][]byte{"config.yaml": []byte(input)}, false)
			if state.Status != "ready" {
				t.Fatalf("prepare failed: %+v", state)
			}
			stage := filepath.Join(deploymentPath(cfg, state.DeploymentID), "stage", "config.yaml")
			prepared, err := os.ReadFile(stage)
			if err != nil {
				t.Fatal(err)
			}
			addresses = []string{"192.168.8.3", "192.168.8.2"}
			if change == "covered-new-address" {
				addresses = []string{"192.168.8.4"}
			}
			state = activateReadyCandidate(t, cfg, state.DeploymentID)
			if state.Status != "succeeded" {
				t.Fatalf("covered addresses blocked activation: %+v", state)
			}
			live, err := os.ReadFile(cfg.ConfigPath)
			if err != nil || !bytes.Equal(live, prepared) {
				t.Fatalf("activation changed prepared bytes: %q, %v", live, err)
			}
		})
	}
}

func TestDeploymentAgentIPCoverageOnlyChecksRequiredModes(t *testing.T) {
	for _, test := range []struct {
		name, content string
		native, need  bool
	}{
		{name: "ordinary", content: "tun: {enable: true, auto-redirect: true}\n"},
		{name: "tun-disabled", native: true, content: "tun: {enable: false, auto-redirect: true}\n"},
		{name: "redirect-disabled", native: true, content: "tun: {enable: true, auto-redirect: false}\n"},
		{name: "native", native: true, need: true, content: "tun: {enable: true, auto-redirect: true}\n"},
		{name: "gateway", need: true, content: "x-configflow-linux-gateway: {enabled: true}\ntun: {enable: true, auto-redirect: true}\n"},
	} {
		t.Run(test.name, func(t *testing.T) {
			cfg := &Config{ServiceType: "mihomo", NativeAIO: test.native, ConfigPath: filepath.Join(t.TempDir(), "config.yaml")}
			calls := 0
			stubAgentIPv4(t, func() (string, error) {
				calls++
				return "", errors.New("no active LAN interface")
			})
			state := &DeploymentState{DeploymentID: "check-ip-mode", Manifest: &DeploymentManifest{ConfigPath: "config.yaml"}}
			stage := filepath.Join(deploymentPath(cfg, state.DeploymentID), "stage")
			if err := os.MkdirAll(stage, 0700); err != nil {
				t.Fatal(err)
			}
			writeActivationFixture(t, filepath.Join(stage, "config.yaml"), []byte(test.content), 0600)
			err := validateDeploymentAgentIPs(cfg, state)
			if test.need && (err == nil || calls != 1) || !test.need && (err != nil || calls != 0) {
				t.Fatalf("mode check = %v, detections = %d, need = %v", err, calls, test.need)
			}
		})
	}
}

func TestReadyActivationRevalidatesExplicitAgentIPAndEveryCurrentLAN(t *testing.T) {
	for _, change := range []string{"second-lan-added", "explicit-ip-removed", "enumeration-failed", "legacy-candidate"} {
		t.Run(change, func(t *testing.T) {
			addresses := []string{"192.168.8.2"}
			var detectionErr error
			stubAgentIPv4s(t, func() ([]string, error) { return addresses, detectionErr })
			cfg := testDeploymentConfig(t)
			cfg.NativeAIO, cfg.AgentIP = change != "legacy-candidate", "192.168.8.2"
			root := filepath.Dir(cfg.ConfigPath)
			cfg.StopCommand = "printf 'stop\\n' >> lifecycle-actions; rm -f running"
			cfg.StartCommand = "printf 'start\\n' >> lifecycle-actions; touch running"
			live := []byte("mode: direct\n")
			writeActivationFixture(t, cfg.ConfigPath, live, 0600)
			writeActivationFixture(t, filepath.Join(root, "running"), nil, 0600)
			// An older candidate may only have trusted agent_ip. Prepare the
			// same immutable bytes without native adaptation to model it.
			const input = "mode: rule\ntun: {enable: true, auto-redirect: true, route-exclude-address: [127.0.0.0/8, 192.168.8.2/32]}\n"
			state := submitBundle(t, cfg, "ready-explicit-ip", map[string][]byte{"config.yaml": []byte(input)}, false)
			if state.Status != "ready" {
				t.Fatalf("prepare failed: %+v", state)
			}
			stage := filepath.Join(deploymentPath(cfg, state.DeploymentID), "stage", "config.yaml")
			prepared, err := os.ReadFile(stage)
			if err != nil {
				t.Fatal(err)
			}
			cfg.NativeAIO = true
			addresses = []string{"192.168.8.2", "192.168.8.3"}
			if change == "explicit-ip-removed" {
				addresses = []string{"192.168.8.3"}
			} else if change == "enumeration-failed" {
				detectionErr = errors.New("LAN enumeration failed")
			}
			if err := loadEffectiveManifest(cfg, state); err != nil {
				t.Fatal(err)
			}
			if err := validateDeploymentAgentIPs(cfg, state); err == nil {
				t.Fatal("explicit Agent IP bypassed current LAN validation")
			}
			if data, err := os.ReadFile(stage); err != nil || !bytes.Equal(data, prepared) {
				t.Fatalf("address guard changed immutable stage: %q, %v", data, err)
			}
			state = activateReadyCandidate(t, cfg, state.DeploymentID)
			if state.Status != "failed" || state.FailedStage != "verifying" || !strings.Contains(state.Error, "重新发布") {
				t.Fatalf("unsafe explicit-IP activation was not rejected: %+v", state)
			}
			if data, err := os.ReadFile(cfg.ConfigPath); err != nil || !bytes.Equal(data, live) {
				t.Fatalf("rejected activation changed live bytes: %q, %v", data, err)
			}
			if _, err := os.Stat(filepath.Join(root, "running")); err != nil {
				t.Fatalf("rejected activation stopped service: %v", err)
			}
			if _, err := os.Stat(filepath.Join(root, "lifecycle-actions")); !os.IsNotExist(err) {
				t.Fatalf("rejected activation ran a lifecycle command: %v", err)
			}
		})
	}
}
