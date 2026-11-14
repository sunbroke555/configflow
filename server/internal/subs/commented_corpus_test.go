package subs

import (
	"errors"
	"fmt"
	"strings"
	"testing"

	"configflow/internal/jsonx"
)

// TestCommentedCorpus compares RelinkGroupListeners with relink_group_listeners
// of the Python code (ruamel.yaml round trip) byte for byte. Rows the Go
// editor declines (ErrUnsupported) are counted separately: Python produced
// output for them (or failed too).
func TestCommentedCorpus(t *testing.T) { runCommentedCorpus(t, "commented.jsonl.gz", 335) }

// TestCommentedAnchorsCorpus covers anchors, aliases and merge keys around
// listeners, with comments at every position (testdata/gen.py
// anchor_sources).
func TestCommentedAnchorsCorpus(t *testing.T) {
	runCommentedCorpus(t, "commented_anchors.jsonl.gz", 336)
}

func runCommentedCorpus(t *testing.T, file string, maxDeclined int) {
	rows := loadCorpus(t, file)
	stats := map[string]int{}
	reasons := map[string]int{}
	var shown int
	for i, row := range rows {
		in := asObj(row.In)
		want := asObj(row.Out)
		profile := jsonx.Obj("mihomo", jsonx.Obj("custom_config", in.GetString("config")))
		var newName *string
		if s, ok := in.Value("new").(string); ok {
			newName = &s
		}
		changed, err := RelinkGroupListeners(profile, in.GetString("old"), newName)
		if want.Has("error") {
			if err == nil {
				stats["python_error_go_ok:"+want.GetString("type")]++
				if shown < 6 {
					shown++
					t.Errorf("row %d python error %q but Go edited it. new=%v\nSRC:\n%s", i, want.GetString("error"), in.Value("new"), strings.ReplaceAll(in.GetString("config"), " ", "·"))
				}
				continue
			}
			if want.GetString("type") == "AssertionError" {
				// ruamel's emitter crashes (an uncaught AssertionError, HTTP 500
				// in Flask); Go keeps the original text with the safe error.
				stats["python_crash_go_error"]++
				continue
			}
			if err.Error() != want.GetString("error") {
				t.Fatalf("row %d error %q want %q", i, err, want.GetString("error"))
			}
			stats["python_error_match"]++
			continue
		}
		if err != nil {
			stats["go_declined"]++
			reasons[causeOf(err)]++
			continue
		}
		wantChanged := compact(t, want.Value("changed"))
		gotConfig := profile.GetObject("mihomo").GetString("custom_config")
		if compact(t, jsonx.ListOf(changed)) != wantChanged || gotConfig != want.GetString("config") {
			stats["mismatch"]++
			if shown < 10 {
				shown++
				t.Errorf("row %d new=%v\nSRC:\n%s\nDIFF:\n%s", i, in.Value("new"), in.GetString("config"), firstDiff(gotConfig, want.GetString("config")))
			}
			continue
		}
		stats["match"]++
	}
	t.Logf("%d rows %v", len(rows), stats)
	t.Logf("declined: %v", reasons)
	// Declines are a documented gap (the caller keeps the original text with
	// Python's failure message); do not let them grow unnoticed.
	if stats["go_declined"] > maxDeclined {
		t.Errorf("declined rows grew to %d (was %d)", stats["go_declined"], maxDeclined)
	}
}

func firstDiff(got, want string) string {
	g, w := strings.Split(got, "\n"), strings.Split(want, "\n")
	for i := 0; i < len(g) || i < len(w); i++ {
		var a, b string
		if i < len(g) {
			a = g[i]
		}
		if i < len(w) {
			b = w[i]
		}
		if a != b || i >= len(g) || i >= len(w) {
			return fmt.Sprintf("line %d (of got %d, want %d)\n got:  %q\n want: %q", i+1, len(g), len(w), a, b)
		}
	}
	return "equal"
}

func causeOf(err error) string {
	var le *ListenerReferenceError
	if errors.As(err, &le) && le.Cause != nil {
		s := le.Cause.Error()
		s = strings.TrimPrefix(s, "comment-preserving edit not supported: ")
		if i := strings.Index(s, " at line"); i > 0 {
			s = s[:i]
		}
		return s
	}
	return err.Error()
}
