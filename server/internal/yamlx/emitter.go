package yamlx

import (
	"fmt"
	"strings"
	"unicode/utf8"
)

// This file is a line-by-line port of PyYAML 6.0's yaml/emitter.py (the pure
// Python emitter behind yaml.dump/safe_dump). Only the paths the application
// can reach are present: canonical output, other line breaks, version and tag
// directives and the folded block style are not.

type eventKind int

const (
	evStreamStart eventKind = iota
	evStreamEnd
	evDocumentStart
	evDocumentEnd
	evAlias
	evScalar
	evSequenceStart
	evSequenceEnd
	evMappingStart
	evMappingEnd
)

type event struct {
	kind      eventKind
	anchor    string
	hasAnchor bool
	tag       string
	hasTag    bool
	implicit  [2]bool // scalars: both flags; collections: [0]
	value     string
	style     byte // 0 (plain/any), '"', '\'', '|'
	flow      bool
	explicit  bool
	mark      *Marked
}

func (ev *event) isNode() bool {
	return ev.kind == evAlias || ev.kind == evScalar || ev.kind == evSequenceStart || ev.kind == evMappingStart
}

type emitterError struct{ msg string }

func (e emitterError) Error() string { return e.msg }

func fail(format string, args ...any) { panic(emitterError{fmt.Sprintf(format, args...)}) }

type scalarAnalysis struct {
	scalar            []rune
	empty             bool
	multiline         bool
	allowFlowPlain    bool
	allowBlockPlain   bool
	allowSingleQuoted bool
	allowDoubleQuoted bool
	allowBlock        bool
}

const noChar rune = -1

type emitter struct {
	out   strings.Builder
	chars int // runes written so far

	states []func()
	state  func()
	events []*event
	event  *event

	indents []int // -1 stands for Python's None
	indent  int

	flowLevel        int
	rootContext      bool
	sequenceContext  bool
	mappingContext   bool
	simpleKeyContext bool

	line       int
	column     int
	whitespace bool
	indention  bool
	openEnded  bool

	allowUnicode bool
	bestIndent   int
	bestWidth    int
	// indentSequences ignores the "indentless" hint, like the application's
	// IndentDumper (increase_indent(flow, False)).
	indentSequences bool

	preparedAnchor    string
	hasPreparedAnchor bool
	preparedTag       string
	hasPreparedTag    bool

	analysis *scalarAnalysis
	style    byte
	styleSet bool

	onMarked func(m Marked, style byte, start, end int)
}

func newEmitter(o DumpOptions) *emitter {
	e := &emitter{indent: -1, whitespace: true, indention: true, bestIndent: 2, bestWidth: 80}
	e.allowUnicode = o.AllowUnicode
	e.indentSequences = o.IndentSequences
	if o.Indent > 1 && o.Indent < 10 {
		e.bestIndent = o.Indent
	}
	if o.Width > 0 && o.Width > e.bestIndent*2 {
		e.bestWidth = o.Width
	}
	e.onMarked = o.OnMarked
	e.state = e.expectStreamStart
	return e
}

func (e *emitter) emit(ev *event) {
	e.events = append(e.events, ev)
	for !e.needMoreEvents() {
		e.event = e.events[0]
		e.events = e.events[1:]
		e.state()
		e.event = nil
	}
}

func (e *emitter) needMoreEvents() bool {
	if len(e.events) == 0 {
		return true
	}
	switch e.events[0].kind {
	case evDocumentStart:
		return e.needEvents(1)
	case evSequenceStart:
		return e.needEvents(2)
	case evMappingStart:
		return e.needEvents(3)
	}
	return false
}

func (e *emitter) needEvents(count int) bool {
	level := 0
	for _, ev := range e.events[1:] {
		switch ev.kind {
		case evDocumentStart, evSequenceStart, evMappingStart:
			level++
		case evDocumentEnd, evSequenceEnd, evMappingEnd:
			level--
		case evStreamEnd:
			level = -1
		}
		if level < 0 {
			return false
		}
	}
	return len(e.events) < count+1
}

