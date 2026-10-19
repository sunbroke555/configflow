package routes

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"golang.org/x/sys/unix"
)

func ruleSnapshotFixture(t *testing.T, service string) (*Config, *DeploymentManifest) {
	t.Helper()
	cfg := &Config{AgentID: "snapshot-agent", Token: "snapshot-secret", ServiceType: service, ConfigPath: filepath.Join(t.TempDir(), "config.yaml")}
	manifest := &DeploymentManifest{ProtocolVersion: 1, DeploymentID: "active-release", AgentID: cfg.AgentID, ProfileID: "profile-1", ServiceType: service, ConfigPath: "config.yaml"}
	for _, item := range []struct{ path, content, role string }{
		{"config.yaml", "mode: rule\n# private secret\n", "config"},
		{"rules/test.txt", "old.test\n", "rule"},
		{"providers/test.yaml", "proxies: []\n", "provider"},
		{"custom/example.txt", "custom config material\n", "custom"},
	} {
		if err := os.MkdirAll(filepath.Dir(filepath.Join(filepath.Dir(cfg.ConfigPath), item.path)), 0700); err != nil {
			t.Fatal(err)
		}
		writeActivationFixture(t, filepath.Join(filepath.Dir(cfg.ConfigPath), item.path), []byte(item.content), 0600)
		manifest.Files = append(manifest.Files, DeploymentFile{Path: item.path, Size: int64(len(item.content)), SHA256: sha256Hex([]byte(item.content)), Role: item.role})
	}
	writeRuleSnapshotManifest(t, cfg, manifest)
	return cfg, manifest
}

func writeRuleSnapshotManifest(t *testing.T, cfg *Config, manifest *DeploymentManifest) {
	t.Helper()
	data, err := json.Marshal(manifest)
	if err != nil {
		t.Fatal(err)
	}
	if err = os.MkdirAll(deploymentRoot(cfg), 0700); err != nil {
		t.Fatal(err)
	}
	writeActivationFixture(t, filepath.Join(deploymentRoot(cfg), "active-manifest.json"), data, 0600)
}

func ruleSnapshotRequest(cfg *Config, method, target, body, token string) *httptest.ResponseRecorder {
	mux := http.NewServeMux()
	RegisterRoutes(mux, cfg)
	request := httptest.NewRequest(method, target, strings.NewReader(body))
	if token != "" {
		request.Header.Set("Authorization", "Bearer "+token)
	}
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, request)
	return w
}

func unpackRuleSnapshot(t *testing.T, response *httptest.ResponseRecorder) (*DeploymentManifest, map[string][]byte) {
	t.Helper()
	if response.Code != http.StatusOK || response.Header().Get("Content-Type") != "application/gzip" || response.Header().Get("Cache-Control") != "no-store" {
		t.Fatalf("snapshot failed: %d %s", response.Code, response.Body.String())
	}
	if response.Header().Get("X-Content-SHA256") != sha256Hex(response.Body.Bytes()) {
		t.Fatal("snapshot archive digest mismatch")
	}
	compressed, err := gzip.NewReader(bytes.NewReader(response.Body.Bytes()))
	if err != nil {
		t.Fatal(err)
	}
	defer compressed.Close()
	archive := tar.NewReader(compressed)
	var manifest DeploymentManifest
	files := map[string][]byte{}
	for {
		header, err := archive.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		data, err := io.ReadAll(archive)
		if err != nil {
			t.Fatal(err)
		}
		if header.Name == "manifest.json" {
			if err = json.Unmarshal(data, &manifest); err != nil {
				t.Fatal(err)
			}
		} else {
			if !strings.HasPrefix(header.Name, "files/") || header.Typeflag != tar.TypeReg {
				t.Fatalf("unexpected archive entry %s", header.Name)
			}
			files[strings.TrimPrefix(header.Name, "files/")] = data
		}
	}
	if len(manifest.Files) != len(files) {
		t.Fatal("manifest does not match archive entries")
	}
	for _, entry := range manifest.Files {
		data, exists := files[entry.Path]
		if !exists || entry.Size != int64(len(data)) || entry.SHA256 != sha256Hex(data) {
			t.Fatalf("snapshot metadata does not match actual bytes: %s", entry.Path)
		}
	}
	return &manifest, files
}

