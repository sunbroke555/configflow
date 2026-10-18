package routes

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"gopkg.in/yaml.v3"
)

func TestAIOPublishesMihomoDNSOn1053(t *testing.T) {
	for _, mode := range []string{"native", "docker-oxidns", "docker-mosdns", "single"} {
		t.Run(mode, func(t *testing.T) {
			t.Setenv("ENABLE_OXIDNS", "false")
			t.Setenv("ENABLE_MOSDNS", "false")
			cfg := testDeploymentConfig(t)
			cfg.NativeAIO = mode == "native"
			if mode == "docker-oxidns" {
				t.Setenv("ENABLE_OXIDNS", "true")
			}
			if mode == "docker-mosdns" {
				t.Setenv("ENABLE_MOSDNS", "true")
			}
			original := []byte("mode: rule\ndns:\n  enable: false\n  listen: 127.0.0.1:53\ntun:\n  enable: false\n  dns-hijack: [any:53, 'tcp://any:53']\n")
			s := submitBundle(t, cfg, "cohost-ports", map[string][]byte{"config.yaml": original}, true)
			if s.Status != "succeeded" {
				t.Fatalf("publish failed: %+v", s)
			}
			published, err := os.ReadFile(cfg.ConfigPath)
			if err != nil {
				t.Fatal(err)
			}
			if mode == "single" {
				var doc struct {
					DNS struct {
						Listen string `yaml:"listen"`
					} `yaml:"dns"`
					TUN struct {
						Hijack []string `yaml:"dns-hijack"`
					} `yaml:"tun"`
				}
				if err := yaml.Unmarshal(published, &doc); err != nil {
					t.Fatal(err)
				}
				if doc.DNS.Listen != "127.0.0.1:53" || len(doc.TUN.Hijack) != 2 || doc.TUN.Hijack[0] != "any:53" || doc.TUN.Hijack[1] != "tcp://any:53" {
					t.Fatalf("single-service publish changed DNS: %s", published)
				}
				return
			}
			assertAIOPorts(t, string(published), false, false)
			digest, size, err := hashFile(cfg.ConfigPath)
			if err != nil {
				t.Fatal(err)
			}
			manifestData, err := os.ReadFile(filepath.Join(deploymentRoot(cfg), "active-manifest.json"))
			if err != nil {
				t.Fatal(err)
			}
			var manifest DeploymentManifest
			if err := json.Unmarshal(manifestData, &manifest); err != nil {
				t.Fatal(err)
			}
			for _, file := range manifest.Files {
				if file.Path == "config.yaml" && (file.SHA256 != digest || file.Size != size) {
					t.Fatal("published manifest does not describe adapted bytes")
				}
			}
		})
	}
}

func assertAIOPorts(t *testing.T, content string, dnsEnabled, tunEnabled interface{}) {
	t.Helper()
	var doc map[string]interface{}
	if err := yaml.Unmarshal([]byte(content), &doc); err != nil {
		t.Fatal(err)
	}
	dns := doc["dns"].(map[string]interface{})
	tun := doc["tun"].(map[string]interface{})
	if dns["listen"] != "0.0.0.0:1053" || dns["enable"] != dnsEnabled || tun["enable"] != tunEnabled {
		t.Fatalf("unexpected DNS/TUN settings: %s", content)
	}
	hijacks, ok := tun["dns-hijack"].([]interface{})
	if !ok || len(hijacks) != 2 || hijacks[0] != "any:1053" || hijacks[1] != "tcp://any:1053" {
		t.Fatalf("port 53 could still be hijacked: %s", content)
	}
}

func TestMihomoAIOAddsMissingPortFieldsWithoutEnablingFeatures(t *testing.T) {
	for _, input := range []string{"mode: rule\n", "mode: rule\ndns: {}\ntun: {}\n", "mode: rule\ndns: null\ntun: null\n"} {
		got, err := modifyMihomoConfig(input)
		if err != nil {
			t.Fatal(err)
		}
		assertAIOPorts(t, got, nil, nil)
		again, err := modifyMihomoConfig(got)
		if err != nil || again != got {
			t.Fatalf("AIO port adaptation is not idempotent: %v\n%s", err, again)
		}
	}
	enabled, err := modifyMihomoConfig("mode: rule\ndns:\n  enable: true\ntun:\n  enable: true\n  auto-route: true\n")
	if err != nil {
		t.Fatal(err)
	}
	assertAIOPorts(t, enabled, true, true)
	if !strings.Contains(enabled, "auto-route: true") {
		t.Fatal("AIO adaptation changed existing TUN configuration")
	}
	for _, invalid := range []string{"", "{}", "[]", "mode: rule\ndns: 53", "mode: rule\ntun: []"} {
		if _, err := modifyMihomoConfig(invalid); err == nil {
			t.Fatalf("accepted invalid mapping %q", invalid)
		}
	}
}

