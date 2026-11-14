// Package yamlx loads and dumps YAML the way PyYAML 6 does, on the jsonx value
// model (ordered *jsonx.Object, []any, string, jsonx.Number, bool, nil).
//
// # Dump
//
// Dump is a line-by-line port of PyYAML's pure-Python representer, serializer
// and emitter (yaml.dump / yaml.safe_dump with SafeDumper), so the bytes equal
// PyYAML's for every option set the application uses: see DumpOptions and the
// presets SafeDump, SafeDumpUnicode and IndentDump. Anchors and aliases are
// produced for a *jsonx.Object or non-empty []any that occurs twice, as PyYAML
// does for a Python dict or list that is the same object; use
// DumpOptions.NoAliases when the Python code would have copied it. FlowList,
// FlowMap, SharedString and Marked stand in for the Python subclasses and
// dumper hooks of agents/docker_ha_compact.py and agents/push_diff.py.
//
// # Load
//
// Load is yaml.safe_load. Syntax is parsed by gopkg.in/yaml.v3 (a libyaml
// port); typing and construction are PyYAML's own: YAML 1.1 booleans
// (yes/no/on/off), octal, binary and hex integers, sexagesimal numbers,
// underscores, timestamps (Timestamp), !!binary, merge keys, "<<" and "="
// quirks, duplicate keys (the last wins), and PyYAML's constructor error
// messages with their "in "<unicode string>", line L, column C:" marks.
// LoadOptions selects NativeLoader's YAML 1.2 style typing
// (oxidns_native.NativeLoader) and lets the caller veto keys.
//
// # Known differences from PyYAML
//
//   - Syntax errors carry yaml.v3's text, not PyYAML's scanner/parser text;
//     Error.Syntax is true for them. Which documents are rejected agrees with
//     PyYAML on the differential corpus, with these exceptions: a second
//     leading byte order mark (PyYAML keeps it as text) fails here.
//   - A mapping key that is not a string (`1: a`, `yes: a`, `~: a`) cannot be
//     held by a jsonx.Object: Load returns *NonStringKeyError.
//   - !!set, !!omap, !!pairs and !!python/* tags are errors.
//   - A shared empty list is not recognised as shared (an empty Go slice has
//     no identity), so PyYAML's anchor for it is not written.
//   - Strings with lone surrogates cannot exist in Go.
//   - Only the pure-Python SafeDumper is reproduced. CSafeDumper (libyaml's
//     emitter, used once, to measure a size in agents/push_diff.py) writes
//     different text for some inputs; the corpus records how often.
package yamlx
