package yamlx

import (
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"math"
	"math/big"
	"regexp"
	"strconv"
	"strings"

	"configflow/internal/jsonx"

	"gopkg.in/yaml.v3"
)

// Schema selects how plain scalars are typed.
type Schema int

const (
	// SchemaPyYAML is yaml.SafeLoader: YAML 1.1 (yes/no/on/off booleans,
	// octal, sexagesimal, timestamps, ~).
	SchemaPyYAML Schema = iota
	// SchemaNative is oxidns_native.NativeLoader: only true/false booleans,
	// 0x/0o/0b integers, no timestamps.
	SchemaNative
)

// LoadOptions tune Load.
type LoadOptions struct {
	Schema Schema
	// Key, when set, is called for every mapping key as it is constructed
	// (merge-key contents included, in PyYAML's order); duplicate tells that
	// the key was already present in the same mapping. A non-nil result aborts
	// the load and is returned unchanged. It lets callers reproduce loaders
	// that override construct_mapping, like NativeLoader.
	Key func(key string, duplicate bool) error
	// Name is the stream name in error marks; empty means "<unicode string>".
	Name string
}

// Error is a PyYAML-style load error: Error() is `str(exc)`, with the same
// "in "<unicode string>", line L, column C:" marks for constructor errors.
// Syntax errors come from gopkg.in/yaml.v3 and read differently from PyYAML's.
type Error struct {
	Msg string
	// Syntax is true for scanner/parser errors (yaml.YAMLError subclasses
	// that PyYAML raises while reading, as opposed to building values).
	Syntax bool
}

func (e *Error) Error() string { return e.Msg }

// NonStringKeyError is returned when a mapping key is not a string (`{80: a}`,
// `{yes: a}`, `{~: a}`): jsonx objects have string keys only, PyYAML would build
// a dict with int, bool or None keys.
type NonStringKeyError struct {
	Key  any
	Line int
	Msg  string
}

func (e *NonStringKeyError) Error() string { return e.Msg }

// Load is yaml.safe_load(text): one document (an empty stream is nil).
func Load(text string) (any, error) {
	return LoadWith(text, LoadOptions{})
}

// SafeLoad is fast_yaml.safe_load: yaml.safe_load. libyaml and the verification
// cache of the Python module have no counterpart; there is one parser.
func SafeLoad(text string) (any, error) {
	return LoadWith(text, LoadOptions{})
}

// LoadBytes is yaml.safe_load(bytes). Input that is not UTF-8 is an error.
func LoadBytes(data []byte) (any, error) {
	return LoadWith(string(data), LoadOptions{Name: "<byte string>"})
}

// LoadWith loads one document with the given options.
func LoadWith(text string, opts LoadOptions) (any, error) {
	if opts.Name == "" {
		opts.Name = "<unicode string>"
	}
	l := &loader{text: []rune(text), opts: opts, memo: map[*yaml.Node]any{}, active: map[*yaml.Node]bool{}}
	source := text
retry:
	dec := yaml.NewDecoder(strings.NewReader(source))
	var doc yaml.Node
	if err := dec.Decode(&doc); err != nil {
		if errors.Is(err, io.EOF) {
			return nil, nil
		}
		// PyYAML accepts what yaml.v3 (a libyaml port) refuses in two harmless
		// cases; rewrite the text once without changing any value and retry.
		if fixed, ok := compatible(source, err); ok {
			source = fixed
			goto retry
		}
		return nil, l.syntaxError(err)
	}
	var next yaml.Node
	if err := dec.Decode(&next); err == nil {
		message := "expected a single document in the stream"
		if root := firstContent(&doc); root != nil && !(root.Kind == yaml.ScalarNode && root.Value == "" && root.Style == 0) {
			message += "\n" + l.mark(root)
		}
		return nil, &Error{Msg: message + "\nbut found another document\n" + l.mark(&next)}
	} else if !errors.Is(err, io.EOF) {
		return nil, l.syntaxError(err)
	}
	if err := l.checkAnchors(&doc, map[string]*yaml.Node{}); err != nil {
		return nil, err
	}
	root := &doc
	if doc.Kind == yaml.DocumentNode {
		if len(doc.Content) == 0 {
			return nil, nil
		}
		root = doc.Content[0]
	}
	return l.construct(root)
}

var yamlDirective = regexp.MustCompile(`(?m)^%YAML[ \t]+1\.[0-9]+`)