func TestRuleSyncSnapshotCapturesCurrentFilesAndKeepsDeploymentIdentity(t *testing.T) {
	for _, service := range []string{"mihomo", "oxidns"} {
		t.Run(service, func(t *testing.T) {
			cfg, active := ruleSnapshotFixture(t, service)
			writeActivationFixture(t, filepath.Join(filepath.Dir(cfg.ConfigPath), "rules/test.txt"), []byte("new.test\nmore.test\n"), 0600)
			writeActivationFixture(t, filepath.Join(filepath.Dir(cfg.ConfigPath), "providers/test.yaml"), []byte("proxies: [{name: updated}]\n"), 0600)
			writeActivationFixture(t, filepath.Join(filepath.Dir(cfg.ConfigPath), "unmanaged-secret"), []byte("not exported"), 0600)
			manifest, files := unpackRuleSnapshot(t, ruleSnapshotRequest(cfg, "GET", "/api/rule-sync/snapshot", "", cfg.Token))
			if manifest.ProtocolVersion != 1 || manifest.DeploymentID != active.DeploymentID || manifest.AgentID != active.AgentID || manifest.ProfileID != active.ProfileID || manifest.ServiceType != service || manifest.ConfigPath != active.ConfigPath {
				t.Fatalf("snapshot changed deployment identity: %+v", manifest)
			}
			if string(files["rules/test.txt"]) != "new.test\nmore.test\n" || string(files["providers/test.yaml"]) != "proxies: [{name: updated}]\n" {
				t.Fatal("snapshot used stale publication bytes")
			}
			if _, exists := files["unmanaged-secret"]; exists {
				t.Fatal("snapshot exported an unmanaged file")
			}
			stored, _ := os.ReadFile(filepath.Join(deploymentRoot(cfg), "active-manifest.json"))
			var unchanged DeploymentManifest
			json.Unmarshal(stored, &unchanged)
			if unchanged.Files[1].SHA256 != active.Files[1].SHA256 {
				t.Fatal("read snapshot changed the durable active manifest")
			}
			temporary, _ := filepath.Glob(filepath.Join(deploymentRoot(cfg), "snapshot-*"))
			if len(temporary) != 0 {
				t.Fatal("snapshot left temporary archives containing private configuration")
			}
		})
	}
}

func TestRuleSyncSnapshotOmitsAgentCarriedResources(t *testing.T) {
	cfg, active := ruleSnapshotFixture(t, "mihomo")
	data := []byte("geo database")
	writeActivationFixture(t, filepath.Join(filepath.Dir(cfg.ConfigPath), "Country.mmdb"), data, 0600)
	active.Files = append(active.Files, DeploymentFile{Path: "Country.mmdb", Role: "resource", Size: int64(len(data)), SHA256: sha256Hex(data)})
	writeRuleSnapshotManifest(t, cfg, active)
	manifest, files := unpackRuleSnapshot(t, ruleSnapshotRequest(cfg, "GET", "/api/rule-sync/snapshot", "", cfg.Token))
	if len(manifest.Files) != len(active.Files)-1 {
		t.Fatal("Agent-carried resources should be omitted from the server baseline")
	}
	if _, exists := files["Country.mmdb"]; exists {
		t.Fatal("Agent-carried Geo file was exported")
	}
}

