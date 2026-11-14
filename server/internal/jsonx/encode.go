package jsonx

import (
	"fmt"
	"sort"
	"unicode/utf8"
)

// Options are the json.dumps arguments the application uses. The zero value
// is json.dumps(v, ensure_ascii=False): note that Python's own default is
// ensure_ascii=True (see Default).
type Options struct {
	// EnsureASCII writes every non-ASCII character (and DEL) as \uXXXX,
	// with a surrogate pair above U+FFFF.
	EnsureASCII bool
	// SortKeys writes object keys sorted by code point.
	SortKeys bool
	// Compact is separators=(',', ':'); otherwise ', ' and ': ' (',' and
	// ': ' when Indent is set).
	Compact bool
	// Indent > 0 is indent=N: one item per line. 0 is indent=None.
	Indent int
}

// UnsupportedTypeError is the TypeError json.dumps raises for a value that
// is not JSON: here, anything outside the jsonx value model.
type UnsupportedTypeError struct {
	Value any
}

func (e *UnsupportedTypeError) Error() string {
	return "Object of type " + e.typeName() + " is not JSON serializable"
}

func (e *UnsupportedTypeError) typeName() string {
	return fmt.Sprintf("%T", e.Value)
}

// Encode is json.dumps(v, ...) with the given options.
func Encode(v any, options Options) ([]byte, error) {
	e := encoder{Options: options, itemSep: ", ", keySep: ": "}
	if options.Compact {
		e.itemSep, e.keySep = ",", ":"
	} else if options.Indent > 0 {
		e.itemSep = ","
	}
	if err := e.value(v, 0); err != nil {
		return nil, err
	}
	return e.out, nil
}

// Compact is json.dumps(v, ensure_ascii=False, separators=(',', ':')): the
// form of every value stored in configflow.sqlite3.
func Compact(v any) ([]byte, error) {
	return Encode(v, Options{Compact: true})
}

// Indent2 is json.dumps(v, indent=2, ensure_ascii=False): the form of the
// JSON files under DATA_DIR. Python callers usually append "\n"; this
// function does not.
func Indent2(v any) ([]byte, error) {
	return Encode(v, Options{Indent: 2})
}

// Sorted is json.dumps(v, sort_keys=True, ensure_ascii=False,
// separators=(',', ':')): the input of content digests.
func Sorted(v any) ([]byte, error) {
	return Encode(v, Options{SortKeys: true, Compact: true})
}

// Default is json.dumps(v) with no arguments: ensure_ascii=True and the
// ', ' and ': ' separators.
func Default(v any) ([]byte, error) {
	return Encode(v, Options{EnsureASCII: true})
}

// Response is the body of flask.jsonify(v) in Flask 3.0 outside debug mode:
// sorted keys, ASCII only, compact separators and a trailing newline.
func Response(v any) ([]byte, error) {
	body, err := Encode(v, Options{EnsureASCII: true, SortKeys: true, Compact: true})
	if err != nil {
		return nil, err
	}
	return append(body, '\n'), nil
}

type encoder struct {
	Options
	itemSep, keySep string
	out             []byte
}

func (e *encoder) newline(level int) {
	e.out = append(e.out, '\n')
	for n := level * e.Indent; n > 0; n-- {
		e.out = append(e.out, ' ')
	}
}

func (e *encoder) value(v any, level int) error {
	switch value := v.(type) {
	case nil:
		e.out = append(e.out, "null"...)
	case bool:
		if value {
			e.out = append(e.out, "true"...)
		} else {
			e.out = append(e.out, "false"...)
		}
	case string:
		e.out = appendString(e.out, value, e.EnsureASCII)
	case Number:
		canonical, ok := canonicalNumber(string(value))
		if !ok {
			return fmt.Errorf("jsonx: invalid Number %q", string(value))
		}
		e.out = append(e.out, canonical...)
	case []any:
		return e.list(value, level)
	case *Object:
		if value == nil {
			e.out = append(e.out, "null"...)
			return nil
		}
		return e.object(value, level)
	default:
		number, ok := goNumber(v)
		if !ok {
			return &UnsupportedTypeError{Value: v}
		}
		e.out = append(e.out, number...)
	}
	return nil
}

func (e *encoder) list(list []any, level int) error {
	if len(list) == 0 {
		e.out = append(e.out, "[]"...)
		return nil
	}
	e.out = append(e.out, '[')
	for i, item := range list {
		if i > 0 {
			e.out = append(e.out, e.itemSep...)
		}
		if e.Indent > 0 {
			e.newline(level + 1)
		}
		if err := e.value(item, level+1); err != nil {
			return err
		}
	}
	if e.Indent > 0 {
		e.newline(level)
	}
	e.out = append(e.out, ']')
	return nil
}

func (e *encoder) object(object *Object, level int) error {
	if object.Len() == 0 {
		e.out = append(e.out, "{}"...)
		return nil
	}
	keys := object.Keys()
	if e.SortKeys {
		// Byte order of UTF-8 is code point order, which is how Python
		// compares str.
		sort.Strings(keys)
	}
	e.out = append(e.out, '{')
	for i, key := range keys {
		if i > 0 {
			e.out = append(e.out, e.itemSep...)
		}
		if e.Indent > 0 {
			e.newline(level + 1)
		}
		e.out = appendString(e.out, key, e.EnsureASCII)
		e.out = append(e.out, e.keySep...)
		if err := e.value(object.Value(key), level+1); err != nil {
			return err
		}
	}
	if e.Indent > 0 {
		e.newline(level)
	}
	e.out = append(e.out, '}')
	return nil
}

const hexDigits = "0123456789abcdef"

func appendU(out []byte, r rune) []byte {
	return append(out, '\\', 'u', hexDigits[r>>12&0xf], hexDigits[r>>8&0xf], hexDigits[r>>4&0xf], hexDigits[r&0xf])
}

// appendString is CPython's encode_basestring (ascii false) or
// encode_basestring_ascii (ascii true). Bytes that are not valid UTF-8 are
// written as U+FFFD.
func appendString(out []byte, text string, ascii bool) []byte {
	out = append(out, '"')
	start := 0 // text[start:i] is pending and needs no escaping
	for i := 0; i < len(text); {
		c := text[i]
		if c < utf8.RuneSelf {
			if c >= ' ' && c != '"' && c != '\\' && (c != 0x7f || !ascii) {
				i++
				continue
			}
			out = append(out, text[start:i]...)
			switch c {
			case '"', '\\':
				out = append(out, '\\', c)
			case '\n':
				out = append(out, '\\', 'n')
			case '\r':
				out = append(out, '\\', 'r')
			case '\t':
				out = append(out, '\\', 't')
			case '\b':
				out = append(out, '\\', 'b')
			case '\f':
				out = append(out, '\\', 'f')
			default:
				out = appendU(out, rune(c))
			}
			i++
			start = i
			continue
		}
		r, size := utf8.DecodeRuneInString(text[i:])
		invalid := r == utf8.RuneError && size == 1
		if !ascii && !invalid {
			i += size
			continue
		}
		out = append(out, text[start:i]...)
		switch {
		case !ascii:
			out = utf8.AppendRune(out, utf8.RuneError)
		case r >= 0x10000:
			r -= 0x10000
			out = appendU(appendU(out, 0xd800+(r>>10)), 0xdc00+(r&0x3ff))
		default:
			out = appendU(out, r)
		}
		i += size
		start = i
	}
	out = append(out, text[start:]...)
	return append(out, '"')
}
