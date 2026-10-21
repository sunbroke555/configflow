package routes

import (
	"encoding/json"
	"testing"
)

func TestOxSourceClassification(t *testing.T) {
	step := func(tag, outcome, kind string) oxSourceStep {
		return oxSourceStep{Sequence: "main", Tag: tag, Outcome: outcome, Kind: kind}
	}
	run := func(tag string) []oxSourceStep {
		return []oxSourceStep{step(tag, "entered", "executor"), step(tag, "next", "executor")}
	}
	cases := []struct {
		name, want string
		steps      []oxSourceStep
	}{
		{"local", "local", run("forward_local")},
		{"remote", "remote", run("forward_remote")},
		{"fallback", "fallback", run("forward_fallback")},
		{"probe_then_remote", "remote", append(run("forward_local"), run("forward_remote")...)},
		{"cache_hit", "cache", []oxSourceStep{step("lazy_cache", "entered", "executor"), step("lazy_cache", "stop", "executor")}},
		{"cache_miss", "local", append(append([]oxSourceStep{step("lazy_cache", "entered", "executor")}, run("forward_local")...), step("lazy_cache", "stop", "executor"))},
		{"failed_forward", "unknown", []oxSourceStep{step("forward_remote", "entered", "executor"), step("forward_remote", "error", "executor")}},
		{"unmatched_completion", "unknown", []oxSourceStep{step("forward_remote", "next", "executor")}},
		{"discard", "unknown", append(run("forward_local"), run("qs.exec.main.2.drop_resp")...)},
		{"reject", "rejected", append(run("forward_local"), step("reject", "stop", "builtin"))},
		// Recorded for a blocked HTTPS query: the enclosing prefer_ipv4 stop follows.
		{"blocked_qtype", "rejected", []oxSourceStep{step("qs.exec.main.4.black_hole", "entered", "executor"),
			step("qs.exec.main.4.black_hole", "stop", "executor"), step("qs.exec.main.3.prefer_ipv4", "stop", "executor")}},
		{"black_hole_continues", "remote", append(run("qs.exec.main.4.black_hole"), run("forward_remote")...)},
		{"hosts", "hosts", append(run("custom_hosts"), step("qs.match.main.1.0.has_resp", "matched", "matcher"))},
		{"block", "blocked", run("adguard_block")},
		{"custom", "unknown", run("forward_custom")},
		{"empty", "unrecorded", nil},
	}
	probe := []oxSourceStep{step("local_probe_cache", "entered", "executor"), step("qs.match.local_probe.1.0.has_resp", "not_matched", "matcher"), step("return", "return", "builtin"), step("local_probe_cache", "return", "executor")}
	for i := range probe {
		probe[i].Sequence = "local_probe"
	}
	cases = append(cases, struct {
		name, want string
		steps      []oxSourceStep
	}{"probe_cache", "cache", probe})
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := oxQuerySource(oxSourceRecord{HasResponse: true, Steps: tc.steps}); got != tc.want {
				t.Fatalf("got %s want %s", got, tc.want)
			}
		})
	}
}

// The reported api.telegram.org. query stops at the standard IPv4 selector;
// the core intentionally does not merge its isolated probe's execution path.
const oxIPv4PolicyUserTrace = `{
  "id":160909,"created_at_ms":1792218836000,"client_ip":"192.168.0.105",
  "questions_json":[{"name":"api.telegram.org.","qtype":"AAAA","qclass":"IN"}],
  "elapsed_ms":0,"has_response":true,"error":null,"rcode":"NOERROR",
  "answer_count":0,"answers_json":[],
  "steps":[
    {"event_index":0,"sequence_tag":"sequence_main","node_index":2,"kind":"executor","tag":"qs.exec.sequence_main.2.prefer_ipv4","outcome":"entered"},
    {"event_index":1,"sequence_tag":"sequence_main","node_index":2,"kind":"executor","tag":"qs.exec.sequence_main.2.prefer_ipv4","outcome":"stop"}
  ]
}`

func oxIPv4PolicyRecord(t *testing.T) oxSourceRecord {
	t.Helper()
	var record oxSourceRecord
	if err := json.Unmarshal([]byte(oxIPv4PolicyUserTrace), &record); err != nil {
		t.Fatal(err)
	}
	return record
}

