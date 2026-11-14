package commented

import (
	"strings"
	"unicode"
)

type parser struct {
	lines   [][]rune
	pos     int
	doc     *document
	anchors map[string]*node
	started bool // the first content line was seen
}

func isSpaceRune(r rune) bool { return r == ' ' }

func isBlankLine(l []rune) bool {
	for _, r := range l {
		if r != ' ' && r != '\t' {
			return false
		}
	}
	return true
}

func leadingSpaces(l []rune) int {
	n := 0
	for n < len(l) && l[n] == ' ' {
		n++
	}
	return n
}

func isCommentLine(l []rune) bool {
	n := leadingSpaces(l)
	return n < len(l) && l[n] == '#'
}

func (p *parser) done() bool { return p.pos >= len(p.lines) }

func (p *parser) note(text string) {
	p.doc.comments = append(p.doc.comments, strings.TrimSpace(text))
}

// takeGap consumes blank and comment-only lines and returns them verbatim
// (blank lines as "").
func (p *parser) takeGap() []string {
	var gap []string
	for !p.done() {
		l := p.lines[p.pos]
		if p.started && (isBlankLine(l) || isCommentLine(l)) {
			p.doc.hasInnerGaps = true
		}
		switch {
		case isBlankLine(l):
			gap = append(gap, "")
		case isCommentLine(l):
			gap = append(gap, string(l))
			p.note(string(l))
		default:
			return gap
		}
		p.pos++
	}
	return gap
}

// peekIndent returns the indentation and text of the next content line.
func (p *parser) peek() (indent int, ok bool) {
	if p.done() {
		return 0, false
	}
	l := p.lines[p.pos]
	for _, r := range l {
		if r == '\t' {
			// tabs outside scalars: not handled
			return leadingSpaces(l), true
		}
		break
	}
	return leadingSpaces(l), true
}

func parseDocument(lines []string) (*document, error) {
	p := &parser{doc: &document{}, anchors: map[string]*node{}}
	for _, l := range lines {
		if strings.ContainsRune(l, '\t') && strings.HasPrefix(strings.TrimLeft(l, " "), "\t") {
			return nil, unsupported("tab indentation")
		}
		p.lines = append(p.lines, []rune(l))
	}
	p.doc.head = p.takeGap()
	if !p.done() {
		t := strings.TrimRight(string(p.lines[p.pos]), " ")
		if t == "---" || strings.HasPrefix(t, "--- ") && strings.HasPrefix(strings.TrimSpace(t[3:]), "#") {
			// comment lines before `---`: the dump drops them (they come back
			// as missing comments on top)
			if t != "---" {
				return nil, unsupported("comment after document start marker")
			}
			p.doc.explicitStart = true
			p.pos++
			p.doc.head = p.takeGap()
		} else if strings.HasPrefix(t, "---") || strings.HasPrefix(t, "%") {
			return nil, unsupported("directive or inline document start")
		}
	}
	if p.done() {
		return nil, unsupported("empty document")
	}
	p.started = true
	ind, _ := p.peek()
	root, err := p.parseBlock(ind, -1)
	if err != nil {
		return nil, err
	}
	p.doc.root = root
	if !p.done() {
		t := strings.TrimRight(string(p.lines[p.pos]), " ")
		if t == "..." {
			p.doc.explicitEnd = true
			p.pos++
			if !p.done() {
				return nil, unsupported("content after document end")
			}
		} else {
			return nil, unsupported("unparsed content at line " + itoa(p.pos+1))
		}
	}
	return p.doc, nil
}

func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	var b []byte
	for i > 0 {
		b = append([]byte{byte('0' + i%10)}, b...)
		i /= 10
	}
	return string(b)
}

// parseBlock parses the block collection whose first line is the next
// content line, at indentation col. parentCol is the indentation of the
// owning construct (used for plain-scalar continuation detection).
func (p *parser) parseBlock(col, parentCol int) (*node, error) {
	l := p.lines[p.pos]
	rest := l[col:]
	if isSeqStart(rest) {
		return p.parseSeq(col)
	}
	if isMapKeyStart(rest) {
		return p.parseMap(col)
	}
	return nil, unsupported("block scalar or multi-line plain scalar at line " + itoa(p.pos+1))
}

