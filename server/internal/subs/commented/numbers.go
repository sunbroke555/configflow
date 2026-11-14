package commented

import (
	"fmt"
	"math"
	"math/big"
	"regexp"
	"strconv"
	"strings"

	"configflow/internal/jsonx"
)

// The implicit resolvers of ruamel.yaml 0.19 (resolver.py), per version.
var (
	rBool11  = regexp.MustCompile(`^(?:y|Y|yes|Yes|YES|n|N|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF)$`)
	rBool12  = regexp.MustCompile(`^(?:true|True|TRUE|false|False|FALSE)$`)
	rFloat11 = regexp.MustCompile(`^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+]?[0-9]+)?|[-+]?(?:[0-9][0-9_]*)(?:[eE][-+]?[0-9]+)|\.[0-9_]+(?:[eE][-+][0-9]+)?|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$`)
	rFloat12 = regexp.MustCompile(`^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+]?[0-9]+)?|[-+]?(?:[0-9][0-9_]*)(?:[eE][-+]?[0-9]+)|[-+]?\.[0-9_]+(?:[eE][-+][0-9]+)?|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$`)
	rInt11   = regexp.MustCompile(`^(?:[-+]?0b[0-1_]+|[-+]?0?[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0x[0-9a-fA-F_]+|[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+)$`)
	rInt12   = regexp.MustCompile(`^(?:[-+]?0b[0-1_]+|[-+]?0o?[0-7_]+|[-+]?[0-9_]+|[-+]?0x[0-9a-fA-F_]+)$`)
	rNull    = regexp.MustCompile(`^(?:~|null|Null|NULL|)$`)
	rStamp   = regexp.MustCompile(`^(?:[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]|[0-9][0-9][0-9][0-9]-[0-9][0-9]?-[0-9][0-9]?(?:[Tt]|[ \t]+)[0-9][0-9]?:[0-9][0-9]:[0-9][0-9](?:\.[0-9]*)?(?:[ \t]*(?:Z|[-+][0-9][0-9]?(?::[0-9][0-9])?))?)$`)
)

// resolveTag is ruamel's resolve() for a plain scalar: bool, float, int,
// merge, null, timestamp, value or str (first match, in that order).
func resolveTag(s string, yaml12 bool) string {
	b, f, i := rBool11, rFloat11, rInt11
	if yaml12 {
		b, f, i = rBool12, rFloat12, rInt12
	}
	// resolvers are looked up by the first character
	first := byte(0)
	if s != "" {
		first = s[0]
	}
	in := func(chars string) bool { return s != "" && strings.IndexByte(chars, first) >= 0 }
	switch {
	case in("yYnNtTfFoO") && b.MatchString(s):
		return "bool"
	case in("-+0123456789.") && f.MatchString(s):
		return "float"
	case in("-+0123456789") && i.MatchString(s):
		return "int"
	case s == "<<":
		return "merge"
	case (s == "" || in("~nN")) && rNull.MatchString(s):
		return "null"
	case in("0123456789") && rStamp.MatchString(s):
		return "timestamp"
	case s == "=":
		return "value"
	}
	return "str"
}

// pyZeroPad is Python's f'{v:0{width}<conv>}' for an integer text with an
// optional leading minus.
func pyZeroPad(s string, width int) string {
	sign := ""
	if strings.HasPrefix(s, "-") {
		sign, s = "-", s[1:]
	}
	for len(sign)+len(s) < width {
		s = "0" + s
	}
	return sign + s
}

// insertUnderscore is RoundTripRepresenter.insert_underscore.
func insertUnderscore(prefix, s string, underscore []int) string {
	if underscore == nil {
		return prefix + s
	}
	if underscore[0] != 0 {
		sl := []rune(s)
		for pos := len(sl) - underscore[0]; pos > 0; pos -= underscore[0] {
			sl = append(sl[:pos], append([]rune{'_'}, sl[pos:]...)...)
		}
		s = string(sl)
	}
	if underscore[1] != 0 {
		s = "_" + s
	}
	if underscore[2] != 0 {
		s += "_"
	}
	return prefix + s
}

func b2i(b bool) int {
	if b {
		return 1
	}
	return 0
}