func (e *emitter) increaseIndent(flow, indentless bool) {
	e.indents = append(e.indents, e.indent)
	if e.indent < 0 {
		if flow {
			e.indent = e.bestIndent
		} else {
			e.indent = 0
		}
	} else if !indentless {
		e.indent += e.bestIndent
	}
}

func (e *emitter) popIndent() {
	e.indent = e.indents[len(e.indents)-1]
	e.indents = e.indents[:len(e.indents)-1]
}

func (e *emitter) popState() {
	e.state = e.states[len(e.states)-1]
	e.states = e.states[:len(e.states)-1]
}

func (e *emitter) pushState(f func()) { e.states = append(e.states, f) }

// Stream and document handlers.

func (e *emitter) expectStreamStart() {
	if e.event.kind != evStreamStart {
		fail("expected StreamStartEvent, but got %v", e.event.kind)
	}
	e.state = e.expectFirstDocumentStart
}

func (e *emitter) expectNothing() { fail("expected nothing, but got %v", e.event.kind) }

func (e *emitter) expectFirstDocumentStart() { e.expectDocumentStart(true) }

func (e *emitter) expectDocumentStartLater() { e.expectDocumentStart(false) }

func (e *emitter) expectDocumentStart(first bool) {
	switch e.event.kind {
	case evDocumentStart:
		implicit := first && !e.event.explicit && !e.checkEmptyDocument()
		if !implicit {
			e.writeIndent()
			e.writeIndicator("---", true, false, false)
		}
		e.state = e.expectDocumentRoot
	case evStreamEnd:
		if e.openEnded {
			e.writeIndicator("...", true, false, false)
			e.writeIndent()
		}
		e.state = e.expectNothing
	default:
		fail("expected DocumentStartEvent, but got %v", e.event.kind)
	}
}

func (e *emitter) expectDocumentEnd() {
	if e.event.kind != evDocumentEnd {
		fail("expected DocumentEndEvent, but got %v", e.event.kind)
	}
	e.writeIndent()
	if e.event.explicit {
		e.writeIndicator("...", true, false, false)
		e.writeIndent()
	}
	e.state = e.expectDocumentStartLater
}

func (e *emitter) expectDocumentRoot() {
	e.pushState(e.expectDocumentEnd)
	e.expectNode(true, false, false, false)
}

// Node handlers.

func (e *emitter) expectNode(root, sequence, mapping, simpleKey bool) {
	e.rootContext = root
	e.sequenceContext = sequence
	e.mappingContext = mapping
	e.simpleKeyContext = simpleKey
	switch e.event.kind {
	case evAlias:
		e.expectAlias()
	case evScalar, evSequenceStart, evMappingStart:
		e.processAnchor("&")
		e.processTag()
		switch e.event.kind {
		case evScalar:
			e.expectScalar()
		case evSequenceStart:
			if e.flowLevel > 0 || e.event.flow || e.checkEmptySequence() {
				e.expectFlowSequence()
			} else {
				e.expectBlockSequence()
			}
		case evMappingStart:
			if e.flowLevel > 0 || e.event.flow || e.checkEmptyMapping() {
				e.expectFlowMapping()
			} else {
				e.expectBlockMapping()
			}
		}
	default:
		fail("expected NodeEvent, but got %v", e.event.kind)
	}
}

func (e *emitter) expectAlias() {
	if !e.event.hasAnchor {
		fail("anchor is not specified for alias")
	}
	e.processAnchor("*")
	e.popState()
}

func (e *emitter) expectScalar() {
	e.increaseIndent(true, false)
	e.processScalar()
	e.popIndent()
	e.popState()
}

// Flow sequence handlers.

