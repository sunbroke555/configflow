package main

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestCLISharedLogRoutesReportToFD3WithoutDuplicateDiagnostics(t *testing.T) {
	root := t.TempDir()
	core := filepath.Join(root, "config.yaml")
	candidate := filepath.Join(root, "candidate.json")
	if err := os.WriteFile(core, []byte("tun:\n  enable: false\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(candidate, []byte(`{"service_type":"mihomo","agent_port":8080,"config_path":"`+core+`"}`), 0600); err != nil {
		t.Fatal(err)
	}
	logPath := filepath.Join(root, "shared.log")
	logFile, err := os.OpenFile(logPath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
	if err != nil {
		t.Fatal(err)
	}
	defer logFile.Close()
	reportPath := filepath.Join(root, "report.txt")
	reportFile, err := os.Create(reportPath)
	if err != nil {
		t.Fatal(err)
	}
	defer reportFile.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, os.Args[0], "-test.run=^TestAgentStartupProcess$", "--", "--config", candidate, "--prepare-tun", "--verbose")
	cmd.Env = append(os.Environ(), "CONFIGFLOW_STARTUP_TEST_HELPER=1", "CONFIGFLOW_LOG_FILE="+logPath,
		"CONFIGFLOW_REPORT_FD=3", "CONFIGFLOW_REPORT_NESTED=1", "CONFIGFLOW_CLI_ACTIVE=1", "CONFIGFLOW_VERBOSE=1")
	cmd.Stdout, cmd.Stderr, cmd.ExtraFiles = logFile, logFile, []*os.File{reportFile}
	if err := cmd.Run(); err != nil {
		t.Fatal(err)
	}
	details, err := os.ReadFile(logPath)
	if err != nil {
		t.Fatal(err)
	}
	report, err := os.ReadFile(reportPath)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Count(string(details), "Using config file:") != 1 || strings.Count(string(details), "[通过] TUN 自动准备") != 1 {
		t.Fatalf("missing or duplicate log diagnostics: %s", details)
	}
	if !strings.Contains(string(report), "TUN 准备完成") || strings.Contains(string(report), "Using config file:") || strings.Contains(string(report), "ConfigFlow ·") || strings.Contains(string(report), "完整日志") {
		t.Fatalf("nested report leaked detail or repeated decoration: %s", report)
	}
}

func TestCLIStandaloneVerboseAndQuietKeepSameFailureAndFullLog(t *testing.T) {
	for _, verbose := range []bool{false, true} {
		name := "quiet"
		if verbose {
			name = "verbose"
		}
		t.Run(name, func(t *testing.T) {
			root := t.TempDir()
			logPath := filepath.Join(root, "details.log")
			args := []string{"-test.run=^TestAgentStartupProcess$", "--", "--check-environment", "--config", filepath.Join(root, "missing.json")}
			if verbose {
				args = append(args, "--verbose")
			}
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			cmd := exec.CommandContext(ctx, os.Args[0], args...)
			cmd.Env = append(os.Environ(), "CONFIGFLOW_STARTUP_TEST_HELPER=1", "CONFIGFLOW_LOG_FILE="+logPath,
				"CONFIGFLOW_REPORT_FD=", "CONFIGFLOW_REPORT_NESTED=", "CONFIGFLOW_CLI_ACTIVE=", "CONFIGFLOW_VERBOSE=", "NO_COLOR=1")
			output, err := cmd.CombinedOutput()
			if exit, ok := err.(*exec.ExitError); !ok || exit.ExitCode() != 1 {
				t.Fatalf("reporting changed failure exit: %v: %s", err, output)
			}
			details, err := os.ReadFile(logPath)
			if err != nil {
				t.Fatal(err)
			}
			if strings.Contains(string(output), "Using config file:") != verbose || strings.Count(string(details), "Using config file:") != 1 || !strings.Contains(string(output), "处理方法") || strings.Contains(string(output), "\x1b") {
				t.Fatalf("wrong verbosity or missing evidence: %s\nLOG: %s", output, details)
			}
		})
	}
}
