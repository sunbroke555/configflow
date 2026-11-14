package commented

import (
	"strings"
	"unicode/utf8"
)

type emitter struct {
	out      []string
	width    int
	seen     map[*node]bool
	refs     map[*node]int
	emitted  []string // comment texts that reached the output
	err      error
	preLines map[*item][]string
	yaml12   bool
	// openEnded: the last scalar was a block scalar that keeps trailing newlines
	openEnded bool
}

func spaces(n int) string { return strings.Repeat(" ", n) }

func (e *emitter) fail(err error) {
	if e.err == nil {
		e.err = err
	}
}

// writeEOL joins text and an end-of-line comment the way ruamel's emitter
// does: the comment keeps its source column, at least one space after text.
func (e *emitter) withEOL(text string, c *comment) string {
	if c == nil {
		return text
	}
	n := c.col - utf8.RuneCountInString(text)
	if n < 1 {
		n = 1
	}
	e.noteComment(c.text)
	return text + spaces(n) + c.text
}

func (e *emitter) noteComment(text string) {
	e.emitted = append(e.emitted, strings.TrimSpace(text))
}

func (e *emitter) gap(g []string) {
	for _, l := range g {
		e.out = append(e.out, l)
		if strings.HasPrefix(strings.TrimLeft(l, " \t"), "#") {
			e.noteComment(l)
		}
	}
}

// line writes one content line, its end-of-line comment, extra body lines
// (block scalars) and the gap that follows.
func (e *emitter) line(text string, r *rec, body []string) {
	e.openEnded = body != nil && strings.Contains(text, "|") && strings.Contains(text[strings.LastIndex(text, "|"):], "+")
	if utf8.RuneCountInString(text) > e.width {
		e.fail(unsupported("line longer than the wrap width"))
	}
	e.out = append(e.out, e.withEOL(text, r.eol))
	e.out = append(e.out, body...)
	e.gap(r.gap)
}

// anchorPrefix decides whether n is written with its anchor, and whether it
// is replaced by an alias because it was already written.
func (e *emitter) resolve(n *node) (target *node, alias string, prefix string) {
	target = n
	if n.kind == kAlias {
		target = n.target
		if e.seen[target] {
			return target, "*" + n.name, ""
		}
	}
	if target.anchor != "" && (target.kind == kScalar || e.refs[target] >= 2) {
		prefix = "&" + target.anchor
	}
	e.seen[target] = true
	return target, "", prefix
}

func (e *emitter) emitMap(n *node, col int, lead string) {
	for idx, en := range n.entries {
		prefix := spaces(col)
		if idx == 0 {
			prefix = lead
		}
		keyNode, _, _ := e.resolveKey(en.key)
		keyText, null, err := scalarText(keyNode, false, e.yaml12)
		if err != nil {
			e.fail(err)
			return
		}
		if null || keyText == "" || keyNode.style == sPlain && !(isStringPlain(keyNode.raw) || e.yaml12 && keyText == keyNode.raw) {
			e.fail(unsupported("non-string mapping key"))
			return
		}
		head := prefix + keyText + ":"
		e.emitValue(head, en.value, &en.rec, col, col+2, true)
		if e.err != nil {
			return
		}
	}
}

func (e *emitter) resolveKey(k *node) (*node, string, string) { return k, "", "" }

func isStringPlain(raw string) bool {
	t, null, err := plainOut(raw)
	return err == nil && !null && t == raw
}

