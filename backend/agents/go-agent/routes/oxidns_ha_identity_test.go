package routes

import (
	"crypto/sha256"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const haTestBootID = "98228bcd-1df5-45c4-bb94-402c52ad8b9b"

// Redirect only identity proof paths; mountinfo remains independently controlled
// so a readable ordinary directory cannot impersonate a read-only Docker volume.
func haTestProofPath(t *testing.T, virtual, actual string) {
	t.Helper()
	oldRead, oldLstat := haEnvironmentRead, haEnvironmentLstat
	t.Cleanup(func() { haEnvironmentRead, haEnvironmentLstat = oldRead, oldLstat })
	translate := func(path string) string {
		if path == virtual || strings.HasPrefix(path, virtual+"/") {
			return actual + strings.TrimPrefix(path, virtual)
		}
		return ""
	}
	haEnvironmentRead = func(path string) ([]byte, error) {
		if redirected := translate(path); redirected != "" {
			return os.ReadFile(redirected)
		}
		return oldRead(path)
	}
	haEnvironmentLstat = func(path string) (os.FileInfo, error) {
		if redirected := translate(path); redirected != "" {
			return os.Lstat(redirected)
		}
		return oldLstat(path)
	}
}

func haTestIdentityVolume(t *testing.T, f *haContainerFixture) string {
	t.Helper()
	t.Setenv("CONFIGFLOW_DNS_HA_IDENTITY", "volume")
	directory := t.TempDir()
	if err := os.WriteFile(filepath.Join(directory, "host-id"), []byte("0123456789abcdef0123456789abcdef\n"), 0600); err != nil {
		t.Fatal(err)
	}
	haTestProofPath(t, haHostIdentityDirectory, directory)
	f.files["/proc/self/mountinfo"] = append(f.files["/proc/self/mountinfo"], []byte("3 0 8:1 / "+haHostIdentityDirectory+" ro - ext4 /dev/vda rw\n")...)
	return directory
}

func haTestBootProof(t *testing.T, f *haContainerFixture) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "boot-id")
	if err := os.WriteFile(path, []byte(haTestBootID+"\n"), 0600); err != nil {
		t.Fatal(err)
	}
	haTestProofPath(t, haHostBootIDPath, path)
	f.files["/proc/self/mountinfo"] = append(f.files["/proc/self/mountinfo"], []byte("4 0 0:5 / "+haHostBootIDPath+" ro - proc proc rw\n")...)
	return path
}

func TestHAIdentityVolumeRequiresReadonlyMountAndOrdinaryFile(t *testing.T) {
	for _, mode := range []string{"host", "macvlan"} {
		t.Run(mode, func(t *testing.T) {
			f := newHAContainerFixture(t, mode)
			haTestIdentityVolume(t, f)
			env := haEnvironment(f.cfg)
			if !env.Ready || env.HostIDSource != "docker-volume" || env.HostID != haHostIdentity(haHostIDPath) {
				t.Fatalf("valid identity volume rejected: %+v", env)
			}
		})
	}
	for _, bad := range []string{"unmounted", "writable", "missing-file", "symlink", "directory", "empty", "multiline", "too-long"} {
		t.Run(bad, func(t *testing.T) {
			f := newHAContainerFixture(t, "host")
			directory := haTestIdentityVolume(t, f)
			path := filepath.Join(directory, "host-id")
			switch bad {
			case "unmounted":
				f.files["/proc/self/mountinfo"] = []byte(strings.ReplaceAll(string(f.files["/proc/self/mountinfo"]), haHostIdentityDirectory+" ro", "/some-other-volume ro"))
			case "writable":
				f.files["/proc/self/mountinfo"] = []byte(strings.ReplaceAll(string(f.files["/proc/self/mountinfo"]), haHostIdentityDirectory+" ro", haHostIdentityDirectory+" rw"))
			case "missing-file":
				os.Remove(path)
			case "symlink":
				os.Rename(path, path+"-real")
				os.Symlink(path+"-real", path)
			case "directory":
				os.Remove(path)
				os.Mkdir(path, 0700)
			case "empty":
				os.WriteFile(path, nil, 0600)
			case "multiline":
				os.WriteFile(path, []byte("1234567890123456\n1234567890123456"), 0600)
			case "too-long":
				os.WriteFile(path, []byte(strings.Repeat("a", 129)), 0600)
			}
			if env := haEnvironment(f.cfg); env.Ready || env.HostID != "" {
				t.Fatalf("unsafe identity %s accepted: %+v", bad, env)
			}
			if err := applyOxiDNSHA(f.cfg, f.spec); err == nil || len(f.commands) != 0 {
				t.Fatalf("invalid identity mutated services: %v %v", err, f.commands)
			}
		})
	}
}

