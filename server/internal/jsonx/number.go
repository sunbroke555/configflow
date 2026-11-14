package jsonx

import (
	"errors"
	"math"
	"math/big"
	"strconv"
	"strings"
)

// Number is a Python int or float, held as the text Python writes for it:
// int.__repr__ ("0", "-12", any number of digits) or float.__repr__ ("1.0",
// "1.5", "1e+16", "1e-05") with JSON's spelling of the non-finite floats
// ("NaN", "Infinity", "-Infinity"). The text decides the Python type: "1" is
// the int 1, "1.0" the float 1.0.
//
// Build numbers with Int, Float, BigInt or ParseNumber. A Number converted
// directly from a string that is not in canonical form (Number("1.50")) is
// still encoded correctly (as 1.5), but compare such values with Equal, not
// with ==.
type Number string

// Integer is any built-in integer type.
type Integer interface {
	~int | ~int8 | ~int16 | ~int32 | ~int64 | ~uint | ~uint8 | ~uint16 | ~uint32 | ~uint64 | ~uintptr
}

// Int returns the Python int i.
func Int[T Integer](i T) Number {
	if i > 0 && int64(i) < 0 { // an unsigned value above MaxInt64
		return Number(strconv.FormatUint(uint64(i), 10))
	}
	return Number(strconv.FormatInt(int64(i), 10))
}

// BigInt returns the Python int i.
func BigInt(i *big.Int) Number {
	return Number(i.String())
}

// Float returns the Python float f; its text is repr(f), with NaN and the
// infinities spelled as json.dumps spells them.
func Float(f float64) Number {
	return Number(formatFloat(f))
}

// ParseNumber converts a JSON number literal (or NaN, Infinity, -Infinity)
// to the Number json.loads would produce for it.
func ParseNumber(text string) (Number, error) {
	canonical, ok := canonicalNumber(text)
	if !ok {
		return "", errors.New("jsonx: invalid number " + strconv.Quote(text))
	}
	return Number(canonical), nil
}

// String returns the canonical text.
func (n Number) String() string {
	if canonical, ok := canonicalNumber(string(n)); ok {
		return canonical
	}
	return string(n)
}

// IsInt reports whether n is a Python int (as opposed to a float).
func (n Number) IsInt() bool {
	return isIntText(string(n))
}

// IsFloat reports whether n is a Python float.
func (n Number) IsFloat() bool {
	return n == "NaN" || n == "Infinity" || n == "-Infinity" || isFloatText(string(n))
}

// Int64 returns the value of an int that fits an int64. ok is false for
// floats (also integral ones like 2.0) and for integers out of range.
func (n Number) Int64() (value int64, ok bool) {
	if !n.IsInt() {
		return 0, false
	}
	value, err := strconv.ParseInt(string(n), 10, 64)
	if err != nil {
		return 0, false
	}
	return value, true
}

// BigInt returns the value of an int of any size; ok is false for floats.
func (n Number) BigInt() (value *big.Int, ok bool) {
	if !n.IsInt() {
		return nil, false
	}
	return new(big.Int).SetString(string(n), 10)
}

// Float64 is float(n): the nearest float64 (±Inf for an int beyond the
// float range, where Python raises OverflowError). It returns NaN for text
// that is not a number.
func (n Number) Float64() float64 {
	switch n {
	case "NaN":
		return math.NaN()
	case "Infinity":
		return math.Inf(1)
	case "-Infinity":
		return math.Inf(-1)
	}
	if !isIntText(string(n)) && !isFloatText(string(n)) {
		return math.NaN()
	}
	value, _ := strconv.ParseFloat(string(n), 64) // ±Inf on overflow, as float() does
	return value
}

// formatFloat is float.__repr__ with JSON's names for the non-finite values:
// the shortest digits that round-trip, exponent form when the decimal
// exponent is below -4 or at least 16, and always a ".0" on integral values
// in fixed form.
func formatFloat(f float64) string {
	switch {
	case math.IsNaN(f):
		return "NaN"
	case math.IsInf(f, 1):
		return "Infinity"
	case math.IsInf(f, -1):
		return "-Infinity"
	}
	text := strconv.FormatFloat(f, 'e', -1, 64) // d[.ddd]e±dd, at least two exponent digits
	exponent, _ := strconv.Atoi(text[strings.IndexByte(text, 'e')+1:])
	if exponent < -4 || exponent >= 16 {
		return text
	}
	text = strconv.FormatFloat(f, 'f', -1, 64)
	if !strings.Contains(text, ".") {
		text += ".0"
	}
	return text
}

func isDigit(c byte) bool { return c >= '0' && c <= '9' }

// isIntText reports whether text matches -?(0|[1-9][0-9]*).
func isIntText(text string) bool {
	if strings.HasPrefix(text, "-") {
		text = text[1:]
	}
	if text == "" || (text[0] == '0' && len(text) > 1) {
		return false
	}
	for i := 0; i < len(text); i++ {
		if !isDigit(text[i]) {
			return false
		}
	}
	return true
}

// isFloatText reports whether text is a JSON number with a fraction or an
// exponent: -?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]+)?, not an integer.
func isFloatText(text string) bool {
	i := 0
	if i < len(text) && text[i] == '-' {
		i++
	}
	start := i
	for i < len(text) && isDigit(text[i]) {
		i++
	}
	if i == start || (text[start] == '0' && i-start > 1) {
		return false
	}
	float := false
	if i < len(text) && text[i] == '.' {
		i++
		start = i
		for i < len(text) && isDigit(text[i]) {
			i++
		}
		if i == start {
			return false
		}
		float = true
	}
	if i < len(text) && (text[i] == 'e' || text[i] == 'E') {
		i++
		if i < len(text) && (text[i] == '-' || text[i] == '+') {
			i++
		}
		start = i
		for i < len(text) && isDigit(text[i]) {
			i++
		}
		if i == start {
			return false
		}
		float = true
	}
	return float && i == len(text)
}

// canonicalNumber returns the text Python writes for the number literal
// text; ok is false when text is not a number.
func canonicalNumber(text string) (canonical string, ok bool) {
	switch {
	case isIntText(text):
		if text == "-0" { // json.loads("-0") is the int 0
			return "0", true
		}
		return text, true
	case text == "NaN" || text == "Infinity" || text == "-Infinity":
		return text, true
	case isFloatText(text):
		value, _ := strconv.ParseFloat(text, 64) // ±Inf on overflow, as float() does
		return formatFloat(value), true
	}
	return "", false
}

// goNumber converts Go's built-in numeric types to a Number.
func goNumber(v any) (Number, bool) {
	switch value := v.(type) {
	case Number:
		return value, true
	case int:
		return Int(value), true
	case int64:
		return Int(value), true
	case float64:
		return Float(value), true
	case int8:
		return Int(value), true
	case int16:
		return Int(value), true
	case int32:
		return Int(value), true
	case uint:
		return Int(value), true
	case uint8:
		return Int(value), true
	case uint16:
		return Int(value), true
	case uint32:
		return Int(value), true
	case uint64:
		return Int(value), true
	case uintptr:
		return Int(value), true
	case float32:
		return Float(float64(value)), true
	}
	return "", false
}

// normalize is applied to values stored through Object.Set.
func normalize(v any) any {
	switch value := v.(type) {
	case nil, string, bool, Number, []any:
		return v
	case *Object:
		if value == nil {
			return nil
		}
		return v
	}
	if number, ok := goNumber(v); ok {
		return number
	}
	return v
}
