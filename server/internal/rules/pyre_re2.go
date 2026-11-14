package rules

import (
	"errors"
	"fmt"
	"regexp"
	"sort"
	"strings"
	"sync"
	"unicode"
)

// UnsupportedRegexError reports a pattern that Python's re accepts but that
// has no RE2 equivalent (look-around, back references, atomic groups,
// possessive quantifiers, conditionals, repeat counts above 1000, ...). The
// caller decides what that means; the matcher treats such a pattern as "does
// not match", which differs from Python where it would match.
type UnsupportedRegexError struct{ Reason string }

func (e *UnsupportedRegexError) Error() string {
	return "regex construct not supported by the Go engine: " + e.Reason
}

// PyRegex is a compiled Python pattern, executed by Go's regexp (RE2).
type PyRegex struct{ re *regexp.Regexp }

// Search is re.search(...) is not None.
func (r *PyRegex) Search(text string) bool { return r.re.MatchString(text) }

var (
	regexCacheMu sync.Mutex
	regexCache   = map[regexKey]regexEntry{}
)

type regexKey struct {
	pattern    string
	ignoreCase bool
}

type regexEntry struct {
	re  *PyRegex
	err error
}

// CompilePyRegex is re.compile(pattern[, re.IGNORECASE]). The error is one of
// *reError (Python's re.error, whose Error() text is Python's), errRecursion /
// errOverflow (the other exceptions compile can raise) or
// *UnsupportedRegexError. Results are cached like Python's re module does.
func CompilePyRegex(pattern string, ignoreCase bool) (*PyRegex, error) {
	key := regexKey{pattern, ignoreCase}
	regexCacheMu.Lock()
	entry, ok := regexCache[key]
	regexCacheMu.Unlock()
	if ok {
		return entry.re, entry.err
	}
	re, err := compilePyRegex(pattern, ignoreCase)
	regexCacheMu.Lock()
	if len(regexCache) >= 512 {
		regexCache = map[regexKey]regexEntry{}
	}
	regexCache[key] = regexEntry{re, err}
	regexCacheMu.Unlock()
	return re, err
}

// IsPyRegexError reports whether err is a pattern Python itself rejects (as
// opposed to a pattern only this implementation cannot run).
func IsPyRegexError(err error) bool {
	var unsupported *UnsupportedRegexError
	return err != nil && !errors.As(err, &unsupported)
}

func compilePyRegex(pattern string, ignoreCase bool) (*PyRegex, error) {
	flags := 0
	if ignoreCase {
		flags = flagIgnoreCase
	}
	parsed, err := parsePyRegex(pattern, flags)
	if err != nil {
		return nil, err
	}
	tr := &translator{ascii: parsed.flags&flagASCII != 0}
	prefix := flagLetters(parsed.flags, 0)
	if prefix != "" {
		tr.b.WriteString("(?" + prefix + ")")
	}
	if err := tr.nodes(parsed.nodes); err != nil {
		return nil, err
	}
	re, err := regexp.Compile(tr.b.String())
	if err != nil {
		return nil, &UnsupportedRegexError{Reason: err.Error()}
	}
	return &PyRegex{re: re}, nil
}

func flagLetters(add, del int) string {
	letters := func(flags int) string {
		var b strings.Builder
		if flags&flagIgnoreCase != 0 {
			b.WriteByte('i')
		}
		if flags&flagMultiline != 0 {
			b.WriteByte('m')
		}
		if flags&flagDotAll != 0 {
			b.WriteByte('s')
		}
		return b.String()
	}
	result := letters(add)
	if d := letters(del); d != "" {
		result += "-" + d
	}
	return result
}

type translator struct {
	b     strings.Builder
	ascii bool
}

func unsupported(reason string) error { return &UnsupportedRegexError{Reason: reason} }

func (tr *translator) nodes(nodes []*reNode) error {
	for _, n := range nodes {
		if err := tr.node(n); err != nil {
			return err
		}
	}
	return nil
}

func (tr *translator) lit(r rune) {
	if r < 128 && (r >= '0' && r <= '9' || r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z') {
		tr.b.WriteRune(r)
		return
	}
	fmt.Fprintf(&tr.b, `\x{%x}`, r)
}

