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
	// noNewline is ruamel's no_newline: set for the first item of a block
	// sequence whose start carries a comment (an end-of-line comment or
	// comment/blank lines after the key); the first wrap inside that item
	// then writes no line break.
	noNewline bool
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
func (e *emitter) lineNoWidth(text string, r *rec) {
	e.out = append(e.out, e.withEOL(text, r.eol))
	e.gap(r.gap)
}

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
			if len(r.gap) > 0 {
				// A comment right after a literal scalar must dedent below the
				// content indentation (ruamel issue 376): column = indent - 1.
				first := r.gap[0]
				trimmed := strings.TrimLeft(first, " ")
				if strings.HasPrefix(trimmed, "#") && len(first)-len(trimmed) >= childCol && childCol > 0 {
					moved := *r
					moved.gap = append([]string{spaces(childCol-1) + trimmed}, r.gap[1:]...)
					r = &moved
				}
			}
			e.line(join(head, strings.Join(parts, " ")), r, body)
			return
		}
		if target.style == sDouble && prefix == "" {
			col := utf8.RuneCountInString(head) + 2
			nonl := e.noNewline && !isMapEntry
			pieces := foldDouble(target.raw, col, childCol, e.width, true, &nonl)
			if len(pieces) > 1 {
				e.out = append(e.out, head+" "+pieces[0])
				for _, p := range pieces[1 : len(pieces)-1] {
					e.out = append(e.out, spaces(childCol)+p)
				}
				e.lineNoWidth(spaces(childCol)+pieces[len(pieces)-1], r)
				return
			}
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
			lines, err := e.writeFlow(head, target, childCol, e.noNewline && !isMapEntry && prefix == "")
			if err != nil {
				e.fail(err)
				return
			}
			e.openEnded = false
			e.out = append(e.out, lines[:len(lines)-1]...)
			e.lineNoWidth(lines[len(lines)-1], r)
			return
		}
		e.line(join(head, prefix), r, nil)
		if target.kind == kMap {
			e.emitMap(target, childCol, spaces(childCol))
		} else {
			e.noNewline = r.eol != nil || len(r.gap) > 0
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
		e.noNewline = false
		if e.err != nil {
			return
		}
	}
}

// flowWriter renders a flow collection the way ruamel's emitter does,
// including its line wrapping at the document width.
type flowWriter struct {
	e     *emitter
	lines []string
	cur   []rune
	ws    bool // the last thing written is whitespace (or an opening bracket)
	// nonl is ruamel's no_newline: the next write_indent writes no line break.
	nonl bool
	err  error
}

func (w *flowWriter) col() int { return len(w.cur) }

func (w *flowWriter) put(s string) { w.cur = append(w.cur, []rune(s)...) }

// indentTo is write_indent at indentation n (always a break here: the
// emitter has just written text, so indention is false).
func (w *flowWriter) indentTo(n int) {
	if w.nonl {
		w.nonl = false
	} else {
		w.lines = append(w.lines, string(w.cur))
		w.cur = nil
	}
	if w.col() < n {
		w.put(spaces(n - w.col()))
		w.ws = true
	}
}

func (w *flowWriter) fail(err error) {
	if w.err == nil {
		w.err = err
	}
}

// space is the leading space of a token (write_indicator need_whitespace).
func (w *flowWriter) space() {
	if !w.ws {
		w.put(" ")
	}
}

// indicator is write_indicator(text, need_whitespace=True): anchors and
// aliases, which never wrap.
func (w *flowWriter) indicator(text string) {
	w.space()
	w.put(text)
	w.ws = false
}