func isSeqStart(rest []rune) bool {
	return len(rest) >= 1 && rest[0] == '-' && (len(rest) == 1 || rest[1] == ' ')
}

// isMapKeyStart reports whether rest begins with `key:` (block mapping entry).
func isMapKeyStart(rest []rune) bool {
	if len(rest) == 0 {
		return false
	}
	switch rest[0] {
	case '"', '\'':
		end, ok := scanQuoted(rest, 0)
		if !ok {
			return false
		}
		i := end
		for i < len(rest) && rest[i] == ' ' {
			i++
		}
		return i < len(rest) && rest[i] == ':' && (i+1 == len(rest) || rest[i+1] == ' ')
	case '-', '?', '#', '[', '{', '&', '*', '!', '|', '>', '%', '@', '`':
		if rest[0] == '-' && len(rest) > 1 && rest[1] != ' ' {
			break
		}
		return false
	}
	for i := 0; i < len(rest); i++ {
		if rest[i] == '#' && i > 0 && rest[i-1] == ' ' {
			return false
		}
		if rest[i] == ':' && (i+1 == len(rest) || rest[i+1] == ' ') {
			return true
		}
	}
	return false
}

// scanQuoted returns the index just after the closing quote of the quoted
// scalar starting at rest[start].
func scanQuoted(rest []rune, start int) (int, bool) {
	q := rest[start]
	for i := start + 1; i < len(rest); i++ {
		switch {
		case q == '"' && rest[i] == '\\':
			i++
		case rest[i] == q:
			if q == '\'' && i+1 < len(rest) && rest[i+1] == '\'' {
				i++
				continue
			}
			return i + 1, true
		}
	}
	return 0, false
}

// parseMap parses a block mapping whose keys start at column col; the
// current line holds the first key at that column (it may be preceded by a
// sequence dash on the same line).
func (p *parser) parseMap(col int) (*node, error) {
	m := &node{kind: kMap}
	first := true
	for {
		if p.done() {
			break
		}
		l := p.lines[p.pos]
		if !first {
			if leadingSpaces(l) != col || isBlankLine(l) || isCommentLine(l) {
				if leadingSpaces(l) > col && !isBlankLine(l) && !isCommentLine(l) {
					return nil, unsupported("over-indented line " + itoa(p.pos+1))
				}
				break
			}
			if isSeqStart(l[col:]) {
				break
			}
		}
		first = false
		rest := l[col:]
		if !isMapKeyStart(rest) {
			return nil, unsupported("not a mapping entry at line " + itoa(p.pos+1))
		}
		key, afterKey, err := parseKey(rest, col)
		if err != nil {
			return nil, err
		}
		e := &entry{key: key}
		line := p.pos
		val, r, err := p.parseValue(l, afterKey, col, col, line)
		if err != nil {
			return nil, err
		}
		e.value, e.rec = val, r
		m.entries = append(m.entries, e)
	}
	return m, nil
}

// parseKey parses a quoted or plain key at rest and returns the column just
// after the colon.
func parseKey(rest []rune, col int) (*node, int, error) {
	if rest[0] == '"' || rest[0] == '\'' {
		end, _ := scanQuoted(rest, 0)
		n, err := decodeQuoted(rest[:end])
		if err != nil {
			return nil, 0, err
		}
		i := end
		for rest[i] != ':' {
			i++
		}
		return n, col + i + 1, nil
	}
	for i := 0; i < len(rest); i++ {
		if rest[i] == ':' && (i+1 == len(rest) || rest[i+1] == ' ') {
			text := strings.TrimRight(string(rest[:i]), " ")
			return &node{kind: kScalar, style: sPlain, raw: text}, col + i + 1, nil
		}
	}
	return nil, 0, unsupported("key without colon")
}