func assertRuleSnapshotError(t *testing.T, w *httptest.ResponseRecorder, status int, code string) {
	t.Helper()
	var result struct {
		Success bool   `json:"success"`
		Code    string `json:"code"`
	}
	if w.Code != status || json.Unmarshal(w.Body.Bytes(), &result) != nil || result.Success || result.Code != code {
		t.Fatalf("unexpected snapshot response: %d %s", w.Code, w.Body.String())
	}
	if strings.Contains(w.Body.String(), "private secret") || strings.Contains(w.Body.String(), "snapshot-secret") || strings.Contains(w.Body.String(), "config.yaml") {
		t.Fatal("snapshot error exposed private file content or names")
	}
}

func TestRuleSyncSnapshotRejectsChangedMainConfiguration(t *testing.T) {
	cfg, _ := ruleSnapshotFixture(t, "mihomo")
	writeActivationFixture(t, cfg.ConfigPath, []byte("mode: global\n# private secret\n"), 0600)
	assertRuleSnapshotError(t, ruleSnapshotRequest(cfg, "GET", "/api/rule-sync/snapshot", "", cfg.Token), 409, "SNAPSHOT_CHANGED")
}

func TestRuleSyncSnapshotUsesConfirmedAgentEffectiveNativeAIOConfig(t *testing.T) {
	cfg, _, render := testMihomoReloadDeployment(t)
	cfg.Token = "snapshot-secret"
	cfg.NativeAIO = true
	cfg.AgentIP = "192.168.50.2"
	uploaded := []byte(render("rules: ['DOMAIN,new.test,DIRECT']\ntun: {enable: true, auto-redirect: true}\n"))
	state := submitBundle(t, cfg, "native-aio-snapshot", map[string][]byte{"config.yaml": uploaded}, true, func(manifest *DeploymentManifest) {
		manifest.ProfileID = "profile-1"
		for index := range manifest.Files {
			if manifest.Files[index].Path == manifest.ConfigPath {
				manifest.Files[index].Role = "config"
			}
		}
	})
	if state.Status != "succeeded" || len(state.ConfigVersion) != 64 || state.ConfigVersion == sha256Hex(uploaded) {
		t.Fatalf("fixture did not confirm an Agent-adjusted configuration: %+v", state)
	}
	manifest, files := unpackRuleSnapshot(t, ruleSnapshotRequest(cfg, "GET", "/api/rule-sync/snapshot", "", cfg.Token))
	main := files[manifest.ConfigPath]
	if sha256Hex(main) != state.ConfigVersion || bytes.Equal(main, uploaded) ||
		!bytes.Contains(main, []byte("127.0.0.0/8")) || !bytes.Contains(main, []byte("192.168.50.2/32")) {
		t.Fatal("snapshot did not preserve the exact confirmed Agent-effective configuration")
	}
	for _, entry := range manifest.Files {
		if entry.Path == manifest.ConfigPath && entry.SHA256 != state.ConfigVersion {
			t.Fatal("snapshot main digest differs from confirmed activation version")
		}
	}
}