// compatible rewrites text for the two PyYAML behaviours yaml.v3 lacks: any
// "%YAML 1.x" directive (yaml.v3 knows 1.1 only) and the "\/" escape in
// double-quoted scalars (written as "\x2F", the same character).
func compatible(text string, err error) (string, bool) {
	message := err.Error()
	switch {
	case strings.Contains(message, "incompatible YAML document"):
		fixed := yamlDirective.ReplaceAllString(text, "%YAML 1.1")
		return fixed, fixed != text
	case strings.Contains(message, "unknown escape character"):
		fixed := escapeSlashInDoubleQuotes(text)
		return fixed, fixed != text
	}
	return text, false
}

// escapeSlashInDoubleQuotes replaces `\/` inside double-quoted scalars. A
// double-quoted scalar starts with a quote that follows the start of a line, a
// block or flow indicator (": ", "- ", "? ", ",", "[", "{") or a tag/anchor.
func escapeSlashInDoubleQuotes(text string) string {
	runes := []rune(text)
	var out strings.Builder
	inDouble, inSingle, inComment := false, false, false
	lastSignificant := rune('\n')
	for i := 0; i < len(runes); i++ {
		r := runes[i]
		switch {
		case inComment:
			if r == '\n' {
				inComment = false
				lastSignificant = '\n'
			}
		case inDouble:
			if r == '\\' && i+1 < len(runes) {
				if runes[i+1] == '/' {
					out.WriteString(`\x2F`)
				} else {
					out.WriteRune(r)
					out.WriteRune(runes[i+1])
				}
				i++
				continue
			}
			if r == '"' {
				inDouble = false
				lastSignificant = '"'
			}
		case inSingle:
			if r == '\'' {
				if i+1 < len(runes) && runes[i+1] == '\'' {
					out.WriteString("''")
					i++
					continue
				}
				inSingle = false
				lastSignificant = '\''
			}
		case r == '#' && (i == 0 || runes[i-1] == ' ' || runes[i-1] == '\t' || runes[i-1] == '\n'):
			inComment = true
		case r == '"' && strings.ContainsRune("\n:-?,[{!", lastSignificant):
			inDouble = true
		case r == '\'' && strings.ContainsRune("\n:-?,[{!", lastSignificant):
			inSingle = true
		case r != ' ' && r != '\t':
			lastSignificant = r
		}
		out.WriteRune(r)
	}
	return out.String()
}

// checkAnchors is the composer's duplicate anchor check (yaml.v3 allows an
// anchor to be redefined, PyYAML does not).
func (l *loader) checkAnchors(n *yaml.Node, seen map[string]*yaml.Node) error {
	if n.Kind == yaml.AliasNode {
		return nil
	}
	if n.Anchor != "" {
		if first, ok := seen[n.Anchor]; ok {
			return &Error{Msg: fmt.Sprintf("found duplicate anchor %s; first occurrence\n%s\nsecond occurrence\n%s",
				jsonx.Repr(n.Anchor), l.mark(first), l.mark(n))}
		}
		seen[n.Anchor] = n
	}
	for _, child := range n.Content {
		if err := l.checkAnchors(child, seen); err != nil {
			return err
		}
	}
	return nil
}

func firstContent(doc *yaml.Node) *yaml.Node {
	if doc.Kind == yaml.DocumentNode && len(doc.Content) > 0 {
		return doc.Content[0]
	}
	return doc
}

var unknownAnchor = regexp.MustCompile(`unknown anchor '([^']*)' referenced`)

// syntaxError converts a yaml.v3 error. An undefined alias gets PyYAML's text
// (located at its first use in the text); everything else keeps yaml.v3's.
func (l *loader) syntaxError(err error) error {
	message := strings.TrimPrefix(err.Error(), "yaml: ")
	if m := unknownAnchor.FindStringSubmatch(message); m != nil {
		needle := []rune("*" + m[1])
		for i := 0; i+len(needle) <= len(l.text); i++ {
			if string(l.text[i:i+len(needle)]) == string(needle) {
				line, column := 1, 1
				for _, r := range l.text[:i] {
					if r == '\n' {
						line, column = line+1, 1
					} else {
						column++
					}
				}
				at := &yaml.Node{Line: line, Column: column}
				return &Error{Msg: fmt.Sprintf("found undefined alias '%s'\n%s", m[1], l.mark(at))}
			}
		}
	}
	return &Error{Msg: message, Syntax: true}
}

