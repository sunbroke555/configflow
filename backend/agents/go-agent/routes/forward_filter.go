package routes

import (
	"agent/netfilter"
	"context"
	"os"
	"strconv"
	"sync"
	"time"

	"gopkg.in/yaml.v3"
)

// Full-TUN hosts only get a warning about FORWARD filtering. Nothing here
// changes the firewall, blocks a request, or alters any other status field.

var forwardFilterInspect = func(ctx context.Context, target netfilter.Target) netfilter.Result {
	return netfilter.Inspect(ctx, netfilter.DefaultProbe(), target)
}

var forwardFilterNow = time.Now

const forwardFilterTTL = 30 * time.Second

var forwardFilterMu sync.Mutex

var forwardFilterCache struct {
	key     string
	at      time.Time
	result  netfilter.Result
	present bool
}

func forwardFilterNotApplicable(reason string) netfilter.Result {
	return netfilter.Result{Protocol: 1, State: netfilter.StateNotApplicable, Reason: reason, Sources: []netfilter.Source{}, Remedies: []netfilter.Remedy{}}
}

// forwardFilterTarget decides applicability from the deployed configuration
// alone. Selective Linux gateway configurations (marker present, enabled or
// not) and route-restricted TUN are not full TUN and are never inspected.
func forwardFilterTarget(cfg *Config) (netfilter.Target, *netfilter.Result) {
	if cfg == nil || cfg.ServiceType != "mihomo" {
		result := forwardFilterNotApplicable("not_mihomo")
		return netfilter.Target{}, &result
	}
	data, err := os.ReadFile(cfg.ConfigPath)
	var doc map[string]interface{}
	if err == nil {
		err = yaml.Unmarshal(data, &doc)
	}
	if err != nil || doc == nil {
		result := netfilter.Result{Protocol: 1, State: netfilter.StateUnknown, Reason: "config_error", Sources: []netfilter.Source{}, Remedies: []netfilter.Remedy{}}
		return netfilter.Target{}, &result
	}
	if _, marker := doc["x-configflow-linux-gateway"]; marker {
		result := forwardFilterNotApplicable("linux_gateway")
		return netfilter.Target{}, &result
	}
	tun, _ := doc["tun"].(map[string]interface{})
	if tun["enable"] != true || tun["auto-route"] != true {
		result := forwardFilterNotApplicable("no_full_tun")
		return netfilter.Target{}, &result
	}
	for _, key := range []string{"route-address", "route-address-set", "inet4-route-address"} {
		if entries, ok := tun[key].([]interface{}); ok && len(entries) > 0 {
			result := forwardFilterNotApplicable("route_restricted")
			return netfilter.Target{}, &result
		}
	}
	if gatewayOS != "linux" {
		result := forwardFilterNotApplicable("not_linux")
		return netfilter.Target{}, &result
	}
	device := "Meta"
	if value, ok := tun["device"].(string); ok && value != "" {
		device = value
	}
	redirect, _ := tun["auto-redirect"].(bool)
	return netfilter.Target{Device: device, AutoRedirect: redirect}, nil
}

func forwardFilterKey(cfg *Config, target netfilter.Target) string {
	info, err := os.Stat(cfg.ConfigPath)
	if err != nil {
		return ""
	}
	redirect := "0"
	if target.AutoRedirect {
		redirect = "1"
	}
	return cfg.ConfigPath + "|" + info.ModTime().UTC().Format(time.RFC3339Nano) + "|" + strconv.FormatInt(info.Size(), 10) + "|" + target.Device + "|" + redirect
}

// forwardFilterStatus is bounded by netfilter.TotalBudget plus a short grace,
// even if an inspection ignores its context; it then reports unknown.
func forwardFilterStatus(cfg *Config) netfilter.Result {
	target, fixed := forwardFilterTarget(cfg)
	if fixed != nil {
		return *fixed
	}
	forwardFilterMu.Lock()
	defer forwardFilterMu.Unlock()
	key := forwardFilterKey(cfg, target)
	now := forwardFilterNow()
	if forwardFilterCache.present && key != "" && forwardFilterCache.key == key && now.Sub(forwardFilterCache.at) < forwardFilterTTL && now.Sub(forwardFilterCache.at) >= 0 {
		result := forwardFilterCache.result
		result.Cached = true
		return result
	}
	budget := netfilter.TotalBudget
	ctx, cancel := context.WithTimeout(context.Background(), budget)
	defer cancel()
	done := make(chan netfilter.Result, 1)
	go func() { done <- forwardFilterInspect(ctx, target) }()
	var result netfilter.Result
	select {
	case result = <-done:
	case <-time.After(budget + 250*time.Millisecond):
		result = netfilter.Result{Protocol: 1, State: netfilter.StateUnknown, Reason: "timeout", Device: target.Device, AutoRedirect: target.AutoRedirect, CheckedAt: now.UTC().Format(time.RFC3339)}
	}
	if result.Sources == nil {
		result.Sources = []netfilter.Source{}
	}
	if result.Remedies == nil {
		result.Remedies = []netfilter.Remedy{}
	}
	result.Cached = false
	forwardFilterCache.key, forwardFilterCache.at, forwardFilterCache.result, forwardFilterCache.present = key, now, result, key != ""
	return result
}
