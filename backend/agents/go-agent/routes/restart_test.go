package routes

import (
	"bytes"
	"errors"
	"log"
	"net/http"
	"net/http/httptest"
	"os/exec"
	"strings"
	"testing"
)

func TestCommandForLogAlwaysRedactsNonURLCommand(t *testing.T) {
	if got := commandForLog("printf command-text-secret; false"); got != "[COMMAND REDACTED]" {
		t.Fatalf("commandForLog() = %q", got)
	}
}

func TestFailedSecretCommandDoesNotLeakLogsOutputOrError(t *testing.T) {
	const commandSecret = "command-text-secret"
	const outputSecret = "output-stream-secret"
	var logs bytes.Buffer
	previousWriter := log.Writer()
	log.SetOutput(&logs)
	t.Cleanup(func() { log.SetOutput(previousWriter) })

	output, err := executeURLCommand("printf " + outputSecret + "; false # " + commandSecret)
	if err == nil {
		t.Fatal("executeURLCommand unexpectedly succeeded")
	}
	if len(output) != 0 {
		t.Fatalf("failed command returned output: %q", output)
	}
	var exitError *exec.ExitError
	if !errors.As(err, &exitError) {
		t.Fatalf("errors.As(err, *exec.ExitError) = false: %v", err)
	}
	if !errors.Is(err, exitError) {
		t.Fatalf("errors.Is(err, exitError) = false: %v", err)
	}
	for _, secret := range []string{commandSecret, outputSecret} {
		if strings.Contains(logs.String(), secret) || strings.Contains(err.Error(), secret) {
			t.Fatalf("failed command leaked %q: logs=%q err=%q", secret, logs.String(), err.Error())
		}
	}
}

func TestRestartHandlerDoesNotReturnFailedCommandOutput(t *testing.T) {
	const commandSecret = "handler-command-secret"
	const outputSecret = "handler-output-secret"
	cfg := &Config{RestartCommand: "printf " + outputSecret + "; false # " + commandSecret}
	request := httptest.NewRequest(http.MethodPost, "/restart", nil)
	recorder := httptest.NewRecorder()

	RestartHandler(cfg).ServeHTTP(recorder, request)

	body := recorder.Body.String()
	if recorder.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d body=%s", recorder.Code, body)
	}
	for _, secret := range []string{commandSecret, outputSecret} {
		if strings.Contains(body, secret) {
			t.Fatalf("restart response leaked %q: %s", secret, body)
		}
	}
	if strings.Contains(body, "output") {
		t.Fatalf("restart response exposed output field: %s", body)
	}
}
