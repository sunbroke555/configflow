package jsonx

import (
	"errors"
	"math"
	"math/big"
	"reflect"
	"strings"
	"testing"
)

func mustCompact(t *testing.T, v any) string {
	t.Helper()
	text, err := Compact(v)
	if err != nil {
		t.Fatal(err)
	}
	return string(text)
}

func TestObjectDictSemantics(t *testing.T) {
	o := NewObject()
	o.Set("b", 1)
	o.Set("a", "x")
	o.Set("c", nil)
	o.Set("b", 2) // keeps its position
	if got := mustCompact(t, o); got != `{"b":2,"a":"x","c":null}` {
		t.Fatal(got)
	}
	if !o.Has("c") || o.Has("d") || o.Len() != 3 {
		t.Fatal("Has/Len")
	}
	if value, ok := o.Get("c"); value != nil || !ok {
		t.Fatal("Get of a stored nil")
	}
	if value, ok := o.Get("d"); value != nil || ok {
		t.Fatal("Get of a missing key")
	}
	if o.GetOr("c", "fallback") != nil || o.GetOr("d", "fallback") != "fallback" {
		t.Fatal("GetOr")
	}
	if value, ok := o.Pop("b"); value != Number("2") || !ok {
		t.Fatal("Pop")
	}
	if _, ok := o.Pop("b"); ok || o.Delete("b") || !o.Delete("c") {
		t.Fatal("Pop/Delete of a missing key")
	}
	o.Set("b", 3) // deleted and set again: appended
	if got := mustCompact(t, o); got != `{"a":"x","b":3}` {
		t.Fatal(got)
	}
	if got := o.SetDefault("a", "y"); got != "x" {
		t.Fatal(got)
	}
	if got := o.SetDefault("n", 7); got != Number("7") {
		t.Fatal(got)
	}
	if !reflect.DeepEqual(o.Keys(), []string{"a", "b", "n"}) || !reflect.DeepEqual(o.Values(), []any{"x", Number("3"), Number("7")}) {
		t.Fatal(o.Keys(), o.Values())
	}
	var keys []string
	for key, value := range o.All() {
		keys = append(keys, key+"="+Str(value))
	}
	if strings.Join(keys, " ") != "a=x b=3 n=7" {
		t.Fatal(keys)
	}

	clone := o.Clone()
	clone.Set("a", "changed")
	clone.Update(Obj("z", true, "b", 4))
	if mustCompact(t, o) != `{"a":"x","b":3,"n":7}` || mustCompact(t, clone) != `{"a":"changed","b":4,"n":7,"z":true}` {
		t.Fatal(mustCompact(t, o), mustCompact(t, clone))
	}
}

func TestObjectManyDeletesKeepOrder(t *testing.T) {
	o := NewObject()
	for i := 0; i < 1000; i++ {
		o.Set(string(Int(i)), i)
	}
	seen := 0
	for key := range o.All() { // deleting while iterating is allowed
		if n, _ := Number(key).Int64(); n%3 != 0 {
			o.Delete(key)
		}
		seen++
	}
	if seen != 1000 || o.Len() != 334 {
		t.Fatal(seen, o.Len())
	}
	o.Set("1", "again")
	keys := o.Keys()
	if keys[0] != "0" || keys[1] != "3" || keys[333] != "999" || keys[334] != "1" || o.Value("999") != Number("999") {
		t.Fatal(keys[:3], keys[332:])
	}
}

func TestNilObjectReads(t *testing.T) {
	var o *Object
	if o.Len() != 0 || o.Has("a") || o.Value("a") != nil || o.Keys() != nil || o.GetObject("a").GetString("b") != "" ||
		o.GetList("a") != nil || o.GetInt("a") != 0 || o.GetBool("a") || o.Clone().Len() != 0 || o.DeepCopy() != nil {
		t.Fatal("nil object")
	}
	for range o.All() {
		t.Fatal("nil object iterates")
	}
	if _, ok := o.Pop("a"); ok {
		t.Fatal("Pop on nil")
	}
	// A nil *Object stored in a document is None.
	if got := mustCompact(t, Obj("a", o, "b", []any{o})); got != `{"a":null,"b":[null]}` {
		t.Fatal(got)
	}
	if _, ok := AsObject(o); ok || !Equal(o, nil) || !Equal(nil, o) || Truthy(o) {
		t.Fatal("nil object is None")
	}
}

