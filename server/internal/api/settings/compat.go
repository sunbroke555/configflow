// TODO(port): replace with package #12 remote rules (backend/utils/rule_fetch.py::validate_rule_url,
// normalize_rule_fetch_proxy) and package #17 (backend/utils/webdav_backup.py::RETENTION_LIMITS,
// retention_settings). The urlsplit port is a copy of internal/repo/compat_health.go.

package settings

import (
	"errors"
	"net/netip"
	"regexp"
	"strconv"
	"strings"
	"unicode"

	"configflow/internal/jsonx"
)

// RetentionLimits is webdav_backup.RETENTION_LIMITS, in its key order.
var retentionKeys = []string{"keep_latest", "keep_daily"}
var retentionLimits = map[string]int64{"keep_latest": 1000, "keep_daily": 3650}

const (
	defaultKeepLatest = 30
	defaultKeepDaily  = 30
)

// retentionSettings is webdav_backup.retention_settings: the saved values,
// or the defaults for missing or invalid ones (not ints in range).
func retentionSettings(settings *jsonx.Object) (any, any) {
	values := []any{}
	for _, item := range []struct {
		key      string
		fallback int64
	}{{"keep_latest", defaultKeepLatest}, {"keep_daily", defaultKeepDaily}} {
		value, present := settings.Get(item.key)
		n, isNumber := value.(jsonx.Number)
		v, fits := n.Int64()
		if present && isNumber && fits && v >= 0 && v <= retentionLimits[item.key] {
			values = append(values, value)
		} else {
			values = append(values, jsonx.Int(item.fallback))
		}
	}
	return values[0], values[1]
}

var errInvalidURL = errors.New("Invalid HTTP/HTTPS URL")

// validateRuleURL is rule_fetch.validate_rule_url.
func validateRuleURL(url string) (splitResult, error) {
	for _, r := range url {
		if r < 32 || r == 127 {
			return splitResult{}, errInvalidURL
		}
	}
	parsed, err := urlSplit(url)
	if err != nil {
		return parsed, errInvalidURL
	}
	port, hasPort, err := parsed.port()
	if err != nil {
		return parsed, errInvalidURL
	}
	if (parsed.scheme != "http" && parsed.scheme != "https") || parsed.hostname() == "" {
		return parsed, errInvalidURL
	}
	if hasPort && (port < 1 || port > 65535) {
		return parsed, errInvalidURL
	}
	for _, r := range parsed.netloc {
		if pyIsSpace(r) {
			return parsed, errInvalidURL
		}
	}
	if strings.Contains(parsed.netloc, "\\") {
		return parsed, errInvalidURL
	}
	return parsed, nil
}

func pyIsSpace(r rune) bool {
	switch r {
	case ' ', '\t', '\n', '\v', '\f', '\r', 0x1c, 0x1d, 0x1e, 0x1f, 0x85:
		return true
	}
	return r > 0x7f && unicode.IsSpace(r)
}

// normalizeRuleFetchProxy is rule_fetch.normalize_rule_fetch_proxy; ok
// false where Python raises ValueError.
func normalizeRuleFetchProxy(value any) (string, bool) {
	text, isString := value.(string)
	if !isString {
		return "", false
	}
	text = strings.TrimFunc(text, pyIsSpace)
	if text == "" {
		return "", true
	}
	parsed, err := validateRuleURL(text)
	if err != nil || (parsed.path != "" && parsed.path != "/") || parsed.query != "" || parsed.fragment != "" {
		return "", false
	}
	return parsed.scheme + "://" + parsed.netloc, true
}

// splitResult is urllib.parse.SplitResult for text URLs.
type splitResult struct {
	scheme, netloc, path, query, fragment string
}

var errURL = errors.New("invalid URL")

// Characters whose NFKC form contains one of "/?#@:" (urllib's
// _checknetloc). The list replaces a full NFKC normalisation.
const netlocNFKCForbidden = "／？＃＠：﹕﹖﹟﹫︓︖" +
	"℀℁℅℆⁇⁈⁉⩴"

const urlSchemeChars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+-."