func TestNativeAIOUninstallRejectsSharedOrMismatchedInstallation(t *testing.T) {
	valid := Config{NativeAIO: true, ServiceType: "mihomo", AgentConfigFile: "/opt/configflow-agent/mihomo/config.json", AgentServiceUnit: "configflow-agent-mihomo", ServiceManager: "systemd"}
	for _, mutate := range []func(*Config){
		func(c *Config) { c.AgentConfigFile = "/opt/configflow-agent/config-mihomo.json" },
		func(c *Config) { c.AgentConfigFile = "/opt/configflow-agent/oxidns/config.json" },
		func(c *Config) { c.AgentServiceUnit = "configflow-agent" },
		func(c *Config) { c.AgentServiceUnit = "configflow-agent-oxidns" },
		func(c *Config) { c.ServiceType = "mosdns" },
		func(c *Config) { c.ServiceManager = "supervisor" },
	} {
		cfg := valid
		mutate(&cfg)
		if _, err := nativeAIOInstallationFor(&cfg); err == nil {
			t.Fatalf("unsafe installation accepted: %+v", cfg)
		}
		w := httptest.NewRecorder()
		UninstallHandler(&cfg)(w, httptest.NewRequest(http.MethodPost, "/uninstall", nil))
		if w.Code != http.StatusConflict {
			t.Fatalf("unsafe uninstall not rejected before asynchronous work: %d", w.Code)
		}
	}
}

func TestNativeAIOUninstallOnlyRemovesSelectedInstance(t *testing.T) {
	for _, manager := range []string{"systemd", "openrc"} {
		for _, kind := range []string{"mihomo", "oxidns"} {
			t.Run(manager+"/"+kind, func(t *testing.T) {
				cfg := &Config{NativeAIO: true, ServiceType: kind, AgentConfigFile: "/opt/configflow-agent/" + kind + "/config.json", AgentServiceUnit: "configflow-agent-" + kind, ServiceManager: manager}
				instance, err := nativeAIOInstallationFor(cfg)
				if err != nil {
					t.Fatal(err)
				}
				root := t.TempDir()
				script := strings.ReplaceAll(instance.uninstallScript(), "'/", "'"+root+"/")
				sibling := "oxidns"
				if kind == sibling {
					sibling = "mihomo"
				}
				removed := []string{instance.Directory + "/config.json", instance.Log, "/etc/systemd/system/" + instance.Unit + ".service"}
				if manager == "openrc" {
					removed[2] = "/etc/init.d/" + instance.Unit
				}
				removed = append(removed, instance.Log+".1", "/etc/logrotate.d/"+instance.Unit)
				preserved := []string{"/opt/configflow-agent/" + sibling + "/config.json", "/usr/local/bin/configflow-agent", "/etc/" + kind + "/config.yaml", "/var/log/configflow-agent-" + sibling + ".log"}
				for _, path := range append(append([]string{}, removed...), preserved...) {
					full := filepath.Join(root, path)
					if err := os.MkdirAll(filepath.Dir(full), 0700); err != nil {
						t.Fatal(err)
					}
					if err := os.WriteFile(full, []byte("keep"), 0600); err != nil {
						t.Fatal(err)
					}
				}
				for _, command := range []string{"systemctl", "rc-service", "rc-update"} {
					if err := os.WriteFile(filepath.Join(root, command), []byte("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$TEST_MANAGER_LOG\"\n"), 0700); err != nil {
						t.Fatal(err)
					}
				}
				cmd := exec.Command("/bin/sh", "-c", script)
				cmd.Env = append(os.Environ(), "PATH="+root+":"+os.Getenv("PATH"), "TEST_MANAGER_LOG="+filepath.Join(root, "manager.log"))
				if output, err := cmd.CombinedOutput(); err != nil {
					t.Fatalf("uninstall failed: %v %s", err, output)
				}
				for _, path := range removed {
					if _, err := os.Stat(filepath.Join(root, path)); !os.IsNotExist(err) {
						t.Fatalf("instance file remains: %s", path)
					}
				}
				for _, path := range preserved {
					if got, err := os.ReadFile(filepath.Join(root, path)); err != nil || string(got) != "keep" {
						t.Fatalf("uninstall touched sibling/core file: %s (%v)", path, err)
					}
				}
				calls, _ := os.ReadFile(filepath.Join(root, "manager.log"))
				if strings.Contains(string(calls), sibling) || !strings.Contains(string(calls), instance.Unit) {
					t.Fatalf("wrong service lifecycle: %s", calls)
				}
			})
		}
	}
}

func TestNativeAIOLogSelection(t *testing.T) {
	for _, kind := range []string{"mihomo", "oxidns"} {
		if got := AgentLogPath(&Config{NativeAIO: true, ServiceType: kind}); got != "/var/log/configflow-agent-"+kind+".log" {
			t.Fatalf("wrong instance log: %s", got)
		}
	}
	if got := AgentLogPath(&Config{ServiceType: "mihomo"}); got != "/var/log/configflow-agent.log" {
		t.Fatalf("single-service log changed: %s", got)
	}
}