func TestTypedGetters(t *testing.T) {
	v, err := ParseString(`{"s":"x","i":42,"f":2.0,"big":18446744073709551616,"t":true,"n":null,"o":{"k":[1,"a",2.5]},"l":[]}`)
	if err != nil {
		t.Fatal(err)
	}
	o, _ := AsObject(v)
	if o.GetString("s") != "x" || o.GetString("i") != "" || o.GetString("missing") != "" {
		t.Fatal("GetString")
	}
	if o.GetInt("i") != 42 || o.GetInt("f") != 0 || o.GetInt("big") != 0 || o.GetInt("t") != 0 || o.GetInt("s") != 0 {
		t.Fatal("GetInt")
	}
	if o.GetFloat("i") != 42 || o.GetFloat("f") != 2 || o.GetFloat("big") != 18446744073709551616 || o.GetFloat("t") != 0 {
		t.Fatal("GetFloat")
	}
	if !o.GetBool("t") || o.GetBool("i") || o.GetBool("n") {
		t.Fatal("GetBool")
	}
	if n, ok := o.GetNumber("big"); !ok || !n.IsInt() || n.IsFloat() || n != "18446744073709551616" {
		t.Fatal("GetNumber")
	}
	if n, ok := o.GetNumber("f"); !ok || n.IsInt() || !n.IsFloat() || n != "2.0" {
		t.Fatal("GetNumber float")
	}
	if _, ok := o.GetNumber("t"); ok {
		t.Fatal("a bool is not a number")
	}
	if o.GetObject("o").GetList("k")[1] != "a" || o.GetObject("s") != nil || o.GetList("l") == nil || o.GetList("o") != nil {
		t.Fatal("GetObject/GetList")
	}
	if Path(v, "o", "k").([]any)[0] != Number("1") || Path(v, "o", "x", "y") != nil || Path(v, "s", "x") != nil || Path(v) != v {
		t.Fatal("Path")
	}
	if !reflect.DeepEqual(Strings(o.GetObject("o").GetList("k")), []string{"a"}) {
		t.Fatal("Strings")
	}
	if got := mustCompact(t, ListOf([]string{"a", "b"})) + mustCompact(t, ListOf([]int{1, 2})); got != `["a","b"][1,2]` {
		t.Fatal(got)
	}
}

func TestNumberConstructors(t *testing.T) {
	tenth := 0.1 // a variable: constant arithmetic would be exact
	cases := []struct {
		got  Number
		want string
	}{
		{Int(0), "0"}, {Int(-5), "-5"}, {Int(int8(-128)), "-128"}, {Int(uint64(math.MaxUint64)), "18446744073709551615"},
		{Int(int64(math.MinInt64)), "-9223372036854775808"}, {Int(uint8(200)), "200"},
		{Float(1), "1.0"}, {Float(1.5), "1.5"}, {Float(1e16), "1e+16"}, {Float(1e15), "1000000000000000.0"},
		{Float(1e-5), "1e-05"}, {Float(1e-4), "0.0001"}, {Float(math.Copysign(0, -1)), "-0.0"}, {Float(tenth + 2*tenth), "0.30000000000000004"},
		{Float(math.NaN()), "NaN"}, {Float(math.Inf(1)), "Infinity"}, {Float(math.Inf(-1)), "-Infinity"},
		{BigInt(new(big.Int).Lsh(big.NewInt(1), 100)), "1267650600228229401496703205376"},
	}
	for _, c := range cases {
		if string(c.got) != c.want {
			t.Errorf("got %s, want %s", c.got, c.want)
		}
	}
	for text, want := range map[string]string{"1.50": "1.5", "1E2": "100.0", "-0": "0", "-0.0": "-0.0", "1e999": "Infinity",
		"12": "12", "NaN": "NaN", "0.1e-6": "1e-07"} {
		if got, err := ParseNumber(text); err != nil || string(got) != want {
			t.Errorf("ParseNumber(%s) = %s, %v; want %s", text, got, err, want)
		}
		// A Number built directly from non-canonical text is still written canonically.
		if got := mustCompact(t, Number(text)); got != want {
			t.Errorf("Compact(Number(%q)) = %s, want %s", text, got, want)
		}
	}
	for _, text := range []string{"", "01", "1.", ".5", "1e", "+1", "abc", "inf", "0x1", " 1", "1 "} {
		if _, err := ParseNumber(text); err == nil {
			t.Errorf("ParseNumber(%q) succeeded", text)
		}
		if _, err := Compact(Number(text)); err == nil {
			t.Errorf("Compact(Number(%q)) succeeded", text)
		}
	}
	if n, ok := Number("9223372036854775808").Int64(); ok || n != 0 {
		t.Error("Int64 out of range")
	}
	if n, ok := Number("-7").Int64(); !ok || n != -7 {
		t.Error("Int64")
	}
	if _, ok := Number("7.0").Int64(); ok {
		t.Error("7.0 is not an int")
	}
	if b, ok := Number("123456789012345678901234567890").BigInt(); !ok || b.String() != "123456789012345678901234567890" {
		t.Error("BigInt")
	}
	if !math.IsInf(Number("1"+strings.Repeat("0", 400)).Float64(), 1) || !math.IsNaN(Number("x").Float64()) || Number("2").Float64() != 2 {
		t.Error("Float64")
	}
}

