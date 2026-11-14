// TODO(port): replace with package #14 health, latency (backend/utils/health_check.py::validate_health)
// and package #13 subscription fetch (backend/utils/subscription_refresh.py::validate_refresh_interval).

package repo

import (
	"errors"
	"net/netip"
	"regexp"
	"strconv"
	"strings"

	"configflow/internal/jsonx"
)

// Keys of PROVIDER_DEFAULTS and of GROUP_DEFAULTS plus tolerance.
var (
	providerHealthKeys = map[string]bool{"enable": true, "url": true, "interval": true, "timeout": true,
		"lazy": true, "expected-status": true}
	groupHealthKeys = map[string]bool{"url": true, "interval": true, "timeout": true, "lazy": true,
		"expected-status": true, "tolerance": true}
)

var expectedStatusPattern = regexp.MustCompile(`^[0-9]{3}(?:-[0-9]{3})?(?:/[0-9]{3}(?:-[0-9]{3})?)*$`)

func valueError(message string) error { return helperError(ClassValueError, message) }

// pyInt is `type(v) is int`: a Number holding an integer, never a bool.
func pyInt(v any) (jsonx.Number, bool) {
	number, ok := v.(jsonx.Number)
	return number, ok && number.IsInt()
}

// intBetween is minimum <= number <= maximum for a Python int of any size.
func intBetween(number jsonx.Number, minimum, maximum int64) bool {
	value, ok := number.Int64()
	return ok && minimum <= value && value <= maximum
}

// validateHealth is validate_health: persisted engine fields are validated
// without coercion. Python walks the fields in set order; here it is the
// object's key order, which only decides the message when several fields
// are invalid.
func validateHealth(values any, provider bool) error {
	object, ok := values.(*jsonx.Object)
	if !ok || object == nil {
		return valueError("健康检查必须是对象")
	}
	allowed := groupHealthKeys
	if provider {
		allowed = providerHealthKeys
		for key := range object.All() {
			if !allowed[key] {
				return valueError("健康检查包含未知字段")
			}
		}
	}
	for key, value := range object.All() {
		if !allowed[key] {
			continue
		}
		switch key {
		case "enable", "lazy":
			if _, ok := value.(bool); !ok {
				return valueError(key + " 必须为布尔值")
			}
		case "interval", "timeout", "tolerance":
			minimum, maximum := int64(1), int64(600000)
			if key == "tolerance" {
				minimum = 0
			}
			if key == "interval" {
				maximum = 86400
			}
			if number, ok := pyInt(value); !ok || !intBetween(number, minimum, maximum) {
				return valueError(key + " 必须为 " + strconv.FormatInt(minimum, 10) + "–" + strconv.FormatInt(maximum, 10) + " 的整数")
			}
		case "url":
			text, ok := value.(string)
			if !ok {
				return valueError("探测 URL 必须是有效 HTTP(S) 地址")
			}
			for _, r := range text {
				if pyIsSpace(r) || r < 32 {
					return valueError("探测 URL 必须是有效 HTTP(S) 地址")
				}
			}
			if !validProbeURL(text) {
				return valueError("探测 URL 必须是无凭据的 HTTP(S) 地址")
			}
		case "expected-status":
			text, ok := value.(string)
			if !ok || !expectedStatusPattern.MatchString(text) || strings.HasSuffix(text, "\n") {
				return valueError("expected-status 格式应为 204 或 200-299/304")
			}
			for _, item := range strings.Split(text, "/") {
				bounds := strings.Split(item, "-")
				first, _ := strconv.Atoi(bounds[0])
				last, _ := strconv.Atoi(bounds[len(bounds)-1])
				if first < 100 || first > 599 || last < 100 || last > 599 || first > last {
					return valueError("expected-status 范围必须在 100–599 内且递增")
				}
			}
		}
	}
	return nil
}

// validProbeURL is the urlsplit condition of validate_health: an http(s)
// URL with a host, no credentials, no fragment and a port in 1..65535.
func validProbeURL(text string) bool {
	parts, err := urlSplit(text)
	if err != nil {
		return false
	}
	port, hasPort, err := parts.port()
	if err != nil {
		return false
	}
	_, hasUser := parts.username()
	return (parts.scheme == "http" || parts.scheme == "https") && parts.hostname() != "" &&
		!hasUser && parts.fragment == "" && (!hasPort || (1 <= port && port <= 65535))
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

// maxRefreshInterval is subscription_refresh.MAX_INTERVAL.
const maxRefreshInterval = 604800

// validateRefreshInterval is validate_refresh_interval with its default label.
func validateRefreshInterval(value any) error {
	number, ok := pyInt(value)
	if ok {
		if n, fits := number.Int64(); fits && (n == 0 || (60 <= n && n <= maxRefreshInterval)) {
			return nil
		}
	}
	return valueError("订阅源更新间隔必须为 0（关闭自动更新）或 60 ~ " + strconv.Itoa(maxRefreshInterval) + " 秒的整数")
}
