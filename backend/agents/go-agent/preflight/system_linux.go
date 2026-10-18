//go:build linux

package preflight

import "golang.org/x/sys/unix"

// TUNGETFEATURES only asks the driver about supported flags. Unlike TUNSETIFF,
// it does not allocate an interface or alter routes, addresses, or sysctls.
func systemTunFeatures(path string) (uint32, error) {
	fd, err := unix.Open(path, unix.O_RDWR|unix.O_CLOEXEC|unix.O_NONBLOCK, 0)
	if err != nil {
		return 0, err
	}
	defer unix.Close(fd)
	features, err := unix.IoctlGetInt(fd, unix.TUNGETFEATURES)
	return uint32(features), err
}
