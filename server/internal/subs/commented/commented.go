// Package commented is the port of backend/utils/commented_mihomo.py.
package commented

import (
	"errors"

	"configflow/internal/jsonx"
)

// ErrUnsupported is returned until the round-trip editor is implemented.
var ErrUnsupported = errors.New("comment-preserving edit not implemented")

// EditListenerTargets is edit_listener_targets.
func EditListenerTargets(source string, indexes []int, newName *string, expected *jsonx.Object) (string, error) {
	return "", ErrUnsupported
}