// intOut is RoundTripConstructor.construct_yaml_int followed by the
// representer of the constructed type (OctalInt is written as a plain
// decimal int, as commented_mihomo registers it).
func intOut(su string, yaml12 bool) (string, error) {
	var underscore []int
	if sx := strings.TrimRight(su, "_"); strings.Contains(sx, "_") {
		underscore = []int{len(sx) - strings.LastIndex(sx, "_") - 1, 0, 0}
	}
	s := strings.ReplaceAll(su, "_", "")
	neg := false
	if s != "" && (s[0] == '+' || s[0] == '-') {
		neg = s[0] == '-'
		s = s[1:]
	}
	parse := func(digits string, base int) (*big.Int, error) {
		n, ok := new(big.Int).SetString(digits, base)
		if !ok || digits == "" {
			return nil, unsupported("integer the constructor rejects")
		}
		if neg {
			n.Neg(n)
		}
		return n, nil
	}
	prefixed := func(prefix string, base int, conv func(*big.Int) string) (string, error) {
		if len(s) < 3 {
			return "", unsupported("integer the constructor rejects")
		}
		width := -1
		if yaml12 && s[2] == '0' {
			width = len(s) - 2
		}
		if underscore != nil {
			underscore[1] = b2i(len(su) > 2 && su[2] == '_')
			underscore[2] = b2i(len(su) > 3 && su[len(su)-1] == '_')
		}
		n, err := parse(s[2:], base)
		if err != nil {
			return "", err
		}
		text := conv(n)
		if width >= 0 {
			text = pyZeroPad(text, width)
		}
		return insertUnderscore(prefix, text, underscore), nil
	}
	switch {
	case strings.HasPrefix(s, "0b"):
		return prefixed("0b", 2, func(n *big.Int) string { return n.Text(2) })
	case strings.HasPrefix(s, "0x"):
		caps := false
		for _, c := range s[2:] {
			if strings.ContainsRune("ABCDEF", c) {
				caps = true
				break
			}
			if strings.ContainsRune("abcdef", c) {
				break
			}
		}
		return prefixed("0x", 16, func(n *big.Int) string {
			if caps {
				return strings.ToUpper(n.Text(16))
			}
			return n.Text(16)
		})
	case strings.HasPrefix(s, "0o"):
		// OctalInt: written as a plain decimal int
		n, err := parse(s[2:], 8)
		if err != nil {
			return "", err
		}
		return n.String(), nil
	case !yaml12 && len(s) > 1 && s[0] == '0':
		n, err := parse(s, 8)
		if err != nil {
			return "", err
		}
		return n.String(), nil
	case !yaml12 && strings.Contains(s, ":"):
		value := new(big.Int)
		for _, part := range strings.Split(s, ":") {
			d, ok := new(big.Int).SetString(part, 10)
			if !ok {
				return "", unsupported("sexagesimal part")
			}
			value.Mul(value, big.NewInt(60))
			value.Add(value, d)
		}
		if neg {
			value.Neg(value)
		}
		return value.String(), nil
	case yaml12 && s != "" && s[0] == '0':
		if underscore != nil {
			underscore[2] = b2i(len(su) > 1 && su[len(su)-1] == '_')
		}
		n, err := parse(s, 10)
		if err != nil {
			return "", err
		}
		return insertUnderscore("", pyZeroPad(n.String(), len(s)), underscore), nil
	case underscore != nil:
		underscore[2] = b2i(len(su) > 1 && su[len(su)-1] == '_')
		n, err := parse(s, 10)
		if err != nil {
			return "", err
		}
		return insertUnderscore("", n.String(), underscore), nil
	}
	n, err := parse(s, 10)
	if err != nil {
		return "", err
	}
	return n.String(), nil
}

func leadingZeros(v string) int {
	lead := 0
	for i := 0; i < len(v) && (v[i] == '0' || v[i] == '.'); i++ {
		if v[i] == '0' {
			lead++
		}
	}
	return lead
}

// pyFloatRepr is repr(float).lower().
func pyFloatRepr(f float64) string { return strings.ToLower(jsonx.Float(f).String()) }

func absInt(f float64) string {
	t := math.Trunc(math.Abs(f))
	return strconv.FormatFloat(t, 'f', 0, 64)
}

// floatOut is RoundTripConstructor.construct_yaml_float followed by
// represent_scalar_float (or represent_float for the plain floats).
func floatOut(so string, yaml12 bool) (string, error) {
	s := strings.ToLower(strings.ReplaceAll(so, "_", ""))
	sign := 1.0
	mSign := ""
	if s != "" && s[0] == '-' {
		sign = -1
	}
	if s != "" && (s[0] == '+' || s[0] == '-') {
		mSign = s[:1]
		s = s[1:]
	}
	switch s {
	case ".inf":
		if sign < 0 {
			return "-.inf", nil
		}
		return ".inf", nil
	case ".nan":
		return "", unsupported("NaN never compares equal")
	}
	if !yaml12 && strings.Contains(s, ":") {
		value := 0.0
		for _, part := range strings.Split(s, ":") {
			d, err := strconv.ParseFloat(part, 64)
			if err != nil {
				return "", unsupported("sexagesimal part")
			}
			value = value*60 + d
		}
		return pyFloatRepr(sign * value), nil
	}
	f, err := strconv.ParseFloat(s, 64)
	if err != nil {
		return "", unsupported("float the constructor rejects")
	}
	data := sign * f
	if math.IsInf(data, 0) {
		return "", unsupported("float overflow")
	}
	var width, prec, lead0, eWidth int
	exp := ""
	eSign := false
	if strings.Contains(s, "e") {
		var mantissa, exponent string
		if parts := strings.Split(so, "e"); len(parts) == 2 {
			mantissa, exponent, exp = parts[0], parts[1], "e"
		} else if parts := strings.Split(so, "E"); len(parts) == 2 {
			mantissa, exponent, exp = parts[0], parts[1], "E"
		} else {
			return "", unsupported("float exponent")
		}
		lead0 = leadingZeros(mantissa)
		width = len(mantissa)
		prec = strings.Index(mantissa, ".")
		if mSign != "" {
			width--
		}
		eWidth = len(exponent)
		eSign = exponent != "" && (exponent[0] == '+' || exponent[0] == '-')
	} else {
		width = len(so)
		prec = strings.Index(so, ".")
		lead0 = leadingZeros(so)
	}
	return scalarFloatText(data, width, prec, mSign, lead0, exp, eWidth, eSign)
}