// emitValue writes head (everything before the value on the first line)
// plus the value. childCol is the indentation of a nested mapping; a nested
// sequence sits at seqCol.
func (e *emitter) emitValue(head string, v *node, r *rec, seqCol, childCol int, isMapEntry bool) {
	target, alias, prefix := e.resolve(v)
	if alias != "" {
		e.line(head+" "+alias, r, nil)
		return
	}
	join := func(head, tail string) string {
		if tail == "" {
			return head
		}
		return head + " " + tail
	}
	switch target.kind {
	case kScalar:
		if target.style == sLiteral {
			hints := literalHints(target.raw)
			parts := []string{}
			if prefix != "" {
				parts = append(parts, prefix)
			}
			parts = append(parts, "|"+hints)
			var body []string
			lines := strings.SplitAfter(target.raw, "\n")
			for _, l := range lines {
				l = strings.TrimSuffix(l, "\n")
				if l == "" {
					body = append(body, "")
				} else {
					body = append(body, spaces(childCol)+l)
				}
			}
			if strings.HasSuffix(target.raw, "\n") && len(body) > 0 {
				body = body[:len(body)-1]
			}
			e.line(join(head, strings.Join(parts, " ")), r, body)
			return
		}
		text, null, err := scalarText(target, false, e.yaml12)
		if err != nil {
			e.fail(err)
			return
		}
		if null && !isMapEntry {
			// "- " for a null sequence item
			text = ""
		}
		tail := text
		if prefix != "" {
			if tail != "" {
				tail = prefix + " " + tail
			} else {
				tail = prefix
			}
		}
		if null && !isMapEntry && prefix == "" {
			switch {
			case r.eol == nil:
				e.line(head+" ", r, nil)
			case target.raw != "":
				// an explicit null token: the comment keeps its column after the dash
				e.line(head, r, nil)
			default:
				// no token at all: the comment moves to the next line
				moved := *r
				moved.eol = nil
				moved.gap = append([]string{spaces(r.eol.col) + r.eol.text}, r.gap...)
				e.noteComment(r.eol.text)
				e.line(head+" ", &moved, nil)
			}
			return
		}
		e.line(join(head, tail), r, nil)
	case kMap, kSeq:
		if target.flow || (target.kind == kMap && len(target.entries) == 0) || (target.kind == kSeq && len(target.items) == 0) {
			text, err := e.flowText(target, false)
			if err != nil {
				e.fail(err)
				return
			}
			tail := text
			if prefix != "" {
				tail = prefix + " " + text
			}
			e.line(join(head, tail), r, nil)
			return
		}
		e.line(join(head, prefix), r, nil)
		if target.kind == kMap {
			e.emitMap(target, childCol, spaces(childCol))
		} else {
			e.emitSeq(target, seqCol, spaces(seqCol), isMapEntry)
		}
	}
}

// emitSeq writes the items of a block sequence with dashes at col; lead
// replaces the indentation of the first line.
func (e *emitter) emitSeq(n *node, col int, lead string, underMap bool) {
	for idx, it := range n.items {
		prefix := spaces(col)
		if idx == 0 {
			prefix = lead
		}
		for _, l := range e.preLines[it] {
			e.out = append(e.out, l)
			if strings.HasPrefix(l, "#") {
				e.noteComment(l)
			}
		}
		head := prefix + "-"
		v := it.value
		target, alias, prefixA := e.resolve(v)
		if alias == "" && target.kind != kScalar && !target.flow && !(target.kind == kMap && len(target.entries) == 0) && !(target.kind == kSeq && len(target.items) == 0) {
			// compact: first element shares the dash line (ruamel always writes it so)
			if it.rec.eol != nil || len(it.rec.gap) > 0 {
				e.fail(unsupported("comment on a dash line that holds a nested block"))
				return
			}
			if prefixA != "" {
				e.fail(unsupported("anchor on a compact collection"))
				return
			}
			if target.kind == kMap {
				e.emitMap(target, col+2, head+" ")
			} else {
				e.emitSeq(target, col+2, head+" ", false)
			}
		} else {
			e.emitValue(head, v, &it.rec, col, col+2, false)
		}
		if e.err != nil {
			return
		}
	}
}

// flowText renders a flow collection or scalar on one line.
func (e *emitter) flowText(n *node, inSeq bool) (string, error) {
	target, alias, prefix := e.resolve(n)
	if alias != "" {
		return alias, nil
	}
	withPrefix := func(s string) string {
		if prefix != "" {
			return prefix + " " + s
		}
		return s
	}
	switch target.kind {
	case kScalar:
		if target.style == sLiteral {
			return "", unsupported("block scalar inside a flow collection")
		}
		text, null, err := scalarText(target, true, e.yaml12)
		if err != nil {
			return "", err
		}
		_ = null
		return withPrefix(text), nil
	case kSeq:
		parts := make([]string, 0, len(target.items))
		for _, it := range target.items {
			s, err := e.flowText(it.value, true)
			if err != nil {
				return "", err
			}
			parts = append(parts, s)
		}
		return withPrefix("[" + strings.Join(parts, ", ") + "]"), nil
	default:
		parts := make([]string, 0, len(target.entries))
		for _, en := range target.entries {
			k, _, err := scalarText(en.key, true, e.yaml12)
			if err != nil {
				return "", err
			}
			v, err := e.flowText(en.value, false)
			if err != nil {
				return "", err
			}
			parts = append(parts, k+": "+v)
		}
		if inSeq && len(parts) == 1 && prefix == "" {
			return parts[0], nil
		}
		return withPrefix("{" + strings.Join(parts, ", ") + "}"), nil
	}
}
