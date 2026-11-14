// TODO(port): replace with package #4 rule engine (backend/utils/rule_utils.py::sanitize_rule_name,
// rule_cache_filename).

package repo

import (
	"crypto/sha256"
	"encoding/hex"
	"strings"

	"configflow/internal/jsonx"
)

// sanitizeRuleName is sanitize_rule_name: a rule name as a file name
// without extension. (A truthy non-string, a TypeError in Python, is taken
// by its str().)
func sanitizeRuleName(name any) string {
	if !jsonx.Truthy(name) {
		return "unnamed"
	}
	safe := strings.Map(func(r rune) rune {
		if strings.ContainsRune(`/\:*?"<>|`, r) {
			return '_'
		}
		return r
	}, jsonx.Str(name))
	safe = strings.Trim(safe, ". ")
	if safe == "" {
		return "unnamed"
	}
	if runes := []rune(safe); len(runes) > 200 {
		return string(runes[:200])
	}
	return safe
}

// ruleCacheFilename is rule_cache_filename: the cache path of a rule source
// under the rules directory, bound to the resource identity and, for remote
// sources, to the declaration.
func ruleCacheFilename(rule *jsonx.Object) string {
	identity := rule.Value("library_rule_id")
	if !jsonx.Truthy(identity) {
		identity = rule.Value("id")
	}
	namespace := "by-id"
	if identity == nil {
		namespace = "by-name"
		identity = rule.GetOr("name", "")
	}
	text := jsonx.Str(identity)
	if jsonx.Equal(rule.GetOr("source_type", "url"), "url") && jsonx.Truthy(rule.Value("url")) {
		// Bind remote bytes to the declaration as well as the resource ID.
		namespace = "by-source"
		format := rule.Value("format")
		if !jsonx.Truthy(format) {
			format = ""
		}
		encoded, err := jsonx.Encode([]any{text, rule.Value("url"), rule.GetOr("behavior", "domain"), format}, jsonx.Options{})
		if err != nil {
			encoded = nil
		}
		text = string(encoded)
	}
	digest := sha256.Sum256([]byte(text))
	return namespace + "/" + hex.EncodeToString(digest[:]) + ".list"
}