func TestHAIdentityLegacyFileAndUnknownMode(t *testing.T) {
	f := newHAContainerFixture(t, "host")
	t.Setenv("CONFIGFLOW_DNS_HA_IDENTITY", "")
	if env := haEnvironment(f.cfg); !env.Ready || env.HostIDSource != "file" || env.HostBootID != "" {
		t.Fatalf("legacy file mode changed: %+v", env)
	}
	t.Setenv("CONFIGFLOW_DNS_HA_IDENTITY", "unexpected")
	if env := haEnvironment(f.cfg); env.Ready {
		t.Fatal("unknown identity mode accepted")
	}
}

func TestHAContainerBootEvidenceRequiresReadonlyMountedValidFile(t *testing.T) {
	for _, bad := range []string{"valid", "unmounted", "writable", "missing", "symlink", "invalid", "nil-uuid"} {
		t.Run(bad, func(t *testing.T) {
			f := newHAContainerFixture(t, "host")
			path := haTestBootProof(t, f)
			switch bad {
			case "unmounted":
				f.files["/proc/self/mountinfo"] = []byte(strings.ReplaceAll(string(f.files["/proc/self/mountinfo"]), haHostBootIDPath+" ro", "/another-file ro"))
			case "writable":
				f.files["/proc/self/mountinfo"] = []byte(strings.ReplaceAll(string(f.files["/proc/self/mountinfo"]), haHostBootIDPath+" ro", haHostBootIDPath+" rw"))
			case "missing":
				os.Remove(path)
			case "symlink":
				os.Rename(path, path+"-real")
				os.Symlink(path+"-real", path)
			case "invalid":
				os.WriteFile(path, []byte("arbitrary-file-is-not-a-boot-id"), 0600)
			case "nil-uuid":
				os.WriteFile(path, []byte("00000000-0000-0000-0000-000000000000"), 0600)
			}
			env := haEnvironment(f.cfg)
			if bad == "valid" {
				if env.HostBootID != fmt.Sprintf("%x", sha256.Sum256([]byte(haTestBootID))) {
					t.Fatalf("boot evidence not hashed correctly: %+v", env)
				}
			} else if env.HostBootID != "" {
				t.Fatalf("unproven Docker boot identity accepted: %s", bad)
			}
			if !env.Ready || env.HostIDSource != "file" {
				t.Fatalf("optional boot proof broke legacy file mode: %+v", env)
			}
		})
	}
}

func TestHANativeBootIdentityAndRebootDoNotChangeJournalOwnership(t *testing.T) {
	f := newHAContainerFixture(t, "host")
	path := haTestBootProof(t, f)
	envBefore := haEnvironment(f.cfg)
	if err := applyOxiDNSHA(f.cfg, f.spec); err != nil {
		t.Fatal(err)
	}
	os.WriteFile(path, []byte("9e4ba5a2-2c84-4cb9-a63a-2a57a48d23b3\n"), 0600)
	f.running = false
	if err := reconcileOxiDNSHA(f.cfg); err != nil || !f.running {
		t.Fatalf("reboot invalidated persistent ownership: %v", err)
	}
	if after := haEnvironment(f.cfg); after.HostBootID == envBefore.HostBootID || after.HostID != envBefore.HostID {
		t.Fatalf("reboot must change only live boot proof: %+v", after)
	}
	f.cfg.ServiceManager = "systemd"
	f.files[haNativeBootIDPath] = []byte(haTestBootID + "\n")
	f.files["/etc/machine-id"] = []byte("native-stable-host-identity\n")
	native := haEnvironment(f.cfg)
	if native.Kind != "native" || native.HostIDSource != "file" || native.HostBootID != envBefore.HostBootID {
		t.Fatalf("native and Docker boot evidence disagree: %+v", native)
	}
}
