package agents

// Port of backend/agents/diagnostics.py: bounded, readable failure details
// shared by Agent response paths.

import (
	"regexp"
	"sort"
	"strings"
	"unicode"
	"unicode/utf8"

	"configflow/internal/jsonx"
	"configflow/internal/urlx"
)

// Limits of diagnostics.py (UTF-8 bytes).
const (
	DetailLimit     = 16 * 1024
	DNSCheckLimit   = 32
	DNSNameLimit    = 128
	DNSMessageLimit = 2048
	DNSFixLimit     = 1024
)

// pyWS is what Python's \s matches in a str pattern, for use inside a class.
const pyWS = `\t\n\x0b\f\r \x{1c}-\x{1f}\x{85}\x{a0}\x{1680}\x{2000}-\x{200a}\x{2028}\x{2029}\x{202f}\x{205f}\x{3000}`

const secretKey = `(?:password|passwd|passphrase|secret|token|api[_-]?key|private[_-]?key|access[_-]?key|authorization|credential)`

var (
	ansiPattern  = regexp.MustCompile(`\x1b\[[0-?]*[ -/]*[@-~]`)
	urlPattern   = regexp.MustCompile(`[a-zA-Z][a-zA-Z0-9+.-]*://[^` + pyWS + `<>"'\\]+`)
	authPattern  = regexp.MustCompile(`(?i)\b(Bearer|Basic)[` + pyWS + `]+[^` + pyWS + `,"'<>]+`)
	blockSecret  = regexp.MustCompile(`(?i)^([ \t]*)(-[` + pyWS + `]+)?["']?` + secretKey + `["']?[` + pyWS + `]*:[ \t]*[|>][0-9+-]*[ \t]*(?:#[^\n]*)?$`)
	pemPattern   = regexp.MustCompile(`(?s)-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)`)
	secretPrefix = regexp.MustCompile(`(?i)(["']?\b` + secretKey + `["']?[` + pyWS + `]*[:=][ \t]*)` +
		`(?:(?:Bearer|Basic)[` + pyWS + `]+)?(?:"(?:\\.|[^"\\])*"|'(?:''|\\.|[^'\\])*'|[^` + pyWS + `,;}&]+)`)
)

// FailureText is failure_text(value, secrets, limit): ANSI escapes and
// control characters removed, private keys, YAML secret blocks, the given
// secrets (also URL-encoded), URL credentials, Authorization values and
// key=value secrets redacted, stripped, and cut to limit UTF-8 bytes with a
// "[输出过长，已截断]" marker. Non-string values give "". limit <= 0 is
// DetailLimit.
//
// Known difference: Python's \b is Unicode-aware, RE2's is ASCII, so a
// secret key directly after a non-ASCII letter is redacted here and not in
// Python.
func FailureText(value any, secrets []any, limit int) string {
	text, ok := value.(string)
	if !ok {
		return ""
	}
	if limit <= 0 {
		limit = DetailLimit
	}
	text = ansiPattern.ReplaceAllString(text, "")
	text = strings.Map(func(r rune) rune {
		if r == '\n' || r == '\t' || (r >= 32 && r != 127) {
			return r
		}
		return -1
	}, text)
	text = hideBlockSecrets(pemPattern.ReplaceAllString(text, "[PRIVATE KEY REDACTED]"))
	var names []string
	seen := map[string]bool{}
	for _, secret := range secrets {
		if s, ok := secret.(string); ok && s != "" && !seen[s] {
			seen[s] = true
			names = append(names, s)
		}
	}
	sort.SliceStable(names, func(i, j int) bool {
		return utf8.RuneCountInString(names[i]) > utf8.RuneCountInString(names[j])
	})
	for _, secret := range names {
		// {secret, quote(secret, safe=''), quote_plus(secret)}
		for _, variant := range []string{secret, strings.ReplaceAll(urlx.QuotePlus(secret), "+", "%20"), urlx.QuotePlus(secret)} {
			text = strings.ReplaceAll(text, variant, "[REDACTED]")
		}
	}
	text = urlPattern.ReplaceAllStringFunc(text, safeDiagnosticURL)
	text = authPattern.ReplaceAllString(text, "$1 [REDACTED]")
	text = secretPrefix.ReplaceAllString(text, "${1}[REDACTED]")
	text = strings.TrimFunc(text, isPySpace)
	if len(text) > limit {
		suffix := "\n[输出过长，已截断]"
		cut := []byte(text)[:limit-len(suffix)]
		for len(cut) > 0 && !utf8.Valid(cut) {
			cut = cut[:len(cut)-1]
		}
		text = string(cut) + suffix
	}
	return text
}

// tokenSecrets is the (agent.get('token'),) tuple.
func tokenSecrets(agent *jsonx.Object) []any { return []any{agent.Value("token")} }

func isPySpace(r rune) bool {
	switch r {
	case 0x1c, 0x1d, 0x1e, 0x1f, 0x85:
		return true
	}
	return unicode.IsSpace(r)
}

func safeDiagnosticURL(match string) string {
	scheme, _, _ := strings.Cut(match, ":")
	switch strings.ToLower(scheme) {
	case "http", "https", "tcp", "udp", "tls", "quic", "h3":
		return urlx.SafeURLForLog(match)
	}
	return "[REDACTED URL]"
}

