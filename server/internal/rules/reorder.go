package rules

import "configflow/internal/jsonx"

// Port of backend/utils/reorder.py: collection ordering by id. The old
// endpoints accepted the full object array and wrote back display data; ids
// let the server reorder stored data instead.

// ReorderByIDs moves the items named by ids, in that order, to one end of the
// collection and keeps the rest in their relative order. Unknown ids are
// returned in missing and the collection comes back unchanged.
func ReorderByIDs(items []any, ids []any, position string) (ordered []any, missing []any) {
	byID := map[string]any{}
	hash := func(v any) string { return jsonx.Repr(v) }
	for _, item := range items {
		if object, ok := item.(*jsonx.Object); ok {
			byID[hash(object.Value("id"))] = item // dict comprehension: the last item wins
		}
	}
	for _, id := range ids {
		if _, ok := byID[hash(id)]; !ok {
			missing = append(missing, id)
		}
	}
	if len(missing) > 0 {
		return items, missing
	}
	selected := map[string]struct{}{}
	for _, id := range ids {
		selected[hash(id)] = struct{}{}
	}
	moved := make([]any, 0, len(ids))
	for _, id := range ids {
		moved = append(moved, byID[hash(id)])
	}
	var rest []any
	for _, item := range items {
		if object, ok := item.(*jsonx.Object); ok {
			if _, hit := selected[hash(object.Value("id"))]; hit {
				continue
			}
		}
		rest = append(rest, item)
	}
	if position == "top" {
		return append(moved, rest...), nil
	}
	return append(rest, moved...), nil
}

// ResolveNewOrder reorders by the ids of existing items; a legacy object array
// only supplies the order and never overwrites resource definitions. The
// second result lists errors (messages) or unknown ids; non-empty means the
// collection was left unchanged. body is the request JSON object.
func ResolveNewOrder(current []any, body *jsonx.Object, objectKey string) ([]any, []any) {
	idsValue, hasIDs := body.Get("ids")
	if !hasIDs || idsValue == nil {
		list, isList := body.Value(objectKey).([]any)
		valid := isList
		if isList {
			for _, item := range list {
				if _, ok := item.(*jsonx.Object); !ok {
					valid = false
					break
				}
			}
		}
		if !valid {
			return current, []any{objectKey + " 必须是对象数组"}
		}
		ids := make([]any, len(list))
		for i, item := range list {
			ids[i] = item.(*jsonx.Object).Value("id")
		}
		idsValue = ids
	}
	ids, ok := idsValue.([]any)
	if !ok {
		return current, []any{"ids 必须是数组"}
	}
	seen := map[string]struct{}{}
	for _, id := range ids {
		text, isStr := id.(string)
		if !isStr {
			return current, []any{"id 必须是互不重复的字符串"}
		}
		if _, dup := seen[text]; dup {
			return current, []any{"id 必须是互不重复的字符串"}
		}
		seen[text] = struct{}{}
	}
	position, _ := body.GetOr("position", "top").(string)
	return ReorderByIDs(current, ids, position)
}
