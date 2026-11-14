// Package version is the port of backend/version.py.
package version

import "configflow/internal/jsonx"

// Version is the release the binary was built as. The image build sets it
// (Python rewrote `__version__ = "dev"` in version.py with sed):
//
//	go build -ldflags "-X configflow/internal/version.Version=${VERSION}"
//
// A local build reports "dev".
var Version = "dev"

// Info is get_version_info(): {"version": Version}.
func Info() *jsonx.Object { return jsonx.Obj("version", Version) }
