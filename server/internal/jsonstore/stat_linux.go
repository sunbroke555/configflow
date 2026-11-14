//go:build linux

package jsonstore

import (
	"io/fs"
	"syscall"
)

func signatureOf(info fs.FileInfo) Signature {
	st := info.Sys().(*syscall.Stat_t)
	return Signature{
		Dev: uint64(st.Dev), Ino: uint64(st.Ino), Size: st.Size,
		MtimeNs: st.Mtim.Sec*1e9 + st.Mtim.Nsec,
		CtimeNs: st.Ctim.Sec*1e9 + st.Ctim.Nsec,
	}
}