func (e *emitter) expectFlowSequence() {
	e.writeIndicator("[", true, true, false)
	e.flowLevel++
	e.increaseIndent(true, false)
	e.state = e.expectFirstFlowSequenceItem
}

func (e *emitter) expectFirstFlowSequenceItem() {
	if e.event.kind == evSequenceEnd {
		e.popIndent()
		e.flowLevel--
		e.writeIndicator("]", false, false, false)
		e.popState()
		return
	}
	if e.column > e.bestWidth {
		e.writeIndent()
	}
	e.pushState(e.expectFlowSequenceItem)
	e.expectNode(false, true, false, false)
}

func (e *emitter) expectFlowSequenceItem() {
	if e.event.kind == evSequenceEnd {
		e.popIndent()
		e.flowLevel--
		e.writeIndicator("]", false, false, false)
		e.popState()
		return
	}
	e.writeIndicator(",", false, false, false)
	if e.column > e.bestWidth {
		e.writeIndent()
	}
	e.pushState(e.expectFlowSequenceItem)
	e.expectNode(false, true, false, false)
}

// Flow mapping handlers.

func (e *emitter) expectFlowMapping() {
	e.writeIndicator("{", true, true, false)
	e.flowLevel++
	e.increaseIndent(true, false)
	e.state = e.expectFirstFlowMappingKey
}

func (e *emitter) expectFirstFlowMappingKey() {
	if e.event.kind == evMappingEnd {
		e.popIndent()
		e.flowLevel--
		e.writeIndicator("}", false, false, false)
		e.popState()
		return
	}
	if e.column > e.bestWidth {
		e.writeIndent()
	}
	e.flowMappingKeyBody()
}

func (e *emitter) flowMappingKeyBody() {
	if e.checkSimpleKey() {
		e.pushState(e.expectFlowMappingSimpleValue)
		e.expectNode(false, false, true, true)
	} else {
		e.writeIndicator("?", true, false, false)
		e.pushState(e.expectFlowMappingValue)
		e.expectNode(false, false, true, false)
	}
}

func (e *emitter) expectFlowMappingKey() {
	if e.event.kind == evMappingEnd {
		e.popIndent()
		e.flowLevel--
		e.writeIndicator("}", false, false, false)
		e.popState()
		return
	}
	e.writeIndicator(",", false, false, false)
	if e.column > e.bestWidth {
		e.writeIndent()
	}
	e.flowMappingKeyBody()
}

func (e *emitter) expectFlowMappingSimpleValue() {
	e.writeIndicator(":", false, false, false)
	e.pushState(e.expectFlowMappingKey)
	e.expectNode(false, false, true, false)
}

func (e *emitter) expectFlowMappingValue() {
	if e.column > e.bestWidth {
		e.writeIndent()
	}
	e.writeIndicator(":", true, false, false)
	e.pushState(e.expectFlowMappingKey)
	e.expectNode(false, false, true, false)
}

// Block sequence handlers.

func (e *emitter) expectBlockSequence() {
	indentless := e.mappingContext && !e.indention
	if e.indentSequences {
		indentless = false
	}
	e.increaseIndent(false, indentless)
	e.state = e.expectFirstBlockSequenceItem
}

func (e *emitter) expectFirstBlockSequenceItem() { e.expectBlockSequenceItem(true) }

func (e *emitter) expectBlockSequenceItemLater() { e.expectBlockSequenceItem(false) }

func (e *emitter) expectBlockSequenceItem(first bool) {
	if !first && e.event.kind == evSequenceEnd {
		e.popIndent()
		e.popState()
		return
	}
	e.writeIndent()
	e.writeIndicator("-", true, false, true)
	e.pushState(e.expectBlockSequenceItemLater)
	e.expectNode(false, true, false, false)
}

// Block mapping handlers.

func (e *emitter) expectBlockMapping() {
	e.increaseIndent(false, false)
	e.state = e.expectFirstBlockMappingKey
}

func (e *emitter) expectFirstBlockMappingKey() { e.expectBlockMappingKey(true) }

