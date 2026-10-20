package routes

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

// The watchdog and the cleanup ticker start together, so every cleanup round
// meets a watchdog tick holding the lock for a few milliseconds.
func TestCleanupDiskWaitsOutBriefLockHolder(t *testing.T) {
	cfg := &Config{ConfigPath: filepath.Join(t.TempDir(), "config.yaml")}
	if err := saveDeployment(cfg, &DeploymentState{DeploymentID: "done"}, "succeeded"); err != nil {
		t.Fatal(err)
	}
	bundle := filepath.Join(deploymentPath(cfg, "done"), "bundle.tar.gz")
	if err := os.WriteFile(bundle, []byte("payload"), 0600); err != nil {
		t.Fatal(err)
	}
	release, err := AcquireServiceOperation(cfg)
	if err != nil {
		t.Fatal(err)
	}
	time.AfterFunc(150*time.Millisecond, release)
	if err := CleanupDisk(cfg); err != nil {
		t.Fatalf("cleanup gave up on a brief lock holder: %v", err)
	}
	if _, err := os.Stat(bundle); !os.IsNotExist(err) {
		t.Fatalf("terminal payload kept: %v", err)
	}
}

func TestCleanupDiskPreservesRecovery(t *testing.T) {
	cfg := &Config{ConfigPath: filepath.Join(t.TempDir(), "config.yaml")}
	for _, status := range []string{"succeeded", "failed", "rolled_back", "ready", "replacing", "rollback_failed", "recovery_pending"} {
		dir := deploymentPath(cfg, status)
		for _, name := range []string{"stage/data", "backup/data", "bundle.tar.gz", "effective-manifest.json"} {
			path := filepath.Join(dir, name)
			if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(path, []byte("keep"), 0600); err != nil {
				t.Fatal(err)
			}
		}
		if err := saveDeployment(cfg, &DeploymentState{DeploymentID: status}, status); err != nil {
			t.Fatal(err)
		}
	}
	outside := t.TempDir()
	if err := os.Symlink(outside, filepath.Join(deploymentRoot(cfg), "linked")); err != nil {
		t.Fatal(err)
	}
	release, err := AcquireServiceOperation(cfg)
	if err != nil {
		t.Fatal(err)
	}
	previousWait := cleanupLockWait
	cleanupLockWait = 300 * time.Millisecond
	defer func() { cleanupLockWait = previousWait }()
	if err := CleanupDisk(cfg); err == nil {
		t.Fatal("cleanup ignored operation lock")
	}
	release()
	for i := 0; i < 2; i++ {
		if err := CleanupDisk(cfg); err != nil {
			t.Fatal(err)
		}
	}
	for _, status := range []string{"succeeded", "failed", "rolled_back", "ready", "replacing", "rollback_failed", "recovery_pending"} {
		terminal := status == "succeeded" || status == "failed" || status == "rolled_back"
		for _, name := range []string{"stage/data", "bundle.tar.gz", "backup/data", "state.json", "effective-manifest.json"} {
			_, err := os.Stat(filepath.Join(deploymentPath(cfg, status), name))
			removed := terminal && (name == "stage/data" || name == "bundle.tar.gz")
			if removed && !os.IsNotExist(err) || !removed && err != nil {
				t.Fatalf("%s/%s: %v", status, name, err)
			}
		}
	}
}