func (w *flowWriter) node(n *node, parent int, inSeq bool) {
	if w.err != nil {
		return
	}
	indent := parent + 2 // the indentation of a collection or scalar written here
	target, alias, prefix := w.e.resolve(n)
	if alias != "" {
		w.indicator(alias)
		return
	}
	if prefix != "" {
		w.indicator(prefix)
		w.nonl = false // process_anchor, issue 288
	}
	switch target.kind {
	case kScalar:
		switch target.style {
		case sLiteral:
			w.fail(unsupported("block scalar inside a flow collection"))
		case sDouble:
			w.space()
			pieces := foldDouble(target.raw, w.col()+1, indent, w.e.width, true, &w.nonl)
			w.put(pieces[0])
			for _, p := range pieces[1:] {
				w.lines = append(w.lines, string(w.cur))
				w.cur = []rune(spaces(indent))
				w.put(p)
			}
			w.ws = false
		case sSingle:
			if _, _, err := scalarText(target, true, w.e.yaml12); err != nil {
				w.fail(err)
				return
			}
			w.single(target.raw, indent, true)
		default:
			text, _, err := scalarText(target, true, w.e.yaml12)
			if err != nil {
				w.fail(err)
				return
			}
			if strings.HasPrefix(text, "'") {
				// a plain source scalar re-emitted quoted
				w.single(text[1:len(text)-1], indent, true)
				return
			}
			w.plain(text, indent, true)
		}
	case kSeq:
		w.space()
		w.put("[")
		w.ws = true
		if len(target.items) == 0 {
			w.put("]")
			w.ws = false
			return
		}
		for i, it := range target.items {
			if i > 0 {
				w.put(",")
				w.ws = false
			}
			if w.col() > w.e.width {
				w.indentTo(indent)
			}
			w.node(it.value, indent, true)
		}
		w.put("]")
		w.ws = false
		w.nonl = false
	default:
		single := inSeq && len(target.entries) == 1 && prefix == ""
		if !single {
			w.space()
			w.put("{")
			w.ws = true
		}
		if len(target.entries) == 0 {
			w.put("}")
			w.ws = false
			return
		}
		for i, en := range target.entries {
			if i > 0 {
				w.put(",")
				w.ws = false
			}
			if w.col() > w.e.width {
				w.indentTo(indent)
			}
			switch en.key.style {
			case sDouble:
				w.space()
				w.put(foldDouble(en.key.raw, 0, 0, 0, false, nil)[0])
				w.ws = false
			case sSingle:
				if _, _, err := scalarText(en.key, true, w.e.yaml12); err != nil {
					w.fail(err)
					return
				}
				w.single(en.key.raw, indent+2, false)
			default:
				k, _, err := scalarText(en.key, true, w.e.yaml12)
				if err != nil {
					w.fail(err)
					return
				}
				if strings.HasPrefix(k, "'") || strings.HasPrefix(k, `"`) {
					w.space()
					w.put(k)
					w.ws = false
				} else {
					w.plain(k, indent+2, false)
				}
			}
			w.put(":")
			w.ws = false
			w.node(en.value, indent, false)
		}
		if !single {
			w.put("}")
		}
		w.ws = false
		w.nonl = false
	}
}

// plain is write_plain: its leading space, then word by word, a word that
// would cross the width moves to a new line; with split, a single space at
// or past the width becomes the line break.
func (w *flowWriter) plain(text string, indent int, split bool) {
	if text == "" {
		return
	}
	if strings.ContainsAny(text, "\n\u0085\u2028\u2029") {
		w.fail(unsupported("multi-line plain scalar in a flow collection"))
		return
	}
	w.space()
	w.ws = false
	rs := []rune(text)
	inSpaces := false
	start := 0
	for end := 0; end <= len(rs); end++ {
		ch := rune(-1)
		if end < len(rs) {
			ch = rs[end]
		}
		if inSpaces {
			if ch != ' ' {
				if start+1 == end && w.col() >= w.e.width && split {
					w.indentTo(indent)
				} else {
					w.put(string(rs[start:end]))
				}
				start = end
			}
		} else if ch == -1 || ch == ' ' {
			if end-start+w.col() > w.e.width && w.col() > indent {
				w.indentTo(indent) // words longer than the line get a line of their own
			}
			w.put(string(rs[start:end]))
			start = end
		}
		if ch != -1 {
			inSpaces = ch == ' '
		}
	}
	w.ws = false
}

// single is write_single_quoted for a value without line breaks.
func (w *flowWriter) single(text string, indent int, split bool) {
	if strings.ContainsAny(text, "\n\u0085\u2028\u2029") {
		w.fail(unsupported("multi-line single-quoted scalar in a flow collection"))
		return
	}
	w.space()
	w.put("'")
	rs := []rune(text)
	inSpaces := false
	start := 0
	for end := 0; end <= len(rs); end++ {
		ch := rune(-1)
		if end < len(rs) {
			ch = rs[end]
		}
		if inSpaces {
			if ch != ' ' {
				if start+1 == end && w.col() > w.e.width && split && start != 0 && end != len(rs) {
					w.indentTo(indent)
				} else {
					w.put(string(rs[start:end]))
				}
				start = end
			}
		} else if ch == -1 || ch == ' ' || ch == '\'' {
			if start < end {
				w.put(string(rs[start:end]))
				start = end
			}
		}
		if ch == '\'' {
			w.put("''")
			start = end + 1
		}
		if ch != -1 {
			inSpaces = ch == ' '
		}
	}
	w.put("'")
	w.ws = false
}

// writeFlow renders n after head and returns the physical lines; nonl is
// the emitter's no_newline flag at that point.
func (e *emitter) writeFlow(head string, n *node, indent int, nonl bool) ([]string, error) {
	w := &flowWriter{e: e, cur: []rune(head), nonl: nonl}
	w.node(n, indent-2, false)
	if w.err != nil {
		return nil, w.err
	}
	return append(w.lines, string(w.cur)), nil
}
