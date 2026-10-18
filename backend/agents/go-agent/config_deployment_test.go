package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestDeploymentConfigSurvivesSaveAndRouteConversion(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.json")
	cfg := &Config{
		ServiceType: "mihomo", ServiceManager: "command", ServiceBinary: "/usr/bin/mihomo",
		ServiceUnit: "mihomo", StopCommand: "stop", StartCommand: "start", StatusCommand: "status",
		HealthURL: "http://127.0.0.1:9090/version", HealthDNSAddress: "127.0.0.1:53",
		HealthDNSName: "health.test", DeploymentHealthTimeout: 12, Token: "private-token", filePath: path,
		NativeAIO: true, AgentServiceUnit: "configflow-agent-mihomo",
	}
	if err := cfg.Save(); err != nil {
		t.Fatal(err)
	}
	loaded, err := LoadConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	mapped := toRoutesConfig(loaded)
	if !mapped.NativeAIO || mapped.AgentServiceUnit != "configflow-agent-mihomo" || mapped.AgentConfigFile != path {
		t.Fatalf("lost native AIO config: %+v", mapped)
	}
	if mapped.ServiceManager != "command" || mapped.ServiceBinary != "/usr/bin/mihomo" || mapped.ServiceUnit != "mihomo" || mapped.StopCommand != "stop" || mapped.StartCommand != "start" || mapped.StatusCommand != "status" || mapped.HealthURL != cfg.HealthURL || mapped.HealthDNSAddress != cfg.HealthDNSAddress || mapped.HealthDNSName != cfg.HealthDNSName || mapped.DeploymentHealthTimeout != 12 {
		t.Fatalf("lost lifecycle config: %+v", mapped)
	}
	stat, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if stat.Mode().Perm() != 0600 {
		t.Fatalf("credentials file mode = %v", stat.Mode().Perm())
	}
}

func TestManagedServiceStatusUsesExplicitLifecycle(t *testing.T) {
	cfg := &Config{ServiceType: "mihomo", ServiceName: "does-not-exist", ServiceManager: "command", ConfigPath: filepath.Join(t.TempDir(), "config.yaml"), StopCommand: "true", StartCommand: "true", StatusCommand: "exit 0"}
	if got := cfg.managedServiceStatus(); got != "active" {
		t.Fatalf("running = %q", got)
	}
	cfg.StatusCommand = "exit 3"
	if got := cfg.managedServiceStatus(); got != "inactive" {
		t.Fatalf("stopped = %q", got)
	}
	cfg.StatusCommand = "exit 8"
	if got := cfg.managedServiceStatus(); got != "unknown" {
		t.Fatalf("error = %q", got)
	}
}
