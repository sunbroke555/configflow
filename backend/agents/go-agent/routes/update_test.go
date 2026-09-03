package routes

import (
	"errors"
	"net/http"
	"net/url"
	"path/filepath"
	"strings"
	"testing"
)

func TestDownloadTransportErrorPreservesIdentityWithoutLeakingURLCredential(t *testing.T) {
	want := errors.New("download transport failure")
	old := http.DefaultTransport
	http.DefaultTransport = updateRoundTripFunc(func(*http.Request) (*http.Response, error) {
		return nil, want
	})
	t.Cleanup(func() { http.DefaultTransport = old })

	err := downloadFile(filepath.Join(t.TempDir(), "agent"), "https://example.test/?token=download-secret")
	if !errors.Is(err, want) {
		t.Fatalf("errors.Is(err, want) = false: %v", err)
	}
	var urlErr *url.Error
	if !errors.As(err, &urlErr) {
		t.Fatalf("errors.As(err, *url.Error) = false: %v", err)
	}
	if got := err.Error(); strings.Contains(got, "download-secret") || strings.Contains(got, "token=download-secret") {
		t.Fatalf("download URL credential leaked in error: %s", got)
	}
}

type updateRoundTripFunc func(*http.Request) (*http.Response, error)

func (f updateRoundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }
