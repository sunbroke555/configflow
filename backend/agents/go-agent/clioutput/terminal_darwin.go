package clioutput

import (
	"golang.org/x/sys/unix"
	"os"
)

func terminal(file *os.File) bool {
	_, err := unix.IoctlGetTermios(int(file.Fd()), unix.TIOCGETA)
	return err == nil
}
