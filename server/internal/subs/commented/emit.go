package commented

import (
	"errors"
	"fmt"
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
	genNames  map[*node]string // generated anchors (id001, ...)
	genCount  int
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
	e.openEnded = false
	if body != nil {
		if i := strings.LastIndexAny(text, "|>"); i >= 0 && strings.Contains(text[i:], "+") {
			e.openEnded = true
		}
	}
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
	target, alias, prefix = e.peekResolve(n)
	if alias == "" {
		e.seen[target] = true
	}
	return target, alias, prefix
}

// peekResolve is resolve without marking the node as written.
func (e *emitter) peekResolve(n *node) (target *node, alias string, prefix string) {
	target = n
	mode := scalarAnchorMode(target, e.yaml12)
	if n.kind == kAlias {
		target = n.target
		mode = scalarAnchorMode(target, e.yaml12)
		if e.seen[target] {
			switch mode {
			case anchorKeep:
				return target, "*" + n.name, ""
			case anchorGenerated:
				return target, "*" + e.genNames[target], ""
			}
		}
	}
	if target.anchor == "" {
		return target, "", ""
	}
	switch {
	case target.kind != kScalar:
		if e.refs[target] >= 2 {
			prefix = "&" + target.anchor
		}
	case mode == anchorKeep:
		prefix = "&" + target.anchor
	case mode == anchorGenerated && e.refs[target] >= 2:
		name, ok := e.genNames[target]
		if !ok {
			e.genCount++
			name = fmt.Sprintf("id%03d", e.genCount)
			e.genNames[target] = name
		}
		prefix = "&" + name
	}
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
	e.gap(n.endGap)
}

func (e *emitter) resolveKey(k *node) (*node, string, string) { return k, "", "" }

func isStringPlain(raw string) bool {
	t, tag, err := plainScalar(raw, false)
	return err == nil && (tag == "str" || tag == "merge" || tag == "value") && t == raw
}

// emitValue writes head (everything before the value on the first line)
// plus the value. childCol is the indentation of a nested mapping; a nested
// sequence sits at seqCol.
func (e *emitter) emitValue(head string, v *node, r *rec, seqCol, childCol int, isMapEntry bool) {
	e.emitValueOnly(head, v, r, seqCol, childCol, isMapEntry)
	if e.err == nil {
		e.gap(r.trail)
	}
}