func (tr *translator) node(n *reNode) error {
	switch n.op {
	case opLiteral:
		tr.lit(n.ch)
	case opNotLiteral:
		tr.b.WriteString(`[^`)
		tr.lit(n.ch)
		tr.b.WriteString(`]`)
	case opAny:
		tr.b.WriteString(`.`)
	case opIn:
		tr.b.WriteString(`[`)
		for _, item := range n.set {
			switch item.kind {
			case setNegate:
				tr.b.WriteString(`^`)
			case setLiteral:
				tr.lit(item.lo)
			case setRange:
				tr.lit(item.lo)
				tr.b.WriteString(`-`)
				tr.lit(item.hi)
			case setCategory:
				tr.b.WriteString(categoryClass(item.cat, tr.ascii))
			}
		}
		tr.b.WriteString(`]`)
	case opAt:
		switch n.at {
		case atBeginning:
			tr.b.WriteString(`^`)
		case atBeginningString:
			tr.b.WriteString(`\A`)
		case atBoundary:
			tr.b.WriteString(`\b`)
		case atNonBoundary:
			tr.b.WriteString(`\B`)
		case atEnd:
			tr.b.WriteString(`$`)
		case atEndString:
			tr.b.WriteString(`\z`)
		}
	case opBranch:
		tr.b.WriteString(`(?:`)
		for i, alt := range n.alts {
			if i > 0 {
				tr.b.WriteString(`|`)
			}
			if err := tr.nodes(alt); err != nil {
				return err
			}
		}
		tr.b.WriteString(`)`)
	case opSubpattern:
		if n.add&(flagASCII|flagLocale) != 0 {
			return unsupported("scoped (?a:) flag")
		}
		flags := flagLetters(n.add, n.del)
		switch {
		case n.group != 0 && flags == "":
			tr.b.WriteString(`(`)
		case flags == "":
			tr.b.WriteString(`(?:`)
		default:
			tr.b.WriteString(`(?` + flags + `:`)
		}
		if err := tr.nodes(n.body); err != nil {
			return err
		}
		tr.b.WriteString(`)`)
	case opMaxRepeat, opMinRepeat:
		tr.b.WriteString(`(?:`)
		if err := tr.nodes(n.body); err != nil {
			return err
		}
		tr.b.WriteString(`)`)
		if n.max == reMaxRepeat {
			fmt.Fprintf(&tr.b, `{%d,}`, n.min)
		} else {
			fmt.Fprintf(&tr.b, `{%d,%d}`, n.min, n.max)
		}
		if n.op == opMinRepeat {
			tr.b.WriteString(`?`)
		}
	case opPossessive:
		return unsupported("possessive quantifier")
	case opAtomic:
		return unsupported("atomic group")
	case opAssert, opAssertNot:
		return unsupported("look-around assertion")
	case opGroupRef:
		return unsupported("back reference")
	case opGroupRefExists:
		return unsupported("conditional group")
	}
	return nil
}

// Character-category class bodies. The positive Unicode categories use RE2's
// property classes; negated ones, which RE2 cannot express inside a larger
// class, are spelled as explicit complement ranges.
func categoryClass(cat catKind, ascii bool) string {
	switch cat {
	case catDigit:
		if ascii {
			return `0-9`
		}
		return `\p{Nd}`
	case catNotDigit:
		if ascii {
			return `\x{0}-/:-\x{10ffff}`
		}
		return `\P{Nd}`
	case catWord:
		if ascii {
			return `0-9A-Za-z_`
		}
		return `\p{L}\p{N}_`
	case catNotWord:
		if ascii {
			return `\x{0}-/:-@\[-\^` + "`" + `{-\x{10ffff}`
		}
		return notWordRanges()
	case catSpace:
		if ascii {
			return `\t-\r `
		}
		return spaceRanges
	case catNotSpace:
		if ascii {
			return `\x{0}-\x{8}\x{e}-\x{1f}!-\x{10ffff}`
		}
		return notSpaceRanges()
	}
	return ``
}

// spaceRanges is str.isspace() in a class body.
const spaceRanges = `\t-\r\x{1c}-\x{20}\x{85}\x{a0}\x{1680}\x{2000}-\x{200a}\x{2028}\x{2029}\x{202f}\x{205f}\x{3000}`

var (
	notWordOnce, notSpaceOnce sync.Once
	notWordClass, notSpaceStr string
)

type span struct{ lo, hi rune }

func complementClass(spans []span) string {
	sort.Slice(spans, func(i, j int) bool { return spans[i].lo < spans[j].lo })
	var b strings.Builder
	next := rune(0)
	for _, s := range spans {
		if s.lo > next {
			fmt.Fprintf(&b, `\x{%x}-\x{%x}`, next, s.lo-1)
		}
		if s.hi+1 > next {
			next = s.hi + 1
		}
	}
	if next <= unicode.MaxRune {
		fmt.Fprintf(&b, `\x{%x}-\x{%x}`, next, unicode.MaxRune)
	}
	return b.String()
}

func tableSpans(tables ...*unicode.RangeTable) []span {
	var spans []span
	for _, table := range tables {
		for _, r := range table.R16 {
			if r.Stride == 1 {
				spans = append(spans, span{rune(r.Lo), rune(r.Hi)})
				continue
			}
			for c := rune(r.Lo); c <= rune(r.Hi); c += rune(r.Stride) {
				spans = append(spans, span{c, c})
			}
		}
		for _, r := range table.R32 {
			if r.Stride == 1 {
				spans = append(spans, span{rune(r.Lo), rune(r.Hi)})
				continue
			}
			for c := rune(r.Lo); c <= rune(r.Hi); c += rune(r.Stride) {
				spans = append(spans, span{c, c})
			}
		}
	}
	return spans
}

func notWordRanges() string {
	notWordOnce.Do(func() {
		spans := tableSpans(unicode.L, unicode.N)
		spans = append(spans, span{'_', '_'})
		notWordClass = complementClass(spans)
	})
	return notWordClass
}

func notSpaceRanges() string {
	notSpaceOnce.Do(func() {
		var spans []span
		for r := rune(0); r <= 0x3000; r++ {
			if pyIsSpace(r) {
				spans = append(spans, span{r, r})
			}
		}
		notSpaceStr = complementClass(spans)
	})
	return notSpaceStr
}