func decodeQuoted(tok []rune) (*node, error) {
	n := &node{kind: kScalar}
	inner := tok[1 : len(tok)-1]
	if tok[0] == '\'' {
		n.style = sSingle
		n.raw = strings.ReplaceAll(string(inner), "''", "'")
		return n, nil
	}
	n.style = sDouble
	var b strings.Builder
	for i := 0; i < len(inner); i++ {
		if inner[i] != '\\' {
			b.WriteRune(inner[i])
			continue
		}
		i++
		if i >= len(inner) {
			return nil, unsupported("bad escape")
		}
		switch c := inner[i]; c {
		case '0':
			b.WriteRune(0)
		case 'a':
			b.WriteRune(7)
		case 'b':
			b.WriteRune(8)
		case 't', '\t':
			b.WriteRune(9)
		case 'n':
			b.WriteRune(10)
		case 'v':
			b.WriteRune(11)
		case 'f':
			b.WriteRune(12)
		case 'r':
			b.WriteRune(13)
		case 'e':
			b.WriteRune(27)
		case ' ':
			b.WriteRune(' ')
		case '"':
			b.WriteRune('"')
		case '/':
			b.WriteRune('/')
		case '\\':
			b.WriteRune('\\')
		case 'N':
			b.WriteRune(0x85)
		case '_':
			b.WriteRune(0xa0)
		case 'L':
			b.WriteRune(0x2028)
		case 'P':
			b.WriteRune(0x2029)
		case 'x', 'u', 'U':
			w := map[rune]int{'x': 2, 'u': 4, 'U': 8}[c]
			if i+1+w > len(inner) {
				return nil, unsupported("bad escape")
			}
			v := 0
			for _, h := range inner[i+1 : i+1+w] {
				d := hexVal(h)
				if d < 0 {
					return nil, unsupported("bad escape")
				}
				v = v*16 + d
			}
			b.WriteRune(rune(v))
			i += w
		default:
			return nil, unsupported("bad escape")
		}
	}
	n.raw = b.String()
	return n, nil
}

func hexVal(r rune) int {
	switch {
	case r >= '0' && r <= '9':
		return int(r - '0')
	case r >= 'a' && r <= 'f':
		return int(r-'a') + 10
	case r >= 'A' && r <= 'F':
		return int(r-'A') + 10
	}
	return -1
}

// parseSeq parses a block sequence with dashes at column col.
func (p *parser) parseSeq(col int) (*node, error) {
	s := &node{kind: kSeq}
	first := true
	for {
		if p.done() {
			break
		}
		l := p.lines[p.pos]
		if !first {
			if leadingSpaces(l) != col || isBlankLine(l) || isCommentLine(l) || !isSeqStart(l[col:]) {
				if leadingSpaces(l) > col && !isBlankLine(l) && !isCommentLine(l) {
					return nil, unsupported("over-indented line " + itoa(p.pos+1))
				}
				break
			}
		}
		first = false
		rest := l[col:]
		if !isSeqStart(rest) {
			return nil, unsupported("not a sequence item at line " + itoa(p.pos+1))
		}
		it := &item{}
		valueCol := col + 1
		for valueCol < len(l) && l[valueCol] == ' ' {
			valueCol++
		}
		line := p.pos
		switch {
		case valueCol < len(l) && l[valueCol] != '#' && isSeqStart(l[valueCol:]) && valueCol > col+1:
			v, err := p.parseSeq(valueCol)
			if err != nil {
				return nil, err
			}
			it.value = v
			it.compact()
		case valueCol < len(l) && l[valueCol] != '#' && isMapKeyStart(l[valueCol:]):
			v, err := p.parseMap(valueCol)
			if err != nil {
				return nil, err
			}
			it.value = v
			it.compactFlag = true
		default:
			after := col + 1
			v, r, err := p.parseValue(l, after, col, col, line)
			if err != nil {
				return nil, err
			}
			it.value, it.rec = v, r
		}
		s.items = append(s.items, it)
	}
	return s, nil
}

