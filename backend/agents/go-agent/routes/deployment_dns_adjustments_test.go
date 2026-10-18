package routes

import (
	"agent/preflight"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

const aioOriginalDNS = "dns: {enable: true, listen: '127.0.0.1:53'}\ntun: {enable: true, dns-hijack: ['any:53', 'tcp://any:53']}\n"

func aioAdjustmentReports(checks []DeploymentDNSCheck) []DeploymentDNSCheck {
	var found []DeploymentDNSCheck
	for _, check := range checks {
		if check.Name == "All in One DNS 自动调整" {
			found = append(found, check)
		}
	}
	return found
}

func assertAIOAdjustmentReport(t *testing.T, checks []DeploymentDNSCheck, peer string) {
	t.Helper()
	found := aioAdjustmentReports(checks)
	if len(found) != 1 || found[0].Level != "warn" {
		t.Fatalf("expected one visible adjustment, got %+v", checks)
	}
	for _, expected := range []string{peer, "dns.listen：'127.0.0.1:53' → '0.0.0.0:1053'", "tun.dns-hijack：['any:53', 'tcp://any:53'] → ['any:1053', 'tcp://any:1053']", "已完成的自动调整，不阻止发布"} {
		if !strings.Contains(found[0].Message, expected) {
			t.Fatalf("missing %q in %s", expected, found[0].Message)
		}
	}
}

func TestDeploymentAIOReportsAppliedDNSAdjustments(t *testing.T) {
	for _, mode := range []string{"native", "docker-oxidns", "docker-mosdns", "single"} {
		t.Run(mode, func(t *testing.T) {
			t.Setenv("ENABLE_OXIDNS", "false")
			t.Setenv("ENABLE_MOSDNS", "false")
			cfg := testDeploymentConfig(t)
			cfg.NativeAIO = mode == "native"
			peer := "OxiDNS"
			if mode == "docker-oxidns" {
				t.Setenv("ENABLE_OXIDNS", "true")
			} else if mode == "docker-mosdns" {
				t.Setenv("ENABLE_MOSDNS", "true")
				peer = "MosDNS"
			}
			useDNSCheck(t, func(candidate preflight.DNSCandidate, _ preflight.DNSOptions) preflight.Report {
				if mode != "single" {
					assertAIOPorts(t, string(candidate.Content), true, true)
				}
				return nil
			})
			s := submitBundle(t, cfg, "aio-adjustment", map[string][]byte{"config.yaml": []byte(aioOriginalDNS)}, true)
			if s.Status != "succeeded" {
				t.Fatalf("adjustment warning blocked publication: %+v", s)
			}
			if mode == "single" {
				if len(s.DNSAdjustments) != 0 || len(aioAdjustmentReports(s.DNSChecks)) != 0 {
					t.Fatalf("standalone deployment reported AIO adjustment: %+v", s)
				}
				return
			}
			assertAIOAdjustmentReport(t, s.DNSChecks, peer)
			assertAIOAdjustmentReport(t, s.DNSAdjustments, peer)
			w := httptest.NewRecorder()
			DeploymentHandler(cfg)(w, httptest.NewRequest(http.MethodGet, "/api/deployments/aio-adjustment", nil))
			var response DeploymentState
			if w.Code != 200 || json.Unmarshal(w.Body.Bytes(), &response) != nil {
				t.Fatalf("cannot retrieve adjustment report: %d %s", w.Code, w.Body.String())
			}
			assertAIOAdjustmentReport(t, response.DNSChecks, peer)
		})
	}
}

func TestDeploymentAIOAdjustmentSurvivesReadyActivationRecheck(t *testing.T) {
	cfg := testDeploymentConfig(t)
	cfg.NativeAIO = true
	calls := 0
	useDNSCheck(t, func(candidate preflight.DNSCandidate, _ preflight.DNSOptions) preflight.Report {
		assertAIOPorts(t, string(candidate.Content), true, true)
		calls++
		if calls == 1 {
			return preflight.Report{{Name: "旧现场提醒", Level: "warn", Message: "原先 DNS 尚未应答"}}
		}
		return preflight.Report{{Name: "最新现场结果", Level: "pass", Message: "当前 DNS 已应答"}}
	})
	s := submitBundle(t, cfg, "aio-adjust-ready", map[string][]byte{"config.yaml": []byte(aioOriginalDNS)}, false)
	if s.Status != "ready" {
		t.Fatalf("prepare failed: %+v", s)
	}
	assertAIOAdjustmentReport(t, s.DNSChecks, "OxiDNS")
	// Activation may revalidate a staged Mihomo file if it carries newer Geo
	// assets. Revalidating the already-adapted bytes must not erase the notice.
	if err := loadEffectiveManifest(cfg, s); err != nil {
		t.Fatal(err)
	}
	if err := validateStagedDeployment(cfg, s); err != nil {
		t.Fatal(err)
	}
	if err := saveDeployment(cfg, s, "ready"); err != nil {
		t.Fatal(err)
	}
	w := httptest.NewRecorder()
	DeploymentHandler(cfg)(w, httptest.NewRequest(http.MethodPost, "/api/deployments/aio-adjust-ready/activate", nil))
	if w.Code != 202 {
		t.Fatalf("activation: %d %s", w.Code, w.Body.String())
	}
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		s, _ = loadDeployment(cfg, "aio-adjust-ready")
		if s != nil && s.Status == "succeeded" {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if s == nil || s.Status != "succeeded" || calls != 2 {
		t.Fatalf("activation did not finish and recheck DNS: %+v (%d calls)", s, calls)
	}
	assertAIOAdjustmentReport(t, s.DNSChecks, "OxiDNS")
	for _, check := range s.DNSChecks {
		if check.Name == "旧现场提醒" {
			t.Fatal("old probe warning survived the new DNS check")
		}
	}
}

func TestDeploymentAIOAlreadyAdjustedDoesNotRepeatNotice(t *testing.T) {
	cfg := testDeploymentConfig(t)
	cfg.NativeAIO = true
	content := "dns: {enable: true, listen: '0.0.0.0:1053'}\ntun: {enable: true, dns-hijack: ['any:1053', 'tcp://any:1053']}\n"
	s := submitBundle(t, cfg, "aio-unchanged", map[string][]byte{"config.yaml": []byte(content)}, false)
	if s.Status != "ready" || len(s.DNSAdjustments) != 0 || len(aioAdjustmentReports(s.DNSChecks)) != 0 {
		t.Fatalf("unchanged AIO fields produced a notice: %+v", s)
	}
}

func TestDeploymentAIOAdjustmentKeepsRemainingTUNListenerWarning(t *testing.T) {
	cfg := testDeploymentConfig(t)
	cfg.NativeAIO = true
	// No external DNS listener is enabled, so the real DNS checker exercises
	// the remaining TUN listener risk without depending on the host network.
	useDNSCheck(t, preflight.CheckDNS)
	content := "dns: {enable: false, listen: ':53'}\ntun: {enable: true, dns-hijack: ['any:53', 'tcp://any:53']}\nlisteners: [{type: tun, name: extra-tun, dns-hijack: ['tcp://any:53']}]\n"
	s := submitBundle(t, cfg, "aio-listener-risk", map[string][]byte{"config.yaml": []byte(content)}, false)
	if s.Status != "ready" || len(aioAdjustmentReports(s.DNSChecks)) != 1 {
		t.Fatalf("top-level adjustment not reported: %+v", s)
	}
	for _, check := range s.DNSChecks {
		if check.Name == "DNS 劫持范围" && check.Level == "warn" {
			return
		}
	}
	t.Fatalf("remaining listener risk was hidden by the adjustment notice: %+v", s.DNSChecks)
}

func TestDeploymentAIOAdjustmentDoesNotEnableTUNOrExposeCredentials(t *testing.T) {
	for _, tun := range []string{"", "tun: {enable: false, dns-hijack: ['any:53', 'tcp://any:53']}\n"} {
		cfg := testDeploymentConfig(t)
		cfg.NativeAIO = true
		s := submitBundle(t, cfg, "aio-inactive-tun", map[string][]byte{"config.yaml": []byte("dns: {enable: false, listen: ':53'}\n" + tun)}, false)
		found := aioAdjustmentReports(s.DNSChecks)
		if s.Status != "ready" || len(found) != 1 || !strings.Contains(found[0].Message, "顶层 TUN 未开启；此次调整不会开启 TUN") {
			t.Fatalf("inactive TUN explanation missing: %+v", s)
		}
	}
	cfg := testDeploymentConfig(t)
	cfg.NativeAIO = true
	cfg.Token = "private-agent-token"
	content := "dns: {enable: false, listen: 'https://private-user:private-password@example.test/path?token=private-url-token'}\ntun: {enable: false, dns-hijack: ['tcp://private-user:private-password@example.test:53', private-agent-token]}\nsecret: core-private-secret\n"
	s := submitBundle(t, cfg, "aio-private-fields", map[string][]byte{"config.yaml": []byte(content)}, false)
	if s.Status != "ready" || len(aioAdjustmentReports(s.DNSChecks)) != 1 {
		t.Fatalf("private original settings failed adaptation: %+v", s)
	}
	state, err := os.ReadFile(filepath.Join(deploymentPath(cfg, s.DeploymentID), "state.json"))
	if err != nil {
		t.Fatal(err)
	}
	for _, private := range []string{"private-user", "private-password", "private-url-token", "private-agent-token", "core-private-secret", "example.test"} {
		if strings.Contains(string(state), private) {
			t.Fatalf("persisted adjustment exposed %q: %s", private, state)
		}
	}
}
