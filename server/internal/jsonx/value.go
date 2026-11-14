package jsonx

import (
	"math"
	"math/big"
)

// AsObject is isinstance(v, dict). A nil *Object is not an object.
func AsObject(v any) (*Object, bool) {
	object, ok := v.(*Object)
	return object, ok && object != nil
}

// AsList is isinstance(v, list).
func AsList(v any) ([]any, bool) {
	list, ok := v.([]any)
	return list, ok
}

// AsString is isinstance(v, str).
func AsString(v any) (string, bool) {
	text, ok := v.(string)
	return text, ok
}

// AsBool is isinstance(v, bool).
func AsBool(v any) (bool, bool) {
	value, ok := v.(bool)
	return value, ok
}

// AsNumber is `isinstance(v, (int, float)) and not isinstance(v, bool)`.
func AsNumber(v any) (Number, bool) {
	return goNumber(v)
}

// AsInt is `isinstance(v, int) and not isinstance(v, bool)`, limited to the
// int64 range: ok is false for floats (2.0 is not an int), bools and
// integers that do not fit.
func AsInt(v any) (int64, bool) {
	number, ok := goNumber(v)
	if !ok {
		return 0, false
	}
	return number.Int64()
}

// AsFloat returns any number as a float64, like float(v) for an int or a
// float; ok is false for everything else, bools included.
func AsFloat(v any) (float64, bool) {
	number, ok := goNumber(v)
	if !ok {
		return 0, false
	}
	return number.Float64(), true
}

// ListOf converts a typed slice to a jsonx list: ListOf([]string{"a"}).
// Integer and float elements become Numbers.
func ListOf[T any](items []T) []any {
	list := make([]any, len(items))
	for i, item := range items {
		list[i] = normalize(item)
	}
	return list
}

// Strings returns the string elements of a list, skipping everything else:
// [x for x in v if isinstance(x, str)].
func Strings(list []any) []string {
	result := make([]string, 0, len(list))
	for _, item := range list {
		if text, ok := item.(string); ok {
			result = append(result, text)
		}
	}
	return result
}

// Path follows keys through nested objects: Path(v, "a", "b") is
// v["a"]["b"]. It returns nil as soon as a step is not an object or the key
// is missing.
func Path(v any, keys ...string) any {
	for _, key := range keys {
		object, ok := AsObject(v)
		if !ok {
			return nil
		}
		v = object.Value(key)
	}
	return v
}

// Truthy is bool(v): false for nil, false, zero numbers (0, 0.0, -0.0), ""
// and empty lists and objects. NaN is true. Values that are not jsonx
// values are true.
func Truthy(v any) bool {
	switch value := v.(type) {
	case nil:
		return false
	case bool:
		return value
	case string:
		return value != ""
	case []any:
		return len(value) > 0
	case *Object:
		return value.Len() > 0
	}
	if number, ok := goNumber(v); ok {
		if number.IsInt() {
			return number != "0" && number != "-0"
		}
		return number.Float64() != 0
	}
	return true
}

// Equal is Python's a == b on jsonx values: numbers compare by value across
// int and float (1 == 1.0, exactly, without rounding big integers), bools
// equal 1 and 0, lists compare element by element, objects compare by keys
// and values regardless of key order.
//
// NaN is not equal to NaN, except as an element of a list or a value of an
// object: Python compares container items by identity first, and every NaN
// that json.loads returns is the same object, so [NaN] == [NaN] there.
func Equal(a, b any) bool {
	switch left := a.(type) {
	case nil:
		if right, ok := b.(*Object); ok {
			return right == nil
		}
		return b == nil
	case string:
		right, ok := b.(string)
		return ok && left == right
	case []any:
		right, ok := b.([]any)
		if !ok || len(left) != len(right) {
			return false
		}
		for i := range left {
			if !itemsEqual(left[i], right[i]) {
				return false
			}
		}
		return true
	case *Object:
		if left == nil {
			return Equal(nil, b)
		}
		right, ok := b.(*Object)
		if !ok || right == nil || left.Len() != right.Len() {
			return false
		}
		for key, value := range left.All() {
			other, ok := right.Get(key)
			if !ok || !itemsEqual(value, other) {
				return false
			}
		}
		return true
	}
	left, ok := numeric(a)
	if !ok {
		return false
	}
	right, ok := numeric(b)
	return ok && numbersEqual(left, right)
}

// itemsEqual is PyObject_RichCompare(a, b, Py_EQ) with the identity
// short-cut containers apply; it only matters for NaN.
func itemsEqual(a, b any) bool {
	if Equal(a, b) {
		return true
	}
	left, ok := goNumber(a)
	if !ok || left.String() != "NaN" {
		return false
	}
	right, ok := goNumber(b)
	return ok && right.String() == "NaN"
}

// numeric returns numbers and bools (True is 1, False is 0) as a Number.
func numeric(v any) (Number, bool) {
	if value, ok := v.(bool); ok {
		if value {
			return "1", true
		}
		return "0", true
	}
	return goNumber(v)
}

func numbersEqual(a, b Number) bool {
	switch aInt, bInt := a.IsInt(), b.IsInt(); {
	case aInt && bInt:
		return a.String() == b.String()
	case !aInt && !bInt:
		return a.Float64() == b.Float64()
	case aInt:
		return intEqualsFloat(a, b.Float64())
	default:
		return intEqualsFloat(b, a.Float64())
	}
}

// intEqualsFloat compares exactly, as Python does: 2**53 + 1 != 2.0**53.
func intEqualsFloat(n Number, f float64) bool {
	if math.IsNaN(f) || math.IsInf(f, 0) || f != math.Trunc(f) {
		return false
	}
	integer, ok := n.BigInt()
	if !ok {
		return false
	}
	exact, _ := new(big.Float).SetFloat64(f).Int(nil)
	return integer.Cmp(exact) == 0
}

// DeepCopy is copy.deepcopy(v): objects and lists are copied recursively,
// scalars are immutable and returned as they are.
func DeepCopy(v any) any {
	switch value := v.(type) {
	case *Object:
		if value == nil {
			return nil
		}
		return value.DeepCopy()
	case []any:
		if value == nil {
			return value
		}
		list := make([]any, len(value))
		for i, item := range value {
			list[i] = DeepCopy(item)
		}
		return list
	}
	return v
}

// DeepCopy is copy.deepcopy(d) with the object type kept.
func (o *Object) DeepCopy() *Object {
	if o == nil {
		return nil
	}
	clone := &Object{entries: make([]entry, 0, len(o.index)), index: make(map[string]int, len(o.index))}
	for key, value := range o.All() {
		clone.index[key] = len(clone.entries)
		clone.entries = append(clone.entries, entry{key: key, value: DeepCopy(value), live: true})
	}
	return clone
}
