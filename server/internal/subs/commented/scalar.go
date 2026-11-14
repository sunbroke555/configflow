package commented

import (
	"fmt"
	"regexp"
	"strings"
	"unicode/utf8"
)

var reDate = regexp.MustCompile(`^[0-9]{4}-[0-9]{2}-[0-9]{2}$`)

// plainOut is how ruamel (YAML 1.1 load, YAML 1.2 dump, preserve_quotes)
// writes a plain scalar of the source: its text, and whether it is null.
func plainOut(raw string) (text string, null bool, err error) {
	return plainOutMode(raw, false)
}

// plainOutMode: yaml12 is true under an explicit `---` (the document then
// loads with YAML 1.2 rules, where yes/no/on/off are plain strings).
func plainOutMode(raw string, yaml12 bool) (text string, null bool, err error) {
	text, tag, err := plainScalar(raw, yaml12)
	return text, tag == "null", err
}

// plainScalar resolves a plain scalar of the source with the load version's
// rules, constructs it as the round-trip constructor does and returns the
// text the representer writes plus the resolved tag.
func plainScalar(raw string, yaml12 bool) (text, tag string, err error) {
	if raw == "-" || raw == "?" || raw == ":" {
		// an indicator on its own: a nested collection or a quoted string in ruamel
		return "", "", unsupported("lone indicator")
	}
	tag = resolveTag(raw, yaml12)
	switch tag {
	case "null":
		return "", tag, nil
	case "bool":
		switch strings.ToLower(raw) {
		case "yes", "true", "on", "y":
			return "true", tag, nil
		}
		return "false", tag, nil
	case "int", "float":
		text, err = numberText(raw, tag, yaml12)
		return text, tag, err
	case "timestamp":
		if reDate.MatchString(raw) {
			return raw, tag, nil
		}
		return "", tag, unsupported("timestamp")
	case "merge", "value":
		return raw, tag, nil
	}
	if resolveTag(raw, true) != "str" {
		// a string that YAML 1.2 would read as something else
		return "'" + strings.ReplaceAll(raw, "'", "''") + "'", tag, nil
	}
	return raw, tag, nil
}

func printableRune(r rune) bool {
	return r == 0x0a || r >= 0x20 && r <= 0x7e || r >= 0xa0 && r <= 0xd7ff || r >= 0xe000 && r <= 0xfffd && r != 0xfeff || r >= 0x10000 && r <= 0x10ffff
}

