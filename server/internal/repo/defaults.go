package repo

import (
	_ "embed"

	"configflow/internal/jsonx"
)

// configTemplate is a copy of backend/config_template.json, the first-start
// template (go:embed cannot reach outside the package directory;
// TestConfigTemplateMatchesBackend keeps the copy identical).
//
//go:embed config_template.json
var configTemplate []byte

func list(items ...any) []any {
	result := make([]any, len(items))
	for index, item := range items {
		if number, ok := item.(int); ok {
			item = jsonx.Int(number)
		}
		result[index] = item
	}
	return result
}

// GetDefaultOxidnsSettings is oxidns_settings.get_default_oxidns_settings.
func GetDefaultOxidnsSettings() *jsonx.Object {
	o := jsonx.Obj
	return o(
		"config_mode", "template", "native_config", "", "additional_files", list(),
		"direct_rulesets", list(), "proxy_rulesets", list(), "direct_rules", list(), "proxy_rules", list(),
		"local_dns", "", "remote_dns", "", "fallback_dns", "",
		"default_forward", "forward_remote", "custom_hosts", "", "custom_config", "",
		"custom_matches", list(), "custom_match_position", "tail", "device_policies", list(),
		"worker_threads", 2, "listen", "0.0.0.0:53", "tcp_enabled", true,
		"prefer_ipv4", true, "block_qtypes", list(65, 255), "metrics_enabled", true,
		"aggregate_rules", true, "local_concurrent", 2, "remote_concurrent", 1,
		"fallback_concurrent", 2, "fallback_threshold", 500, "always_standby", false,
		"remote_ttl", 1, "cache_enabled", true, "cache_size", 10240,
		"cache_lazy_ttl", 3600, "cache_dump_enabled", false,
		"cache_dump_file", "./state/cache.dump", "cache_dump_interval", 300,
		"probe_cache_enabled", true, "probe_cache_size", 2048,
		"api", o("enabled", true, "listen", "127.0.0.1:9199", "username", "",
			"password", "", "webui_root", "", "ssl", o()),
		"log", o("level", "info", "file", "", "rotation", "daily", "max_files", 7),
		"query_recorder", o("enabled", false, "args", o(
			"path", "./state/queries.db", "queue_size", 8192, "batch_size", 256,
			"flush_interval_ms", 200, "memory_tail", 1024, "retention_days", 7,
			"cleanup_interval_hours", 1, "reader_concurrency", 2)),
		"ip_selector", o("enabled", false, "args", o(
			"selection_mode", "background", "probe_methods", list("tcp:443", "tcp:80"),
			"probe_stagger", 200, "probe_timeout", 600, "max_wait", 1000, "top_n", 0,
			"dnssec_policy", "reorder_only", "max_parallel_probes", 64,
			"cache", o("enabled", true, "size", 4096, "ttl", 3600, "failure_ttl", 60))),
		"dynamic_domain_set", o("enabled", false, "route", "direct", "learn", false,
			"args", o("path", "./state/learned-domains.txt", "bootstrap_rules", list(),
				"queue_size", 1024, "batch_size", 256, "flush_interval_ms", 200),
			"learn_args", o("phase", "after", "questions", "first", "qtypes", list("A", "AAAA"),
				"success_only", true, "answer_required", true, "rule_kind", "full",
				"async", true, "error_mode", "continue", "timeout", "1s")),
		"adguard", o("enabled", false, "content", "", "urls", list(), "mode", "nxdomain"),
		"ros_address_list", o("enabled", false, "args", o(
			"address", "", "username", "", "password", "", "async", true,
			"address_list4", "oxidns", "address_list6", "oxidns",
			"min_ttl", 60, "max_ttl", 3600, "queue_capacity", 16384,
			"comment_prefix", "configflow", "cleanup_on_shutdown", true)),
		"ros_route", o("enabled", false, "args", o(
			"address", "", "username", "", "password", "", "async", true,
			"routing_table", "", "gateway4", "", "gateway6", "", "distance", 100,
			"min_ttl", 60, "max_ttl", 3600, "queue_capacity", 16384,
			"comment_prefix", "configflow", "cleanup_on_shutdown", true)),
		"rule_update", o("enabled", false, "args", o(
			"schedule", "30 3 * * *", "timezone", "Asia/Shanghai", "timeout", "60s",
			"outbound", "", "socks5", "")),
		"rule_actions", o(), "rule_dns_follow", list(), "rule_dns_parked", o(),
	)
}