// urlSplit is urllib.parse.urlsplit(url) of CPython 3.11 for a str. Callers
// have already rejected whitespace and C0 controls, which urlsplit would
// strip.
func urlSplit(url string) (splitResult, error) {
	var result splitResult
	url = strings.TrimLeft(url, "\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\x0c\r\x0e\x0f"+
		"\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19\x1a\x1b\x1c\x1d\x1e\x1f ")
	for _, unsafe := range []string{"\t", "\r", "\n"} {
		url = strings.ReplaceAll(url, unsafe, "")
	}
	if i := strings.Index(url, ":"); i > 0 && isASCIILetter(url[0]) {
		valid := true
		for _, r := range url[:i] {
			if !strings.ContainsRune(urlSchemeChars, r) {
				valid = false
				break
			}
		}
		if valid {
			result.scheme, url = strings.ToLower(url[:i]), url[i+1:]
		}
	}
	if strings.HasPrefix(url, "//") {
		delimiter := len(url)
		for _, c := range "/?#" {
			if at := strings.IndexRune(url[2:], c); at >= 0 && at+2 < delimiter {
				delimiter = at + 2
			}
		}
		result.netloc, url = url[2:delimiter], url[delimiter:]
		open, closed := strings.Contains(result.netloc, "["), strings.Contains(result.netloc, "]")
		if open != closed {
			return result, errURL
		}
		if open && closed && !validBracketedNetloc(result.netloc) {
			return result, errURL
		}
	}
	if before, after, found := strings.Cut(url, "#"); found {
		url, result.fragment = before, after
	}
	if before, after, found := strings.Cut(url, "?"); found {
		url, result.query = before, after
	}
	result.path = url
	if !validNetloc(result.netloc) {
		return result, errURL
	}
	return result, nil
}

func isASCIILetter(c byte) bool {
	return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z')
}

var ipvFuture = regexp.MustCompile(`^v[a-fA-F0-9]+\..+$`)

// validBracketedNetloc is urllib's _check_bracketed_netloc (CPython 3.11.17),
// which mirrors the splitting of hostinfo.
func validBracketedNetloc(netloc string) bool {
	hostinfo := netloc
	if at := strings.LastIndex(hostinfo, "@"); at >= 0 {
		hostinfo = hostinfo[at+1:]
	}
	before, bracketed, found := strings.Cut(hostinfo, "[")
	if !found {
		hostname, _, _ := strings.Cut(hostinfo, ":")
		return validBracketedHost(hostname)
	}
	if before != "" { // no data is allowed before a bracket
		return false
	}
	hostname, port, _ := strings.Cut(bracketed, "]")
	if port != "" && !strings.HasPrefix(port, ":") { // nor after it, before the port delimiter
		return false
	}
	return validBracketedHost(hostname)
}

// validBracketedHost is urllib's _check_bracketed_host.
func validBracketedHost(host string) bool {
	if strings.HasPrefix(host, "v") {
		return ipvFuture.MatchString(host) && !strings.Contains(host, "\n")
	}
	address, zone, scoped := strings.Cut(host, "%")
	if scoped && (zone == "" || strings.Contains(zone, "%")) {
		return false
	}
	parsed, err := netip.ParseAddr(address)
	return err == nil && parsed.Is6()
}

// validNetloc is urllib's _checknetloc.
func validNetloc(netloc string) bool {
	ascii := true
	for i := 0; i < len(netloc); i++ {
		if netloc[i] >= 0x80 {
			ascii = false
			break
		}
	}
	return ascii || !strings.ContainsAny(netloc, netlocNFKCForbidden)
}

// hostinfo is SplitResult._hostinfo: (hostname, port, port present).
func (r splitResult) hostinfo() (string, string, bool) {
	hostinfo := r.netloc
	if at := strings.LastIndex(hostinfo, "@"); at >= 0 {
		hostinfo = hostinfo[at+1:]
	}
	var hostname, port string
	if _, bracketed, found := strings.Cut(hostinfo, "["); found {
		var rest string
		hostname, rest, _ = strings.Cut(bracketed, "]")
		_, port, _ = strings.Cut(rest, ":")
	} else {
		hostname, port, _ = strings.Cut(hostinfo, ":")
	}
	return hostname, port, port != ""
}

// hostname is SplitResult.hostname; "" stands for None.
func (r splitResult) hostname() string {
	hostname, _, _ := r.hostinfo()
	if hostname == "" {
		return ""
	}
	name, zone, found := strings.Cut(hostname, "%")
	if found {
		return strings.ToLower(name) + "%" + zone
	}
	return strings.ToLower(name)
}

// port is SplitResult.port: the number, whether there is one, and the
// ValueError for a port that is not a number in 0..65535.
func (r splitResult) port() (int, bool, error) {
	_, port, present := r.hostinfo()
	if !present {
		return 0, false, nil
	}
	for i := 0; i < len(port); i++ {
		if port[i] < '0' || port[i] > '9' {
			return 0, false, errURL
		}
	}
	digits := strings.TrimLeft(port, "0")
	if len(digits) > 5 {
		return 0, false, errURL
	}
	value, _ := strconv.Atoi("0" + digits)
	if value > 65535 {
		return 0, false, errURL
	}
	return value, true, nil
}

// username is SplitResult.username and whether it is not None.
func (r splitResult) username() (string, bool) {
	at := strings.LastIndex(r.netloc, "@")
	if at < 0 {
		return "", false
	}
	username, _, _ := strings.Cut(r.netloc[:at], ":")
	return username, true
}