// parseValue parses what follows an indicator (the colon of a key or the dash
// of an item) on line l starting at column from. ownerCol is the column of
// the owning collection (for nested-block detection and block scalars). The
// returned rec holds the end-of-line comment and the gap after the element's
// line(s); nested blocks are parsed after the gap.
func (p *parser) parseValue(l []rune, from, ownerCol, parentCol, line int) (*node, rec, error) {
	var r rec
	i := from
	for i < len(l) && l[i] == ' ' {
		i++
	}
	var anchor string
	if i < len(l) && l[i] == '&' {
		j := i + 1
		for j < len(l) && l[j] != ' ' {
			j++
		}
		anchor = string(l[i+1 : j])
		i = j
		for i < len(l) && l[i] == ' ' {
			i++
		}
	}
	finishLine := func(end int) error {
		// everything after end must be blank or a comment
		k := end
		for k < len(l) && l[k] == ' ' {
			k++
		}
		if k < len(l) {
			if l[k] != '#' || (k == end && k > 0 && l[k-1] != ' ') {
				return unsupported("unexpected text after value at line " + itoa(line+1))
			}
			r.eol = &comment{col: k, text: string(l[k:])}
			p.note(string(l[k:]))
		}
		return nil
	}
	setAnchor := func(n *node) *node {
		if anchor != "" {
			n.anchor = anchor
			p.anchors[anchor] = n
		}
		return n
	}
	switch {
	case i >= len(l) || l[i] == '#':
		if err := finishLine(i); err != nil {
			return nil, r, err
		}
		if r.eol != nil {
			p.doc.crlfRisk = true
		}
		p.pos++
		r.gap = p.takeGap()
		// nested block?
		if !p.done() {
			ind := leadingSpaces(p.lines[p.pos])
			rest := p.lines[p.pos][ind:]
			if ind > ownerCol || (ind == ownerCol && isSeqStart(rest) && ownerCol >= 0 && p.isMapOwner(l, from, ownerCol)) {
				var n *node
				var err error
				if isSeqStart(rest) {
					n, err = p.parseSeq(ind)
				} else if isMapKeyStart(rest) {
					n, err = p.parseMap(ind)
				} else {
					return nil, r, unsupported("scalar on its own line at line " + itoa(p.pos+1))
				}
				if err != nil {
					return nil, r, err
				}
				return setAnchor(n), r, nil
			}
		}
		return setAnchor(&node{kind: kScalar, style: sPlain, raw: ""}), r, nil
	case l[i] == '*':
		j := i + 1
		for j < len(l) && l[j] != ' ' {
			j++
		}
		name := string(l[i+1 : j])
		target := p.anchors[name]
		if target == nil {
			return nil, r, unsupported("alias to unknown anchor")
		}
		if err := finishLine(j); err != nil {
			return nil, r, err
		}
		p.pos++
		r.gap = p.takeGap()
		return &node{kind: kAlias, name: name, target: target}, r, nil
	case l[i] == '|' || l[i] == '>':
		folded := l[i] == '>'
		hdr := i + 1
		j := hdr
		for j < len(l) && l[j] != ' ' {
			j++
		}
		indicator := string(l[hdr:j])
		if err := finishLine(j); err != nil {
			return nil, r, err
		}
		p.pos++
		n, err := p.parseLiteral(indicator, parentCol, folded)
		if err != nil {
			return nil, r, err
		}
		r.gap = p.takeGap()
		return setAnchor(n), r, nil
	case l[i] == '!' && anchor == "":
		j := i
		for j < len(l) && l[j] != ' ' {
			j++
		}
		tag, verbatim, ok := standardTag(string(l[i:j]))
		if !ok {
			return nil, r, unsupported("tag at line " + itoa(line+1))
		}
		k := j
		for k < len(l) && l[k] == ' ' {
			k++
		}
		var n *node
		if k >= len(l) || l[k] == '#' {
			if err := finishLine(j); err != nil {
				return nil, r, err
			}
			p.pos++
			if err := p.rejectContinuation(ownerCol); err != nil {
				return nil, r, err
			}
			r.gap = p.takeGap()
			n = &node{kind: kScalar, style: sPlain, raw: ""}
		} else {
			if l[k] == '&' || l[k] == '!' || l[k] == '[' || l[k] == '{' || l[k] == '|' || l[k] == '>' || l[k] == '*' {
				return nil, r, unsupported("tagged node at line " + itoa(line+1))
			}
			var err error
			n, r, err = p.parseValue(l, k, ownerCol, parentCol, line)
			if err != nil {
				return nil, r, err
			}
		}
		if n.kind != kScalar {
			return nil, r, unsupported("tagged collection at line " + itoa(line+1))
		}
		n.tag, n.verbatimTag = tag, verbatim
		return n, r, nil
	case l[i] == '!' || l[i] == '%' || l[i] == '@' || l[i] == '`':
		return nil, r, unsupported("tag or reserved indicator at line " + itoa(line+1))
	case l[i] == '[' || l[i] == '{':
		n, endLine, endCol, inner, err := p.parseFlowAcrossLines(line, i)
		if err != nil {
			return nil, r, err
		}
		n.flowComments = inner
		last := p.lines[endLine]
		l = last
		line = endLine
		if err := finishLine(endCol); err != nil {
			return nil, r, err
		}
		p.pos = endLine + 1
		r.gap = p.takeGap()
		return setAnchor(n), r, nil
	case l[i] == '"' || l[i] == '\'':
		end, ok := scanQuoted(l, i)
		if !ok {
			return nil, r, unsupported("multi-line quoted scalar at line " + itoa(line+1))
		}
		n, err := decodeQuoted(l[i:end])
		if err != nil {
			return nil, r, err
		}
		if err := finishLine(end); err != nil {
			return nil, r, err
		}
		p.pos++
		if err := p.rejectContinuation(ownerCol); err != nil {
			return nil, r, err
		}
		r.gap = p.takeGap()
		return setAnchor(n), r, nil
	}
	// plain scalar
	end := len(l)
	for k := i; k < len(l); k++ {
		if l[k] == '#' && k > i && l[k-1] == ' ' {
			end = k
			break
		}
	}
	text := strings.TrimRight(string(l[i:end]), " ")
	if text == "" {
		return nil, r, unsupported("empty value")
	}
	if strings.Contains(text, ": ") || strings.HasSuffix(text, ":") {
		return nil, r, unsupported("ambiguous plain scalar")
	}
	if end < len(l) {
		r.eol = &comment{col: end, text: string(l[end:])}
		p.note(string(l[end:]))
	}
	p.pos++
	if err := p.rejectContinuation(ownerCol); err != nil {
		return nil, r, err
	}
	r.gap = p.takeGap()
	return setAnchor(&node{kind: kScalar, style: sPlain, raw: text}), r, nil
}

