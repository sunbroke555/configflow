package httpx

import (
	"math/big"
	"strings"
	"unicode"

	"configflow/internal/jsonx"
)

// PyParseInt is Python's int(text) for a str: surrounding whitespace, an
// optional sign, decimal digits (any Unicode decimal digit) with single
// underscores between them. ok is false where Python raises ValueError.
func PyParseInt(text string) (*big.Int, bool) {
	s := strings.TrimFunc(text, isPySpace)
	negative := false
	if s != "" && (s[0] == '+' || s[0] == '-') {
		negative = s[0] == '-'
		s = s[1:]
	}
	if s == "" {
		return nil, false
	}
	var digits strings.Builder
	previousUnderscore := true // no leading underscore
	for _, r := range s {
		if r == '_' {
			if previousUnderscore {
				return nil, false
			}
			previousUnderscore = true
			continue
		}
		if !unicode.IsDigit(r) {
			return nil, false
		}
		digits.WriteByte(byte('0' + digitValue(r)))
		previousUnderscore = false
	}
	if previousUnderscore {
		return nil, false
	}
	value, ok := new(big.Int).SetString(digits.String(), 10)
	if !ok {
		return nil, false
	}
	if negative {
		value.Neg(value)
	}
	return value, true
}

func digitValue(r rune) int {
	if r >= '0' && r <= '9' {
		return int(r - '0')
	}
	// Unicode Nd characters come in contiguous runs of ten starting at zero.
	for zero := r; zero >= r-9; zero-- {
		if !unicode.IsDigit(zero - 1) {
			return int(r - zero)
		}
	}
	return int(r-'0') % 10
}

// isPySpace is str.isspace() for one character.
func isPySpace(r rune) bool {
	switch r {
	case ' ', '\t', '\n', '\v', '\f', '\r', 0x1c, 0x1d, 0x1e, 0x1f, 0x85:
		return true
	}
	return r > 0x7f && unicode.IsSpace(r)
}

// PyTypeName is type(v).__name__ for a JSON value, for messages such as
// "'list' object has no attribute 'get'".
func PyTypeName(v any) string {
	switch value := v.(type) {
	case nil:
		return "NoneType"
	case string:
		return "str"
	case bool:
		return "bool"
	case *jsonx.Object:
		if value == nil {
			return "NoneType"
		}
		return "dict"
	case []any:
		return "list"
	case jsonx.Number:
		if value.IsInt() {
			return "int"
		}
		return "float"
	}
	return "object"
}

// NoAttributeGet is the AttributeError text of calling .get on a value that
// is not a dict: "'NoneType' object has no attribute 'get'".
func NoAttributeGet(v any) string {
	return "'" + PyTypeName(v) + "' object has no attribute 'get'"
}
