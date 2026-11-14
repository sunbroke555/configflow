package jsonx

import "iter"

// Object is a Python dict with string keys: a map that remembers insertion
// order. The zero value is not usable; create one with NewObject or Obj.
//
// All read methods are safe on a nil *Object and treat it as empty, so
// lookups can be chained (`o.GetObject("a").GetString("b")`). Methods that
// modify the object panic on nil. An Object is not safe for concurrent use
// when any goroutine modifies it.
type Object struct {
	entries []entry        // insertion order; deleted entries stay as tombstones
	index   map[string]int // key -> position in entries
}

type entry struct {
	key   string
	value any
	live  bool
}

// NewObject returns an empty object.
func NewObject() *Object {
	return &Object{index: map[string]int{}}
}

// Obj builds an object from alternating keys and values, in the order given,
// like a Python dict literal: Obj("id", "a", "port", 80). It panics when a
// key is not a string or a value is missing, which is a programming error.
func Obj(pairs ...any) *Object {
	if len(pairs)%2 != 0 {
		panic("jsonx.Obj: odd number of arguments")
	}
	o := &Object{entries: make([]entry, 0, len(pairs)/2), index: make(map[string]int, len(pairs)/2)}
	for i := 0; i < len(pairs); i += 2 {
		key, ok := pairs[i].(string)
		if !ok {
			panic("jsonx.Obj: key is not a string")
		}
		o.Set(key, pairs[i+1])
	}
	return o
}

// Len is len(d).
func (o *Object) Len() int {
	if o == nil {
		return 0
	}
	return len(o.index)
}

// Has is `key in d`.
func (o *Object) Has(key string) bool {
	if o == nil {
		return false
	}
	_, ok := o.index[key]
	return ok
}

// Get returns the value stored under key and whether the key exists.
func (o *Object) Get(key string) (any, bool) {
	if o == nil {
		return nil, false
	}
	i, ok := o.index[key]
	if !ok {
		return nil, false
	}
	return o.entries[i].value, true
}

// Value is d.get(key): the value, or nil when the key is missing.
func (o *Object) Value(key string) any {
	value, _ := o.Get(key)
	return value
}

// GetOr is d.get(key, fallback): fallback only replaces a missing key, not
// a stored nil.
func (o *Object) GetOr(key string, fallback any) any {
	if value, ok := o.Get(key); ok {
		return value
	}
	return fallback
}

// Set is d[key] = value. An existing key keeps its position, a new key is
// appended. Go integers and floats are stored as Number, a nil *Object as
// nil.
func (o *Object) Set(key string, value any) {
	value = normalize(value)
	if i, ok := o.index[key]; ok {
		o.entries[i].value = value
		return
	}
	o.index[key] = len(o.entries)
	o.entries = append(o.entries, entry{key: key, value: value, live: true})
}

// SetDefault is d.setdefault(key, value): it stores value only when the key
// is missing and returns what is stored afterwards.
func (o *Object) SetDefault(key string, value any) any {
	if current, ok := o.Get(key); ok {
		return current
	}
	o.Set(key, value)
	return o.Value(key)
}

// Delete removes key and reports whether it was present (`del d[key]`
// without the KeyError). Setting the key again later appends it at the end.
func (o *Object) Delete(key string) bool {
	_, ok := o.Pop(key)
	return ok
}

// Pop is d.pop(key, None) plus whether the key was present.
func (o *Object) Pop(key string) (any, bool) {
	if o == nil {
		return nil, false
	}
	i, ok := o.index[key]
	if !ok {
		return nil, false
	}
	value := o.entries[i].value
	o.entries[i] = entry{}
	delete(o.index, key)
	if dead := len(o.entries) - len(o.index); dead > 8 && dead > len(o.index) {
		o.compact()
	}
	return value, true
}

// compact drops the tombstones. It builds a new slice, so an iteration that
// started earlier keeps walking its own (now frozen) view.
func (o *Object) compact() {
	entries := make([]entry, 0, len(o.index))
	for _, item := range o.entries {
		if item.live {
			o.index[item.key] = len(entries)
			entries = append(entries, item)
		}
	}
	o.entries = entries
}

// Keys is list(d): a new slice of the keys in insertion order.
func (o *Object) Keys() []string {
	if o == nil {
		return nil
	}
	keys := make([]string, 0, len(o.index))
	for _, item := range o.entries {
		if item.live {
			keys = append(keys, item.key)
		}
	}
	return keys
}

// Values is list(d.values()): a new slice of the values in insertion order.
func (o *Object) Values() []any {
	if o == nil {
		return nil
	}
	values := make([]any, 0, len(o.index))
	for _, item := range o.entries {
		if item.live {
			values = append(values, item.value)
		}
	}
	return values
}

// All iterates over d.items() in insertion order:
//
//	for key, value := range o.All() { ... }
//
// Replacing the value of an existing key or deleting keys while iterating is
// allowed (deleted keys are skipped). Keys added while iterating may or may
// not be visited; collect Keys() first when the loop adds keys.
func (o *Object) All() iter.Seq2[string, any] {
	return func(yield func(string, any) bool) {
		if o == nil {
			return
		}
		entries := o.entries
		for i := 0; i < len(entries); i++ {
			if item := entries[i]; item.live && !yield(item.key, item.value) {
				return
			}
		}
	}
}

// Clone is dict(d): a new object with the same keys and the same (shared)
// values. Use DeepCopy for copy.deepcopy.
func (o *Object) Clone() *Object {
	clone := &Object{entries: make([]entry, 0, o.Len()), index: make(map[string]int, o.Len())}
	clone.Update(o)
	return clone
}

// Update is d.update(other): every key of other is set in order; existing
// keys keep their position. `{**a, **b}` is `c := a.Clone(); c.Update(b)`.
func (o *Object) Update(other *Object) {
	for key, value := range other.All() {
		o.Set(key, value)
	}
}

// GetString returns the string under key, or "" when the key is missing or
// holds something else.
func (o *Object) GetString(key string) string {
	value, _ := o.Value(key).(string)
	return value
}

// GetBool returns the bool under key, or false when the key is missing or
// holds something else (it does not apply Python truthiness; see Truthy).
func (o *Object) GetBool(key string) bool {
	value, _ := o.Value(key).(bool)
	return value
}

// GetObject returns the object under key, or nil when the key is missing or
// holds something else. The nil result can still be read from.
func (o *Object) GetObject(key string) *Object {
	value, _ := o.Value(key).(*Object)
	return value
}

// GetList returns the list under key, or nil when the key is missing or
// holds something else.
func (o *Object) GetList(key string) []any {
	value, _ := o.Value(key).([]any)
	return value
}

// GetNumber returns the number under key; ok is false when the key is
// missing or holds something else (including a bool).
func (o *Object) GetNumber(key string) (Number, bool) {
	return AsNumber(o.Value(key))
}

// GetInt returns the integer under key, or 0 when the key is missing, is not
// an integer (floats and bools are not) or does not fit an int64. See AsInt.
func (o *Object) GetInt(key string) int64 {
	value, _ := AsInt(o.Value(key))
	return value
}

// GetFloat returns the number under key as a float64 (integers included), or
// 0 when the key is missing or is not a number. See AsFloat.
func (o *Object) GetFloat(key string) float64 {
	value, _ := AsFloat(o.Value(key))
	return value
}