func (e *emitter) emitValueOnly(head string, v *node, r *rec, seqCol, childCol int, isMapEntry bool) {
	target, alias, prefix := e.resolve(v)
	if alias == "" && r.eol == nil && target.post != nil {
		// an alias expansion: the node brings its own comment
		withPost := *r
		withPost.eol = target.post
		r = &withPost
	}
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
		if target.style == sLiteral || target.style == sFolded {
			hints := literalHints(target.raw)
			parts := []string{}
			if prefix != "" {
				parts = append(parts, prefix)
			}
			var body []string
			if target.style == sFolded {
				parts = append(parts, ">"+hints)
				var err error
				body, err = foldedBody(target, childCol, e.width)
				if err == errOpenLine {
					// the following blank line (a comment token "\n") only ends
					// the open line
					err = nil
					switch {
					case len(r.gap) > 0 && r.gap[0] == "":
						moved := *r
						moved.gap = r.gap[1:]
						r = &moved
					case len(r.gap) > 0 || r.eol != nil:
						err = unsupported("comment after a folded scalar ending in spaces")
					}
				}
				if err != nil {
					e.fail(err)
					return
				}
			} else {
				parts = append(parts, "|"+hints)
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
		if target.style == sDouble && prefix == "" && target.tag == "" {
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
		if target.head != nil {
			// the collection's own comment: on its own line, at its column
			withHead := *r
			withHead.gap = append([]string{spaces(target.head.col) + target.head.text}, r.gap...)
			r = &withHead
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
		head := prefix + "-"
		v := it.value
		target, alias, prefixA := e.peekResolve(v)
		compact := alias == "" && target.kind != kScalar && !target.flow && !(target.kind == kMap && len(target.entries) == 0) && !(target.kind == kSeq && len(target.items) == 0)
		if compact && target.head != nil {
			e.out = append(e.out, spaces(target.head.col)+target.head.text)
			e.noteComment(target.head.text)
		}
		if compact && (it.rec.eol != nil || len(it.rec.gap) > 0) {
			// a comment on a bare dash line (and the lines up to the first
			// entry) moves above the dash, at its column, before the comments
			// set before this item
			if it.rec.eol != nil {
				e.out = append(e.out, spaces(it.rec.eol.col)+it.rec.eol.text)
				e.noteComment(it.rec.eol.text)
			}
			e.gap(it.rec.gap)
		}
		for _, l := range e.preLines[it] {
			e.out = append(e.out, l)
			if strings.HasPrefix(l, "#") {
				e.noteComment(l)
			}
		}
		if compact {
			// compact: first element shares the dash line (ruamel always writes it so)
			if prefixA != "" {
				e.fail(unsupported("anchor on a compact collection"))
				return
			}
			e.resolve(v)
			if len(it.dashBreak) > 0 && target.kind == kMap {
				e.out = append(e.out, head)
				e.gap(it.dashBreak)
				e.emitMap(target, col+2, spaces(col+2))
			} else if target.kind == kSeq && nullItemWithComment(target) {
				e.fail(unsupported("comment on an empty item of a nested sequence"))
				return
			} else if target.kind == kMap {
				e.emitMap(target, col+2, head+" ")
			} else {
				e.emitSeq(target, col+2, head+" ", false)
			}
			if e.err == nil {
				e.gap(it.rec.trail)
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
	// top is the parent indentation of the outermost collection
	top int
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
		if target.tag != "" {
			w.fail(unsupported("tagged scalar in a flow collection"))
			return
		}
		switch target.style {
		case sLiteral, sFolded:
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
		if !target.flow && (target.head != nil || subtreeCommented(target, map[*node]bool{})) {
			w.fail(unsupported("commented block collection expanded inside a flow collection"))
			return
		}
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
		w.postComment(target, parent)
	default:
		if !target.flow && (target.head != nil || subtreeCommented(target, map[*node]bool{})) {
			w.fail(unsupported("commented block collection expanded inside a flow collection"))
			return
		}
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
		w.postComment(target, parent)
	}
}

// postComment writes the own comment of a nested flow collection after its
// closing bracket (write_post_comment): at its column, then a line break.
// The top-level collection's comment is the line's end-of-line comment.
func (w *flowWriter) postComment(n *node, parent int) {
	if n.post == nil || parent < w.top {
		return
	}
	pad := n.post.col - w.col()
	if pad < 1 {
		pad = 1
	}
	w.put(spaces(pad) + n.post.text)
	w.e.noteComment(n.post.text)
	w.lines = append(w.lines, string(w.cur))
	w.cur = nil
	w.ws = true
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
	w := &flowWriter{e: e, cur: []rune(head), nonl: nonl, top: indent - 1}
	w.node(n, indent-2, false)
	if w.err != nil {
		return nil, w.err
	}
	return append(w.lines, string(w.cur)), nil
}

const (
	anchorKeep = iota
	anchorDrop
	anchorGenerated
)

// scalarAnchorMode: the round-trip constructor builds a plain Python value
// (no place for an anchor, aliases written out in full: ints and floats are
// not tracked) for null, sexagesimal numbers, infinities and NaN; an octal
// integer (OctalInt, written as a plain int) and a date are tracked by
// identity, so the serializer gives them a generated anchor (id001, ...)
// when they are used twice.
func scalarAnchorMode(n *node, yaml12 bool) int {
	if n.kind != kScalar || n.style != sPlain {
		return anchorKeep
	}
	switch resolveTag(n.raw, yaml12) {
	case "null":
		return anchorDrop
	case "int":
		s := strings.TrimLeft(strings.ReplaceAll(n.raw, "_", ""), "+-")
		if strings.HasPrefix(s, "0o") || !yaml12 && len(s) > 1 && s[0] == '0' && s[1] != 'b' && s[1] != 'x' {
			return anchorGenerated
		}
		if !yaml12 && strings.Contains(s, ":") {
			return anchorDrop
		}
	case "float":
		s := strings.ToLower(strings.TrimLeft(strings.ReplaceAll(n.raw, "_", ""), "+-"))
		if s == ".inf" || s == ".nan" || !yaml12 && strings.Contains(s, ":") {
			return anchorDrop
		}
	case "timestamp":
		if reDate.MatchString(n.raw) {
			return anchorGenerated
		}
	}
	return anchorKeep
}

func nullItemWithComment(n *node) bool {
	for _, it := range n.items {
		if it.value.kind == kScalar && it.value.style == sPlain && rNull.MatchString(it.value.raw) && it.rec.eol != nil {
			return true
		}
	}
	return false
}

func isPySpace(r rune) bool {
	return r == ' ' || r == '\t' || r == '\n' || r == '\r' || r == '\v' || r == '\f' || r == 0x1c || r == 0x1d || r == 0x1e || r == 0x1f || r == 0x85 || r == 0xa0 ||
		r == 0x1680 || r >= 0x2000 && r <= 0x200a || r == 0x2028 || r == 0x2029 || r == 0x202f || r == 0x205f || r == 0x3000
}

// foldedBody is represent_folded_scalarstring (fold marks put back where a
// single space between two non-spaces remains) followed by
// Emitter.write_folded; it returns the lines after the header.
func foldedBody(n *node, indent, width int) ([]string, error) {
	data := []rune(n.raw)
	for k := len(n.foldPos) - 1; k >= 0; k-- {
		fp := n.foldPos[k]
		if fp < len(data) && data[fp] == ' ' && fp > 0 && !isPySpace(data[fp-1]) {
			if fp+1 >= len(data) {
				return nil, unsupported("fold mark at the end of a folded scalar")
			}
			if !isPySpace(data[fp+1]) {
				data = append(data[:fp], append([]rune{'\a'}, data[fp:]...)...)
			}
		}
	}
	text := data
	var lines []string
	var cur []rune
	column := 0
	whitespace, indention := true, true
	lineBreak := func() {
		lines = append(lines, string(cur))
		cur = nil
		column = 0
		whitespace, indention = true, true
	}
	writeIndent := func() {
		if !indention || column > indent || (column == indent && !whitespace) {
			lineBreak()
		}
		if column < indent {
			whitespace = true
			cur = append(cur, []rune(spaces(indent-column))...)
			column = indent
		}
	}
	write := func(s []rune) {
		cur = append(cur, s...)
		column += len(s)
	}
	isBreak := func(r rune) bool { return r == '\n' || r == 0x85 || r == 0x2028 || r == 0x2029 }
	lineBreak() // after the header
	lines = nil
	leadingSpace, spacesMode, breaks := true, false, true
	start := 0
	for end := 0; end <= len(text); end++ {
		ch := rune(-1)
		if end < len(text) {
			ch = text[end]
		}
		switch {
		case breaks:
			if ch == -1 || !(isBreak(ch) || ch == '\a') {
				if !leadingSpace && ch != -1 && ch != ' ' && text[start] == '\n' {
					lineBreak()
				}
				leadingSpace = ch == ' '
				for _, br := range text[start:end] {
					if br != '\n' {
						return nil, unsupported("special line break in a folded scalar")
					}
					lineBreak()
				}
				if ch != -1 {
					writeIndent()
				}
				start = end
			}
		case spacesMode:
			if ch != ' ' {
				if start+1 == end && column > width {
					writeIndent()
				} else {
					write(text[start:end])
				}
				start = end
			}
		default:
			if ch == -1 || ch == ' ' || isBreak(ch) || ch == '\a' {
				write(text[start:end])
				if ch == '\a' {
					if end < len(text)-1 && end+2 < len(text) && !isPySpace(text[end+2]) {
						lineBreak()
						writeIndent()
						end += 2
					} else {
						return nil, unsupported("fold mark before a space")
					}
				}
				if ch == -1 {
					lineBreak()
				}
				start = end
			}
		}
		if ch != -1 {
			breaks = isBreak(ch)
			spacesMode = ch == ' '
		}
	}
	if len(cur) > 0 {
		// ended on trailing spaces without a line break: whatever is written
		// next ends this line
		lines = append(lines, string(cur))
		return lines, errOpenLine
	}
	return lines, nil
}

var errOpenLine = errors.New("folded scalar ends without a line break")
