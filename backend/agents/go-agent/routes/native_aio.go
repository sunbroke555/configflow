package routes

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
)

type nativeAIOInstallation struct {
	Directory string
	Unit      string
	Log       string
	Manager   string
}

// Native AIO deliberately owns one fixed directory and service per core. Never
// infer an uninstall target from an arbitrary Agent config directory.
func nativeAIOInstallationFor(cfg *Config) (nativeAIOInstallation, error) {
	var instance nativeAIOInstallation
	if !cfg.NativeAIO || (cfg.ServiceType != "mihomo" && cfg.ServiceType != "oxidns") {
		return instance, fmt.Errorf("unsupported native AIO service")
	}
	instance = nativeAIOInstallation{
		Directory: "/opt/configflow-agent/" + cfg.ServiceType,
		Unit:      "configflow-agent-" + cfg.ServiceType,
		Log:       "/var/log/configflow-agent-" + cfg.ServiceType + ".log",
		Manager:   cfg.ServiceManager,
	}
	if cfg.AgentConfigFile != filepath.Join(instance.Directory, "config.json") || strings.TrimSuffix(cfg.AgentServiceUnit, ".service") != instance.Unit {
		return nativeAIOInstallation{}, fmt.Errorf("native AIO Agent path or service unit does not match its managed installation")
	}
	if instance.Manager != "systemd" && instance.Manager != "openrc" {
		return nativeAIOInstallation{}, fmt.Errorf("native AIO uninstall requires systemd or OpenRC")
	}
	return instance, nil
}

func (instance nativeAIOInstallation) validateDirectory() error {
	for _, path := range []string{filepath.Dir(instance.Directory), instance.Directory} {
		info, err := os.Lstat(path)
		if err != nil {
			return err
		}
		if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
			return fmt.Errorf("native AIO installation directory must not be a symlink")
		}
	}
	return nil
}

func quoteNativeArgument(value string) string {
	return "'" + strings.ReplaceAll(value, "'", "'\\''") + "'"
}

func (instance nativeAIOInstallation) uninstallScript() string {
	unit := quoteNativeArgument(instance.Unit)
	remove := "rm -rf -- " + quoteNativeArgument(instance.Directory) + "\nrm -f -- " + quoteNativeArgument(instance.Log) + " " + quoteNativeArgument(instance.Log) + ".* " + quoteNativeArgument("/etc/logrotate.d/"+instance.Unit) + "\n"
	if instance.Manager == "systemd" {
		return "set -e\nsystemctl disable " + unit + "\nsystemctl stop " + unit + "\n" + remove +
			"rm -f -- " + quoteNativeArgument("/etc/systemd/system/"+instance.Unit+".service") + "\nsystemctl daemon-reload\n"
	}
	return "set -e\nrc-update del " + unit + " default\nrc-service " + unit + " stop\n" + remove +
		"rm -f -- " + quoteNativeArgument("/etc/init.d/"+instance.Unit) + "\nrc-update -u\n"
}

// Run cleanup outside the Agent service so stopping it cannot kill cleanup
// halfway through. The OpenRC child has its own session; systemd gets its own
// transient unit and therefore its own service control group.
func (instance nativeAIOInstallation) startUninstall() error {
	if err := instance.validateDirectory(); err != nil {
		return err
	}
	if instance.Manager == "systemd" {
		out, err := exec.Command("systemd-run", "--quiet", "--collect", "--unit=configflow-uninstall-"+instance.Unit,
			"/bin/sh", "-c", instance.uninstallScript()).CombinedOutput()
		if err != nil {
			return fmt.Errorf("could not start native AIO uninstall: %w: %s", err, strings.TrimSpace(string(out)))
		}
		return nil
	}
	cmd := exec.Command("/bin/sh", "-c", instance.uninstallScript())
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := cmd.Start(); err != nil {
		return err
	}
	go cmd.Wait()
	return nil
}

// AgentLogPath selects only this installation's native Agent log.
func AgentLogPath(cfg *Config) string {
	if cfg.NativeAIO {
		if cfg.ServiceType == "mihomo" || cfg.ServiceType == "oxidns" {
			return "/var/log/configflow-agent-" + cfg.ServiceType + ".log"
		}
		return ""
	}
	return "/var/log/configflow-agent.log"
}