// hideBlockSecrets hides a YAML block value without swallowing the next line.
func hideBlockSecrets(text string) string {
	lines := splitLinesKeepEnds(text)
	var out strings.Builder
	for index := 0; index < len(lines); {
		line := lines[index]
		match := blockSecret.FindStringSubmatch(strings.TrimRight(line, "\r\n"))
		index++
		if match == nil {
			out.WriteString(line)
			continue
		}
		out.WriteString(line[:strings.Index(line, ":")+1] + " [REDACTED]\n")
		indent := utf8.RuneCountInString(match[1]) + utf8.RuneCountInString(match[2])
		for index < len(lines) {
			child := lines[index]
			stripped := strings.TrimLeft(child, " \t")
			if strings.TrimFunc(child, isPySpace) != "" && utf8.RuneCountInString(child)-utf8.RuneCountInString(stripped) <= indent {
				break
			}
			index++
		}
	}
	return out.String()
}

// splitLinesKeepEnds is str.splitlines(keepends=True).
func splitLinesKeepEnds(text string) []string {
	var lines []string
	runes := []rune(text)
	start := 0
	for i := 0; i < len(runes); i++ {
		switch runes[i] {
		case '\r':
			if i+1 < len(runes) && runes[i+1] == '\n' {
				i++
			}
		case '\n', '\v', '\f', 0x1c, 0x1d, 0x1e, 0x85, 0x2028, 0x2029:
		default:
			continue
		}
		lines = append(lines, string(runes[start:i+1]))
		start = i + 1
	}
	if start < len(runes) {
		lines = append(lines, string(runes[start:]))
	}
	return lines
}

// DNSReportFields is dns_report_fields(payload, secrets): bounded, safe DNS
// results, without inventing a result for old Agents.
func DNSReportFields(payload *jsonx.Object, secrets []any) *jsonx.Object {
	result := jsonx.NewObject()
	if checks, ok := payload.Value("dns_checks").([]any); ok {
		safeChecks := []any{}
		incomplete := len(checks) > DNSCheckLimit
		limit := min(len(checks), DNSCheckLimit)
		for _, item := range checks[:limit] {
			check, isObject := item.(*jsonx.Object)
			level := check.Value("level")
			name, nameOK := check.Value("name").(string)
			message, messageOK := check.Value("message").(string)
			if !isObject || check == nil || (level != "pass" && level != "warn" && level != "error") || !nameOK || !messageOK {
				incomplete = true
				continue
			}
			name = FailureText(name, secrets, DNSNameLimit)
			message = FailureText(message, secrets, DNSMessageLimit)
			if name == "" || message == "" {
				incomplete = true
				continue
			}
			safe := jsonx.Obj("name", name, "level", level, "message", message)
			if fix, ok := check.Value("fix").(string); ok {
				if fix = FailureText(fix, secrets, DNSFixLimit); fix != "" {
					safe.Set("fix", fix)
				}
			}
			safeChecks = append(safeChecks, safe)
		}
		if incomplete {
			// A partial report must not look like an exhaustive all-pass result.
			if len(safeChecks) > DNSCheckLimit-1 {
				safeChecks = safeChecks[:DNSCheckLimit-1]
			}
			safeChecks = append(safeChecks, jsonx.Obj("name", "DNS 报告完整性", "level", "warn",
				"message", "部分 DNS 检测结果格式无效或数量过多，未完整显示。",
				"fix", "请减少重复监听配置，或检查 Agent 返回的 DNS 检测结果。"))
		}
		result.Set("dns_checks", safeChecks)
	}
	if checkedAt, ok := payload.Value("dns_checked_at").(string); ok && utf8.RuneCountInString(checkedAt) <= 64 && strings.Contains(checkedAt, "T") {
		if _, naive, ok := fromISO(strings.ReplaceAll(checkedAt, "Z", "+00:00")); ok && !naive {
			result.Set("dns_checked_at", checkedAt)
		}
	}
	return result
}

// ResponseFailure is response_failure(response, fallback=..., secrets=...):
// keeps a legacy client's explanation even when its HTTP status is non-200.
func ResponseFailure(response *Response, fallback string, secrets []any) *jsonx.Object {
	payload, err := response.JSON()
	result := jsonx.Obj("success", false)
	object, isObject := payload.(*jsonx.Object)
	if err == nil && isObject {
		for _, key := range []string{"message", "error", "error_details", "rollback_error", "rollback_error_details", "failed_stage"} {
			if text, ok := object.Value(key).(string); ok {
				result.Set(key, FailureText(text, secrets, 0))
			}
		}
	}
	if !jsonx.Truthy(result.Value("message")) {
		message := result.Value("error")
		if !jsonx.Truthy(message) {
			message = fallback
		}
		result.Set("message", message)
	}
	// Plain text is useful for older clients; HTML proxy error pages are not diagnostics.
	if !(err == nil && isObject) {
		contentType, _, _ := strings.Cut(response.Header.Get("Content-Type"), ";")
		if contentType == "text/plain" {
			if detail := FailureText(response.Text(), secrets, 0); detail != "" {
				result.Set("error_details", detail)
			}
		}
	}
	if response.StatusCode >= 400 {
		result.Set("http_status", jsonx.Int(response.StatusCode))
	}
	return result
}