func TestRuleSyncSnapshotRejectsUnsafeManifestAndFiles(t *testing.T) {
	for _, scenario := range []string{"traversal", "absolute", "hidden", "duplicate", "wrong-agent", "wrong-service", "wrong-config", "wrong-role", "missing-main", "invalid-digest", "file-symlink", "parent-symlink", "resource-symlink", "manifest-symlink", "missing-file", "directory", "fifo"} {
		t.Run(scenario, func(t *testing.T) {
			cfg, active := ruleSnapshotFixture(t, "mihomo")
			root := filepath.Dir(cfg.ConfigPath)
			switch scenario {
			case "traversal":
				active.Files[1].Path = "../private"
			case "absolute":
				active.Files[1].Path = "/etc/passwd"
			case "hidden":
				active.Files[1].Path = ".configflow-deployments/private"
			case "duplicate":
				active.Files = append(active.Files, active.Files[1])
			case "wrong-agent":
				active.AgentID = "other-agent"
			case "wrong-service":
				active.ServiceType = "oxidns"
			case "wrong-config":
				active.ConfigPath = "other.yaml"
			case "wrong-role":
				active.Files[1].Role = "arbitrary"
			case "missing-main":
				active.Files = active.Files[1:]
			case "invalid-digest":
				active.Files[1].SHA256 = "invalid"
			case "file-symlink", "resource-symlink":
				os.Remove(filepath.Join(root, "rules/test.txt"))
				os.Symlink(cfg.ConfigPath, filepath.Join(root, "rules/test.txt"))
				if scenario == "resource-symlink" {
					active.Files[1].Role = "resource"
				}
			case "parent-symlink":
				os.Rename(filepath.Join(root, "rules"), filepath.Join(root, "real-rules"))
				os.Symlink(filepath.Join(root, "real-rules"), filepath.Join(root, "rules"))
			case "missing-file":
				os.Remove(filepath.Join(root, "rules/test.txt"))
			case "directory":
				os.Remove(filepath.Join(root, "rules/test.txt"))
				os.Mkdir(filepath.Join(root, "rules/test.txt"), 0700)
			case "fifo":
				os.Remove(filepath.Join(root, "rules/test.txt"))
				if err := unix.Mkfifo(filepath.Join(root, "rules/test.txt"), 0600); err != nil {
					t.Fatal(err)
				}
			}
			writeRuleSnapshotManifest(t, cfg, active)
			if scenario == "manifest-symlink" {
				manifest := filepath.Join(deploymentRoot(cfg), "active-manifest.json")
				os.Rename(manifest, filepath.Join(root, "outside-manifest.json"))
				os.Symlink(filepath.Join(root, "outside-manifest.json"), manifest)
			}
			assertRuleSnapshotError(t, ruleSnapshotRequest(cfg, "GET", "/api/rule-sync/snapshot", "", cfg.Token), 409, "SNAPSHOT_INVALID")
		})
	}
}

func TestRuleSyncSnapshotRequiresAuthAndParameterlessGet(t *testing.T) {
	cfg, _ := ruleSnapshotFixture(t, "mihomo")
	for _, token := range []string{"", "wrong-token"} {
		assertRuleSnapshotError(t, ruleSnapshotRequest(cfg, "GET", "/api/rule-sync/snapshot", "", token), 401, "UNAUTHORIZED")
	}
	for _, target := range []string{"/api/rule-sync/snapshot?path=/etc/passwd", "/api/rule-sync/snapshot?", "/api/rule-sync/%73napshot"} {
		assertRuleSnapshotError(t, ruleSnapshotRequest(cfg, "GET", target, "", cfg.Token), 400, "INVALID_REQUEST")
	}
	assertRuleSnapshotError(t, ruleSnapshotRequest(cfg, "GET", "/api/rule-sync/snapshot", "{}", cfg.Token), 400, "INVALID_REQUEST")
	assertRuleSnapshotError(t, ruleSnapshotRequest(cfg, "POST", "/api/rule-sync/snapshot", "", cfg.Token), 405, "INVALID_REQUEST")
	cfg.ServiceType = "mosdns"
	assertRuleSnapshotError(t, ruleSnapshotRequest(cfg, "GET", "/api/rule-sync/snapshot", "", cfg.Token), 400, "WRONG_SERVICE")
}

func TestRuleSyncSnapshotRequiresQuiescentActiveDeployment(t *testing.T) {
	cfg, _ := ruleSnapshotFixture(t, "mihomo")
	release, err := AcquireServiceOperation(cfg)
	if err != nil {
		t.Fatal(err)
	}
	assertRuleSnapshotError(t, ruleSnapshotRequest(cfg, "GET", "/api/rule-sync/snapshot", "", cfg.Token), 409, "SERVICE_BUSY")
	release()
	state := &DeploymentState{DeploymentID: "requires-recovery"}
	if err = saveDeployment(cfg, state, "recovery_pending"); err != nil {
		t.Fatal(err)
	}
	assertRuleSnapshotError(t, ruleSnapshotRequest(cfg, "GET", "/api/rule-sync/snapshot", "", cfg.Token), 409, "RECOVERY_REQUIRED")
	os.RemoveAll(deploymentPath(cfg, state.DeploymentID))
	os.Remove(filepath.Join(deploymentRoot(cfg), "active-manifest.json"))
	assertRuleSnapshotError(t, ruleSnapshotRequest(cfg, "GET", "/api/rule-sync/snapshot", "", cfg.Token), 409, "SNAPSHOT_UNAVAILABLE")
}