func TestOxSourceIPv4PolicyUserTrace(t *testing.T) {
	if got := oxQuerySource(oxIPv4PolicyRecord(t)); got != "ipv4_preferred" {
		t.Fatalf("user trace got %s, want ipv4_preferred", got)
	}
}

func TestOxSourceIPv4PolicyDNSCodeRepresentations(t *testing.T) {
	for _, qtype := range []string{`"AAAA"`, `"28"`, `28`, `" aaaa "`} {
		for _, rcode := range []string{`"No Error"`, `"NOERROR"`, `"0"`, `0`} {
			t.Run(qtype+"_"+rcode, func(t *testing.T) {
				record := oxIPv4PolicyRecord(t)
				record.Questions = json.RawMessage(`[{"qtype":` + qtype + `}]`)
				record.Rcode = json.RawMessage(rcode)
				if got := oxQuerySource(record); got != "ipv4_preferred" {
					t.Fatalf("qtype %s rcode %s got %s", qtype, rcode, got)
				}
			})
		}
	}
}

func TestOxSourceIPv4PolicyRequiresCompleteResponseEvidence(t *testing.T) {
	for _, questions := range []string{
		``, `null`, `[]`, `[{}]`, `[{"qtype":null}]`, `[{"qtype":true}]`,
		`[{"qtype":"A"}]`, `[{"qtype":1}]`, `[{"qtype":"HTTPS"}]`,
		`[{"qtype":"AAAA"},{"qtype":"AAAA"}]`, `{"qtype":"AAAA"}`,
	} {
		t.Run("questions_"+questions, func(t *testing.T) {
			record := oxIPv4PolicyRecord(t)
			record.Questions = json.RawMessage(questions)
			if got := oxQuerySource(record); got != "unknown" {
				t.Fatalf("incomplete/non-AAAA questions gave %s", got)
			}
		})
	}
	for _, rcode := range []string{``, `null`, `false`, `[]`, `{}`, `""`, `"NXDOMAIN"`, `3`, `"SERVFAIL"`, `2`} {
		t.Run("rcode_"+rcode, func(t *testing.T) {
			record := oxIPv4PolicyRecord(t)
			record.Rcode = json.RawMessage(rcode)
			if got := oxQuerySource(record); got != "unknown" {
				t.Fatalf("non-NOERROR rcode gave %s", got)
			}
		})
	}
	for _, count := range []string{``, `null`, `false`, `[]`, `{}`, `"0"`, `1`, `-1`, `0.5`} {
		t.Run("answer_count_"+count, func(t *testing.T) {
			record := oxIPv4PolicyRecord(t)
			record.AnswerCount = json.RawMessage(count)
			if got := oxQuerySource(record); got != "unknown" {
				t.Fatalf("missing/nonzero/non-numeric answer_count gave %s", got)
			}
		})
	}
}

