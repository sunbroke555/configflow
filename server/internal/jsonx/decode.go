package jsonx

import (
	"bytes"
	"errors"
	"fmt"
	"strconv"
	"unicode/utf8"
)

// MaxDepth is the deepest nesting of lists and objects Parse accepts.
const MaxDepth = 10000

// maxIntDigits is CPython 3.11's default sys.get_int_max_str_digits().
const maxIntDigits = 4300

// SyntaxError is json.JSONDecodeError. Error() is the Python message, for
// example "Expecting value: line 1 column 1 (char 0)".
type SyntaxError struct {
	Msg    string // "Expecting value", "Extra data", ...
	Offset int    // position in code points from the start (Python's pos)
	Line   int    // 1-based
	Column int    // 1-based, in code points
}

func (e *SyntaxError) Error() string {
	return fmt.Sprintf("%s: line %d column %d (char %d)", e.Msg, e.Line, e.Column, e.Offset)
}

// Parse is json.loads(text) for UTF-8 text: it returns the value in the
// model described in the package comment, with object keys in input order
// and numbers normalised to Python's text. For a repeated key the last
// value wins and the key keeps the position of its first occurrence, as in
// a Python dict.
//
// It accepts what CPython accepts: any value at the top level, the
// constants NaN, Infinity and -Infinity, only space, \t, \n and \r as
// whitespace, and no raw control characters in strings. Syntax errors are
// *SyntaxError with CPython's message and position.
func Parse(data []byte) (any, error) {
	if !utf8.Valid(data) {
		return nil, errors.New("jsonx: input is not valid UTF-8")
	}
	d := decoder{data: data}
	if bytes.HasPrefix(data, []byte("\ufeff")) {
		return nil, d.fail("Unexpected UTF-8 BOM (decode using utf-8-sig)", 0)
	}
	start := d.skipSpace(0)
	value, end, err := d.scan(start)
	if err != nil {
		return nil, err
	}
	if end = d.skipSpace(end); end != len(data) {
		return nil, d.fail("Extra data", end)
	}
	return value, nil
}

// ParseString is Parse for a string.
func ParseString(text string) (any, error) {
	return Parse([]byte(text))
}

// The decoder follows Modules/_json.c function by function, so that errors
// are reported for the same position with the same message.
type decoder struct {
	data  []byte
	depth int
}

func (d *decoder) fail(msg string, pos int) error {
	head := d.data[:pos]
	line := bytes.Count(head, []byte{'\n'}) + 1
	column := utf8.RuneCount(head[bytes.LastIndexByte(head, '\n')+1:]) + 1
	return &SyntaxError{Msg: msg, Offset: utf8.RuneCount(head), Line: line, Column: column}
}

func (d *decoder) skipSpace(i int) int {
	for i < len(d.data) {
		switch d.data[i] {
		case ' ', '\t', '\n', '\r':
			i++
		default:
			return i
		}
	}
	return i
}

func (d *decoder) hasAt(i int, literal string) bool {
	return len(d.data)-i >= len(literal) && string(d.data[i:i+len(literal)]) == literal
}

// scan is scan_once_unicode: one value starting at i.
func (d *decoder) scan(i int) (value any, next int, err error) {
	if i >= len(d.data) {
		return nil, 0, d.fail("Expecting value", i)
	}
	switch d.data[i] {
	case '"':
		return d.scanString(i + 1)
	case '{', '[':
		if d.depth >= MaxDepth {
			return nil, 0, fmt.Errorf("jsonx: nesting deeper than %d levels", MaxDepth)
		}
		d.depth++
		if d.data[i] == '{' {
			value, next, err = d.scanObject(i + 1)
		} else {
			value, next, err = d.scanList(i + 1)
		}
		d.depth--
		return value, next, err
	case 'n':
		if d.hasAt(i, "null") {
			return nil, i + 4, nil
		}
	case 't':
		if d.hasAt(i, "true") {
			return true, i + 4, nil
		}
	case 'f':
		if d.hasAt(i, "false") {
			return false, i + 5, nil
		}
	case 'N':
		if d.hasAt(i, "NaN") {
			return Number("NaN"), i + 3, nil
		}
	case 'I':
		if d.hasAt(i, "Infinity") {
			return Number("Infinity"), i + 8, nil
		}
	case '-':
		if d.hasAt(i, "-Infinity") {
			return Number("-Infinity"), i + 9, nil
		}
	}
	return d.scanNumber(i)
}

// scanObject is _parse_object_unicode; i is just after '{'.
func (d *decoder) scanObject(i int) (any, int, error) {
	object := NewObject()
	i = d.skipSpace(i)
	if i < len(d.data) && d.data[i] == '}' {
		return object, i + 1, nil
	}
	for {
		if i >= len(d.data) || d.data[i] != '"' {
			return nil, 0, d.fail("Expecting property name enclosed in double quotes", i)
		}
		key, next, err := d.scanString(i + 1)
		if err != nil {
			return nil, 0, err
		}
		i = d.skipSpace(next)
		if i >= len(d.data) || d.data[i] != ':' {
			return nil, 0, d.fail("Expecting ':' delimiter", i)
		}
		i = d.skipSpace(i + 1)
		value, next, err := d.scan(i)
		if err != nil {
			return nil, 0, err
		}
		object.Set(key, value)
		i = d.skipSpace(next)
		if i < len(d.data) && d.data[i] == '}' {
			return object, i + 1, nil
		}
		if i >= len(d.data) || d.data[i] != ',' {
			return nil, 0, d.fail("Expecting ',' delimiter", i)
		}
		i = d.skipSpace(i + 1)
	}
}

