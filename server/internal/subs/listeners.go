package subs

import (
	"fmt"

	"configflow/internal/jsonx"
	"configflow/internal/rules"
	"configflow/internal/subs/commented"
	"configflow/internal/yamlx"
)

// ListenerReferenceError is mihomo_listeners.ListenerReferenceError, a
// StrategyReferenceError.
type ListenerReferenceError struct {
	Message string
	// Cause is the Go-side reason (for example commented.ErrUnsupported); not part of the message.
	Cause error
}

func (e *ListenerReferenceError) Error() string { return e.Message }

// Unwrap exposes the StrategyReferenceError base class.
func (e *ListenerReferenceError) Unwrap() error {
	return &rules.StrategyReferenceError{Message: e.Message}
}

func lre(msg string) error { return &ListenerReferenceError{Message: msg} }

var listenerBuiltins = []string{"DIRECT", "REJECT", "REJECT-DROP", "PASS", "PASS-RULE", "COMPATIBLE", "GLOBAL"}

// listenersOf is _listeners: the document's listeners, validated.
func listenersOf(document *jsonx.Object) ([]any, error) {
	v := document.Value("listeners")
	if v == nil {
		return nil, nil
	}
	listeners, ok := v.([]any)
	if !ok {
		return nil, lre("Mihomo listeners 必须是列表，无法确认入口的固定策略引用")
	}
	for i, l := range listeners {
		o, ok := l.(*jsonx.Object)
		if !ok {
			return nil, lre(fmt.Sprintf("Mihomo listeners[%d] 必须是对象", i))
		}
		if t := o.Value("proxy"); t != nil && !isString(t) {
			return nil, lre(fmt.Sprintf("Mihomo listeners[%d].proxy 必须是策略名称或留空", i))
		}
	}
	return listeners, nil
}

// RelinkGroupListeners is relink_group_listeners: it keeps listeners'
// explicit `proxy` references aligned with a managed group edit, rewriting
// profile["mihomo"]["custom_config"] with its comments preserved, and returns
// the changed paths. newName nil restores the listener's own rules.
func RelinkGroupListeners(profile *jsonx.Object, oldName string, newName *string) ([]string, error) {
	settings := profile.GetObject("mihomo")
	source, _ := settings.Value("custom_config").(string)
	if pyStrip(source) == "" || (newName != nil && oldName == *newName) {
		return nil, nil
	}
	parsed, err := yamlx.SafeLoad(source)
	if err != nil {
		return nil, lre("Mihomo 基础配置无法解析，未修改策略组或入口引用")
	}
	document, ok := parsed.(*jsonx.Object)
	if !ok {
		return nil, lre("Mihomo 基础配置必须是对象，未修改策略组或入口引用")
	}
	listeners, err := listenersOf(document)
	if err != nil {
		return nil, err
	}
	var changed []string
	var indexes []int
	replacements := append([]any(nil), listeners...)
	for i, l := range listeners {
		listener := l.(*jsonx.Object)
		target := listener.Value("proxy")
		if s, ok := target.(string); ok && containsText(s, "${") {
			return nil, lre(fmt.Sprintf("Mihomo listeners[%d].proxy 含未解析变量，无法安全联动此策略组；原配置已保留", i))
		}
		if target != any(oldName) {
			continue
		}
		replacement := listener.DeepCopy()
		if newName == nil {
			replacement.Delete("proxy")
		} else {
			replacement.Set("proxy", *newName)
		}
		replacements[i] = replacement
		indexes = append(indexes, i)
		changed = append(changed, fmt.Sprintf("listeners[%d].proxy", i))
	}
	if len(changed) > 0 {
		document.Set("listeners", replacements)
		updated, err := commented.EditListenerTargets(source, indexes, newName, document)
		if err != nil {
			return nil, &ListenerReferenceError{Message: "无法安全保留 Mihomo 基础配置的注释与引用，原配置已保留", Cause: err}
		}
		if settings == nil {
			settings = jsonx.NewObject()
			profile.Set("mihomo", settings)
		}
		settings.Set("custom_config", updated)
	}
	return changed, nil
}

func containsText(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}

// ValidateListenerPolicies is validate_listener_policies: every fixed
// listener target must exist, even without a dialer topology.
func ValidateListenerPolicies(document *jsonx.Object) error {
	available := map[string]bool{}
	for _, b := range listenerBuiltins {
		available[b] = true
	}
	for _, key := range []string{"proxies", "proxy-groups"} {
		items, _ := iterate(document.Value(key))
		for _, item := range items {
			if o, ok := item.(*jsonx.Object); ok {
				if n, ok := o.Value("name").(string); ok {
					available[n] = true
				}
			}
		}
	}
	listeners, err := listenersOf(document)
	if err != nil {
		return err
	}
	for i, l := range listeners {
		target := l.(*jsonx.Object).Value("proxy")
		if jsonx.Truthy(target) && !available[target.(string)] {
			return lre(fmt.Sprintf("Mihomo listeners[%d].proxy 引用了不存在或未启用的策略「%s」", i, target.(string)))
		}
	}
	return nil
}