func (e *emitter) expectBlockMappingKeyLater() { e.expectBlockMappingKey(false) }

func (e *emitter) expectBlockMappingKey(first bool) {
	if !first && e.event.kind == evMappingEnd {
		e.popIndent()
		e.popState()
		return
	}
	e.writeIndent()
	if e.checkSimpleKey() {
		e.pushState(e.expectBlockMappingSimpleValue)
		e.expectNode(false, false, true, true)
	} else {
		e.writeIndicator("?", true, false, true)
		e.pushState(e.expectBlockMappingValue)
		e.expectNode(false, false, true, false)
	}
}

func (e *emitter) expectBlockMappingSimpleValue() {
	e.writeIndicator(":", false, false, false)
	e.pushState(e.expectBlockMappingKeyLater)
	e.expectNode(false, false, true, false)
}

func (e *emitter) expectBlockMappingValue() {
	e.writeIndent()
	e.writeIndicator(":", true, false, true)
	e.pushState(e.expectBlockMappingKeyLater)
	e.expectNode(false, false, true, false)
}

// Checkers.

func (e *emitter) checkEmptySequence() bool {
	return e.event.kind == evSequenceStart && len(e.events) > 0 && e.events[0].kind == evSequenceEnd
}

func (e *emitter) checkEmptyMapping() bool {
	return e.event.kind == evMappingStart && len(e.events) > 0 && e.events[0].kind == evMappingEnd
}

func (e *emitter) checkEmptyDocument() bool {
	if e.event.kind != evDocumentStart || len(e.events) == 0 {
		return false
	}
	ev := e.events[0]
	return ev.kind == evScalar && !ev.hasAnchor && !ev.hasTag && ev.implicit[0] && ev.value == ""
}

func (e *emitter) checkSimpleKey() bool {
	length := 0
	if e.event.isNode() && e.event.hasAnchor {
		if !e.hasPreparedAnchor {
			e.preparedAnchor, e.hasPreparedAnchor = e.prepareAnchor(e.event.anchor), true
		}
		length += utf8.RuneCountInString(e.preparedAnchor)
	}
	if (e.event.kind == evScalar || e.event.kind == evSequenceStart || e.event.kind == evMappingStart) && e.event.hasTag {
		if !e.hasPreparedTag {
			e.preparedTag, e.hasPreparedTag = e.prepareTag(e.event.tag), true
		}
		length += utf8.RuneCountInString(e.preparedTag)
	}
	if e.event.kind == evScalar {
		if e.analysis == nil {
			e.analysis = analyzeScalar(e.event.value, e.allowUnicode)
		}
		length += len(e.analysis.scalar)
	}
	return length < 128 && (e.event.kind == evAlias ||
		(e.event.kind == evScalar && !e.analysis.empty && !e.analysis.multiline) ||
		e.checkEmptySequence() || e.checkEmptyMapping())
}

// Anchor, tag and scalar processors.

func (e *emitter) processAnchor(indicator string) {
	if !e.event.hasAnchor {
		e.hasPreparedAnchor = false
		return
	}
	if !e.hasPreparedAnchor {
		e.preparedAnchor, e.hasPreparedAnchor = e.prepareAnchor(e.event.anchor), true
	}
	if e.preparedAnchor != "" {
		e.writeIndicator(indicator+e.preparedAnchor, true, false, false)
	}
	e.hasPreparedAnchor = false
}

func (e *emitter) processTag() {
	tag, hasTag := e.event.tag, e.event.hasTag
	if e.event.kind == evScalar {
		if !e.styleSet {
			e.style, e.styleSet = e.chooseScalarStyle(), true
		}
		if (e.style == 0 && e.event.implicit[0]) || (e.style != 0 && e.event.implicit[1]) {
			e.hasPreparedTag = false
			return
		}
		if e.event.implicit[0] && !hasTag {
			tag, hasTag = "!", true
			e.hasPreparedTag = false
		}
	} else if e.event.implicit[0] {
		e.hasPreparedTag = false
		return
	}
	if !hasTag {
		fail("tag is not specified")
	}
	if !e.hasPreparedTag {
		e.preparedTag, e.hasPreparedTag = e.prepareTag(tag), true
	}
	if e.preparedTag != "" {
		e.writeIndicator(e.preparedTag, true, false, false)
	}
	e.hasPreparedTag = false
}

