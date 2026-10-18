package routes

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

// Use actual names, since Mihomo locates databases case-insensitively. Scanning
// entries also avoids reporting duplicate aliases on case-insensitive hosts.
func mihomoGeoAssetNames(root string) []string {
	assets := []string{}
	entries, _ := os.ReadDir(root)
	for _, entry := range entries {
		for _, known := range mihomoGeoAssets {
			if strings.EqualFold(entry.Name(), known) {
				if info, err := entry.Info(); err == nil && info.Mode().IsRegular() && info.Size() > 0 {
					assets = append(assets, entry.Name())
				}
				break
			}
		}
	}
	return assets
}

var geoDownloadFailure = regexp.MustCompile(`(?i)(?:can't|cannot|failed to) download (GeoSite\.dat|GeoIP\.dat|Country\.mmdb|geoip\.metadb|geoip\.db|ASN\.mmdb|MMDB|ASN)\b`)

// Native output can contain nonfatal errors before the actual download failure.
// Keep its full diagnostics but present the resource and a usable repair path.
func mihomoResourceFailure(cfg *Config, err error) error {
	_, details := deploymentErrorFields(err)
	match := geoDownloadFailure.FindStringSubmatch(details)
	if match == nil {
		return err
	}
	asset, label, key := match[1], "GeoIP", "mmdb"
	switch strings.ToLower(asset) {
	case "geosite.dat":
		asset, label, key = "GeoSite.dat", "GeoSite", "geosite"
	case "geoip.dat":
		asset, key = "GeoIP.dat", "geoip"
	case "asn", "asn.mmdb":
		asset, label, key = "ASN.mmdb", "ASN", "asn"
	case "mmdb":
		asset = "geoip.metadb"
	}
	target := filepath.Join(filepath.Dir(cfg.ConfigPath), asset)
	details += fmt.Sprintf("\n目标文件：%s\n修复：更换 geox-url.%s；更新 Agent 后可由面板使用「GitHub 代理域名」和「规则下载代理」补齐资源。\n也可手动下载对应文件，放到 Agent 机器的 %s，确保 Mihomo 可读取，再重新发布。", target, key, target)
	return &deploymentDiagnosticError{
		summary: label + " 下载失败，本次发布已停止，设备仍使用原配置。",
		details: boundedDiagnosticTail(sanitizeDiagnostic(details), maxDeploymentErrorDetails),
		cause:   err,
	}
}