func TestRuleSyncSnapshotBoundsFilesAndManifest(t *testing.T) {
	t.Run("file bytes", func(t *testing.T) {
		cfg, _ := ruleSnapshotFixture(t, "mihomo")
		if err := os.Truncate(filepath.Join(filepath.Dir(cfg.ConfigPath), "rules/test.txt"), ruleSnapshotFileLimit+1); err != nil {
			t.Fatal(err)
		}
		assertRuleSnapshotError(t, ruleSnapshotRequest(cfg, "GET", "/api/rule-sync/snapshot", "", cfg.Token), 413, "SNAPSHOT_TOO_LARGE")
	})
	t.Run("file count", func(t *testing.T) {
		cfg, active := ruleSnapshotFixture(t, "mihomo")
		for len(active.Files) <= ruleSnapshotFileCount {
			active.Files = append(active.Files, active.Files[1])
		}
		writeRuleSnapshotManifest(t, cfg, active)
		assertRuleSnapshotError(t, ruleSnapshotRequest(cfg, "GET", "/api/rule-sync/snapshot", "", cfg.Token), 413, "SNAPSHOT_TOO_LARGE")
	})
	t.Run("total bytes", func(t *testing.T) {
		cfg, active := ruleSnapshotFixture(t, "mihomo")
		for index := 0; index < 4; index++ {
			name := fmt.Sprintf("rules/large-%d.bin", index)
			file, err := os.Create(filepath.Join(filepath.Dir(cfg.ConfigPath), name))
			if err != nil {
				t.Fatal(err)
			}
			if err = file.Truncate(ruleSnapshotFileLimit); err != nil {
				file.Close()
				t.Fatal(err)
			}
			file.Close()
			active.Files = append(active.Files, DeploymentFile{Path: name, Role: "rule", Size: ruleSnapshotFileLimit, SHA256: strings.Repeat("0", 64)})
		}
		writeRuleSnapshotManifest(t, cfg, active)
		assertRuleSnapshotError(t, ruleSnapshotRequest(cfg, "GET", "/api/rule-sync/snapshot", "", cfg.Token), 413, "SNAPSHOT_TOO_LARGE")
	})
	t.Run("manifest bytes", func(t *testing.T) {
		cfg, _ := ruleSnapshotFixture(t, "mihomo")
		if err := os.Truncate(filepath.Join(deploymentRoot(cfg), "active-manifest.json"), ruleSnapshotManifestLimit+1); err != nil {
			t.Fatal(err)
		}
		assertRuleSnapshotError(t, ruleSnapshotRequest(cfg, "GET", "/api/rule-sync/snapshot", "", cfg.Token), 413, "SNAPSHOT_TOO_LARGE")
	})
}

func TestRuleSyncSnapshotAdvertisesProtocol(t *testing.T) {
	cfg, _ := ruleSnapshotFixture(t, "mihomo")
	w := ruleSnapshotRequest(cfg, "GET", "/api/capabilities", "", cfg.Token)
	var result struct {
		Protocols []int `json:"rule_sync_snapshot_protocols"`
	}
	if w.Code != http.StatusOK || json.Unmarshal(w.Body.Bytes(), &result) != nil || len(result.Protocols) != 1 || result.Protocols[0] != 1 {
		t.Fatalf("snapshot capability missing: %s", w.Body.String())
	}
}
