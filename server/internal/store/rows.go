package store

import (
	"sort"

	"configflow/internal/jsonx"
)

// A document is stored as one row per (scope, owner, field): the root keys
// besides the sections, the fields of "system" and "shared", and the fields
// of each profile. Order is data, so section and field positions and the
// top-level key order are stored too.

const profilesKey = "profiles"

func isSectionKey(key string) bool { return key == "system" || key == "shared" }

type sectionKey struct{ scope, owner string }

type fieldKey struct{ scope, owner, field string }

// fieldRow is the stored form of one field: its position in the section
// and its value as compact JSON text.
type fieldRow struct {
	position int64
	value    string
}

// parsedField is a field with its value parsed.
type parsedField struct {
	position int64
	value    any
}

// ordered is a map that iterates in insertion order (a Python dict).
type ordered[K comparable, V any] struct {
	keys   []K
	values map[K]V
}

func newOrdered[K comparable, V any]() *ordered[K, V] {
	return &ordered[K, V]{values: map[K]V{}}
}

func (o *ordered[K, V]) set(key K, value V) {
	if _, ok := o.values[key]; !ok {
		o.keys = append(o.keys, key)
	}
	o.values[key] = value
}

func (o *ordered[K, V]) get(key K) (V, bool) {
	value, ok := o.values[key]
	return value, ok
}

func (o *ordered[K, V]) has(key K) bool {
	_, ok := o.values[key]
	return ok
}

// rows is a document split by documentRows.
type rows struct {
	rootOrder []string
	sections  *ordered[sectionKey, int64]
	fields    *ordered[fieldKey, fieldRow]
}

// dumps is the stored form of a value. No sort_keys: key order is part of
// the document.
func dumps(value any) (string, error) {
	text, err := jsonx.Compact(value)
	if err != nil {
		return "", &Error{Kind: ErrDocument, Message: err.Error(), Cause: err}
	}
	return string(text), nil
}

func sectionRows(section any, scope, owner string, fields *ordered[fieldKey, fieldRow]) error {
	object, ok := jsonx.AsObject(section)
	if !ok {
		return newError(ErrDocument, "配置节 "+scope+"/"+owner+" 必须是对象")
	}
	position := int64(0)
	for field, value := range object.All() {
		text, err := dumps(value)
		if err != nil {
			return err
		}
		fields.set(fieldKey{scope, owner, field}, fieldRow{position, text})
		position++
	}
	return nil
}

// documentRows splits a document into root order, sections and fields.
func documentRows(document *jsonx.Object) (*rows, error) {
	if document == nil {
		return nil, newError(ErrDocument, "配置文档必须是对象")
	}
	result := &rows{sections: newOrdered[sectionKey, int64](), fields: newOrdered[fieldKey, fieldRow]()}
	result.sections.set(sectionKey{"root", ""}, 0)
	rootPosition := int64(0)
	for key, value := range document.All() {
		result.rootOrder = append(result.rootOrder, key)
		switch {
		case isSectionKey(key):
			result.sections.set(sectionKey{key, ""}, 0)
			if err := sectionRows(value, key, "", result.fields); err != nil {
				return nil, err
			}
		case key == profilesKey:
			profiles, ok := jsonx.AsObject(value)
			if !ok {
				return nil, newError(ErrDocument, "profiles 必须是对象")
			}
			position := int64(0)
			for owner, profile := range profiles.All() {
				if owner == "" {
					return nil, newError(ErrDocument, "配置空间 ID 必须是非空字符串")
				}
				result.sections.set(sectionKey{"profile", owner}, position)
				if err := sectionRows(profile, "profile", owner, result.fields); err != nil {
					return nil, err
				}
				position++
			}
		default:
			text, err := dumps(value)
			if err != nil {
				return nil, err
			}
			result.fields.set(fieldKey{"root", "", key}, fieldRow{rootPosition, text})
			rootPosition++
		}
	}
	return result, nil
}

