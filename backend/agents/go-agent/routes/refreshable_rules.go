package routes

import (
	"fmt"
	"os"
	"path"
	"path/filepath"
	"strings"

	"gopkg.in/yaml.v3"
)

// refreshableRuleFiles lists published rule files the core itself replaces:
// OxiDNS download targets and Mihomo HTTP rule providers. Scheduled, manual
// and rule-sync refreshes rewrite them without a new publication, so checks
// for edits made outside ConfigFlow must not treat their new bytes as drift.
func refreshableRuleFiles(config []byte, manifest *DeploymentManifest) map[string]bool {
	var doc map[string]interface{}
	if yaml.Unmarshal(config, &doc) != nil {
		return nil
	}
	targets := make(map[string]bool)
	add := func(value string) {
		value = strings.TrimSpace(value)
		if value == "" || path.IsAbs(value) {
			return
		}
		if clean := path.Clean(value); safeRelative(clean) {
			targets[clean] = true
		}
	}
	plugins, _ := doc["plugins"].([]interface{})
	for _, item := range plugins {
		plugin, _ := item.(map[string]interface{})
		if plugin["type"] != "download" {
			continue
		}
		args, _ := plugin["args"].(map[string]interface{})
		downloads, _ := args["downloads"].([]interface{})
		for _, entry := range downloads {
			download, _ := entry.(map[string]interface{})
			dir, _ := download["dir"].(string)
			name, _ := download["filename"].(string)
			if name != "" {
				add(path.Join(dir, name))
			}
		}
	}
	providers, _ := doc["rule-providers"].(map[string]interface{})
	for _, item := range providers {
		provider, _ := item.(map[string]interface{})
		if provider["type"] == "http" {
			value, _ := provider["path"].(string)
			add(value)
		}
	}
	result := make(map[string]bool)
	for _, file := range manifest.Files {
		// Only files published as rules; configuration and attachments stay strict.
		if file.Role == "rule" && targets[file.Path] {
			result[file.Path] = true
		}
	}
	return result
}

// verifyActiveManagedFiles detects edits made outside ConfigFlow. Refreshable
// rule files must still be regular files in place, but may hold newer bytes.
func verifyActiveManagedFiles(root string, manifest *DeploymentManifest, config []byte) error {
	refreshable := refreshableRuleFiles(config, manifest)
	strict := *manifest
	strict.Files = nil
	for _, file := range manifest.Files {
		if !refreshable[file.Path] {
			strict.Files = append(strict.Files, file)
			continue
		}
		if err := rejectSymlinkPath(root, file.Path); err != nil {
			return err
		}
		info, err := os.Lstat(filepath.Join(root, filepath.FromSlash(file.Path)))
		if err != nil || !info.Mode().IsRegular() {
			return fmt.Errorf("managed rule file missing: %s", file.Path)
		}
	}
	return verifyManifestFiles(root, &strict)
}
