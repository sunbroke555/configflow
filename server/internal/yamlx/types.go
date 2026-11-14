package yamlx

import "configflow/internal/jsonx"

// FlowList is a list the dumper writes in flow style, `[a, b]` (the Python
// FlowList(list) subclass of agents/docker_ha_compact.py).
type FlowList []any

// FlowMap is a mapping the dumper writes in flow style, `{a: 1}` (the Python
// FlowMap(dict) subclass of agents/docker_ha_compact.py).
type FlowMap struct{ *jsonx.Object }

// SharedString is a string with identity: the same pointer used twice in a
// document is written once with an anchor and then as an alias, like a Python
// str subclass whose ignore_aliases returns False (ImageName in
// agents/docker_ha_compact.py). Anchor names the anchor; empty means the
// generated id001, id002, ...
type SharedString struct {
	Value  string
	Anchor string
}

// Marked is a string scalar that reports where it landed in the output (see
// DumpOptions.OnMarked); the document is otherwise identical to one holding
// Value. It stands in for the marking dumper of agents/push_diff.py.
type Marked struct {
	Value string
	Mark  any
}
