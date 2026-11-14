package subs

import (
	"errors"
	"testing"

	"configflow/internal/jsonx"
)

// matchError checks a Go error against a recorded Python exception: the text
// must be equal for DialerReferenceError, any error will do for the other
// exception types (TypeError, KeyError ... of malformed documents).
func matchError(t *testing.T, where string, err error, want any) bool {
	t.Helper()
	msg, isErr := isErrorOut(want)
	if !isErr {
		if err != nil {
			t.Fatalf("%s: unexpected error %v", where, err)
		}
		return false
	}
	if err == nil {
		t.Fatalf("%s: want error %q, got none", where, msg)
	}
	var de *DialerReferenceError
	if errors.As(err, &de) && asObj(want).GetString("type") == "DialerReferenceError" && de.Message != msg {
		t.Fatalf("%s: error %q, want %q", where, de.Message, msg)
	}
	if asObj(want).GetString("type") == "DialerReferenceError" && !errors.As(err, &de) {
		t.Fatalf("%s: error %v is not a DialerReferenceError (%q)", where, err, msg)
	}
	return true
}

func TestChainsCorpus(t *testing.T) {
	rows := loadCorpus(t, "chains.jsonl")
	stats := map[string]int{}
	for i, row := range rows {
		in := asObj(row.In)
		want := asObj(row.Out)
		where := func(what string) string { return what + " row " + jsonx.Str(jsonx.Int(i)) + " " + compact(t, in) }
		profile := in.DeepCopy()
		if matchError(t, where("validate"), ValidateDialers(profile), want.Value("validate")) {
			stats["validate_errors"]++
		}
		profile = in.DeepCopy()
		lowered, err := LowerProxyChains(profile)
		if matchError(t, where("lower"), err, want.Value("lower")) {
			stats["lower_errors"]++
			if !want.Has("providers_main") {
				continue
			}
		} else {
			stats["lowered"]++
			if g, w := compact(t, lowered), compact(t, want.Value("lower")); g != w {
				t.Fatalf("%s\n got  %s\n want %s", where("lower"), g, w)
			}
		}
		if !want.Has("providers_main") {
			continue
		}
		stats["providers"]++
		main := want.GetObject("providers_in").DeepCopy()
		perr := LowerChainProviders(lowered, main)
		matchError(t, where("providers"), perr, want.Value("providers_result"))
		if g, w := compact(t, main), compact(t, want.Value("providers_main")); g != w {
			t.Fatalf("%s\n got  %s\n want %s", where("providers"), g, w)
		}
	}
	t.Logf("%d rows %v", len(rows), stats)
}