func TestOxSourceIPv4PolicyRequiresExactAdjacentCall(t *testing.T) {
	cases := []struct {
		name, want string
		change     func(*oxSourceRecord)
	}{
		{"pass_through_next", "unknown", func(r *oxSourceRecord) { r.Steps[1].Outcome = "next" }},
		{"pass_through_return", "unknown", func(r *oxSourceRecord) { r.Steps[1].Outcome = "return" }},
		{"failed_selector", "unknown", func(r *oxSourceRecord) { r.Steps[1].Outcome = "error" }},
		{"missing_entry", "unknown", func(r *oxSourceRecord) { r.Steps = r.Steps[1:] }},
		{"missing_completion", "unknown", func(r *oxSourceRecord) { r.Steps = r.Steps[:1] }},
		{"different_sequence", "unknown", func(r *oxSourceRecord) { r.Steps[0].Sequence = "other" }},
		{"different_node", "unknown", func(r *oxSourceRecord) { node := 3; r.Steps[0].Node = &node }},
		{"different_tag", "unknown", func(r *oxSourceRecord) { r.Steps[0].Tag = "qs.exec.sequence_main.3.prefer_ipv4" }},
		{"not_executor", "unknown", func(r *oxSourceRecord) { r.Steps[1].Kind = "matcher" }},
		{"custom_tag", "unknown", func(r *oxSourceRecord) {
			for i := range r.Steps {
				r.Steps[i].Tag = "prefer_ipv4"
			}
		}},
		{"wrong_quick_tag", "unknown", func(r *oxSourceRecord) {
			for i := range r.Steps {
				r.Steps[i].Tag = "qs.custom.prefer_ipv4"
			}
		}},
		{"tag_sequence_mismatch", "unknown", func(r *oxSourceRecord) {
			for i := range r.Steps {
				r.Steps[i].Sequence = "other"
			}
		}},
		{"tag_node_mismatch", "unknown", func(r *oxSourceRecord) {
			node := 3
			for i := range r.Steps {
				r.Steps[i].Node = &node
			}
		}},
		{"missing_node", "unknown", func(r *oxSourceRecord) {
			for i := range r.Steps {
				r.Steps[i].Node = nil
			}
		}},
		{"negative_node", "unknown", func(r *oxSourceRecord) {
			node := -1
			for i := range r.Steps {
				r.Steps[i].Node = &node
				r.Steps[i].Tag = "qs.exec.sequence_main.-1.prefer_ipv4"
			}
		}},
		{"empty_sequence", "unknown", func(r *oxSourceRecord) {
			for i := range r.Steps {
				r.Steps[i].Sequence = ""
				r.Steps[i].Tag = "qs.exec..2.prefer_ipv4"
			}
		}},
		{"prefer_ipv6", "unknown", func(r *oxSourceRecord) {
			for i := range r.Steps {
				r.Steps[i].Tag = "qs.exec.sequence_main.2.prefer_ipv6"
			}
		}},
		{"intermediate_step", "unknown", func(r *oxSourceRecord) {
			r.Steps = []oxSourceStep{r.Steps[0], {Kind: "matcher", Outcome: "matched"}, r.Steps[1]}
		}},
		{"wrapped_forward", "remote", func(r *oxSourceRecord) {
			r.Steps = []oxSourceStep{r.Steps[0], {Sequence: "sequence_main", Kind: "executor", Tag: "forward_remote", Outcome: "entered"}, {Sequence: "sequence_main", Kind: "executor", Tag: "forward_remote", Outcome: "stop"}, r.Steps[1]}
		}},
		{"query_failed", "error", func(r *oxSourceRecord) { text := "timeout"; r.Error = &text }},
		{"no_response", "no_response", func(r *oxSourceRecord) { r.HasResponse = false }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			record := oxIPv4PolicyRecord(t)
			tc.change(&record)
			if got := oxQuerySource(record); got != tc.want {
				t.Fatalf("got %s want %s", got, tc.want)
			}
		})
	}
}

func TestOxSourceIPv4PolicySurvivesWrappingCacheButNotLaterReplacement(t *testing.T) {
	record := oxIPv4PolicyRecord(t)
	record.Steps = append([]oxSourceStep{{Sequence: "sequence_main", Kind: "executor", Tag: "lazy_cache", Outcome: "entered"}}, record.Steps...)
	record.Steps = append(record.Steps, oxSourceStep{Sequence: "sequence_main", Kind: "executor", Tag: "lazy_cache", Outcome: "stop"})
	if got := oxQuerySource(record); got != "ipv4_preferred" {
		t.Fatalf("wrapping cache incorrectly replaced source with %s", got)
	}
	record.Steps = append(record.Steps, oxSourceStep{Sequence: "sequence_main", Kind: "executor", Tag: "drop_resp", Outcome: "entered"}, oxSourceStep{Sequence: "sequence_main", Kind: "executor", Tag: "drop_resp", Outcome: "next"})
	if got := oxQuerySource(record); got != "unknown" {
		t.Fatalf("discarded response retained source %s", got)
	}
	record.Steps = append(record.Steps, oxSourceStep{Sequence: "sequence_main", Kind: "executor", Tag: "forward_remote", Outcome: "entered"}, oxSourceStep{Sequence: "sequence_main", Kind: "executor", Tag: "forward_remote", Outcome: "next"})
	if got := oxQuerySource(record); got != "remote" {
		t.Fatalf("replacement response retained source %s", got)
	}
}
