package upgrade

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestProgressSurvivesRestartAndExplainsDownloadAndVerification(t *testing.T) {
	binary := filepath.Join(t.TempDir(), "agent")
	path := CurrentPath(binary)
	job := Job{Status: Status{ID: "progress123456789", AgentID: "owner", Version: Version}, Binary: binary, Size: 1234}
	for _, stage := range []string{"queued", "queued", "downloading", "verifying"} {
		if err := job.save(path, stage); err != nil {
			t.Fatal(err)
		}
	}
	loaded, err := loadJob(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(loaded.Events) != 3 {
		t.Fatalf("duplicate stage or lost history: %+v", loaded.Events)
	}
	if !strings.Contains(loaded.Events[1].Message, "configflow-agent-linux-") || !strings.Contains(loaded.Events[1].Message, "1234 字节") || !strings.Contains(loaded.Events[1].Message, Version) {
		t.Fatalf("missing download object: %s", loaded.Events[1].Message)
	}
	if !strings.Contains(loaded.Message, "SHA-256 校验通过") || !strings.Contains(loaded.Message, "可执行性和版本号") {
		t.Fatalf("verification phase confused with download checksum: %s", loaded.Message)
	}
	for _, event := range loaded.Events {
		if _, err := time.Parse(time.RFC3339Nano, event.At); err != nil {
			t.Fatal(err)
		}
	}
	if err := loaded.save(path, "backing_up"); err != nil {
		t.Fatal(err)
	}
	status, err := ReadStatus(binary, "owner")
	if err != nil || len(status.Events) != 4 {
		t.Fatalf("lost prior worker events: %+v %v", status, err)
	}
}

func TestProgressIsBoundedAndDoesNotIncludePrivateJobData(t *testing.T) {
	binary := filepath.Join(t.TempDir(), "agent")
	job := Job{Status: Status{ID: "progress123456789", AgentID: "owner", Version: "https://secret.test/?token=private", Error: "original-error"},
		Binary: binary, ConfigFile: "/private/config-token", Digest: "private-digest", Size: 1234}
	for i := 0; i < maxEvents+5; i++ {
		stage := "downloading"
		if i%2 == 1 {
			stage = "verifying"
		}
		if err := job.save(CurrentPath(binary), stage); err != nil {
			t.Fatal(err)
		}
	}
	if len(job.Events) != maxEvents {
		t.Fatalf("unbounded events: %d", len(job.Events))
	}
	encoded, err := json.Marshal(job.Events)
	if err != nil {
		t.Fatal(err)
	}
	for _, private := range []string{"secret.test", "private", "original-error"} {
		if strings.Contains(string(encoded), private) {
			t.Fatalf("private data in progress: %s", encoded)
		}
	}
}

func TestProgressDoesNotAnnounceTerminalResultUntilWorkerUnlocks(t *testing.T) {
	binary := filepath.Join(t.TempDir(), "agent")
	job := Job{Status: Status{ID: "progress123456789", AgentID: "owner", Version: Version}, Binary: binary}
	for _, stage := range []string{"checking", "succeeded"} {
		if err := job.save(CurrentPath(binary), stage); err != nil {
			t.Fatal(err)
		}
	}
	release, err := lock(filepath.Join(rootFor(binary), "update.lock"))
	if err != nil {
		t.Fatal(err)
	}
	status, err := ReadStatus(binary, "owner")
	release()
	if err != nil {
		t.Fatal(err)
	}
	if status.Status != "checking" || !strings.Contains(status.Message, "释放操作锁") || len(status.Events) != 1 || status.Events[0].Status != "checking" {
		t.Fatalf("premature final result: %+v", status)
	}
	status, err = ReadStatus(binary, "owner")
	if err != nil || status.Status != "succeeded" || len(status.Events) != 2 {
		t.Fatalf("lost confirmed history: %+v %v", status, err)
	}
}

func TestLegacyJournalWithoutProgressRemainsReadable(t *testing.T) {
	binary := filepath.Join(t.TempDir(), "agent")
	path := CurrentPath(binary)
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(`{"update_id":"progress123456789","agent_id":"owner","status":"checking"}`), 0600); err != nil {
		t.Fatal(err)
	}
	status, err := ReadStatus(binary, "owner")
	if err != nil || status.Status != "checking" || len(status.Events) != 0 {
		t.Fatalf("legacy journal rejected: %+v %v", status, err)
	}
}