var escapes = map[rune]string{
	0: "0", 7: "a", 8: "b", 9: "t", 10: "n", 11: "v", 12: "f", 13: "r", 27: "e", '"': `"`, '\\': `\`,
	0x85: "N", 0xa0: "_", 0x2028: "L", 0x2029: "P",
}

// foldDouble is write_double_quoted: it returns the physical pieces of the
// quoted text. piece 0 continues the current line; every later piece starts
// a new line indented by indent (the caller writes the indentation). column
// is the column after the opening quote has been written; split enables
// folding (not for simple keys). nonl is ruamel's no_newline flag: when it
// is set, the first fold writes no line break and clears it.
func foldDouble(text string, column, indent, width int, split bool, nonl *bool) []string {
	rs := []rune(text)
	n := len(rs)
	var pieces []string
	cur := `"`
	start, end := 0, 0
	for end <= n {
		var ch rune
		has := end < n
		if has {
			ch = rs[end]
		}
		if !has || ch == '"' || ch == '\\' || ch == 0x85 || ch == 0x2028 || ch == 0x2029 || ch == 0xfeff ||
			!(ch >= 0x20 && ch <= 0x7e || ch >= 0xa0 && ch <= 0xd7ff || ch >= 0xe000 && ch <= 0xfffd || ch >= 0x10000) {
			if start < end {
				column += end - start
				cur += string(rs[start:end])
				start = end
			}
			if has {
				var data string
				if e, ok := escapes[ch]; ok {
					data = `\` + e
				} else if ch <= 0xff {
					data = fmt.Sprintf(`\x%02X`, ch)
				} else if ch <= 0xffff {
					data = fmt.Sprintf(`\u%04X`, ch)
				} else {
					data = fmt.Sprintf(`\U%08X`, ch)
				}
				column += len(data)
				cur += data
				start = end + 1
			}
		}
		if end > 0 && end < n-1 && (ch == ' ' || start >= end) && column+(end-start) > width && split {
			need := needBackslash(rs, start, end)
			data := ""
			if start < end {
				data = string(rs[start:end])
			}
			if need {
				data += `\`
			}
			if start < end {
				start = end
			}
			column += utf8.RuneCountInString(data)
			cur += data
			if nonl != nil && *nonl {
				// write_indent with no_newline: no break; pad up to indent
				*nonl = false
				if column < indent {
					cur += spaces(indent - column)
					column = indent
				}
			} else {
				pieces = append(pieces, cur)
				cur = ""
				column = indent
			}
			if rs[start] == ' ' {
				if !need {
					start++
				} else {
					column++
					cur += `\`
				}
			}
		}
		end++
	}
	cur += `"`
	pieces = append(pieces, cur)
	return pieces
}

// needBackslash is the need_backslash decision of write_double_quoted
// (ruamel 0.19: a fold before a word that is followed by a plain space
// needs no escaped line end).
func needBackslash(rs []rune, start, end int) bool {
	sp := -1
	for i := end; i < len(rs); i++ {
		if rs[i] == ' ' {
			sp = i
			break
		}
	}
	if sp < 0 {
		return true // ValueError
	}
	for i := end; i < sp; i++ {
		if rs[i] == '\n' {
			sp = i
			break
		}
	}
	if sp+1 >= len(rs) {
		return true // IndexError
	}
	if rs[sp] == '\n' && rs[sp+1] != ' ' {
		return true
	}
	for _, r := range rs[end:sp] {
		if r == '"' || r == '\'' {
			return true
		}
	}
	if rs[sp+1] == ' ' || rs[sp+1] == '\n' || string(rs[end-1:end+1]) == "  " || start == end {
		return true
	}
	return false
}

// doubleQuoted is write_double_quoted for a value that needs no folding.
func doubleQuoted(s string) string {
	return foldDouble(s, 0, 0, 1<<30, false, nil)[0]
}

// scalarText returns the text written for a non-literal scalar node and
// whether it is a null (written empty in block context).
func scalarText(n *node, inFlow bool, yaml12 bool) (string, bool, error) {
	switch n.style {
	case sPlain:
		t, null, err := plainOutMode(n.raw, yaml12)
		if null && inFlow {
			return "null", false, err
		}
		return t, null, err
	case sSingle:
		for _, r := range n.raw {
			if !printableRune(r) || r == 0x0a || r == 0x85 || r == 0x2028 || r == 0x2029 {
				return "", false, unsupported("special characters in single-quoted scalar")
			}
		}
		return "'" + strings.ReplaceAll(n.raw, "'", "''") + "'", false, nil
	case sDouble:
		return doubleQuoted(n.raw), false, nil
	}
	return "", false, unsupported("scalar style")
}

// literalHints is the header of a block scalar for the given value.
func literalHints(value string) string {
	hints := ""
	if value != "" {
		first := []rune(value)[0]
		if first == ' ' || first == '\n' || first == 0x85 || first == 0x2028 || first == 0x2029 {
			hints += "2"
		}
		rs := []rune(value)
		last := rs[len(rs)-1]
		if last != '\n' && last != 0x85 && last != 0x2028 && last != 0x2029 {
			hints += "-"
		} else if len(rs) == 1 || rs[len(rs)-2] == '\n' || rs[len(rs)-2] == 0x85 || rs[len(rs)-2] == 0x2028 || rs[len(rs)-2] == 0x2029 {
			hints += "+"
		}
	}
	return hints
}