// chooseScalarStyle returns 0 for plain, or '"', '\”, '|'.
func (e *emitter) chooseScalarStyle() byte {
	if e.analysis == nil {
		e.analysis = analyzeScalar(e.event.value, e.allowUnicode)
	}
	a := e.analysis
	if e.event.style == '"' {
		return '"'
	}
	if e.event.style == 0 && e.event.implicit[0] {
		if !(e.simpleKeyContext && (a.empty || a.multiline)) &&
			((e.flowLevel > 0 && a.allowFlowPlain) || (e.flowLevel == 0 && a.allowBlockPlain)) {
			return 0
		}
	}
	if e.event.style == '|' || e.event.style == '>' {
		if e.flowLevel == 0 && !e.simpleKeyContext && a.allowBlock {
			return e.event.style
		}
	}
	if e.event.style == 0 || e.event.style == '\'' {
		if a.allowSingleQuoted && !(e.simpleKeyContext && a.multiline) {
			return '\''
		}
	}
	return '"'
}

func (e *emitter) processScalar() {
	if e.analysis == nil {
		e.analysis = analyzeScalar(e.event.value, e.allowUnicode)
	}
	if !e.styleSet {
		e.style, e.styleSet = e.chooseScalarStyle(), true
	}
	split := !e.simpleKeyContext
	start := e.chars
	switch e.style {
	case '"':
		e.writeDoubleQuoted(e.analysis.scalar, split)
	case '\'':
		e.writeSingleQuoted(e.analysis.scalar, split)
	case '|':
		e.writeLiteral(e.analysis.scalar)
	case '>':
		fail("the folded block style is not supported")
	default:
		e.writePlain(e.analysis.scalar, split)
	}
	if e.event.mark != nil && e.onMarked != nil {
		e.onMarked(*e.event.mark, e.style, start, e.chars)
	}
	e.analysis = nil
	e.styleSet = false
}

// Analyzers.

func (e *emitter) prepareTag(tag string) string {
	if tag == "" {
		fail("tag must not be empty")
	}
	if tag == "!" {
		return tag
	}
	handle, suffix := "", tag
	switch {
	case strings.HasPrefix(tag, "tag:yaml.org,2002:") && len("tag:yaml.org,2002:") < len(tag):
		handle, suffix = "!!", tag[len("tag:yaml.org,2002:"):]
	case strings.HasPrefix(tag, "!"):
		handle, suffix = "!", tag[1:]
	}
	var b strings.Builder
	for _, ch := range suffix {
		switch {
		case ch >= '0' && ch <= '9', ch >= 'A' && ch <= 'Z', ch >= 'a' && ch <= 'z',
			strings.ContainsRune("-;/?:@&=+$,_.~*'()[]", ch), ch == '!' && handle != "!":
			b.WriteRune(ch)
		default:
			for _, c := range []byte(string(ch)) {
				fmt.Fprintf(&b, "%%%02X", c)
			}
		}
	}
	if handle != "" {
		return handle + b.String()
	}
	return "!<" + b.String() + ">"
}

func (e *emitter) prepareAnchor(anchor string) string {
	if anchor == "" {
		fail("anchor must not be empty")
	}
	for _, ch := range anchor {
		if !(ch >= '0' && ch <= '9' || ch >= 'A' && ch <= 'Z' || ch >= 'a' && ch <= 'z' || ch == '-' || ch == '_') {
			fail("invalid character %q in the anchor: %q", ch, anchor)
		}
	}
	return anchor
}

