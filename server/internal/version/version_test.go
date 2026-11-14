package version

import (
	"testing"

	"configflow/internal/jsonx"
)

func TestInfo(t *testing.T) {
	body, _ := jsonx.Compact(Info())
	if string(body) != `{"version":"dev"}` {
		t.Errorf("got %s", body)
	}
	Version = "1.2.3"
	defer func() { Version = "dev" }()
	if Info().GetString("version") != "1.2.3" {
		t.Error("Info does not follow Version")
	}
}
