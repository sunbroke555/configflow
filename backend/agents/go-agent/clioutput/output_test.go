package clioutput

import (
	"bytes"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

func sessionEnvironment(t *testing.T) (string, string) {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "details.log")
	report := filepath.Join(dir, "report.txt")
	file, err := os.Create(report)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { file.Close() })
	for key, value := range map[string]string{
		"CONFIGFLOW_LOG_FILE": path, "CONFIGFLOW_REPORT_FD": strconv.Itoa(int(file.Fd())),
		"CONFIGFLOW_REPORT_NESTED": "", "CONFIGFLOW_VERBOSE": "", "CONFIGFLOW_CLI_ACTIVE": "", "CONFIGFLOW_LOG_DIR": "", "NO_COLOR": "1",
	} {
		t.Setenv(key, value)
	}
	return path, report
}

func read(t *testing.T, path string) string {
	t.Helper()
	value, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(value)
}

func TestSessionKeepsDiagnosticDetailsPrivateAndAppends(t *testing.T) {
	path, report := sessionEnvironment(t)
	if err := os.WriteFile(path, []byte("prior record\n"), 0644); err != nil {
		t.Fatal(err)
	}
	s := New("check", false)
	s.Title("环境检查")
	log.Print("internal startup evidence")
	s.Result("pass", "检查通过", "complete diagnostic evidence", "")
	s.Close()
	console, details := read(t, report), read(t, path)
	for _, needle := range []string{"internal startup evidence", "complete diagnostic evidence", "prior record"} {
		if strings.Count(details, needle) != 1 || strings.Contains(console, needle) {
			t.Fatalf("detail leaked or duplicated (%s): console %q, log %q", needle, console, details)
		}
	}
	if !strings.Contains(console, "检查通过") || !strings.Contains(console, path) || strings.Contains(console, "\x1b") {
		t.Fatalf("missing plain completion or log path: %q", console)
	}
	info, err := os.Stat(path)
	if err != nil || info.Mode().Perm() != 0600 {
		t.Fatalf("log is not private: %v %v", info, err)
	}
}

func TestVerboseExpansionHasSingleOwner(t *testing.T) {
	for _, parent := range []bool{false, true} {
		t.Run(fmt.Sprint(parent), func(t *testing.T) {
			path, report := sessionEnvironment(t)
			t.Setenv("CONFIGFLOW_VERBOSE", "1")
			if parent {
				t.Setenv("CONFIGFLOW_CLI_ACTIVE", "1")
				t.Setenv("CONFIGFLOW_REPORT_NESTED", "1")
			}
			s := New("check", false)
			log.Print("startup diagnostic")
			s.Result("pass", "检查通过", "detailed evidence", "")
			s.Close()
			console, details := read(t, report), read(t, path)
			if strings.Contains(console, "detailed evidence") == parent || strings.Count(details, "detailed evidence") != 1 {
				t.Fatalf("wrong verbose ownership: %q; %q", console, details)
			}
			if parent && strings.Contains(console, "完整日志") {
				t.Fatalf("nested report repeated log footer: %q", console)
			}
		})
	}
}

func TestLogFailureFallsBackAndRemainsVisible(t *testing.T) {
	_, report := sessionEnvironment(t)
	badPath := filepath.Join(t.TempDir(), "missing", "details.log")
	t.Setenv("CONFIGFLOW_LOG_FILE", badPath)
	s := New("check", false)
	if s.file == nil || s.Path == badPath {
		t.Fatal("did not create safe fallback")
	}
	t.Cleanup(func() { os.RemoveAll(filepath.Dir(s.Path)) })
	log.Print("retained after fallback")
	s.Close()
	if !strings.Contains(read(t, s.Path), "retained after fallback") || !strings.Contains(read(t, report), "完整日志改存于") {
		t.Fatal("fallback was silent or lost the log")
	}
}

func TestLogRejectsSymlinkAndReportsWriteFailure(t *testing.T) {
	path, report := sessionEnvironment(t)
	target := filepath.Join(t.TempDir(), "protected")
	if err := os.WriteFile(target, []byte("untouched"), 0644); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, path); err != nil {
		t.Skip(err)
	}
	s := New("check", false)
	t.Cleanup(func() { os.RemoveAll(filepath.Dir(s.Path)) })
	if s.file == nil {
		t.Fatal("missing fallback log")
	}
	s.file.Close()
	fmt.Fprintln(s, "diagnostic after disk failure")
	s.Close()
	if read(t, target) != "untouched" || !strings.Contains(read(t, report), "完整日志写入失败") || !strings.Contains(read(t, report), "diagnostic after disk failure") {
		t.Fatal("unsafe log target or hidden write failure")
	}
}

