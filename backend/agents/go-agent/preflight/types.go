// Package preflight inspects the local environment before native Agent installation.
// Run is read-only. Explicit PrepareTUN may load the driver and persist module
// loading, but neither operation starts cores, binds ports or creates interfaces.
package preflight

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"unicode"

	"gopkg.in/yaml.v3"
)

// Config contains only the installation metadata needed for environment checks.
// Registration credentials are deliberately not decoded or printed.
type Config struct {
	AgentConfigFile  string `json:"-"`
	AgentName        string `json:"agent_name"`
	AgentHost        string `json:"agent_host"`
	AgentPort        int    `json:"agent_port"`
	AgentServiceUnit string `json:"agent_service_unit"`
	ServiceType      string `json:"service_type"`
	ServiceManager   string `json:"service_manager"`
	ServiceUnit      string `json:"service_unit"`
	ServiceBinary    string `json:"service_binary"`
	ConfigPath       string `json:"config_path"`
	NativeAIO        bool   `json:"native_aio"`
}

type Check struct {
	Name    string
	Level   string // pass, warn or error
	Message string
	Fix     string
}

type Report []Check

func (r Report) HasErrors() bool {
	for _, check := range r {
		if check.Level == "error" {
			return true
		}
	}
	return false
}

// Print keeps values on one line, so a filename cannot forge another result or
// inject terminal control sequences. Repair instructions are explanatory only.
func (r Report) Print(out io.Writer) {
	r.print(out, false)
}

// PrintPreparation deliberately distinguishes the opt-in installer mutation
// from the read-only environment report.
func (r Report) PrintPreparation(out io.Writer) {
	r.print(out, true)
}

func (r Report) print(out io.Writer, preparation bool) {
	if preparation {
		fmt.Fprintln(out, "TUN 安装准备（本机可加载驱动并设置开机加载；容器仅检查设备）")
	} else {
		fmt.Fprintln(out, "安装前环境检查（仅检测，不自动修改系统）")
	}
	warnings, failures := 0, 0
	for _, check := range r {
		label := "通过"
		switch check.Level {
		case "warn":
			label = "提醒"
			warnings++
		case "error":
			label = "失败"
			failures++
		}
		fmt.Fprintf(out, "[%s] %s：%s\n", label, printable(check.Name), printable(check.Message))
		if check.Fix != "" {
			fmt.Fprintf(out, "  处理方法：%s\n", printable(check.Fix))
		}
	}
	if preparation && failures > 0 {
		fmt.Fprintf(out, "TUN 准备未完成（%d 项错误，%d 项提醒）；安装已停止，请按上方结果处理后重跑。已成功加载的驱动不会卸载。\n", failures, warnings)
	} else if preparation {
		fmt.Fprintf(out, "TUN 准备完成（%d 项提醒）；继续检查核心权限、网络和端口。实际核心运行与重启后的状态仍需验证。\n", warnings)
	} else if failures > 0 {
		fmt.Fprintf(out, "环境检查未通过（%d 项错误，%d 项提醒）；安装已停止，请处理后重跑脚本。\n", failures, warnings)
	} else {
		fmt.Fprintf(out, "未发现阻止安装的环境问题（%d 项提醒）。检测结果仅代表当前状态；服务启动及重启后的实际效果仍需验证。\n", warnings)
	}
}

func printable(value string) string {
	return strings.Map(func(r rune) rune {
		if unicode.IsControl(r) || r == '\u2028' || r == '\u2029' {
			return ' '
		}
		return r
	}, value)
}

func LoadConfig(path string) (Config, error) {
	var cfg Config
	data, err := os.ReadFile(path)
	if err != nil {
		return cfg, fmt.Errorf("无法读取 Agent 候选配置：%s", printable(path))
	}
	if err := json.Unmarshal(data, &cfg); err != nil {
		return cfg, fmt.Errorf("Agent 候选配置不是有效 JSON：%s", printable(path))
	}
	if cfg.ServiceType != "mihomo" && cfg.ServiceType != "mosdns" && cfg.ServiceType != "oxidns" {
		return cfg, fmt.Errorf("环境检查不支持该服务类型")
	}
	if cfg.AgentPort < 1 || cfg.AgentPort > 65535 || !filepath.IsAbs(cfg.ConfigPath) {
		return cfg, fmt.Errorf("Agent 端口或核心配置路径无效，请检查安装参数")
	}
	cfg.AgentConfigFile = path
	return cfg, nil
}

func loadCoreConfig(cfg Config) (map[string]interface{}, error) {
	data, err := os.ReadFile(cfg.ConfigPath)
	if err != nil {
		return nil, fmt.Errorf("无法读取 %s 核心配置，请检查安装路径与读取权限", cfg.ServiceType)
	}
	var doc map[string]interface{}
	if err := yaml.Unmarshal(data, &doc); err != nil || len(doc) == 0 {
		// YAML errors can quote credentials or entire configuration lines.
		return nil, fmt.Errorf("%s 核心配置需要是有效、非空的 YAML 对象", cfg.ServiceType)
	}
	return doc, nil
}

func Run(configs []Config) Report {
	if len(configs) == 0 {
		return Report{{Name: "配置", Level: "error", Message: "没有待检查的 Agent 配置"}}
	}
	var report Report
	for _, cfg := range configs {
		if _, err := loadCoreConfig(cfg); err != nil {
			report = append(report, Check{Name: cfg.ServiceType + " 配置", Level: "error", Message: err.Error(), Fix: "先安装核心并确认配置路径正确，再重新生成安装脚本。"})
		}
	}
	// Continue collecting independent diagnostics even if one core is invalid.
	report = append(report, systemChecks(configs)...)
	report = append(report, portChecks(configs)...)
	return report
}
