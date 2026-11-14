package subs

import (
	"bufio"
	"compress/gzip"
	"os"
	"testing"

	"configflow/internal/jsonx"
)

// corpusRow is one line of a testdata/*.jsonl file written by gen.py.
type corpusRow struct {
	Fn  string
	In  any
	Out any
}

func loadCorpus(t *testing.T, name string) []corpusRow {
	t.Helper()
	f, err := os.Open("testdata/" + name)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	zr, err := gzip.NewReader(f)
	if err != nil {
		t.Fatal(err)
	}
	var rows []corpusRow
	sc := bufio.NewScanner(zr)
	sc.Buffer(make([]byte, 1<<20), 1<<26)
	for sc.Scan() {
		v, err := jsonx.Parse(sc.Bytes())
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		o := v.(*jsonx.Object)
		rows = append(rows, corpusRow{o.GetString("fn"), o.Value("in"), o.Value("out")})
	}
	if err := sc.Err(); err != nil {
		t.Fatal(err)
	}
	return rows
}

func compact(t *testing.T, v any) string {
	t.Helper()
	b, err := jsonx.Compact(v)
	if err != nil {
		t.Fatalf("encode: %v", err)
	}
	return string(b)
}

// isErrorOut reports whether a recorded output is a Python exception, with its text.
func isErrorOut(out any) (string, bool) {
	o, ok := out.(*jsonx.Object)
	if !ok || !o.Has("error") || !o.Has("type") && o.Len() != 1 {
		return "", false
	}
	return o.GetString("error"), true
}