// scanList is _parse_array_unicode; i is just after '['.
func (d *decoder) scanList(i int) (any, int, error) {
	list := []any{}
	i = d.skipSpace(i)
	if i < len(d.data) && d.data[i] == ']' {
		return list, i + 1, nil
	}
	for {
		value, next, err := d.scan(i)
		if err != nil {
			return nil, 0, err
		}
		list = append(list, value)
		i = d.skipSpace(next)
		if i < len(d.data) && d.data[i] == ']' {
			return list, i + 1, nil
		}
		if i >= len(d.data) || d.data[i] != ',' {
			return nil, 0, d.fail("Expecting ',' delimiter", i)
		}
		i = d.skipSpace(i + 1)
	}
}

// scanNumber is _match_number_unicode: the longest prefix that is a number;
// whatever follows is the caller's problem ("01" is 0 followed by 1).
func (d *decoder) scanNumber(start int) (any, int, error) {
	data, i := d.data, start
	if data[i] == '-' {
		i++
		if i >= len(data) {
			return nil, 0, d.fail("Expecting value", start)
		}
	}
	digits := i
	switch {
	case data[i] >= '1' && data[i] <= '9':
		for i < len(data) && isDigit(data[i]) {
			i++
		}
	case data[i] == '0':
		i++
	default:
		return nil, 0, d.fail("Expecting value", start)
	}
	digits = i - digits
	float := false
	if i+1 < len(data) && data[i] == '.' && isDigit(data[i+1]) {
		float = true
		for i += 2; i < len(data) && isDigit(data[i]); i++ {
		}
	}
	if i+1 < len(data) && (data[i] == 'e' || data[i] == 'E') {
		mark := i
		i++
		if i+1 < len(data) && (data[i] == '-' || data[i] == '+') {
			i++
		}
		for i < len(data) && isDigit(data[i]) {
			i++
		}
		if isDigit(data[i-1]) {
			float = true
		} else {
			i = mark
		}
	}
	text := string(data[start:i])
	if !float {
		if digits > maxIntDigits {
			return nil, 0, fmt.Errorf("Exceeds the limit (%d digits) for integer string conversion: "+
				"value has %d digits; use sys.set_int_max_str_digits() to increase the limit", maxIntDigits, digits)
		}
		if text == "-0" {
			text = "0"
		}
		return Number(text), i, nil
	}
	value, _ := strconv.ParseFloat(text, 64) // ±Inf on overflow, as float() does
	return Number(formatFloat(value)), i, nil
}

// scanString is scanstring_unicode in strict mode; i is just after the
// opening quote.
func (d *decoder) scanString(i int) (string, int, error) {
	data, begin := d.data, i-1
	var buffer []byte // nil until the first escape
	for {
		next := i
		for next < len(data) && data[next] != '"' && data[next] != '\\' {
			if data[next] <= 0x1f {
				return "", 0, d.fail("Invalid control character at", next)
			}
			next++
		}
		if next >= len(data) {
			return "", 0, d.fail("Unterminated string starting at", begin)
		}
		if data[next] == '"' {
			if buffer == nil {
				return string(data[i:next]), next + 1, nil
			}
			return string(append(buffer, data[i:next]...)), next + 1, nil
		}
		buffer = append(buffer, data[i:next]...)
		next++ // the character after the backslash
		if next == len(data) {
			return "", 0, d.fail("Unterminated string starting at", begin)
		}
		c := data[next]
		if c != 'u' {
			switch c {
			case '"', '\\', '/':
			case 'b':
				c = '\b'
			case 'f':
				c = '\f'
			case 'n':
				c = '\n'
			case 'r':
				c = '\r'
			case 't':
				c = '\t'
			default:
				return "", 0, d.fail("Invalid \\escape", next-1)
			}
			buffer = append(buffer, c)
			i = next + 1
			continue
		}
		first := next + 1 // the first hex digit
		i = first + 4
		if i >= len(data) {
			return "", 0, d.fail("Invalid \\uXXXX escape", next)
		}
		r, ok := hex4(data[first:i])
		if !ok {
			return "", 0, d.fail("Invalid \\uXXXX escape", next)
		}
		if r >= 0xd800 && r <= 0xdbff && i+6 < len(data) && data[i] == '\\' && data[i+1] == 'u' {
			low, ok := hex4(data[i+2 : i+6])
			if !ok {
				return "", 0, d.fail("Invalid \\uXXXX escape", i+1)
			}
			if low >= 0xdc00 && low <= 0xdfff {
				r = 0x10000 + (r-0xd800)<<10 + (low - 0xdc00)
				i += 6
			}
		}
		if r >= 0xd800 && r <= 0xdfff {
			r = utf8.RuneError // a lone surrogate has no UTF-8 form
		}
		buffer = utf8.AppendRune(buffer, r)
	}
}

func hex4(digits []byte) (rune, bool) {
	var r rune
	for _, c := range digits {
		r <<= 4
		switch {
		case c >= '0' && c <= '9':
			r |= rune(c - '0')
		case c >= 'a' && c <= 'f':
			r |= rune(c-'a') + 10
		case c >= 'A' && c <= 'F':
			r |= rune(c-'A') + 10
		default:
			return 0, false
		}
	}
	return r, true
}
