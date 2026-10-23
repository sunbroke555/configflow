package routes

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// fakeRCService installs an rc-service whose status output and exit code come
// from files, and which records every invocation in calls.log.
func fakeRCService(t *testing.T, cfg *Config) string {
	t.Helper()
	bin := t.TempDir()
	cfg.ServiceManager, cfg.ServiceUnit = "openrc", "test-core"
	script := "#!/bin/sh\necho \"$2\" >> " + filepath.Join(bin, "calls.log") + "\n" +
		"if [ \"$2\" = status ]; then cat " + filepath.Join(bin, "status.out") + "; exit $(cat " + filepath.Join(bin, "status.code") + "); fi\n" +
		"if [ \"$2\" = zap ]; then printf ' * status: stopped\\n' > " + filepath.Join(bin, "status.out") + "; echo 3 > " + filepath.Join(bin, "status.code") + "; fi\nexit 0\n"
	writeActivationFixture(t, filepath.Join(bin, "rc-service"), []byte(script), 0700)
	t.Setenv("PATH", bin+string(os.PathListSeparator)+os.Getenv("PATH"))
	return bin
}

func setRCStatus(t *testing.T, bin, output string, code string) {
	t.Helper()
	writeActivationFixture(t, filepath.Join(bin, "status.out"), []byte(output+"\n"), 0600)
	writeActivationFixture(t, filepath.Join(bin, "status.code"), []byte(code+"\n"), 0600)
}

func TestOpenRCStatusCodes(t *testing.T) {
	cfg := testDeploymentConfig(t)
	bin := fakeRCService(t, cfg)
	for _, tc := range []struct {
		output, code string
		running      bool
		transition   bool
	}{
		{" * status: started", "0", true, false},
		{" * status: stopped", "3", false, false},
		{" * status: crashed", "32", false, false},
		{" * status: stopping", "4", false, true},
		{" * status: starting", "8", false, true},
	} {
		setRCStatus(t, bin, tc.output, tc.code)
		running, err := serviceRunning(cfg)
		if tc.transition {
			if !errors.Is(err, errServiceTransition) {
				t.Fatalf("%s: expected transition, got %t %v", tc.output, running, err)
			}
			continue
		}
		if err != nil || running != tc.running {
			t.Fatalf("%s: expected running=%t, got %t %v", tc.output, tc.running, running, err)
		}
	}
}

func TestOpenRCStartZapsCrashedService(t *testing.T) {
	cfg := testDeploymentConfig(t)
	bin := fakeRCService(t, cfg)
	setRCStatus(t, bin, " * status: crashed", "32")
	if err := startService(cfg); err != nil {
		t.Fatal(err)
	}
	calls, _ := os.ReadFile(filepath.Join(bin, "calls.log"))
	if string(calls) != "status\nzap\nstart\n" {
		t.Fatalf("crashed service must be zapped before start: %q", calls)
	}
	setRCStatus(t, bin, " * status: stopped", "3")
	os.Remove(filepath.Join(bin, "calls.log"))
	if err := startService(cfg); err != nil {
		t.Fatal(err)
	}
	if calls, _ = os.ReadFile(filepath.Join(bin, "calls.log")); string(calls) != "status\nstart\n" {
		t.Fatalf("stopped service needs no zap: %q", calls)
	}
}

func TestHealthFailureIncludesServiceErrorLog(t *testing.T) {
	cfg := testDeploymentConfig(t)
	cfg.ServiceType = "oxidns"
	bin := fakeRCService(t, cfg)
	setRCStatus(t, bin, " * status: crashed", "32")
	dir := stubLogEnvironment(t, "openrc", nil)
	errLog := filepath.Join(dir, "oxidns.err")
	writeActivationFixture(t, filepath.Join(logOpenRCInitDir, "test-core"), []byte("#!/sbin/openrc-run\noutput_log=\""+filepath.Join(dir, "oxidns.log")+"\"\nerror_log=\""+errLog+"\"\n"), 0755)
	writeActivationFixture(t, errLog, []byte("INFO loading config\nERROR listen udp 0.0.0.0:53: bind: address already in use token="+cfg.Token+"x\n"), 0600)

	summary, details := deploymentErrorFields(checkDeploymentHealth(cfg))
	if !strings.HasPrefix(summary, "service is not running: ") || !strings.Contains(summary, "address already in use") {
		t.Fatalf("summary should carry the core's error line: %q", summary)
	}
	if !strings.Contains(details, errLog) || !strings.Contains(details, "INFO loading config") {
		t.Fatalf("details should carry the error_log tail: %q", details)
	}
	if strings.Contains(details, "token="+cfg.Token+"x") {
		t.Fatalf("credentials in the core log must be redacted: %q", details)
	}
}

func TestHealthFailureWithoutServiceLogIsUnchanged(t *testing.T) {
	cfg := testDeploymentConfig(t)
	bin := fakeRCService(t, cfg)
	setRCStatus(t, bin, " * status: crashed", "32")
	stubLogEnvironment(t, "openrc", nil)
	if err := checkDeploymentHealth(cfg); err == nil || err.Error() != "service is not running" {
		t.Fatalf("unexpected error: %v", err)
	}
}
