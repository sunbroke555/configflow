//go:build !linux

package preflight

import "fmt"

func systemTunFeatures(string) (uint32, error) {
	return 0, fmt.Errorf("Linux TUN inspection is unavailable on this platform")
}
