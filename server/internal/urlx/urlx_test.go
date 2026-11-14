package urlx

import (
	"bufio"
	"encoding/json"
	"errors"
	"os"
	"strings"
	"testing"
)

// Ported from backend/test_safe_url_queries.py (safe_url_for_log cases) and
// backend/test_request_exception_redaction.py (safe_exception_details).

func TestSafeURLForLogRemovesUserinfoAndRedactsSensitiveQueryVariants(t *testing.T) {
	cases := []struct {
		url   string
		parts []string
	}{
		{"https://alice:hunter2@example.test/path?config_token=raw-secret&api-key=key-secret&ok=visible",
			[]string{"https://example.test/path", "config_token=%5BREDACTED%5D", "api-key=%5BREDACTED%5D", "ok=visible"}},
		{"https%253A%252F%252Fbob%253Apass%2540example.test%252Fp%253F%252574oken%253Ddeep-secret",
			[]string{"https://example.test/p", "token=%5BREDACTED%5D"}},
	}
	for _, c := range cases {
		out := SafeURLForLog(c.url)
		for _, part := range c.parts {
			if !strings.Contains(out, part) {
				t.Errorf("%q: %q missing from %q", c.url, part, out)
			}
		}
		for _, secret := range []string{"alice", "hunter2", "bob", "pass", "raw-secret", "key-secret", "deep-secret"} {
			if strings.Contains(out, secret) {
				t.Errorf("%q: %q leaked in %q", c.url, secret, out)
			}
		}
	}
}

func TestSafeURLForLogFailsClosedForInvalidURL(t *testing.T) {
	out := SafeURLForLog("https://user:password@[broken?token=raw-secret")
	if out != "[INVALID URL REDACTED]" {
		t.Errorf("got %q", out)
	}
}

func TestSafeURLForLogRedactsNestedCredentials(t *testing.T) {
	cases := map[string]string{
		"https://example.test/p?next=https%253A%252F%252Fnested.test%252Fp%253Ftoken%253Dnested-url-secret": "nested-url-secret",
		"https://example.test/p?next=token%253Dnested-expression-secret":                                    "nested-expression-secret",
		"https://example.test/p?next=api%252Bkey%253Dplus-layer-secret":                                     "plus-layer-secret",
	}
	for url, secret := range cases {
		out := SafeURLForLog(url)
		if !strings.Contains(out, "next=%5BREDACTED%5D") || strings.Contains(out, secret) {
			t.Errorf("%q -> %q", url, out)
		}
	}
}

func TestSafeURLForLogFailsClosedWhenDecodingRevealsC0(t *testing.T) {
	for _, control := range []string{"%250d%250a", "%2500", "%251f", "%257f"} {
		if out := SafeURLForLog("https://example.test/p?next=ok" + control + "token=secret"); out != "[INVALID URL REDACTED]" {
			t.Errorf("%s: %q", control, out)
		}
	}
}

func TestSafeURLForLogNeverPreservesFragmentCredentials(t *testing.T) {
	for _, url := range []string{
		"https://example.test/path#token=plain-fragment-secret",
		"https://example.test/path#token=encoded%2Dfragment%2Dsecret",
		"https://example.test/path#outer=https%253A%252F%252Fexample.test%252F%253Ftoken%253Dnested-secret",
		"https://example.test/path#safe=ok&api_key=mixed-fragment-secret",
	} {
		out := SafeURLForLog(url)
		if strings.Contains(out, "#") || strings.Contains(out, "secret") {
			t.Errorf("%q -> %q", url, out)
		}
	}
}

type statusError struct{ status int }

func (e statusError) Error() string {
	return "failed for https://upstream.test/sub?token=dynamic-request-secret#secret-fragment"
}
func (e statusError) HTTPStatus() int { return e.status }

func TestSafeExceptionDetails(t *testing.T) {
	got := SafeExceptionDetails(statusError{502})
	if got != "exception_type=urlx.statusError status=502" {
		t.Errorf("got %q", got)
	}
	got = SafeExceptionDetails(errors.New("boom https://x/?token=1"))
	if got != "exception_type=errors.errorString" {
		t.Errorf("got %q", got)
	}
	wrapped := SafeExceptionDetails(wrap{statusError{404}})
	if !strings.HasSuffix(wrapped, " status=404") {
		t.Errorf("wrapped status lost: %q", wrapped)
	}
}

type wrap struct{ error }

func (w wrap) Unwrap() error { return w.error }

type corpusLine struct {
	Kind string `json:"kind"`
	In   string `json:"in"`
	Out  string `json:"out"`
}

func TestMatchesPythonURLLib(t *testing.T) {
	file, err := os.Open("testdata/corpus.jsonl")
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	counts := map[string]int{}
	scanner := bufio.NewScanner(file)
	scanner.Buffer(make([]byte, 1<<20), 1<<24)
	for scanner.Scan() {
		var line corpusLine
		if err := json.Unmarshal(scanner.Bytes(), &line); err != nil {
			t.Fatal(err)
		}
		var got string
		switch line.Kind {
		case "url":
			got = SafeURLForLog(line.In)
		case "unquote":
			got = Unquote(line.In)
		case "unquote_plus":
			got = UnquotePlus(line.In)
		case "quote_plus":
			got = QuotePlus(line.In)
		default:
			t.Fatalf("unknown kind %q", line.Kind)
		}
		if got != line.Out {
			t.Errorf("%s(%q):\n got %q\nwant %q", line.Kind, line.In, got, line.Out)
			continue
		}
		counts[line.Kind]++
	}
	t.Logf("identical to Python: %v", counts)
	if counts["url"] < 1000 {
		t.Fatalf("corpus too small: %v", counts)
	}
}