// scalarFloatText is RoundTripRepresenter.represent_scalar_float.
func scalarFloatText(data float64, width, prec int, mSign string, lead0 int, exp string, eWidth int, eSign bool) (string, error) {
	if exp == "" && prec > 0 && prec == width-1 {
		return mSign + absInt(data) + ".", nil
	}
	if exp == "" {
		var value string
		if prec < 0 {
			value = mSign + pyZeroPad(absInt(data), width-len(mSign))
		} else {
			digits := width - prec - 1
			if digits < 0 {
				return "", unsupported("float format")
			}
			body := strconv.FormatFloat(math.Abs(data), 'f', digits, 64)
			value = mSign + pyZeroPad(body, width-len(mSign))
			if prec == 0 || (prec == 1 && mSign != "") {
				value = strings.ReplaceAll(value, "0.", ".")
			}
		}
		for len(value) < width {
			value += "0"
		}
		return value, nil
	}
	precision := width
	if mSign != "" {
		precision++
	}
	formatted := fmt.Sprintf("%*.*e", width, precision, data)
	m, es, ok := strings.Cut(formatted, "e")
	if !ok {
		return "", unsupported("float format")
	}
	w := width
	if prec <= 0 {
		w = width + 1
	}
	if data < 0 {
		w++
	}
	if len(m) > w {
		m = m[:w]
	}
	e, err := strconv.Atoi(es)
	if err != nil {
		return "", unsupported("float format")
	}
	m1, m2, ok := strings.Cut(m, ".")
	if !ok {
		return "", unsupported("float format")
	}
	dot := 0
	if prec >= 0 {
		dot = 1
	}
	for len(m1)+len(m2) < width-dot {
		m2 += "0"
	}
	if mSign != "" && data > 0 {
		m1 = "+" + m1
	}
	expText := func(e int) string {
		s := strconv.Itoa(e)
		if eSign && e >= 0 {
			s = "+" + s
		}
		// f'{e:{esgn}0{e_width}d}': zero padding after the sign
		sign := ""
		if s[0] == '+' || s[0] == '-' {
			sign, s = s[:1], s[1:]
		}
		for len(sign)+len(s) < eWidth {
			s = "0" + s
		}
		return sign + s
	}
	switch {
	case prec < 0:
		if m2 != "0" {
			e -= len(m2)
		} else {
			m2 = ""
		}
		ms := 0
		if mSign != "" {
			ms = 1
		}
		for len(m1)+len(m2)-ms < width {
			m2 += "0"
			e--
		}
		return m1 + m2 + exp + expText(e), nil
	case prec == 0:
		e -= len(m2)
		return m1 + m2 + "." + exp + expText(e), nil
	default:
		if lead0 > 0 {
			m2 = strings.Repeat("0", lead0-1) + m1 + m2
			m1 = "0"
			if len(m2) < lead0 {
				return "", unsupported("float format")
			}
			m2 = m2[:len(m2)-lead0]
			e += lead0
		}
		for len(m1) < prec {
			if m2 == "" {
				return "", unsupported("float format")
			}
			m1 += m2[:1]
			m2 = m2[1:]
			e--
		}
		return m1 + "." + m2 + exp + expText(e), nil
	}
}

// numberText writes a plain int or float scalar of the source the way the
// round trip does: the representer's text, plain when the YAML 1.2 dump
// resolver reads it back with the same tag, otherwise `!!int 'text'`.
func numberText(raw, tag string, yaml12 bool) (string, error) {
	var text string
	var err error
	if tag == "int" {
		text, err = intOut(raw, yaml12)
	} else {
		text, err = floatOut(raw, yaml12)
	}
	if err != nil {
		return "", err
	}
	if resolveTag(text, true) != tag { // the dump resolver: version None is YAML 1.2
		return "!!" + tag + " '" + text + "'", nil
	}
	return text, nil
}