// isMapOwner is true when the line holding the value is a mapping entry (a
// sequence may then sit at the key's own column), not a sequence item.
func (p *parser) isMapOwner(l []rune, from, ownerCol int) bool {
	return from > 0 && from-1 < len(l) && l[from-1] == ':'
}

// rejectContinuation fails when the next content line is more indented than
// the owner, i.e. continues a multi-line plain/quoted scalar.
func (p *parser) rejectContinuation(ownerCol int) error {
	for k := p.pos; k < len(p.lines); k++ {
		l := p.lines[k]
		if isBlankLine(l) || isCommentLine(l) {
			continue
		}
		if leadingSpaces(l) > ownerCol {
			return unsupported("multi-line scalar at line " + itoa(k+1))
		}
		return nil
	}
	return nil
}

// parseLiteral reads the body of a `|` block scalar whose header was just
// consumed; parentCol is the owner's indentation.
func (p *parser) parseLiteral(indicator string, parentCol int, folded bool) (*node, error) {
	chomp := byte(0)
	explicit := 0
	for _, c := range indicator {
		switch {
		case c == '-' || c == '+':
			chomp = byte(c)
		case c >= '1' && c <= '9':
			explicit = int(c - '0')
		default:
			return nil, unsupported("block scalar indicator")
		}
	}
	if parentCol < 0 {
		parentCol = 0
	}
	start := p.pos
	contentIndent := -1
	if explicit > 0 {
		contentIndent = parentCol + explicit
	}
	var body [][]rune
	k := start
	for k < len(p.lines) {
		l := p.lines[k]
		if isBlankLine(l) {
			body = append(body, nil)
			k++
			continue
		}
		ind := leadingSpaces(l)
		if contentIndent < 0 {
			if ind <= parentCol {
				break
			}
			contentIndent = ind
		}
		if ind < contentIndent {
			break
		}
		body = append(body, l[contentIndent:])
		k++
	}
	// trailing blank lines
	trailing := 0
	for len(body) > 0 && body[len(body)-1] == nil {
		body = body[:len(body)-1]
		trailing++
		k--
	}
	p.pos = k
	var text strings.Builder
	for i, b := range body {
		if i > 0 {
			text.WriteByte('\n')
		}
		text.WriteString(string(b))
	}
	value := text.String()
	switch {
	case len(body) == 0:
		if chomp == '+' {
			value = strings.Repeat("\n", trailing)
		}
	case chomp == '-':
	case chomp == '+':
		value += "\n" + strings.Repeat("\n", trailing)
	default:
		value += "\n"
	}
	if chomp == '+' {
		p.pos = k + trailing
	} else {
		// clip/strip: trailing blank lines stay in the document as blank lines
		p.pos = k
	}
	if folded {
		return foldedNode(body, chomp, trailing)
	}
	n := &node{kind: kScalar, style: sLiteral, raw: value}
	return n, nil
}