func isBreak(ch rune) bool {
	return ch == '\n' || ch == 0x85 || ch == 0x2028 || ch == 0x2029
}

func isSpaceOrBreakOrNul(ch rune) bool {
	return ch == 0 || ch == ' ' || ch == '\t' || ch == '\r' || isBreak(ch)
}

func analyzeScalar(value string, allowUnicode bool) *scalarAnalysis {
	scalar := []rune(value)
	if len(scalar) == 0 {
		return &scalarAnalysis{scalar: scalar, empty: true, allowBlockPlain: true,
			allowSingleQuoted: true, allowDoubleQuoted: true}
	}
	var blockIndicators, flowIndicators, lineBreaks, specialCharacters bool
	var leadingSpace, leadingBreak, trailingSpace, trailingBreak, breakSpace, spaceBreak bool

	if strings.HasPrefix(value, "---") || strings.HasPrefix(value, "...") {
		blockIndicators, flowIndicators = true, true
	}
	precededByWhitespace := true
	followedByWhitespace := len(scalar) == 1 || isSpaceOrBreakOrNul(scalar[1])
	previousSpace, previousBreak := false, false

	for index := 0; index < len(scalar); {
		ch := scalar[index]
		if index == 0 {
			if strings.ContainsRune("#,[]{}&*!|>'\"%@`", ch) {
				flowIndicators, blockIndicators = true, true
			}
			if ch == '?' || ch == ':' {
				flowIndicators = true
				if followedByWhitespace {
					blockIndicators = true
				}
			}
			if ch == '-' && followedByWhitespace {
				flowIndicators, blockIndicators = true, true
			}
		} else {
			if strings.ContainsRune(",?[]{}", ch) {
				flowIndicators = true
			}
			if ch == ':' {
				flowIndicators = true
				if followedByWhitespace {
					blockIndicators = true
				}
			}
			if ch == '#' && precededByWhitespace {
				flowIndicators, blockIndicators = true, true
			}
		}

		if isBreak(ch) {
			lineBreaks = true
		}
		if !(ch == '\n' || ch >= 0x20 && ch <= 0x7E) {
			if (ch == 0x85 || ch >= 0xA0 && ch <= 0xD7FF || ch >= 0xE000 && ch <= 0xFFFD ||
				ch >= 0x10000 && ch < 0x10ffff) && ch != 0xFEFF {
				if !allowUnicode {
					specialCharacters = true
				}
			} else {
				specialCharacters = true
			}
		}

		if ch == ' ' {
			if index == 0 {
				leadingSpace = true
			}
			if index == len(scalar)-1 {
				trailingSpace = true
			}
			if previousBreak {
				breakSpace = true
			}
			previousSpace, previousBreak = true, false
		} else if isBreak(ch) {
			if index == 0 {
				leadingBreak = true
			}
			if index == len(scalar)-1 {
				trailingBreak = true
			}
			if previousSpace {
				spaceBreak = true
			}
			previousSpace, previousBreak = false, true
		} else {
			previousSpace, previousBreak = false, false
		}

		index++
		precededByWhitespace = isSpaceOrBreakOrNul(ch)
		followedByWhitespace = index+1 >= len(scalar) || isSpaceOrBreakOrNul(scalar[index+1])
	}

	a := &scalarAnalysis{scalar: scalar, multiline: lineBreaks,
		allowFlowPlain: true, allowBlockPlain: true, allowSingleQuoted: true, allowDoubleQuoted: true, allowBlock: true}
	if leadingSpace || leadingBreak || trailingSpace || trailingBreak {
		a.allowFlowPlain, a.allowBlockPlain = false, false
	}
	if trailingSpace {
		a.allowBlock = false
	}
	if breakSpace {
		a.allowFlowPlain, a.allowBlockPlain, a.allowSingleQuoted = false, false, false
	}
	if spaceBreak || specialCharacters {
		a.allowFlowPlain, a.allowBlockPlain, a.allowSingleQuoted, a.allowBlock = false, false, false, false
	}
	if lineBreaks {
		a.allowFlowPlain, a.allowBlockPlain = false, false
	}
	if flowIndicators {
		a.allowFlowPlain = false
	}
	if blockIndicators {
		a.allowBlockPlain = false
	}
	return a
}

