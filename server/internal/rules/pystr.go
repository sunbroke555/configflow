package rules

import (
	"strings"
	"unicode"
	"unicode/utf8"
)

// This file holds the few Python str behaviours the rule engine depends on and
// that differ from Go's strings package: the whitespace set of str.strip(),
// str.lower() special cases and str.splitlines() separators.

// pyIsSpace reports whether r is whitespace for str.isspace()/str.strip().
func pyIsSpace(r rune) bool {
	switch {
	case r >= 0x09 && r <= 0x0d, r >= 0x1c && r <= 0x20:
		return true
	case r < 0x85:
		return false
	}
	switch r {
	case 0x85, 0xa0, 0x1680, 0x2028, 0x2029, 0x202f, 0x205f, 0x3000:
		return true
	}
	return r >= 0x2000 && r <= 0x200a
}

// pyStrip is s.strip().
func pyStrip(s string) string {
	return strings.TrimFunc(s, pyIsSpace)
}

// pyStripChars is s.strip(chars) for a set of characters.
func pyStripChars(s, chars string) string {
	return strings.Trim(s, chars)
}

// pyLower is s.lower(): full Unicode lowercasing, including the two
// context/length special cases Go's strings.ToLower does not have
// (U+0130 becomes "i" + U+0307, a final capital sigma becomes U+03C2).
func pyLower(s string) string {
	ascii := true
	for i := 0; i < len(s); i++ {
		if c := s[i]; c >= utf8.RuneSelf {
			ascii = false
			break
		}
	}
	if ascii {
		return strings.ToLower(s)
	}
	runes := []rune(s)
	var b strings.Builder
	for i, r := range runes {
		switch r {
		case 0x130:
			b.WriteString("i̇")
		case 0x3a3:
			if finalSigma(runes, i) {
				b.WriteRune(0x3c2)
			} else {
				b.WriteRune(0x3c3)
			}
		default:
			b.WriteRune(unicode.ToLower(r))
		}
	}
	return b.String()
}

func caseIgnorable(r rune) bool {
	switch r {
	case '\'', '.', ':', 0xb7, 0x2019, 0x2018, 0x2024, 0x2027, 0xfe13, 0xfe52, 0xfe55, 0xff07, 0xff0e, 0xff1a:
		return true
	}
	return unicode.In(r, unicode.Mn, unicode.Me, unicode.Cf, unicode.Lm, unicode.Sk)
}

func cased(r rune) bool {
	return unicode.IsUpper(r) || unicode.IsLower(r) || unicode.IsTitle(r)
}

// finalSigma implements the Final_Sigma condition of the Unicode standard.
func finalSigma(runes []rune, i int) bool {
	before := false
	for j := i - 1; j >= 0; j-- {
		if caseIgnorable(runes[j]) {
			continue
		}
		before = cased(runes[j])
		break
	}
	if !before {
		return false
	}
	for j := i + 1; j < len(runes); j++ {
		if caseIgnorable(runes[j]) {
			continue
		}
		return !cased(runes[j])
	}
	return true
}

// pyIsASCIIDigits is s.isascii() and s.isdigit() for a non-empty string.
func pyIsASCIIDigits(s string) bool {
	if s == "" {
		return false
	}
	for i := 0; i < len(s); i++ {
		if s[i] < '0' || s[i] > '9' {
			return false
		}
	}
	return true
}

// isLineEnd reports the separators of the Python regex
// `\r\n|[\n\r\v\f\x1c-\x1e\x85  ]`; width is the byte length.
func lineEndAt(s string, i int) (width int) {
	c := s[i]
	switch c {
	case '\r':
		if i+1 < len(s) && s[i+1] == '\n' {
			return 2
		}
		return 1
	case '\n', '\v', '\f', 0x1c, 0x1d, 0x1e:
		return 1
	}
	if c >= utf8.RuneSelf {
		r, size := utf8.DecodeRuneInString(s[i:])
		if r == 0x85 || r == 0x2028 || r == 0x2029 {
			return size
		}
	}
	return 0
}

// iterRuleLines is _iter_rule_lines: str.splitlines() semantics, yielding
// slices of s (no copies).
func iterRuleLines(s string, yield func(string) bool) {
	start := 0
	for i := 0; i < len(s); {
		if w := lineEndAt(s, i); w > 0 {
			if !yield(s[start:i]) {
				return
			}
			i += w
			start = i
			continue
		}
		i++
	}
	if start < len(s) {
		yield(s[start:])
	}
}

// pyIsIdentifier is str.isidentifier() (XID_Start/XID_Continue approximated by
// letter/digit/mark/connector categories).
func pyIsIdentifier(s string) bool {
	if s == "" {
		return false
	}
	for i, r := range s {
		if r == '_' || unicode.IsLetter(r) || unicode.Is(unicode.Nl, r) {
			continue
		}
		if i > 0 && (unicode.IsDigit(r) || unicode.In(r, unicode.Mn, unicode.Mc, unicode.Pc)) {
			continue
		}
		return false
	}
	return true
}

// pyWordRune reports whether r matches the Unicode \w of Python's re.
func pyWordRune(r rune) bool {
	return r == '_' || unicode.IsLetter(r) || unicode.IsNumber(r)
}

var upperSpecial = map[rune]string{
	0xdf: "SS", 0x149: "ʼN", 0x1f0: "J̌", 0xfb00: "FF", 0xfb01: "FI", 0xfb02: "FL",
	0xfb03: "FFI", 0xfb04: "FFL", 0xfb05: "ST", 0xfb06: "ST",
}

// pyUpper is s.upper(): Go's ToUpper plus the one-to-many mappings of
// SpecialCasing.txt that can turn a non-ASCII letter into ASCII ones.
func pyUpper(s string) string {
	if isASCII(s) {
		return strings.ToUpper(s)
	}
	var b strings.Builder
	for _, r := range s {
		if text, ok := upperSpecial[r]; ok {
			b.WriteString(text)
		} else {
			b.WriteRune(unicode.ToUpper(r))
		}
	}
	return b.String()
}
