package stats

import (
	"context"
	"testing"
	"time"

	"configflow/internal/httpx"
	"configflow/internal/jsonx"
	"configflow/internal/repo"
)

func overviewOf(t *testing.T, app *httpx.App, repository *repo.Repository, header string) (int, *jsonx.Object) {
	t.Helper()
	request := httpx.InvokeRequest{Method: "GET", Path: "/api/stats/overview"}
	if header != "" {
		request.Header = map[string][]string{"X-ConfigFlow-Profile": {header}}
	}
	res, err := app.Invoke(repo.WithRepository(context.Background(), repository), request)
	if err != nil {
		t.Fatal(err)
	}
	value, err := jsonx.Parse(res.Body)
	if err != nil {
		t.Fatalf("%v %s", err, res.Body)
	}
	return res.Status, value.(*jsonx.Object)
}

func TestOverviewCountsAndHistorySampling(t *testing.T) {
	repository, err := repo.Open(t.TempDir(), repo.Options{})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	if _, err := repository.UpdateSharedTransaction(ctx, func(_ context.Context, shared *jsonx.Object) (*jsonx.Object, error) {
		shared.Set("subscriptions", []any{jsonx.Obj("id", "s1", "name", "S", "url", "https://sub.test/x")})
		return nil, nil
	}); err != nil {
		t.Fatal(err)
	}
	app := httpx.New()
	Register(app.Router)
	clock := time.Date(2026, 11, 14, 12, 0, 0, 0, time.UTC)
	Now = func() time.Time { return clock }
	defer func() { Now = time.Now }()

	status, body := overviewOf(t, app, repository, "")
	data := body.GetObject("data")
	if status != 200 || data.GetObject("subscriptions").GetInt("total") != 1 ||
		!jsonx.Equal(data.GetObject("subscriptions").Value("history"), []any{1}) ||
		!jsonx.Equal(data.GetObject("rules").Value("history"), []any{0}) {
		t.Fatal(jsonx.Repr(body))
	}
	profile := data.GetObject("profile")
	if _, ok := profile.Value("updated_at").(string); !ok || profile.GetInt("revision") < 1 {
		t.Fatal(jsonx.Repr(profile))
	}
	saved, err := repository.ReadProfileJSON("default", HistoryFile)
	if err != nil || len(saved.GetList("samples")) != 1 ||
		saved.GetList("samples")[0].(*jsonx.Object).GetString("at") != "2026-11-14T12:00:00Z" {
		t.Fatalf("%v %v", err, jsonx.Repr(saved))
	}
	// Within the sampling interval: not saved, the last point shows the current value.
	clock = clock.Add(10 * time.Minute)
	if _, err := repository.UpdateSharedTransaction(ctx, func(_ context.Context, shared *jsonx.Object) (*jsonx.Object, error) {
		shared.Set("subscriptions", []any{})
		return nil, nil
	}); err != nil {
		t.Fatal(err)
	}
	_, body = overviewOf(t, app, repository, "")
	if !jsonx.Equal(body.GetObject("data").GetObject("subscriptions").Value("history"), []any{0}) {
		t.Fatal(jsonx.Repr(body))
	}
	saved, _ = repository.ReadProfileJSON("default", HistoryFile)
	if len(saved.GetList("samples")) != 1 {
		t.Fatal("sampled too early")
	}
	clock = clock.Add(30 * time.Minute)
	_, body = overviewOf(t, app, repository, "")
	if !jsonx.Equal(body.GetObject("data").GetObject("subscriptions").Value("history"), []any{1, 0}) {
		t.Fatal(jsonx.Repr(body))
	}
	status, body = overviewOf(t, app, repository, "missing")
	if status != 404 || body.GetString("message") != "Profile not found: missing" {
		t.Fatal(status, jsonx.Repr(body))
	}
}

func TestIsoformatZ(t *testing.T) {
	if got := isoformatZ(time.Date(2026, 1, 2, 3, 4, 5, 123456789, time.UTC)); got != "2026-01-02T03:04:05.123456Z" {
		t.Fatal(got)
	}
	if got := isoformatZ(time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)); got != "2026-01-02T03:04:05Z" {
		t.Fatal(got)
	}
}
