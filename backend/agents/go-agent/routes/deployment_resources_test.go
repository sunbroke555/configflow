package routes

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestGeoCapabilitiesReportOnlyReusableRegularFiles(t *testing.T) {
	cfg := testDeploymentConfig(t)
	root := filepath.Dir(cfg.ConfigPath)
	os.WriteFile(filepath.Join(root, "GeoSite.dat"), []byte("existing"), 0600)
	os.WriteFile(filepath.Join(root, "GeoIP.dat"), nil, 0600)
	os.Symlink("GeoSite.dat", filepath.Join(root, "ASN.mmdb"))
	recorder := httptest.NewRecorder()
	CapabilitiesHandler(cfg)(recorder, httptest.NewRequest(http.MethodGet, "/api/capabilities", nil))
	var value struct {
		Resources []string `json:"mihomo_resources"`
		Directory string   `json:"config_directory"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &value); err != nil {
		t.Fatal(err)
	}
	if value.Directory != root || len(value.Resources) != 1 || value.Resources[0] != "GeoSite.dat" {
		t.Fatalf("wrong inventory: %+v", value)
	}
}

func TestGeoFailureWinsOverNonfatalFingerprintLog(t *testing.T) {
	cfg := testDeploymentConfig(t)
	output := []byte("level=error msg=\"The global-client-fingerprint configuration is removed\"\n" +
		"level=info msg=\"Can't find GeoSite.dat, start download\"\n" +
		"level=error msg=\"can't download GeoSite.dat: EOF\"\nconfiguration file staged.yaml test failed\n")
	err := diagnosticFailure("Mihomo configuration check failed", errors.New("exit status 1"), output)
	summary, _ := deploymentErrorFields(err)
	if !strings.Contains(summary, "GeoSite.dat: EOF") || strings.Contains(summary, "fingerprint") {
		t.Fatalf("selected nonfatal log: %s", summary)
	}
	summary, details := deploymentErrorFields(mihomoResourceFailure(cfg, err))
	if !strings.Contains(summary, "GeoSite 下载失败") || !strings.Contains(details, "geox-url.geosite") ||
		!strings.Contains(details, filepath.Join(filepath.Dir(cfg.ConfigPath), "GeoSite.dat")) || !strings.Contains(details, "EOF") {
		t.Fatalf("missing repair details: %s %s", summary, details)
	}
}

func TestPanelGeoResourcePersistsForNextOfflineDeployment(t *testing.T) {
	cfg := testDeploymentConfig(t)
	if err := os.WriteFile(cfg.ServiceBinary, []byte("#!/bin/sh\ntest -s GeoSite.dat || exit 1\n"), 0700); err != nil {
		t.Fatal(err)
	}
	first := submitBundle(t, cfg, "panel-geo", map[string][]byte{
		"config.yaml": []byte("mode: rule\n"), "GeoSite.dat": []byte("panel-downloaded-database"),
	}, true, func(manifest *DeploymentManifest) {
		for index := range manifest.Files {
			if manifest.Files[index].Path == "GeoSite.dat" {
				manifest.Files[index].Role = "resource"
			}
		}
	})
	if first.Status != "succeeded" {
		t.Fatalf("first publish: %+v", first)
	}
	second := submitBundle(t, cfg, "panel-geo-offline", map[string][]byte{"config.yaml": []byte("mode: global\n")}, true)
	if second.Status != "succeeded" {
		t.Fatalf("offline publish: %+v", second)
	}
	if value, err := os.ReadFile(filepath.Join(filepath.Dir(cfg.ConfigPath), "GeoSite.dat")); err != nil || string(value) != "panel-downloaded-database" {
		t.Fatalf("lost Geo resource: %s %v", value, err)
	}
}

func TestGeoDownloadFailureLeavesLiveConfigurationUntouched(t *testing.T) {
	cfg := testDeploymentConfig(t)
	old := []byte("mode: direct\n")
	os.WriteFile(cfg.ConfigPath, old, 0600)
	os.WriteFile(cfg.ServiceBinary, []byte("#!/bin/sh\nprintf '%s\\n' 'level=error global-client-fingerprint configuration is removed' 'level=error cannot download GeoSite.dat: EOF'; exit 1\n"), 0700)
	result := submitBundle(t, cfg, "failed-geo-download", map[string][]byte{"config.yaml": []byte("mode: rule\n")}, true)
	if result.Status != "failed" || result.FailedStage != "verifying" || !strings.Contains(result.Error, "GeoSite 下载失败") {
		t.Fatalf("wrong failure: %+v", result)
	}
	if value, err := os.ReadFile(cfg.ConfigPath); err != nil || string(value) != string(old) {
		t.Fatalf("failed validation changed live configuration: %s %v", value, err)
	}
}