func defaultMosdnsSettings() *jsonx.Object {
	return jsonx.Obj(
		"direct_rulesets", list(),
		"proxy_rulesets", list(),
		"direct_rules", list(),
		"proxy_rules", list(),
		"local_dns", "",
		"remote_dns", "",
		"fallback_dns", "",
		"default_forward", "forward_remote",
		"custom_hosts", "",
		"custom_config", "",
		"custom_matches", list(),
		"custom_match_position", "tail",
		"cache_enabled", true,
		"cache_size", 10240,
		"cache_lazy_ttl", 21600,
		"cache_dump_enabled", true,
		"cache_dump_file", "./cache.dump",
		"cache_dump_interval", 300,
	)
}

// defaultProfileConfig is config_repository._default_profile_config.
func defaultProfileConfig() *jsonx.Object {
	return jsonx.Obj(
		"subscriptions", list(),
		"nodes", list(),
		"subscription_aggregations", list(),
		"rule_configs", list(),
		"rule_library", list(),
		"proxy_groups", list(),
		"mihomo", jsonx.Obj("custom_config", ""),
		"oxidns", GetDefaultOxidnsSettings(),
		"mosdns", defaultMosdnsSettings(),
		"surge", jsonx.Obj("custom_config", "", "smart_groups", list()),
		"loon", jsonx.Obj("custom_config", ""),
		"routers", jsonx.Obj("items", list()),
	)
}

// GetDefaultConfig is common/config.py::get_default_config, the
// default_config_factory of the application's repository.
func GetDefaultConfig() *jsonx.Object {
	return jsonx.Obj(
		"subscriptions", list(),
		"nodes", list(),
		"rule_configs", list(),
		"proxy_groups", list(),
		"rule_library", list(),
		"system_config", jsonx.Obj(
			"server_domain", "",
			"github_proxy_domain", "",
			"rule_fetch_proxy", "",
		),
		"subscription_aggregations", list(),
		"mihomo", jsonx.Obj("custom_config", ""),
		"oxidns", GetDefaultOxidnsSettings(),
		"mosdns", defaultMosdnsSettings(),
	)
}

// GetInitialConfig is common/config.py::_get_initial_config: the defaults
// with the first-start template merged over them. An unreadable template is
// ignored, as in Python.
func GetInitialConfig() *jsonx.Object {
	config := GetDefaultConfig()
	if value, err := jsonx.Parse(configTemplate); err == nil {
		if template, ok := value.(*jsonx.Object); ok {
			config = configDeepMerge(config, template)
		}
	}
	return config
}

// Field groups (module constants of config_repository.py).
var (
	SharedFields   = []string{"subscriptions", "nodes", "subscription_aggregations", "rule_library"}
	ResourceFields = SharedFields[:3]
	ProfileFields  = []string{"proxy_groups", "rule_configs", "mihomo", "surge", "mosdns", "oxidns", "loon",
		"domain_discovery", "routers", "device_notes", "client_fixed_ips"}
	RuleSourceFields       = []string{"name", "url", "behavior", "content", "source_type", "format"}
	RuleDNSFields          = []string{"rule_actions", "rule_dns_follow", "rule_dns_parked"}
	RouterIdentityFields   = []string{"type", "endpoint"}
	RouterConnectionFields = []string{"type", "endpoint", "username", "password", "verify_tls"}
	RouterManifestFields   = []string{"ros_policy_state", "dns_guard_state", "ipv6_fakeip_state", "ipv6_routes_state",
		"ipv6_pd_guard_state"}
	AgentHistoryFields = []string{"deployments", "ipv6_verifications"}
	systemSections     = []string{"system_config", "backup", "agents"}
)

// profileObjectFields are the profile fields whose missing default is {}.
var profileObjectFields = map[string]bool{"mihomo": true, "surge": true, "mosdns": true, "oxidns": true, "loon": true,
	"domain_discovery": true, "routers": true, "device_notes": true, "client_fixed_ips": true}

// IsProfileField reports whether key is one of ProfileFields.
func IsProfileField(key string) bool {
	return profileObjectFields[key] || key == "proxy_groups" || key == "rule_configs"
}

func isBuiltinPolicy(value any) bool {
	return value == "DIRECT" || value == "REJECT"
}