func TestWrapRespectsChineseWidthAndStripsControls(t *testing.T) {
	var out bytes.Buffer
	Wrap(&out, "    原因：", "无法读取核心进程\x1b[2J；"+strings.Repeat("网络配置", 20), 48)
	for _, line := range strings.Split(strings.TrimRight(out.String(), "\n"), "\n") {
		if cells(line) > 48 || strings.ContainsRune(line, '\x1b') {
			t.Fatalf("unsafe or overwide line: %q", line)
		}
	}
}

func TestRedirectedReportNeverUsesANSI(t *testing.T) {
	_, report := sessionEnvironment(t)
	if err := os.Unsetenv("NO_COLOR"); err != nil {
		t.Fatal(err)
	}
	t.Setenv("TERM", "xterm-256color")
	s := New("check", false)
	s.Result("pass", "检查通过", "", "")
	s.Close()
	if s.Options.Color || strings.Contains(read(t, report), "\x1b") {
		t.Fatal("redirected output unexpectedly contains ANSI")
	}
}

func TestCustomLogDirectoryAndExplicitFilePrecedence(t *testing.T) {
	path, _ := sessionEnvironment(t)
	dir := filepath.Join(t.TempDir(), "logs")
	t.Setenv("CONFIGFLOW_LOG_DIR", dir)
	s := New("environment", false)
	s.Close()
	if s.Path != path {
		t.Fatalf("explicit shared file must take precedence: %s", s.Path)
	}
	t.Setenv("CONFIGFLOW_LOG_FILE", "")
	s = New("environment", false)
	s.Close()
	if filepath.Dir(s.Path) != dir {
		t.Fatalf("custom directory ignored: %s", s.Path)
	}
	if info, err := os.Stat(s.Path); err != nil || info.Mode().Perm() != 0600 {
		t.Fatalf("custom directory log is not private: %v %v", info, err)
	}
}

func TestResultLogUsesSharedWarningAndFailureLabels(t *testing.T) {
	path, _ := sessionEnvironment(t)
	s := New("check", false)
	s.Result("warn", "发布记录需要处理", "未完成的发布", "显式恢复")
	s.Result("error", "更新记录无效", "记录无法读取", "检查记录")
	s.Close()
	details := read(t, path)
	if strings.Count(details, "[提醒]") != 1 || strings.Count(details, "[失败]") != 1 {
		t.Fatalf("parent script cannot reliably count command diagnostics: %s", details)
	}
}

func TestWrapKeepsWordsAndPunctuationTogether(t *testing.T) {
	var out bytes.Buffer
	// Lab v5-3: the installer split "ConfigFlow" and began a line with "，UDP".
	Wrap(&out, "    原因：", "这台 Linux 的防火墙会拦截转发到 Mihomo TUN（Meta）的流量：TCP 仍可用，UDP（如 QUIC、语音通话、游戏）和 ping 会失败。ConfigFlow 不会改动防火墙。", 78)
	Wrap(&out, "    处理方法：", "需要放行时在这台 Linux 上执行下面的命令；发布 Mihomo 配置后以 ConfigFlow 页面显示的命令为准（TUN 设备名可能不同）。", 78)
	Wrap(&out, "  ✓ ", "IPv4 转发  当前为 0，安装器将开启并持久化（/etc/sysctl.d/99-configflow-aio-forward.conf）", 78)
	text := out.String()
	for _, line := range strings.Split(strings.TrimRight(text, "\n"), "\n") {
		trimmed := strings.TrimLeft(line, " ")
		if cells(line) > 78 || strings.HasPrefix(trimmed, "，") || strings.HasPrefix(trimmed, "。") || strings.HasPrefix(trimmed, "）") {
			t.Fatalf("bad line %q in\n%s", line, text)
		}
	}
	for _, word := range []string{"ConfigFlow", "/etc/sysctl.d/99-configflow-aio-forward.conf"} {
		if !strings.Contains(text, word) {
			t.Fatalf("%s was split:\n%s", word, text)
		}
	}
	// A word wider than the line is still split rather than overflowing.
	out.Reset()
	Wrap(&out, "  ", strings.Repeat("x", 50), 30)
	for _, line := range strings.Split(strings.TrimRight(out.String(), "\n"), "\n") {
		if cells(line) > 30 {
			t.Fatalf("overwide: %q", line)
		}
	}
	if got := strings.ReplaceAll(strings.ReplaceAll(out.String(), "\n", ""), " ", ""); got != strings.Repeat("x", 50) {
		t.Fatalf("lost text: %q", out.String())
	}
}