// foldedNode is scan_block_scalar for '>' in round-trip mode (a '\a'
// marks every line break the scanner meets between content lines) followed
// by the construction of a FoldedScalarString: the value without the marks
// and the fold positions.
func foldedNode(body [][]rune, chomp byte, trailing int) (*node, error) {
	var chunks []rune
	i := 0
	breaks := 0
	for i < len(body) && body[i] == nil {
		breaks++
		i++
	}
	lineBreak := ""
	for i < len(body) {
		for ; breaks > 0; breaks-- {
			chunks = append(chunks, '\n')
		}
		line := body[i]
		for _, r := range line {
			if r == 0x85 || r == 0x2028 || r == 0x2029 || r == '\a' {
				return nil, unsupported("special line break in a folded scalar")
			}
		}
		leadingNonSpace := len(line) > 0 && line[0] != ' ' && line[0] != '\t'
		chunks = append(chunks, line...)
		lineBreak = "\n"
		i++
		for i < len(body) && body[i] == nil {
			breaks++
			i++
		}
		if i < len(body) {
			chunks = append(chunks, '\a')
			next := body[i]
			if leadingNonSpace && len(next) > 0 && next[0] != ' ' && next[0] != '\t' {
				if breaks == 0 {
					chunks = append(chunks, ' ')
				}
			} else {
				chunks = append(chunks, '\n')
			}
		}
	}
	if chomp != '-' {
		chunks = append(chunks, []rune(lineBreak)...)
	}
	if chomp == '+' {
		for k := 0; k < trailing; k++ {
			chunks = append(chunks, '\n')
		}
	}
	var clean []rune
	var pos []int
	for _, r := range chunks {
		if r == '\a' {
			pos = append(pos, len(clean))
			continue
		}
		clean = append(clean, r)
	}
	return &node{kind: kScalar, style: sFolded, raw: string(clean), foldPos: pos}, nil
}

