package repo

import (
	"configflow/internal/jsonx"
)

// DeepMerge is config_repository._deep_merge: a deep copy of base with
// override's keys laid over it, objects merged recursively.
func DeepMerge(base, override *jsonx.Object) *jsonx.Object {
	result := base.DeepCopy()
	if result == nil {
		result = jsonx.NewObject()
	}
	for key, value := range override.All() {
		current, _ := jsonx.AsObject(result.Value(key))
		incoming, isObject := jsonx.AsObject(value)
		if current != nil && isObject {
			result.Set(key, DeepMerge(current, incoming))
		} else {
			result.Set(key, jsonx.DeepCopy(value))
		}
	}
	return result
}

// configDeepMerge is common/config.py::_deep_merge: base's key order, then
// override's new keys; values are shared, not copied.
func configDeepMerge(base, override *jsonx.Object) *jsonx.Object {
	result := jsonx.NewObject()
	for key, value := range base.All() {
		other, ok := override.Get(key)
		if !ok {
			result.Set(key, value)
			continue
		}
		left, leftObject := jsonx.AsObject(value)
		right, rightObject := jsonx.AsObject(other)
		if leftObject && rightObject {
			result.Set(key, configDeepMerge(left, right))
		} else {
			result.Set(key, other)
		}
	}
	for key, value := range override.All() {
		if !result.Has(key) {
			result.Set(key, value)
		}
	}
	return result
}

// listItemKey is _list_item_key: items with a string or integer id are
// identified by it, everything else by its sorted JSON text.
func listItemKey(value any) string {
	if object, ok := jsonx.AsObject(value); ok {
		switch id := object.Value("id").(type) {
		case string:
			return "id:s:" + id
		case bool: // an int in Python: ("id", True) == ("id", 1)
			if id {
				return "id:i:1"
			}
			return "id:i:0"
		case jsonx.Number:
			if id.IsInt() {
				return "id:i:" + id.String()
			}
		}
	}
	text, err := jsonx.Encode(value, jsonx.Options{SortKeys: true})
	if err != nil {
		return "repr:" + jsonx.Repr(value)
	}
	return "value:" + string(text)
}

// orderedItems is a dict keyed by listItemKey: insertion-ordered, a later
// duplicate replaces the value and keeps the first position.
type orderedItems struct {
	keys   []string
	values map[string]any
}

func newOrderedItems() *orderedItems { return &orderedItems{values: map[string]any{}} }

func (o *orderedItems) set(key string, value any) {
	if _, ok := o.values[key]; !ok {
		o.keys = append(o.keys, key)
	}
	o.values[key] = value
}

func (o *orderedItems) has(key string) bool { _, ok := o.values[key]; return ok }

// ThreeWayMerge is _three_way_merge: it applies the caller's
// baseline-to-proposed delta to freshly read data and returns a new value.
func ThreeWayMerge(current, baseline, proposed any) any {
	if jsonx.Equal(proposed, baseline) {
		return jsonx.DeepCopy(current)
	}
	currentObject, isCurrentObject := jsonx.AsObject(current)
	baselineObject, isBaselineObject := jsonx.AsObject(baseline)
	proposedObject, isProposedObject := jsonx.AsObject(proposed)
	if isCurrentObject && isBaselineObject && isProposedObject {
		result := currentObject.DeepCopy()
		for key, old := range baselineObject.All() {
			if proposedObject.Has(key) {
				continue
			}
			if value, ok := result.Get(key); ok && jsonx.Equal(value, old) {
				result.Delete(key)
			}
		}
		for key, value := range proposedObject.All() {
			old, inBaseline := baselineObject.Get(key)
			if !inBaseline {
				existing, ok := result.Get(key)
				if !ok {
					result.Set(key, jsonx.DeepCopy(value))
				} else if left, ok := jsonx.AsObject(existing); ok {
					if right, ok := jsonx.AsObject(value); ok {
						result.Set(key, DeepMerge(left, right))
					}
				}
			} else if !jsonx.Equal(value, old) {
				result.Set(key, ThreeWayMerge(result.Value(key), old, value))
			}
		}
		return result
	}
	currentList, isCurrentList := jsonx.AsList(current)
	baselineList, isBaselineList := jsonx.AsList(baseline)
	proposedList, isProposedList := jsonx.AsList(proposed)
	if isCurrentList && isBaselineList && isProposedList {
		baselineByKey, proposedByKey := newOrderedItems(), newOrderedItems()
		for _, item := range baselineList {
			baselineByKey.set(listItemKey(item), item)
		}
		proposedKeys := make([]string, 0, len(proposedList))
		for _, item := range proposedList {
			key := listItemKey(item)
			proposedByKey.set(key, item)
			proposedKeys = append(proposedKeys, key)
		}
		result := make([]any, 0, len(currentList))
		for _, item := range currentList {
			key := listItemKey(item)
			if baselineByKey.has(key) && !proposedByKey.has(key) {
				continue
			}
			result = append(result, jsonx.DeepCopy(item))
		}
		positions := map[string]int{}
		for index, item := range result {
			positions[listItemKey(item)] = index
		}
		for _, key := range proposedKeys {
			item := proposedByKey.values[key]
			index, inResult := positions[key]
			if !baselineByKey.has(key) {
				if !inResult {
					result = append(result, jsonx.DeepCopy(item))
					positions[key] = len(result) - 1
				}
			} else if old := baselineByKey.values[key]; !jsonx.Equal(item, old) && inResult {
				result[index] = ThreeWayMerge(result[index], old, item)
			}
		}

		var retainedProposed, retainedBaseline []string
		for _, key := range proposedKeys {
			if baselineByKey.has(key) {
				retainedProposed = append(retainedProposed, key)
			}
		}
		for _, item := range baselineList {
			if key := listItemKey(item); proposedByKey.has(key) {
				retainedBaseline = append(retainedBaseline, key)
			}
		}
		if !equalStrings(retainedProposed, retainedBaseline) {
			resultByKey := newOrderedItems()
			for _, item := range result {
				resultByKey.set(listItemKey(item), item)
			}
			var orderedKeys []string
			seen := map[string]bool{}
			for _, key := range proposedKeys {
				if resultByKey.has(key) {
					orderedKeys = append(orderedKeys, key)
					seen[key] = true
				}
			}
			for _, key := range resultByKey.keys {
				if !seen[key] {
					orderedKeys = append(orderedKeys, key)
				}
			}
			result = make([]any, 0, len(orderedKeys))
			for _, key := range orderedKeys {
				result = append(result, resultByKey.values[key])
			}
		}
		return result
	}
	return jsonx.DeepCopy(proposed)
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