// Go's own numeric types are accepted wherever a Number is.
func TestGoNumbersAreAccepted(t *testing.T) {
	list := []any{1, int64(-2), uint8(3), 1.5, float32(0.5), 2.0, true}
	if got := mustCompact(t, list); got != `[1,-2,3,1.5,0.5,2.0,true]` {
		t.Fatal(got)
	}
	parsed, _ := ParseString(`[1,-2,3,1.5,0.5,2,1]`)
	if !Equal(list, parsed) || !Equal(parsed, list) {
		t.Fatal("Equal with Go numbers")
	}
	if Truthy(0) || Truthy(0.0) || !Truthy(1) || !Truthy(math.NaN()) || Truthy(Number("0.0")) || Truthy(Number("-0.0")) || !Truthy(Number("1e-300")) {
		t.Fatal("Truthy")
	}
	if n, ok := AsInt(7); !ok || n != 7 {
		t.Fatal("AsInt")
	}
	if _, ok := AsInt(true); ok {
		t.Fatal("a bool is not an int")
	}
	if Repr([]any{1, 2.0, math.Inf(-1), math.NaN()}) != "[1, 2.0, -inf, nan]" {
		t.Fatal(Repr([]any{1, 2.0, math.Inf(-1), math.NaN()}))
	}
}

func TestUnsupportedTypes(t *testing.T) {
	for _, v := range []any{map[string]any{"a": 1}, []string{"a"}, struct{}{}, []any{Obj("k", []int{1})}, new(int)} {
		_, err := Compact(v)
		var unsupported *UnsupportedTypeError
		if !errors.As(err, &unsupported) || !strings.HasSuffix(err.Error(), "is not JSON serializable") {
			t.Errorf("Compact(%T): %v", v, err)
		}
	}
}

func TestInvalidUTF8(t *testing.T) {
	if _, err := Parse([]byte("\"a\xffb\"")); err == nil {
		t.Fatal("Parse accepted invalid UTF-8")
	}
	bad := string(rune(0xfffd)) // U+FFFD, one per invalid byte
	if got := mustCompact(t, "a\xff\xfeb"); got != `"a`+bad+bad+`b"` {
		t.Fatalf("%q", got)
	}
	escaped := `\u` + "fffd"
	if got, _ := Default(Obj("k\xc3", "\xe4\xbe")); string(got) != `{"k`+escaped+`": "`+escaped+escaped+`"}` {
		t.Fatalf("%q", got)
	}
}

func TestEmptyAndNilContainers(t *testing.T) {
	var list []any
	if got := mustCompact(t, Obj("a", list, "b", []any{}, "c", NewObject())); got != `{"a":[],"b":[],"c":{}}` {
		t.Fatal(got)
	}
	if got, _ := Indent2(Obj("a", list, "c", NewObject(), "d", []any{NewObject()})); string(got) != "{\n  \"a\": [],\n  \"c\": {},\n  \"d\": [\n    {}\n  ]\n}" {
		t.Fatalf("%q", got)
	}
}

func TestDeepCopyIsIndependent(t *testing.T) {
	original, _ := ParseString(`{"a":{"b":[1,{"c":2}]},"l":[[1]]}`)
	copied := DeepCopy(original).(*Object)
	copied.GetObject("a").GetList("b")[1].(*Object).Set("c", 3)
	copied.GetList("l")[0].([]any)[0] = "x"
	copied.Set("new", true)
	if got := mustCompact(t, original); got != `{"a":{"b":[1,{"c":2}]},"l":[[1]]}` {
		t.Fatal(got)
	}
	if got := mustCompact(t, copied); got != `{"a":{"b":[1,{"c":3}]},"l":[["x"]],"new":true}` {
		t.Fatal(got)
	}
}

func TestParseDepthLimit(t *testing.T) {
	if _, err := ParseString(strings.Repeat("[", MaxDepth) + strings.Repeat("]", MaxDepth)); err != nil {
		t.Fatal(err)
	}
	if _, err := ParseString(strings.Repeat("[", MaxDepth+1) + strings.Repeat("]", MaxDepth+1)); err == nil {
		t.Fatal("no depth error")
	}
}

func TestSyntaxErrorFields(t *testing.T) {
	_, err := ParseString("{\n \"例\": tru}")
	var syntax *SyntaxError
	if !errors.As(err, &syntax) || syntax.Msg != "Expecting value" || syntax.Line != 2 || syntax.Column != 7 || syntax.Offset != 8 {
		t.Fatalf("%#v", err)
	}
}
