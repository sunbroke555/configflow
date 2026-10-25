package routes

import (
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"
)

type activationCounts struct{ backupCopies, replaceCopies, directorySyncs int }

// countActivationIO counts file copies and directory flushes until cleanup.
func countActivationIO(t *testing.T) *activationCounts {
	t.Helper()
	counts := &activationCounts{}
	backup, replace, sync := deploymentBackupCopy, deploymentReplaceCopy, syncDirectory
	deploymentBackupCopy = func(src, dst string, mode os.FileMode) error {
		counts.backupCopies++
		return backup(src, dst, mode)
	}
	deploymentReplaceCopy = func(src, dst string, mode os.FileMode, uid, gid int) error {
		counts.replaceCopies++
		return replace(src, dst, mode, uid, gid)
	}
	syncDirectory = func(name string) error {
		counts.directorySyncs++
		return sync(name)
	}
	t.Cleanup(func() { deploymentBackupCopy, deploymentReplaceCopy, syncDirectory = backup, replace, sync })
	return counts
}

func ruleFiles(count int, changed string) map[string][]byte {
	files := map[string][]byte{"config.yaml": []byte("mode: rule\n")}
	for i := 0; i < count; i++ {
		files[fmt.Sprintf("rules/r%03d.list", i)] = []byte(fmt.Sprintf("+.domain-%d.test\n", i))
	}
	if changed != "" {
		files[changed] = []byte("+.changed.test\n")
	}
	return files
}

func TestUnchangedFilesAreLinkedNotCopiedAndDirectoriesSyncOnce(t *testing.T) {
	cfg := testDeploymentConfig(t)
	root := filepath.Dir(cfg.ConfigPath)
	os.WriteFile(filepath.Join(root, "running"), nil, 0600)
	if s := submitBundle(t, cfg, "first", ruleFiles(40, ""), true); s.Status != "succeeded" {
		t.Fatalf("%+v", s)
	}
	old := time.Now().Add(-48 * time.Hour)
	unchanged := filepath.Join(root, "rules/r001.list")
	if e := os.Chtimes(unchanged, old, old); e != nil {
		t.Fatal(e)
	}
	counts := countActivationIO(t)
	second := ruleFiles(40, "rules/r000.list")
	if s := submitBundle(t, cfg, "second", second, true); s.Status != "succeeded" {
		t.Fatalf("%+v", s)
	}
	// Only the changed file is copied, once into the backup and once live. The
	// unchanged files no longer cost 2 copies and 2 directory flushes each:
	// before, 41 + 41 copies and about 100 flushes; the ~20 left are journal
	// writes and new transaction directories.
	if counts.backupCopies != 1 || counts.replaceCopies != 1 || counts.directorySyncs > 30 {
		t.Fatalf("activation I/O not reduced: %+v", *counts)
	}
	for path, want := range second {
		if got, e := os.ReadFile(filepath.Join(root, path)); e != nil || string(got) != string(want) {
			t.Fatalf("%s = %q, %v", path, got, e)
		}
	}
	if info, e := os.Stat(unchanged); e != nil || time.Since(info.ModTime()) > time.Hour {
		t.Fatalf("unchanged file keeps its old modification time: %v %v", info, e)
	}
	s, e := loadDeployment(cfg, "second")
	if e != nil || !s.BackupComplete || len(s.Backups) != 41 {
		t.Fatalf("%+v %v", s, e)
	}
	for _, b := range s.Backups {
		h, _, e := hashFile(filepath.Join(deploymentPath(cfg, "second"), "backup", b.Path))
		if e != nil || !b.Existed || h != b.SHA256 {
			t.Fatalf("backup %s: %v %v", b.Path, h, e)
		}
	}
}

func TestLinkedBackupsRestoreOriginalFilesOnRollback(t *testing.T) {
	cfg := testDeploymentConfig(t)
	root := filepath.Dir(cfg.ConfigPath)
	os.WriteFile(filepath.Join(root, "running"), nil, 0600)
	if s := submitBundle(t, cfg, "first", ruleFiles(5, ""), true); s.Status != "succeeded" {
		t.Fatalf("%+v", s)
	}
	if e := os.Chmod(filepath.Join(root, "rules/r002.list"), 0640); e != nil {
		t.Fatal(e)
	}
	// Unchanged rules are linked from stage; the changed main file fails the
	// health check, and every file must come back byte for byte with its mode.
	failing := ruleFiles(5, "")
	failing["config.yaml"] = []byte("mode: rule\nreject-start: true\n")
	if s := submitBundle(t, cfg, "unhealthy", failing, true); s.Status != "rolled_back" {
		t.Fatalf("%+v", s)
	}
	for path, want := range ruleFiles(5, "") {
		if got, e := os.ReadFile(filepath.Join(root, path)); e != nil || string(got) != string(want) {
			t.Fatalf("%s = %q, %v", path, got, e)
		}
	}
	if info, e := os.Stat(filepath.Join(root, "rules/r002.list")); e != nil || info.Mode().Perm() != 0640 {
		t.Fatalf("mode not restored: %v %v", info, e)
	}
	// Stage payloads are pruned once a transaction completes; linked backups
	// are separate names for the same verified bytes and stay intact.
	s, e := loadDeployment(cfg, "unhealthy")
	if e != nil {
		t.Fatal(e)
	}
	for _, b := range s.Backups {
		if h, _, e := hashFile(filepath.Join(deploymentPath(cfg, "unhealthy"), "backup", b.Path)); e != nil || h != b.SHA256 {
			t.Fatalf("backup %s after cleanup: %v", b.Path, e)
		}
	}
}

func TestChangedOwnerOrModeStillReplacesFile(t *testing.T) {
	cfg := testDeploymentConfig(t)
	root := filepath.Dir(cfg.ConfigPath)
	os.WriteFile(filepath.Join(root, "running"), nil, 0600)
	if s := submitBundle(t, cfg, "first", ruleFiles(2, ""), true); s.Status != "succeeded" {
		t.Fatalf("%+v", s)
	}
	live := filepath.Join(root, "rules/r000.list")
	if e := os.Chmod(live, 0604); e != nil {
		t.Fatal(e)
	}
	counts := countActivationIO(t)
	if s := submitBundle(t, cfg, "second", ruleFiles(2, ""), true); s.Status != "succeeded" {
		t.Fatalf("%+v", s)
	}
	// The recorded prior mode is kept, as before; equal bytes alone do not skip.
	if info, e := os.Stat(live); e != nil || info.Mode().Perm() != 0604 {
		t.Fatalf("%v %v", info, e)
	}
	if counts.replaceCopies != 0 {
		t.Fatalf("file whose mode already matches was copied: %+v", *counts)
	}
}
