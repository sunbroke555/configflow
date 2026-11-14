// TODO(port): replace with package #20 agent registry
// (backend/agents/operation_events.py::event_message, MESSAGE_LIMIT and
// backend/agents/diagnostics.py::failure_text) and package #32 router core
// (backend/routers/store.py::routers_of).

package notifications

import (
	"regexp"
	"sort"
	"strings"
	"unicode"
	"unicode/utf8"

	"configflow/internal/jsonx"
	"configflow/internal/urlx"
)

// MessageLimit is operation_events.MESSAGE_LIMIT (UTF-8 bytes).
const MessageLimit = 1024

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

// EventMessage is operation_events.event_message(value, secrets): download
// addresses hidden, then failure_text with the 1024-byte limit. Non-string
// values give "".
func EventMessage(value any, secrets ...any) string {
	text, ok := value.(string)
	if !ok {
		return ""
	}
	text = urlPattern.ReplaceAllString(text, "[下载地址已隐藏]")
	return failureText(text, secrets, MessageLimit)
}

func failureText(text string, secrets []any, limit int) string {
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

// RoutersOf is routers.store.routers_of(config).
func RoutersOf(config *jsonx.Object) []*jsonx.Object {
	routers, ok := config.Value("routers").(*jsonx.Object)
	if !ok || routers == nil {
		return nil
	}
	items, ok := routers.Value("items").([]any)
	if !ok {
		return nil
	}
	var out []*jsonx.Object
	for _, item := range items {
		if object, ok := item.(*jsonx.Object); ok && object != nil {
			out = append(out, object)
		}
	}
	return out
}