type loader struct {
	text   []rune
	opts   LoadOptions
	memo   map[*yaml.Node]any
	active map[*yaml.Node]bool
}

func (l *loader) resolvers() []implicitResolver {
	if l.opts.Schema == SchemaNative {
		return nativeResolvers
	}
	return pyResolvers
}

// mark renders Mark.__str__ for the start of n.
func (l *loader) mark(n *yaml.Node) string {
	line, column := n.Line, n.Column
	if line < 1 {
		line = 1
	}
	if column < 1 {
		column = 1
	}
	pointer, current := 0, 1
	for pointer < len(l.text) && current < line {
		if l.text[pointer] == '\n' {
			current++
		}
		pointer++
	}
	pointer += column - 1
	if pointer > len(l.text) {
		pointer = len(l.text)
	}
	buffer := append(append([]rune{}, l.text...), 0)
	const indent, maxLength = 4, 75
	head, tail := "", ""
	start := pointer
	for start > 0 && !strings.ContainsRune("\x00\r\n\u0085\u2028\u2029", buffer[start-1]) {
		start--
		if pointer-start > maxLength/2-1 {
			head = " ... "
			start += 5
			break
		}
	}
	end := pointer
	for end < len(buffer) && !strings.ContainsRune("\x00\r\n\u0085\u2028\u2029", buffer[end]) {
		end++
		if end-pointer > maxLength/2-1 {
			tail = " ... "
			end -= 5
			break
		}
	}
	snippet := strings.Repeat(" ", indent) + head + string(buffer[start:end]) + tail + "\n" +
		strings.Repeat(" ", indent+pointer-start+len([]rune(head))) + "^"
	return fmt.Sprintf("  in \"%s\", line %d, column %d:\n%s", l.opts.Name, line, column, snippet)
}

// constructorError is ConstructorError(context, contextMark, problem, problemMark).
func (l *loader) constructorError(context string, contextNode *yaml.Node, problem string, problemNode *yaml.Node) error {
	var lines []string
	if context != "" {
		lines = append(lines, context)
	}
	if contextNode != nil && (problemNode == nil || contextNode.Line != problemNode.Line || contextNode.Column != problemNode.Column) {
		lines = append(lines, l.mark(contextNode))
	}
	if problem != "" {
		lines = append(lines, problem)
	}
	if problemNode != nil {
		lines = append(lines, l.mark(problemNode))
	}
	return &Error{Msg: strings.Join(lines, "\n")}
}

func deref(n *yaml.Node) *yaml.Node {
	for n.Kind == yaml.AliasNode {
		n = n.Alias
	}
	return n
}

func longTag(tag string) string {
	if strings.HasPrefix(tag, "!!") {
		return "tag:yaml.org,2002:" + tag[2:]
	}
	return tag
}

func nodeID(n *yaml.Node) string {
	switch n.Kind {
	case yaml.MappingNode:
		return "mapping"
	case yaml.SequenceNode:
		return "sequence"
	}
	return "scalar"
}

// tagOf is the tag PyYAML's composer gives the node.
func (l *loader) tagOf(n *yaml.Node) string {
	if n.Style&yaml.TaggedStyle != 0 && n.Tag != "" && n.Tag != "!" {
		return longTag(n.Tag)
	}
	switch n.Kind {
	case yaml.SequenceNode:
		return tagSeq
	case yaml.MappingNode:
		return tagMap
	}
	if n.Style&(yaml.DoubleQuotedStyle|yaml.SingleQuotedStyle|yaml.LiteralStyle|yaml.FoldedStyle) != 0 {
		return tagStr
	}
	return detectWith(l.resolvers(), n.Value)
}

func (l *loader) construct(n *yaml.Node) (any, error) {
	n = deref(n)
	if v, ok := l.memo[n]; ok {
		return v, nil
	}
	if l.active[n] {
		return nil, l.constructorError("", nil, "found unconstructable recursive node", n)
	}
	l.active[n] = true
	defer delete(l.active, n)
	tag := l.tagOf(n)
	var value any
	var err error
	switch n.Kind {
	case yaml.ScalarNode:
		value, err = l.scalar(n, tag)
	case yaml.SequenceNode:
		if tag != tagSeq {
			return nil, l.undefined(tag, n)
		}
		list := make([]any, 0, len(n.Content))
		for _, child := range n.Content {
			item, cerr := l.construct(child)
			if cerr != nil {
				return nil, cerr
			}
			list = append(list, item)
		}
		value = list
	case yaml.MappingNode:
		if tag != tagMap {
			return nil, l.undefined(tag, n)
		}
		value, err = l.mapping(n)
	default:
		err = l.constructorError("", nil, "unexpected node", n)
	}
	if err != nil {
		return nil, err
	}
	l.memo[n] = value
	return value, nil
}

