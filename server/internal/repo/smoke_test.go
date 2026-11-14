package repo

import (
	"context"
	"testing"

	"configflow/internal/jsonx"
)

func TestSmokeFreshOpen(t *testing.T) {
	ctx := context.Background()
	r, err := OpenDefault(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	profiles, err := r.ListProfiles(ctx)
	if err != nil || len(profiles) != 1 {
		t.Fatalf("profiles %v %v", profiles, err)
	}
	if _, err := r.CreateProfile(ctx, jsonx.Obj("id", "work", "name", "Work"), "default"); err != nil {
		t.Fatal(err)
	}
	if _, _, err := r.UpdateProfileFields(ctx, "work", jsonx.Obj("mihomo", jsonx.Obj("custom_config", "x: 1")), nil); err != nil {
		t.Fatal(err)
	}
	exported, err := r.ExportAll(ctx, false)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := r.ImportAll(ctx, exported); err != nil {
		t.Fatal(err)
	}
	if err := r.DeleteProfile(ctx, "work", ""); err != nil {
		t.Fatal(err)
	}
	again, err := OpenDefault(r.DataDir())
	if err != nil {
		t.Fatal(err)
	}
	if id, _ := again.DefaultProfileID(ctx); id != "default" {
		t.Fatal(id)
	}
}