// Writers.

func (e *emitter) put(data string) {
	e.out.WriteString(data)
	e.chars += utf8.RuneCountInString(data)
}

func (e *emitter) writeIndicator(indicator string, needWhitespace, whitespace, indention bool) {
	data := indicator
	if !e.whitespace && needWhitespace {
		data = " " + indicator
	}
	e.whitespace = whitespace
	e.indention = e.indention && indention
	e.column += utf8.RuneCountInString(data)
	e.openEnded = false
	e.put(data)
}

func (e *emitter) writeIndent() {
	indent := e.indent
	if indent < 0 {
		indent = 0
	}
	if !e.indention || e.column > indent || (e.column == indent && !e.whitespace) {
		e.writeLineBreak("")
	}
	if e.column < indent {
		e.whitespace = true
		e.put(strings.Repeat(" ", indent-e.column))
		e.column = indent
	}
}

func (e *emitter) writeLineBreak(data string) {
	if data == "" {
		data = "\n"
	}
	e.whitespace = true
	e.indention = true
	e.line++
	e.column = 0
	e.put(data)
}

func (e *emitter) writeText(data []rune) {
	e.column += len(data)
	e.put(string(data))
}

func (e *emitter) writeBreaks(text []rune) {
	for _, br := range text {
		if br == '\n' {
			e.writeLineBreak("")
		} else {
			e.writeLineBreak(string(br))
		}
	}
}

func (e *emitter) writeSingleQuoted(text []rune, split bool) {
	e.writeIndicator("'", true, false, false)
	spaces, breaks := false, false
	start, end := 0, 0
	for end <= len(text) {
		ch := noChar
		if end < len(text) {
			ch = text[end]
		}
		switch {
		case spaces:
			if ch == noChar || ch != ' ' {
				if start+1 == end && e.column > e.bestWidth && split && start != 0 && end != len(text) {
					e.writeIndent()
				} else {
					e.writeText(text[start:end])
				}
				start = end
			}
		case breaks:
			if ch == noChar || !isBreak(ch) {
				if text[start] == '\n' {
					e.writeLineBreak("")
				}
				e.writeBreaks(text[start:end])
				e.writeIndent()
				start = end
			}
		default:
			if ch == noChar || ch == ' ' || isBreak(ch) || ch == '\'' {
				if start < end {
					e.writeText(text[start:end])
					start = end
				}
			}
		}
		if ch == '\'' {
			e.column += 2
			e.put("''")
			start = end + 1
		}
		if ch != noChar {
			spaces = ch == ' '
			breaks = isBreak(ch)
		}
		end++
	}
	e.writeIndicator("'", false, false, false)
}

var escapeReplacements = map[rune]string{
	0: "0", 0x07: "a", 0x08: "b", 0x09: "t", 0x0A: "n", 0x0B: "v", 0x0C: "f", 0x0D: "r", 0x1B: "e",
	'"': "\"", '\\': "\\", 0x85: "N", 0xA0: "_", 0x2028: "L", 0x2029: "P",
}

