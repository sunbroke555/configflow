package routes

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestNativeOxiDNSValidationLocation(t *testing.T) {
	binary := os.Getenv("OXIDNS_TEST_BINARY")
	if binary == "" {
		t.Skip("set OXIDNS_TEST_BINARY to test locations against the native OxiDNS core")
	}
	binary, err := filepath.Abs(binary)
	if err != nil {
		t.Fatal(err)
	}
	cfg := &Config{ServiceType: "oxidns", ServiceManager: "command", ServiceBinary: binary,
		ConfigPath: filepath.Join(t.TempDir(), "config.yaml")}
	const live = "existing live configuration\n"
	const candidate = "log:\n  level: error\nplugins:\n  - tag: invalid_plugin\n    type: invalid_native_fixture\n"
	if err := os.WriteFile(cfg.ConfigPath, []byte(live), 0600); err != nil {
		t.Fatal(err)
	}
	stage := filepath.Join(deploymentPath(cfg, "native-oxi-location"), "stage")
	if err := os.MkdirAll(stage, 0700); err != nil {
		t.Fatal(err)
	}
	stagedConfig := filepath.Join(stage, "config.yaml")
	if err := os.WriteFile(stagedConfig, []byte(candidate), 0600); err != nil {
		t.Fatal(err)
	}
	digest, size, err := hashFile(stagedConfig)
	if err != nil {
		t.Fatal(err)
	}
	state := &DeploymentState{DeploymentID: "native-oxi-location", Manifest: &DeploymentManifest{
		ConfigPath: "config.yaml", Files: []DeploymentFile{{Path: "config.yaml", SHA256: digest, Size: size}},
	}}
	err = validateStagedDeployment(cfg, state)
	var nativeExit *exec.ExitError
	if !errors.As(err, &nativeExit) || nativeExit.ExitCode() != 1 {
		t.Fatalf("expected a native core rejection, got %v", err)
	}
	_, details := deploymentErrorFields(err)
	for _, want := range []string{"文件：config.yaml（生成配置）", "行号：5\n", "内容：type: invalid_native_fixture",
		stagedConfig + ":5:11:", "Unknown plugin type: invalid_native_fixture"} {
		if !strings.Contains(details, want) {
			t.Errorf("native location %q missing from diagnostic:\n%s", want, details)
		}
	}
	// OxiDNS identifies the file and line, but does not provide a plugin index.
	// Never invent an index, nor map a nested resource's location to this file.
	if strings.Contains(details, "位置：") {
		t.Errorf("native output was assigned an unsupported field path: %s", details)
	}
	if strings.Contains(details, "行号：") && !strings.Contains(details, "行号：5\n") {
		t.Errorf("native output was assigned an incorrect line: %s", details)
	}
	current, readErr := os.ReadFile(cfg.ConfigPath)
	if readErr != nil || string(current) != live {
		t.Fatal("native rejection changed the existing configuration")
	}
}

// This test invokes only the core's -t validation command, with no listeners,
// TUN, external DNS, network providers or Geo rules. Its staged files are local.
func TestNativeMihomoValidationLocations(t *testing.T) {
	binary := os.Getenv("MIHOMO_BINARY")
	if binary == "" {
		t.Skip("set MIHOMO_BINARY to test locations against the native Mihomo core")
	}
	binary, err := filepath.Abs(binary)
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("ENABLE_MOSDNS", "false")
	t.Setenv("ENABLE_OXIDNS", "false")
	const prefix = "mode: rule\nsecret: private-core-secret\ndns:\n  enable: false\ntun:\n  enable: false\ngeo-auto-update: false\n"
	cases := []struct {
		name, configuration, position, lineContains, nativeError string
	}{
		{
			name:          "rule refers to an unknown policy",
			configuration: prefix + "rules:\n  - DOMAIN,valid.test,DIRECT\n  - DOMAIN,invalid.test,UnknownPolicy\n  - MATCH,DIRECT\n",
			position:      "rules[1]", lineContains: "DOMAIN,invalid.test,UnknownPolicy", nativeError: "proxy [UnknownPolicy] not found",
		},
		{
			name:          "invalid proxy type with secret in its name",
			configuration: prefix + "proxies:\n  - name: bad-node private-node-secret\n    type: unsupported-local-test\n    server: 127.0.0.1\n    port: 1080\n    password: private-node-secret\nrules:\n  - MATCH,DIRECT\n",
			position:      "proxies[0]", lineContains: "name: bad-node", nativeError: "unsupport proxy type: unsupported-local-test",
		},
		{
			name:          "group refers to an unknown node",
			configuration: prefix + "proxy-groups:\n  - name: TestGroup\n    type: select\n    proxies: [UnknownNode]\nrules:\n  - MATCH,DIRECT\n",
			position:      "proxy-groups[0]", lineContains: "name: TestGroup", nativeError: "'UnknownNode' not found",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cfg := &Config{
				ServiceType: "mihomo", ServiceBinary: binary, Token: "private-agent-token",
				ConfigPath: filepath.Join(t.TempDir(), "config.yaml"),
			}
			const live = "live configuration remains unchanged\n"
			if err := os.WriteFile(cfg.ConfigPath, []byte(live), 0600); err != nil {
				t.Fatal(err)
			}
			stage := filepath.Join(deploymentPath(cfg, "native-locations"), "stage")
			if err := os.MkdirAll(stage, 0700); err != nil {
				t.Fatal(err)
			}
			stagedConfig := filepath.Join(stage, "config.yaml")
			if err := os.WriteFile(stagedConfig, []byte(tc.configuration), 0600); err != nil {
				t.Fatal(err)
			}
			digest, size, err := hashFile(stagedConfig)
			if err != nil {
				t.Fatal(err)
			}
			state := &DeploymentState{DeploymentID: "native-locations", Manifest: &DeploymentManifest{
				ConfigPath: "config.yaml", Files: []DeploymentFile{{Path: "config.yaml", SHA256: digest, Size: size}},
			}}
			err = validateStagedDeployment(cfg, state)
			if err == nil {
				t.Fatal("native core accepted the invalid fixture")
			}
			_, details := deploymentErrorFields(err)
			// Validation normalizes YAML before invoking the core. Verify the line
			// against that exact file, independently of the diagnostic resolver.
			checked, readErr := os.ReadFile(stagedConfig)
			if readErr != nil {
				t.Fatal(readErr)
			}
			line, content := 0, ""
			for index, text := range strings.Split(string(checked), "\n") {
				if strings.Contains(text, tc.lineContains) {
					line, content = index+1, strings.TrimSpace(text)
					break
				}
			}
			if line == 0 {
				t.Fatalf("checked YAML no longer contains the expected source: %s", checked)
			}
			content = strings.ReplaceAll(content, "private-node-secret", "[REDACTED]")
			for _, want := range []string{"文件：config.yaml（生成配置）", "位置：" + tc.position,
				fmt.Sprintf("行号：%d\n", line), "内容：" + content, tc.nativeError} {
				if !strings.Contains(details, want) {
					t.Errorf("missing %q in diagnostic:\n%s", want, details)
				}
			}
			for _, secret := range []string{"private-core-secret", "private-node-secret", "private-agent-token"} {
				if strings.Contains(err.Error()+details, secret) {
					t.Errorf("diagnostic exposed %s", secret)
				}
			}
			current, readErr := os.ReadFile(cfg.ConfigPath)
			if readErr != nil || string(current) != live {
				t.Fatal("failed validation changed the live configuration")
			}
		})
	}
}