// parseFlowAcrossLines parses a flow collection starting at (line, col),
// possibly spanning several lines. It returns the node, the line and column
// just after the closing bracket, and the comments met inside.
func (p *parser) parseFlowAcrossLines(line, col int) (*node, int, int, []string, error) {
	var buf []rune
	type loc struct{ line, col int }
	var locs []loc
	var inner []string
	cur := line
	for cur < len(p.lines) {
		l := p.lines[cur]
		startCol := 0
		if cur == line {
			startCol = col
		}
		inQuote := rune(0)
		for k := startCol; k < len(l); k++ {
			c := l[k]
			if inQuote != 0 {
				buf = append(buf, c)
				locs = append(locs, loc{cur, k})
				if inQuote == '"' && c == '\\' && k+1 < len(l) {
					k++
					buf = append(buf, l[k])
					locs = append(locs, loc{cur, k})
				} else if c == inQuote {
					if inQuote == '\'' && k+1 < len(l) && l[k+1] == '\'' {
						k++
						buf = append(buf, l[k])
						locs = append(locs, loc{cur, k})
					} else {
						inQuote = 0
					}
				}
				continue
			}
			if c == '#' && (k == 0 || l[k-1] == ' ') {
				// comment inside the flow collection: only valid while it is open
				if flowBalanced(buf) && len(buf) > 0 {
					break
				}
				inner = append(inner, string(l[k:]))
				p.note(string(l[k:]))
				break
			}
			if (c == '"' || c == '\'') && flowQuoteStart(buf) {
				inQuote = c
			}
			buf = append(buf, c)
			locs = append(locs, loc{cur, k})
			if flowBalanced(buf) {
				// closing bracket reached
				fp := &flowParser{s: buf}
				n, end, err := fp.parseNode()
				if err != nil {
					return nil, 0, 0, nil, err
				}
				_ = end
				end2 := locs[len(locs)-1]
				p.attachAliases(n)
				return n, end2.line, end2.col + 1, inner, nil
			}
		}
		if inQuote != 0 {
			return nil, 0, 0, nil, unsupported("multi-line quoted scalar in flow collection")
		}
		buf = append(buf, ' ')
		locs = append(locs, loc{cur, len(l)})
		cur++
	}
	return nil, 0, 0, nil, unsupported("unterminated flow collection")
}

func flowQuoteStart(buf []rune) bool {
	for i := len(buf) - 1; i >= 0; i-- {
		switch buf[i] {
		case ' ':
			continue
		case '[', '{', ',', ':':
			return true
		}
		return false
	}
	return true
}

// flowBalanced reports whether buf (starting with an opening bracket) has
// its brackets closed, ignoring quoted text.
func flowBalanced(buf []rune) bool {
	depth := 0
	var q rune
	for i := 0; i < len(buf); i++ {
		c := buf[i]
		if q != 0 {
			if q == '"' && c == '\\' {
				i++
			} else if c == q {
				if q == '\'' && i+1 < len(buf) && buf[i+1] == '\'' {
					i++
				} else {
					q = 0
				}
			}
			continue
		}
		switch c {
		case '"', '\'':
			if flowQuoteStart(buf[:i]) {
				q = c
			}
		case '[', '{':
			depth++
		case ']', '}':
			depth--
			if depth == 0 {
				return true
			}
		}
	}
	return false
}

func (p *parser) attachAliases(n *node) {
	switch n.kind {
	case kAlias:
		n.target = p.anchors[n.name]
	case kSeq:
		for _, it := range n.items {
			p.attachAliases(it.value)
		}
	case kMap:
		for _, e := range n.entries {
			p.attachAliases(e.value)
		}
	}
	if n.anchor != "" {
		p.anchors[n.anchor] = n
	}
}

type flowParser struct {
	s   []rune
	pos int
}

func (f *flowParser) skip() {
	for f.pos < len(f.s) && f.s[f.pos] == ' ' {
		f.pos++
	}
}