// buildDocument is the inverse of documentRows.
func buildDocument(rootOrder []string, sections *ordered[sectionKey, int64], fields *ordered[fieldKey, parsedField]) (*jsonx.Object, error) {
	type item struct {
		position int64
		field    string
		value    any
	}
	grouped := map[sectionKey][]item{}
	for _, key := range fields.keys {
		owner := sectionKey{key.scope, key.owner}
		if !sections.has(owner) {
			return nil, newError(ErrCorrupt, "字段 "+key.scope+"/"+key.owner+"/"+key.field+" 没有所属配置节")
		}
		field := fields.values[key]
		grouped[owner] = append(grouped[owner], item{field.position, key.field, field.value})
	}
	section := func(key sectionKey) *jsonx.Object {
		items := grouped[key]
		// Stable, by position only: rows with equal positions keep the
		// order they were read in, as Python's sorted() does.
		sort.SliceStable(items, func(i, j int) bool { return items[i].position < items[j].position })
		object := jsonx.NewObject()
		for _, item := range items {
			object.Set(item.field, item.value)
		}
		return object
	}

	root := section(sectionKey{"root", ""})
	seen := map[string]bool{}
	expected := 0
	consistent := true
	for _, key := range rootOrder {
		if seen[key] {
			consistent = false
		}
		seen[key] = true
		if !isSectionKey(key) && key != profilesKey {
			expected++
			consistent = consistent && root.Has(key)
		}
	}
	if !consistent || expected != root.Len() {
		return nil, newError(ErrCorrupt, "顶层键顺序与数据库内容不一致")
	}
	for _, scope := range []string{"system", "shared"} {
		if seen[scope] != sections.has(sectionKey{scope, ""}) {
			return nil, newError(ErrCorrupt, "配置节 "+scope+" 与顶层键顺序不一致")
		}
	}
	type profile struct {
		position int64
		owner    string
	}
	var profiles []profile
	for _, key := range sections.keys {
		if key.scope == "profile" {
			profiles = append(profiles, profile{sections.values[key], key.owner})
		}
	}
	sort.Slice(profiles, func(i, j int) bool {
		if profiles[i].position != profiles[j].position {
			return profiles[i].position < profiles[j].position
		}
		return profiles[i].owner < profiles[j].owner
	})
	if len(profiles) > 0 && !seen[profilesKey] {
		return nil, newError(ErrCorrupt, "配置空间与顶层键顺序不一致")
	}
	document := jsonx.NewObject()
	for _, key := range rootOrder {
		switch {
		case isSectionKey(key):
			document.Set(key, section(sectionKey{key, ""}))
		case key == profilesKey:
			object := jsonx.NewObject()
			for _, item := range profiles {
				object.Set(item.owner, section(sectionKey{"profile", item.owner}))
			}
			document.Set(key, object)
		default:
			document.Set(key, root.Value(key))
		}
	}
	return document, nil
}

// lookup returns the value of one row in a loaded document.
func lookup(document *jsonx.Object, key fieldKey) (any, bool) {
	section := document
	switch key.scope {
	case "root":
	case "profile":
		section = document.GetObject(profilesKey).GetObject(key.owner)
	default:
		section = document.GetObject(key.scope)
	}
	return section.Get(key.field)
}

// changes is what a commit has to write.
type changes struct {
	deleteSections []sectionKey
	upsertSections []sectionKey
	deleteFields   []fieldKey
	upsertFields   []fieldKey
	rootOrder      bool // the top-level key order differs
}

func (c *changes) any() bool {
	return len(c.deleteSections)+len(c.upsertSections)+len(c.deleteFields)+len(c.upsertFields) > 0 || c.rootOrder
}

func diff(storedRootOrder any, storedSections *ordered[sectionKey, int64], storedFields *ordered[fieldKey, fieldRow], target *rows) *changes {
	result := &changes{}
	for _, key := range storedSections.keys {
		if !target.sections.has(key) {
			result.deleteSections = append(result.deleteSections, key)
		}
	}
	for _, key := range target.sections.keys {
		if position, ok := storedSections.get(key); !ok || position != target.sections.values[key] {
			result.upsertSections = append(result.upsertSections, key)
		}
	}
	for _, key := range storedFields.keys {
		// Fields of a deleted section go with it (ON DELETE CASCADE).
		if !target.fields.has(key) && target.sections.has(sectionKey{key.scope, key.owner}) {
			result.deleteFields = append(result.deleteFields, key)
		}
	}
	for _, key := range target.fields.keys {
		if row, ok := storedFields.get(key); !ok || row != target.fields.values[key] {
			result.upsertFields = append(result.upsertFields, key)
		}
	}
	result.rootOrder = !jsonx.Equal(jsonx.ListOf(target.rootOrder), storedRootOrder)
	return result
}