func (l *loader) undefined(tag string, n *yaml.Node) error {
	return l.constructorError("", nil, fmt.Sprintf("could not determine a constructor for the tag '%s'", tag), n)
}

func (l *loader) scalar(n *yaml.Node, tag string) (any, error) {
	switch tag {
	case tagNull:
		return nil, nil
	case tagBool:
		return strings.ToLower(n.Value) == "yes" || strings.ToLower(n.Value) == "true" || strings.ToLower(n.Value) == "on", nil
	case tagInt:
		return l.integer(n)
	case tagFloat:
		return constructFloat(n.Value), nil
	case tagStr:
		return n.Value, nil
	case tagBinary:
		for i, r := range []rune(n.Value) {
			if r > 127 {
				return nil, l.constructorError("", nil, fmt.Sprintf(
					"failed to convert base64 data into ascii: 'ascii' codec can't encode character %s in position %d: ordinal not in range(128)",
					pyCharRepr(r), i), n)
			}
		}
		decoded, err := decodeBase64(n.Value)
		if err != nil {
			return nil, l.constructorError("", nil, "failed to decode base64 data: "+err.Error(), n)
		}
		return decoded, nil
	case tagTimestamp:
		return constructTimestamp(n.Value)
	}
	return nil, l.undefined(tag, n)
}

func pyCharRepr(r rune) string {
	switch {
	case r <= 0xFF:
		return fmt.Sprintf("'\\x%02x'", r)
	case r <= 0xFFFF:
		return fmt.Sprintf("'\\u%04x'", r)
	}
	return fmt.Sprintf("'\\U%08x'", r)
}

// decodeBase64 is base64.decodebytes: characters outside the alphabet are
// skipped, padding must be complete.
func decodeBase64(text string) ([]byte, error) {
	var data []byte
	pad := 0
	for i := 0; i < len(text); i++ {
		c := text[i]
		switch {
		case c >= 'A' && c <= 'Z', c >= 'a' && c <= 'z', c >= '0' && c <= '9', c == '+', c == '/':
			data = append(data, c)
		case c == '=':
			pad++
		}
	}
	switch len(data) % 4 {
	case 1:
		return nil, fmt.Errorf("Invalid base64-encoded string: number of data characters (%d) cannot be 1 more than a multiple of 4", len(data))
	case 2:
		if pad < 2 {
			return nil, errors.New("Incorrect padding")
		}
	case 3:
		if pad < 1 {
			return nil, errors.New("Incorrect padding")
		}
	}
	return base64.RawStdEncoding.DecodeString(string(data))
}

func (l *loader) integer(n *yaml.Node) (any, error) {
	if l.opts.Schema == SchemaNative {
		return nativeInt(n.Value)
	}
	return constructInt(n.Value)
}

func newBig(text string, base int) (*big.Int, error) {
	v, ok := new(big.Int).SetString(text, base)
	if !ok || text == "" || strings.HasPrefix(text, "+") || strings.HasPrefix(text, "-") || strings.Contains(text, "_") {
		return nil, fmt.Errorf("invalid literal for int() with base %d: %s", base, jsonx.Repr(text))
	}
	return v, nil
}

// constructInt is SafeConstructor.construct_yaml_int.
func constructInt(raw string) (any, error) {
	value := strings.ReplaceAll(raw, "_", "")
	sign := int64(1)
	if value[0] == '-' {
		sign = -1
	}
	if value[0] == '+' || value[0] == '-' {
		value = value[1:]
	}
	var result *big.Int
	var err error
	switch {
	case value == "0":
		return jsonx.Int(0), nil
	case strings.HasPrefix(value, "0b"):
		result, err = newBig(value[2:], 2)
	case strings.HasPrefix(value, "0x"):
		result, err = newBig(value[2:], 16)
	case value[0] == '0':
		result, err = newBig(value, 8)
	case strings.Contains(value, ":"):
		parts := strings.Split(value, ":")
		result = new(big.Int)
		base := big.NewInt(1)
		for i := len(parts) - 1; i >= 0; i-- {
			digit, perr := newBig(parts[i], 10)
			if perr != nil {
				return nil, perr
			}
			result.Add(result, new(big.Int).Mul(digit, base))
			base = new(big.Int).Mul(base, big.NewInt(60))
		}
	default:
		result, err = newBig(value, 10)
	}
	if err != nil {
		return nil, err
	}
	if sign < 0 {
		result.Neg(result)
	}
	return jsonx.BigInt(result), nil
}

