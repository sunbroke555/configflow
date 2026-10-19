package routes

import (
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
)

func TestLinuxGatewayUninstallPreservesAppliedNetwork(t *testing.T) {
	f := newGatewayFixture(t)
	if err := checkLinuxGatewayUninstall(f.cfg); err != nil {
		t.Fatalf("ordinary uninstall blocked: %v", err)
	}
	if err := applyLinuxGateway(f.cfg, f.spec, "test-profile"); err != nil {
		t.Fatal(err)
	}
	mutations := f.mutations
	recorder := httptest.NewRecorder()
	UninstallHandler(f.cfg)(recorder, httptest.NewRequest(http.MethodPost, "/uninstall", nil))
	if recorder.Code != http.StatusConflict {
		t.Fatalf("uninstall did not stop before async cleanup: %d", recorder.Code)
	}
	if f.mutations != mutations {
		t.Fatal("uninstall changed network before explicit restore")
	}
	if state, err := loadGatewayState(f.cfg); err != nil || state == nil {
		t.Fatalf("uninstall removed ownership record: %v", err)
	}
	if err := removeLinuxGateway(f.cfg); err != nil {
		t.Fatal(err)
	}
	if err := checkLinuxGatewayUninstall(f.cfg); err != nil {
		t.Fatalf("restored network still blocks uninstall: %v", err)
	}
}

func TestLinuxGatewayUninstallPreservesCorruptJournal(t *testing.T) {
	f := newGatewayFixture(t)
	path, err := gatewayPath(f.cfg)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("broken journal"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := checkLinuxGatewayUninstall(f.cfg); err == nil {
		t.Fatal("uninstall can erase unknown ownership state")
	}
	f.cfg.ServiceType = "oxidns"
	if err := checkLinuxGatewayUninstall(f.cfg); err != nil {
		t.Fatalf("gateway guard altered another service: %v", err)
	}
}
