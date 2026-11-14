package subs

import (
	"errors"

	"configflow/internal/jsonx"
	"configflow/internal/logx"
	"configflow/internal/rules"
)

var filterLogger = logx.Get("backend.utils.subscription_filter")

// Matcher is a compiled Python regex used with re.search.
type Matcher interface{ Search(text string) bool }

// compileRegex is re.compile(pattern). A pattern Python accepts but the
// engine cannot run yields *rules.UnsupportedRegexError (pending until the
// rules package gains its backtracking fallback).
func compileRegex(pattern string) (Matcher, error) {
	re, err := rules.CompilePyRegex(pattern, false)
	if err != nil {
		return nil, err
	}
	return re, nil
}

// isUnsupported tells apart "Python would run this pattern, this engine cannot".
func isUnsupported(err error) bool {
	var u *rules.UnsupportedRegexError
	return errors.As(err, &u)
}

// ValidateRegexFilter is validate_regex_filter: "" means valid (None).
func ValidateRegexFilter(value any) string {
	if value == nil {
		return ""
	}
	s, ok := value.(string)
	if !ok {
		return "节点筛选正则必须是字符串"
	}
	if pyStrip(s) == "" {
		return ""
	}
	if _, err := compileRegex(pyStrip(s)); err != nil && !isUnsupported(err) {
		return "节点筛选正则无效: " + err.Error()
	}
	return ""
}

// CompileSubscriptionFilter is compile_subscription_filter: nil when no
// filter is configured or the pattern is invalid. A pattern the Go engine
// cannot run is also treated as "no filter" until the regexp2 fallback of
// package rules exists (Python would apply it).
func CompileSubscriptionFilter(sub any) Matcher {
	obj, ok := sub.(*jsonx.Object)
	if !ok {
		return nil
	}
	pattern, ok := obj.Get("regex_filter")
	text, isStr := pattern.(string)
	if !ok || !isStr || pyStrip(text) == "" {
		return nil
	}
	re, err := compileRegex(pyStrip(text))
	if err != nil {
		if !isUnsupported(err) {
			filterLogger.Warningf("订阅 '%s' 的节点筛选正则无效，已忽略: %s", jsonx.Str(obj.Value("name")), err.Error())
		}
		return nil
	}
	return re
}

// FilterSubscriptionItems is filter_subscription_items; items are
// node/proxy objects matched on their name.
func FilterSubscriptionItems(sub any, items []any) []any {
	out := append([]any(nil), items...)
	re := CompileSubscriptionFilter(sub)
	if re == nil {
		return out
	}
	kept := []any{}
	for _, item := range out {
		if obj, ok := item.(*jsonx.Object); ok && re.Search(jsonx.Str(getOr(obj, "name", ""))) {
			kept = append(kept, item)
		}
	}
	return kept
}
