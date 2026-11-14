package commented

import "errors"

// ErrUnsupported marks a document the editor does not reproduce byte for
// byte; the caller keeps the original configuration (the same outcome as a
// ruamel failure in Python).
var ErrUnsupported = errors.New("comment-preserving edit not supported for this document")

func unsupported(reason string) error { return &unsupportedError{reason} }

type unsupportedError struct{ reason string }

func (e *unsupportedError) Error() string {
	return "comment-preserving edit not supported: " + e.reason
}
func (e *unsupportedError) Is(target error) bool { return target == ErrUnsupported }

type nodeKind int

const (
	kScalar nodeKind = iota
	kMap
	kSeq
	kAlias
)

type scalarStyle int

const (
	sPlain scalarStyle = iota
	sSingle
	sDouble
	sLiteral
)

// comment is an end-of-line comment: its source column and text ("#...").
type comment struct {
	col  int
	text string
}

// rec belongs to the element that owns a source line (a mapping entry or a
// sequence item): its end-of-line comment and the comment/blank lines that
// follow it before the next content line.
type rec struct {
	eol *comment
	gap []string
	// trail: comment lines written after the whole value (an alias's
	// comments, which ruamel keeps before the next key)
	trail []string
}

type node struct {
	kind   nodeKind
	anchor string
	// scalars
	style scalarStyle
	raw   string // plain: source token; quoted/literal: decoded value
	// collections
	flow    bool
	entries []*entry
	items   []*item
	// alias
	name   string
	target *node
	// comments inside a flow collection (not representable; re-added on top)
	flowComments []string
	// post is the end-of-line comment after a flow collection's closing
	// bracket: ruamel keeps it on the collection itself (comment[0]), so it
	// travels with the collection to alias expansions and copies.
	post *comment
	// endGap: comment lines written after a block mapping's last entry
	endGap []string
}

type entry struct {
	key   *node
	value *node
	rec   rec
}

type item struct {
	compactFlag bool // the dash line is owned by the first element of the nested collection
	// dashBreak: comment lines written between a bare dash and the nested
	// block mapping (a materialized merge key's comment)
	dashBreak []string
	value     *node
	rec       rec
}

type document struct {
	explicitStart bool
	explicitEnd   bool
	head          []string // comment/blank lines before the first content line
	preDoc        []string // comment lines before an explicit `---` (stripped)
	root          *node
	rootRec       rec // gap after a scalar root (unused for collections)
	comments      []string
	crlfRisk      bool // an end-of-line comment on the header line of a nested value
	hasInnerGaps  bool // comment or blank lines after a content line
}

func (it *item) compact() { it.compactFlag = true }