func (e *emitter) writeDoubleQuoted(text []rune, split bool) {
	e.writeIndicator("\"", true, false, false)
	start, end := 0, 0
	for end <= len(text) {
		ch := noChar
		if end < len(text) {
			ch = text[end]
		}
		if ch == noChar || ch == '"' || ch == '\\' || ch == 0x85 || ch == 0x2028 || ch == 0x2029 || ch == 0xFEFF ||
			!(ch >= 0x20 && ch <= 0x7E || e.allowUnicode && (ch >= 0xA0 && ch <= 0xD7FF || ch >= 0xE000 && ch <= 0xFFFD)) {
			if start < end {
				e.writeText(text[start:end])
				start = end
			}
			if ch != noChar {
				var data string
				if r, ok := escapeReplacements[ch]; ok {
					data = "\\" + r
				} else if ch <= 0xFF {
					data = fmt.Sprintf("\\x%02X", ch)
				} else if ch <= 0xFFFF {
					data = fmt.Sprintf("\\u%04X", ch)
				} else {
					data = fmt.Sprintf("\\U%08X", ch)
				}
				e.column += utf8.RuneCountInString(data)
				e.put(data)
				start = end + 1
			}
		}
		if end > 0 && end < len(text)-1 && (ch == ' ' || start >= end) && e.column+(end-start) > e.bestWidth && split {
			data := "\\"
			if start < end {
				data = string(text[start:end]) + data
				start = end
			}
			e.column += utf8.RuneCountInString(data)
			e.put(data)
			e.writeIndent()
			e.whitespace = false
			e.indention = false
			if text[start] == ' ' {
				e.column++
				e.put("\\")
			}
		}
		end++
	}
	e.writeIndicator("\"", false, false, false)
}

func (e *emitter) determineBlockHints(text []rune) string {
	hints := ""
	if len(text) > 0 {
		if text[0] == ' ' || isBreak(text[0]) {
			hints += fmt.Sprint(e.bestIndent)
		}
		if !isBreak(text[len(text)-1]) {
			hints += "-"
		} else if len(text) == 1 || isBreak(text[len(text)-2]) {
			hints += "+"
		}
	}
	return hints
}

func (e *emitter) writeLiteral(text []rune) {
	hints := e.determineBlockHints(text)
	e.writeIndicator("|"+hints, true, false, false)
	if strings.HasSuffix(hints, "+") {
		e.openEnded = true
	}
	e.writeLineBreak("")
	breaks := true
	start, end := 0, 0
	for end <= len(text) {
		ch := noChar
		if end < len(text) {
			ch = text[end]
		}
		if breaks {
			if ch == noChar || !isBreak(ch) {
				e.writeBreaks(text[start:end])
				if ch != noChar {
					e.writeIndent()
				}
				start = end
			}
		} else if ch == noChar || isBreak(ch) {
			e.put(string(text[start:end]))
			if ch == noChar {
				e.writeLineBreak("")
			}
			start = end
		}
		if ch != noChar {
			breaks = isBreak(ch)
		}
		end++
	}
}

func (e *emitter) writePlain(text []rune, split bool) {
	if e.rootContext {
		e.openEnded = true
	}
	if len(text) == 0 {
		return
	}
	if !e.whitespace {
		e.column++
		e.put(" ")
	}
	e.whitespace = false
	e.indention = false
	spaces, breaks := false, false
	start, end := 0, 0
	for end <= len(text) {
		ch := noChar
		if end < len(text) {
			ch = text[end]
		}
		switch {
		case spaces:
			if ch != ' ' {
				if start+1 == end && e.column > e.bestWidth && split {
					e.writeIndent()
					e.whitespace = false
					e.indention = false
				} else {
					e.writeText(text[start:end])
				}
				start = end
			}
		case breaks:
			// Python raises TypeError for ch None here; plain scalars never contain breaks.
			if ch == noChar || !isBreak(ch) {
				if text[start] == '\n' {
					e.writeLineBreak("")
				}
				e.writeBreaks(text[start:end])
				e.writeIndent()
				e.whitespace = false
				e.indention = false
				start = end
			}
		default:
			if ch == noChar || ch == ' ' || isBreak(ch) {
				e.writeText(text[start:end])
				start = end
			}
		}
		if ch != noChar {
			spaces = ch == ' '
			breaks = isBreak(ch)
		}
		end++
	}
}
