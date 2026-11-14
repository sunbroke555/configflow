package subs

import (
	"errors"
	"os"
		"testing"

	"configflow/internal/jsonx"
	"configflow/internal/yamlx"
)

// goldenForm writes values JSON cannot hold the way the golden does: a
// timestamp as a one-element list with its text.
func goldenForm(v any) any {
	switch x := v.(type) {
	case yamlx.Timestamp:
		return []any{x.String()}
	case *jsonx.Object:
		out := jsonx.NewObject()
		for k, e := range x.All() {
			out.Set(k, goldenForm(e))
		}
		return out
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = goldenForm(e)
		}
		return out
	}
	return v
}

// TestCompatAgainstRepoGolden checks the private dialer_references copy of
// compat.go (ValidateDialers, the structured proxy-string parser) against the
// Python goldens package repo generated for its own copy
// (server/internal/repo/testdata/compat_golden.json).
func TestCompatAgainstRepoGolden(t *testing.T) {
	data, err := os.ReadFile("../repo/testdata/compat_golden.json")
	if err != nil {
		t.Skip("repo golden not present:", err)
	}
	parsed, err := jsonx.Parse(data)
	if err != nil {
		t.Fatal(err)
	}
	stats := map[string]int{}
	for i, item := range parsed.(*jsonx.Object).GetList("cases") {
		record := item.(*jsonx.Object)
		input := record.Value("input")
		switch record.GetString("fn") {
		case "validate_dialers":
			profile, _ := input.(*jsonx.Object)
			err := ValidateDialers(profile.DeepCopy())
			if !record.GetBool("raised") {
				if err != nil {
					t.Errorf("case %d: unexpected error %v for %s", i, err, compact(t, input))
				}
				stats["ok"]++
				continue
			}
			var de *DialerReferenceError
			switch {
			case record.GetString("error_class") == "DialerReferenceError":
				if !errors.As(err, &de) || de.Message != record.GetString("error") {
					t.Errorf("case %d: got %v, python %s for %s", i, err, record.GetString("error"), compact(t, input))
				}
				stats["dialer_error"]++
			case err == nil:
				// Python raised TypeError/AttributeError/... on a malformed profile
				stats["python_crash_go_ok:"+record.GetString("error_class")]++
				t.Logf("case %d: python %s (%s), Go accepts %s", i, record.GetString("error_class"), record.GetString("error"), compact(t, input))
			default:
				stats["python_crash_go_error"]++
			}
		case "parse_proxy_string":
			got := parseStructuredProxyString(input.(string))
			var g any
			if got != nil {
				g = goldenForm(got)
			}
			if record.GetBool("raised") {
				t.Errorf("case %d: python raised", i)
				continue
			}
			var nsk *yamlx.NonStringKeyError
			if _, lerr := yamlx.SafeLoad(pyStrip(input.(string))); got == nil && record.Value("ok") != nil && errors.As(lerr, &nsk) {
				// Documented gap: a mapping with non-string keys (yamlx
				// refuses them); Python returns the dict.
				stats["proxy_string_non_string_keys"]++
				continue
			}
			if compact(t, g) != compact(t, record.Value("ok")) {
				stats["proxy_string_mismatch"]++
				t.Errorf("case %d parse(%q): got %s python %s", i, input, compact(t, g), compact(t, record.Value("ok")))
				continue
			}
			stats["proxy_string_ok"]++
		}
	}
	t.Logf("%v", stats)
}
