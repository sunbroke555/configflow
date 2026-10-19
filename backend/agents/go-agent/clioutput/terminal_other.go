//go:build !linux && !darwin

package clioutput

import "os"

func terminal(file *os.File) bool { return false }
