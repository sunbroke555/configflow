package rules

import (
	"errors"
	"strings"
	"testing"

	"configflow/internal/jsonx"
)

// strategy_references.validate_rule_policies is exercised in Python through the
// Surge/Loon/Mihomo converters (test_loon_generation.py and others, left for
// packages #6/#7); these tests pin the function itself.

func TestValidateRulePolicies(t *testing.T) {
	config := func(rules ...any) *jsonx.Object { return obj("rule_configs", rules) }
	ok := []struct {
		format string
		config *jsonx.Object
	}{
		{"mihomo", config(obj("itemType", "rule", "policy", "Proxy A"), obj("itemType", "ruleset", "policy", "DIRECT"), obj("itemType", "ruleset"))},
		{"loon", config(obj("itemType", "ruleset"), obj("itemType", "rule", "policy", "REJECT-IMG"))},
		{"surge", config(obj("itemType", "rule", "policy", "CELLULAR"))},
		{"mihomo", config(obj("itemType", "rule", "policy", "Gone", "enabled", false), obj("itemType", "rule", "policy", "Gone", "library_enabled", false), obj("itemType", "other", "policy", "Gone"))},
	}
	for _, c := range ok {
		if err := ValidateRulePolicies(c.config, []string{"Proxy A", "PROXY", "Proxy"}, c.format); err != nil {
			t.Errorf("%s %s rejected: %v", c.format, jsonx.Repr(c.config), err)
		}
	}
	err := ValidateRulePolicies(config(obj("id", "r1", "itemType", "rule", "policy", "Missing")), []string{"Proxy A"}, "loon")
	var reference *StrategyReferenceError
	if !errors.As(err, &reference) || err.Error() != "规则 r1 引用了不存在或未启用的策略 'Missing'，请修改策略引用" {
		t.Errorf("err = %v", err)
	}
	// A rule without a policy targets "" and fails; a rule set defaults to the main strategy.
	if err := ValidateRulePolicies(config(obj("id", "r2", "itemType", "rule")), nil, "mihomo"); err == nil || !strings.Contains(err.Error(), "''") {
		t.Errorf("empty policy: %v", err)
	}
	if err := ValidateRulePolicies(config(obj("name", "set", "itemType", "ruleset")), nil, "surge"); err == nil || !strings.Contains(err.Error(), "'Proxy'") {
		t.Errorf("default surge policy: %v", err)
	}
	if err := ValidateRulePolicies(config(obj("itemType", "ruleset")), nil, "mihomo"); err == nil || !strings.Contains(err.Error(), "'PROXY'") {
		t.Errorf("default mihomo policy: %v", err)
	}
}
