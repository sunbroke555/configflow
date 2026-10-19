package routes

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"gopkg.in/yaml.v3"
)

func TestGatewayDNSStagingDoesNotEnableOrdinaryDNSOrTUN(t *testing.T) {
	t.Setenv("ENABLE_MOSDNS", "false")
	t.Setenv("ENABLE_OXIDNS", "false")
	for _, nativeAIO := range []bool{false, true} {
		name := "standalone"
		if nativeAIO {
			name = "native-aio"
		}
		t.Run(name, func(t *testing.T) {
			cfg := testDeploymentConfig(t)
			cfg.NativeAIO = nativeAIO
			// Ordinary agents need no gateway LAN address. Existing routing
			// exclusions and disabled DNS/TUN must survive the actual staging path.
			cfg.AgentIP = ""
			input := "mode: direct\ndns: {enable: false, listen: '127.0.0.1:5454'}\ntun: {enable: false, route-exclude-address: [10.0.0.0/8]}\nx-configflow-linux-gateway: {enabled: false, version: 1}\n"
			state := submitBundle(t, cfg, "ordinary-dns-boundary", map[string][]byte{"config.yaml": []byte(input)}, false)
			if state.Status != "ready" {
				t.Fatalf("ordinary staging failed: %+v", state)
			}
			data, err := os.ReadFile(filepath.Join(deploymentPath(cfg, state.DeploymentID), "stage", "config.yaml"))
			if err != nil {
				t.Fatal(err)
			}
			var doc map[string]interface{}
			if err := yaml.Unmarshal(data, &doc); err != nil {
				t.Fatal(err)
			}
			dns := doc["dns"].(map[string]interface{})
			tun := doc["tun"].(map[string]interface{})
			if dns["enable"] != false || tun["enable"] != false || doc["mode"] != "direct" ||
				!reflect.DeepEqual(tun["route-exclude-address"], []interface{}{"10.0.0.0/8"}) {
				t.Fatalf("ordinary DNS/TUN behavior changed: %s", data)
			}
			wantListen := "127.0.0.1:5454"
			if dns["listen"] != wantListen {
				t.Fatalf("ordinary listener changed: %s", data)
			}
			if _, err := os.Stat(cfg.ConfigPath); !os.IsNotExist(err) {
				t.Fatalf("staging wrote live configuration: %v", err)
			}
		})
	}
}

func TestGatewayDNSOverridesNativeAIOOnlyInMarkedStagedBundle(t *testing.T) {
	original := gatewayCoreVersion
	t.Cleanup(func() { gatewayCoreVersion = original })
	gatewayCoreVersion = func(string) (string, error) { return "Mihomo Meta v1.19.32 linux arm64", nil }
	cfg := testDeploymentConfig(t)
	cfg.NativeAIO = true
	cfg.AgentIP = "192.0.2.2"
	input := `mode: rule
dns: {enable: true, listen: '0.0.0.0:1053', nameserver: ['https://223.5.5.5/dns-query']}
tun: {enable: true, auto-route: true, auto-redirect: true, route-address-set: [cf_gateway_fake_ip], dns-hijack: [any:1053]}
rule-providers:
  cf_gateway_fake_ip: {type: inline, behavior: ipcidr, payload: [198.18.0.0/16]}
x-configflow-linux-gateway: {enabled: true, version: 1, profile_id: gateway-test}
`
	state := submitBundle(t, cfg, "gateway-aio-dns-boundary", map[string][]byte{"config.yaml": []byte(input)}, false)
	if state.Status != "ready" {
		t.Fatalf("gateway staging failed: %+v", state)
	}
	if err := loadEffectiveManifest(cfg, state); err != nil {
		t.Fatalf("adapted stage no longer matches its immutable manifest: %v", err)
	}
	data, err := os.ReadFile(filepath.Join(deploymentPath(cfg, state.DeploymentID), "stage", "config.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	var doc map[string]interface{}
	if err := yaml.Unmarshal(data, &doc); err != nil {
		t.Fatal(err)
	}
	tun := doc["tun"].(map[string]interface{})
	dns := doc["dns"].(map[string]interface{})
	if dns["listen"] != "127.0.0.1:1053" ||
		!reflect.DeepEqual(tun["dns-hijack"], []interface{}{}) ||
		!reflect.DeepEqual(tun["route-exclude-address"], []interface{}{"127.0.0.0/8", "192.0.2.2/32"}) ||
		!reflect.DeepEqual(tun["route-address-set"], []interface{}{"cf_gateway_fake_ip"}) ||
		!reflect.DeepEqual(dns["nameserver"], []interface{}{"https://223.5.5.5/dns-query"}) {
		t.Fatalf("native AIO overrode gateway DNS protection or routing: %s", data)
	}
	if _, err := os.Stat(cfg.ConfigPath); !os.IsNotExist(err) {
		t.Fatalf("staging wrote live configuration: %v", err)
	}
}