// nativeInt is NativeLoader's int constructor: int(text.replace('_', ”), 0 if
// prefixed else 10).
func nativeInt(raw string) (any, error) {
	text := strings.ReplaceAll(raw, "_", "")
	trimmed := strings.TrimLeft(text, "-+")
	sign := 1
	if strings.HasPrefix(text, "-") {
		sign = -1
	}
	base := 10
	body := text
	if len(text)-len(trimmed) > 1 {
		return nil, fmt.Errorf("invalid literal for int() with base 10: %s", jsonx.Repr(text))
	}
	body = trimmed
	switch {
	case strings.HasPrefix(body, "0x"):
		base, body = 16, body[2:]
	case strings.HasPrefix(body, "0o"):
		base, body = 8, body[2:]
	case strings.HasPrefix(body, "0b"):
		base, body = 2, body[2:]
	}
	v, err := newBig(body, base)
	if err != nil {
		return nil, fmt.Errorf("invalid literal for int() with base %d: %s", base, jsonx.Repr(text))
	}
	if sign < 0 {
		v.Neg(v)
	}
	return jsonx.BigInt(v), nil
}

// constructFloat is SafeConstructor.construct_yaml_float.
func constructFloat(raw string) any {
	value := strings.ToLower(strings.ReplaceAll(raw, "_", ""))
	sign := 1.0
	if value[0] == '-' {
		sign = -1
	}
	if value[0] == '+' || value[0] == '-' {
		value = value[1:]
	}
	switch {
	case value == ".inf":
		return jsonx.Float(sign * math.Inf(1))
	case value == ".nan":
		return jsonx.Float(math.NaN())
	case strings.Contains(value, ":"):
		parts := strings.Split(value, ":")
		total, base := 0.0, 1.0
		for i := len(parts) - 1; i >= 0; i-- {
			digit, _ := strconv.ParseFloat(parts[i], 64)
			total += digit * base
			base *= 60
		}
		return jsonx.Float(sign * total)
	}
	f, _ := strconv.ParseFloat(value, 64)
	return jsonx.Float(sign * f)
}

var timestampRe = regexp.MustCompile(`^([0-9][0-9][0-9][0-9])-([0-9][0-9]?)-([0-9][0-9]?)(?:(?:[Tt]|[ \t]+)([0-9][0-9]?):([0-9][0-9]):([0-9][0-9])(?:\.([0-9]*))?(?:[ \t]*(Z|([-+])([0-9][0-9]?)(?::([0-9][0-9]))?))?)?$`)

func daysIn(year, month int) int {
	switch month {
	case 2:
		if year%4 == 0 && (year%100 != 0 || year%400 == 0) {
			return 29
		}
		return 28
	case 4, 6, 9, 11:
		return 30
	}
	return 31
}