func (f *flowParser) parseNode() (*node, int, error) {
	f.skip()
	if f.pos >= len(f.s) {
		return nil, 0, unsupported("empty flow node")
	}
	var anchor string
	if f.s[f.pos] == '&' {
		j := f.pos + 1
		for j < len(f.s) && f.s[j] != ' ' {
			j++
		}
		anchor = string(f.s[f.pos+1 : j])
		f.pos = j
		f.skip()
	}
	var n *node
	switch c := f.s[f.pos]; c {
	case '[':
		f.pos++
		n = &node{kind: kSeq, flow: true}
		for {
			f.skip()
			if f.pos >= len(f.s) {
				return nil, 0, unsupported("unterminated flow sequence")
			}
			if f.s[f.pos] == ']' {
				f.pos++
				break
			}
			v, _, err := f.parseNode()
			if err != nil {
				return nil, 0, err
			}
			f.skip()
			if f.pos < len(f.s) && f.s[f.pos] == ':' && (f.pos+1 >= len(f.s) || f.s[f.pos+1] == ' ') {
				return nil, 0, unsupported("single pair in flow sequence")
			}
			n.items = append(n.items, &item{value: v})
			f.skip()
			if f.pos < len(f.s) && f.s[f.pos] == ',' {
				f.pos++
			}
		}
	case '{':
		f.pos++
		n = &node{kind: kMap, flow: true}
		for {
			f.skip()
			if f.pos >= len(f.s) {
				return nil, 0, unsupported("unterminated flow mapping")
			}
			if f.s[f.pos] == '}' {
				f.pos++
				break
			}
			k, _, err := f.parseNode()
			if err != nil {
				return nil, 0, err
			}
			f.skip()
			var v *node
			if f.pos < len(f.s) && f.s[f.pos] == ':' {
				f.pos++
				f.skip()
				if f.pos < len(f.s) && (f.s[f.pos] == ',' || f.s[f.pos] == '}') {
					v = &node{kind: kScalar, style: sPlain}
				} else {
					v, _, err = f.parseNode()
					if err != nil {
						return nil, 0, err
					}
				}
			} else {
				v = &node{kind: kScalar, style: sPlain}
			}
			n.entries = append(n.entries, &entry{key: k, value: v})
			f.skip()
			if f.pos < len(f.s) && f.s[f.pos] == ',' {
				f.pos++
			}
		}
	case '"', '\'':
		end, ok := scanQuoted(f.s, f.pos)
		if !ok {
			return nil, 0, unsupported("unterminated quoted scalar")
		}
		q, err := decodeQuoted(f.s[f.pos:end])
		if err != nil {
			return nil, 0, err
		}
		f.pos = end
		n = q
	case '*':
		j := f.pos + 1
		for j < len(f.s) && f.s[j] != ' ' && f.s[j] != ',' && f.s[j] != ']' && f.s[j] != '}' {
			j++
		}
		n = &node{kind: kAlias, name: string(f.s[f.pos+1 : j])}
		f.pos = j
	case '!', '|', '>', '%', '@', '`':
		return nil, 0, unsupported("tag or indicator in flow collection")
	default:
		j := f.pos
		for j < len(f.s) {
			c := f.s[j]
			if c == ',' || c == '[' || c == ']' || c == '{' || c == '}' {
				break
			}
			if c == ':' && (j+1 >= len(f.s) || f.s[j+1] == ' ' || f.s[j+1] == ',' || f.s[j+1] == ']' || f.s[j+1] == '}') {
				break
			}
			j++
		}
		text := strings.TrimRightFunc(string(f.s[f.pos:j]), unicode.IsSpace)
		if text == "" {
			return nil, 0, unsupported("empty flow scalar")
		}
		n = &node{kind: kScalar, style: sPlain, raw: text}
		f.pos = j
	}
	if anchor != "" {
		n.anchor = anchor
	}
	return n, f.pos, nil
}

// standardTag recognises the scalar tags the editor reproduces: `!!str`,
// `!!int`, `!!float`, `!!bool`, `!!null` and their verbatim
// `!<tag:yaml.org,2002:...>` forms.
func standardTag(t string) (tag string, verbatim bool, ok bool) {
	switch {
	case strings.HasPrefix(t, "!<tag:yaml.org,2002:") && strings.HasSuffix(t, ">"):
		tag, verbatim = strings.TrimSuffix(strings.TrimPrefix(t, "!<tag:yaml.org,2002:"), ">"), true
	case strings.HasPrefix(t, "!!"):
		tag = t[2:]
	default:
		return "", false, false
	}
	switch tag {
	case "str", "int", "float", "bool", "null":
		return tag, verbatim, true
	}
	return "", false, false
}