// constructTimestamp is SafeConstructor.construct_yaml_timestamp; errors carry
// the ValueError text of datetime.
func constructTimestamp(value string) (any, error) {
	m := timestampRe.FindStringSubmatch(strings.TrimSuffix(value, "\n"))
	if m == nil {
		return nil, &Error{Msg: "invalid timestamp " + jsonx.Repr(value)}
	}
	atoi := func(s string) int { v, _ := strconv.Atoi(s); return v }
	t := Timestamp{Year: atoi(m[1]), Month: atoi(m[2]), Day: atoi(m[3])}
	if t.Year < 1 {
		return nil, fmt.Errorf("year %d is out of range", t.Year)
	}
	if t.Month < 1 || t.Month > 12 {
		return nil, errors.New("month must be in 1..12")
	}
	if t.Day < 1 || t.Day > daysIn(t.Year, t.Month) {
		return nil, errors.New("day is out of range for month")
	}
	if m[4] == "" {
		return t, nil
	}
	t.HasTime = true
	t.Hour, t.Minute, t.Second = atoi(m[4]), atoi(m[5]), atoi(m[6])
	if t.Hour > 23 {
		return nil, errors.New("hour must be in 0..23")
	}
	if t.Minute > 59 {
		return nil, errors.New("minute must be in 0..59")
	}
	if t.Second > 59 {
		return nil, errors.New("second must be in 0..59")
	}
	if m[7] != "" {
		fraction := m[7]
		if len(fraction) > 6 {
			fraction = fraction[:6]
		}
		for len(fraction) < 6 {
			fraction += "0"
		}
		t.Microsecond = atoi(fraction)
	}
	if m[9] != "" {
		offset := atoi(m[10])*3600 + atoi(m[11])*60
		if offset >= 24*3600 {
			return nil, errors.New("offset must be a timedelta strictly between -timedelta(hours=24) and timedelta(hours=24).")
		}
		if m[9] == "-" {
			offset = -offset
		}
		t.HasZone, t.ZoneOffsetSeconds = true, offset
	} else if m[8] != "" {
		t.HasZone = true
	}
	return t, nil
}

type nodePair struct{ key, value *yaml.Node }

// flatten is SafeConstructor.flatten_mapping without mutating the tree: the
// pairs of a mapping with every `<<` merge key replaced by the merged pairs.
func (l *loader) flatten(n *yaml.Node) ([]nodePair, error) {
	var merge, rest []nodePair
	for i := 0; i+1 < len(n.Content); i += 2 {
		key, value := n.Content[i], deref(n.Content[i+1])
		if l.opts.Schema == SchemaPyYAML && l.keyTag(key) == tagMerge {
			switch value.Kind {
			case yaml.MappingNode:
				sub, err := l.flatten(value)
				if err != nil {
					return nil, err
				}
				merge = append(merge, sub...)
			case yaml.SequenceNode:
				var subs [][]nodePair
				for _, item := range value.Content {
					item = deref(item)
					if item.Kind != yaml.MappingNode {
						return nil, l.constructorError("while constructing a mapping", n,
							"expected a mapping for merging, but found "+nodeID(item), item)
					}
					sub, err := l.flatten(item)
					if err != nil {
						return nil, err
					}
					subs = append(subs, sub)
				}
				for j := len(subs) - 1; j >= 0; j-- {
					merge = append(merge, subs[j]...)
				}
			default:
				return nil, l.constructorError("while constructing a mapping", n,
					"expected a mapping or list of mappings for merging, but found "+nodeID(value), value)
			}
			continue
		}
		rest = append(rest, nodePair{key, n.Content[i+1]})
	}
	return append(merge, rest...), nil
}

func (l *loader) keyTag(key *yaml.Node) string {
	key = deref(key)
	if key.Kind != yaml.ScalarNode {
		return ""
	}
	return l.tagOf(key)
}

func (l *loader) mapping(n *yaml.Node) (any, error) {
	pairs, err := l.flatten(n)
	if err != nil {
		return nil, err
	}
	object := jsonx.NewObject()
	for _, p := range pairs {
		keyNode := deref(p.key)
		if keyNode.Kind != yaml.ScalarNode {
			return nil, l.constructorError("while constructing a mapping", n, "found unhashable key", keyNode)
		}
		var key any
		if l.keyTag(keyNode) == tagValue && l.opts.Schema == SchemaPyYAML {
			key = keyNode.Value // flatten_mapping retags a "=" key as a string
		} else {
			key, err = l.construct(keyNode)
			if err != nil {
				return nil, err
			}
		}
		name, ok := key.(string)
		if !ok {
			return nil, &NonStringKeyError{Key: key, Line: keyNode.Line,
				Msg: fmt.Sprintf("mapping key %s at line %d is not a string; jsonx objects have string keys only", jsonx.Repr(keyForRepr(key)), keyNode.Line)}
		}
		if l.opts.Key != nil {
			if err := l.opts.Key(name, object.Has(name)); err != nil {
				return nil, err
			}
		}
		value, err := l.construct(p.value)
		if err != nil {
			return nil, err
		}
		object.Set(name, value)
	}
	return object, nil
}

func keyForRepr(key any) any {
	switch key.(type) {
	case Timestamp, []byte:
		return fmt.Sprint(key)
	}
	return key
}
