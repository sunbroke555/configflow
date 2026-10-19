package main

import (
	"agent/preflight"
	"agent/routes"
	"agent/upgrade"
	"flag"
	"fmt"
	"log"
	"os"
	"path/filepath"
)

func main() {
	// 使用命令行参数指定配置文件路径
	// Go的flag包支持单横线(-)和双横线(--)两种格式的参数
	configPath := flag.String("config", "", "Path to the configuration file")
	printVersion := flag.Bool("version", false, "Print Agent version and exit")
	cleanupOnly := flag.Bool("cleanup", false, "Reclaim completed deployment payloads and oversized Agent logs, then exit")
	upgradeWorker := flag.String("upgrade-worker", "", "Run a durable Agent update outside the Agent service")
	checkDeployments := flag.Bool("check-deployments", false, "Check deployment journals without changing files or services; do not register or listen")
	requireCleanDeployments := flag.Bool("require-clean-deployments", false, "Read-only installation preflight; exit nonzero if deployment or native Agent update journals require attention")
	recoverOnly := flag.Bool("recover-only", false, "Deprecated compatibility alias for -check-deployments; does not restore files")
	recoverAndExit := flag.Bool("recover-and-exit", false, "Recover interrupted deployments with service lifecycle, then exit without registration")
	validateService := flag.Bool("validate-service", false, "Validate OxiDNS service working directory without registration or mutation")
	checkEnvironment := flag.Bool("check-environment", false, "Inspect native installation TUN, permissions, IPv4 forwarding and port occupancy without changing the system")
	prepareTUN := flag.Bool("prepare-tun", false, "Prepare enabled TUN for native AIO: load the local driver and persist it, or explain required container host changes")
	environmentPeerConfig := flag.String("environment-peer-config", "", "Peer Agent candidate configuration for a joint native AIO environment check")

	// 解析命令行参数
	flag.Parse()
	if *prepareTUN && (*configPath == "" || *printVersion || *upgradeWorker != "" || *cleanupOnly ||
		*checkDeployments || *requireCleanDeployments || *recoverOnly || *recoverAndExit ||
		*validateService || *checkEnvironment || *environmentPeerConfig != "") {
		fmt.Fprintln(os.Stderr, "-prepare-tun 必须明确指定 -config，不能与其他操作参数一起使用。")
		os.Exit(2)
	}
	if *printVersion {
		fmt.Println(upgrade.Version)
		return
	}
	if *upgradeWorker != "" {
		if err := upgrade.Run(*upgradeWorker); err != nil {
			log.Fatalf("Agent update: %v", err)
		}
		return
	}

	// 如果没有通过命令行参数指定配置文件路径，则使用自动检测
	if *configPath == "" {
		// 获取agent配置目录，优先使用环境变量，否则使用默认路径
		agentDirs := []string{"/opt/configflow-agent", "/opt/sublink-agent"}
		if envAgentDir := os.Getenv("AGENT_DIR"); envAgentDir != "" {
			agentDirs = []string{envAgentDir}
		}
		configFiles := []string{"config-mihomo.json", "config-mosdns.json", "config-oxidns.json", "config.json"}
		for _, agentDir := range agentDirs {
			for _, configFile := range configFiles {
				fullPath := filepath.Join(agentDir, configFile)
				if _, err := os.Stat(fullPath); err == nil {
					*configPath = fullPath
					break
				}
			}
			if *configPath != "" {
				break
			}
		}
		if *configPath == "" {
			*configPath = filepath.Join(agentDirs[0], "config.json")
		}
	}

	log.Printf("Using config file: %s", *configPath)
	if *prepareTUN {
		candidate, err := preflight.LoadConfig(*configPath)
		var report preflight.Report
		if err != nil {
			report = preflight.Report{{Name: "安装配置", Level: "error", Message: err.Error(), Fix: "核对 Mihomo 安装参数并重新生成脚本。"}}
		} else {
			report = preflight.PrepareTUN(candidate)
		}
		report.PrintPreparation(os.Stdout)
		if report.HasErrors() {
			os.Exit(1)
		}
		return
	}
	if *checkEnvironment {
		paths := []string{*configPath}
		if *environmentPeerConfig != "" {
			paths = append(paths, *environmentPeerConfig)
		}
		configs := make([]preflight.Config, 0, len(paths))
		for _, path := range paths {
			candidate, err := preflight.LoadConfig(path)
			if err != nil {
				preflight.Report{{Name: "安装配置", Level: "error", Message: err.Error(), Fix: "核对两个核心的安装参数并重新生成脚本。"}}.Print(os.Stdout)
				os.Exit(1)
			}
			configs = append(configs, candidate)
		}
		report := preflight.Run(configs)
		report.Print(os.Stdout)
		if report.HasErrors() {
			os.Exit(1)
		}
		return
	}
	if !*checkDeployments && !*requireCleanDeployments && !*recoverOnly && !*recoverAndExit && !*cleanupOnly && !*validateService {
		binary, err := os.Executable()
		if err != nil {
			log.Fatal(err)
		}
		if err = upgrade.EnsureInstalled(*configPath, binary); err != nil {
			log.Fatalf("Agent migration: %v", err)
		}
	}

	// 1. 加载配置
	cfg, err := LoadConfig(*configPath)
	if err != nil {
		log.Fatalf("Fatal: Could not load configuration from %s: %v", *configPath, err)
	}
	log.Println("Configuration loaded successfully.")
	if *validateService {
		if err := routes.ValidateOxiDNSServiceDirectory(toRoutesConfig(cfg)); err != nil {
			log.Fatal(err)
		}
		return
	}
	if *cleanupOnly {
		if err := cleanupDisk(cfg); err != nil {
			log.Fatalf("Disk cleanup: %v", err)
		}
		log.Println("Disk cleanup completed.")
		return
	}

	// Recovery changes live files and can stop the core, so only an explicit
	// operator request may run it. Old installed boot gates still invoke
	// -recover-only during upgrades; keep that entry point read-only as well.
	if *recoverAndExit {
		if err := routes.RecoverDeployments(toRoutesConfig(cfg), true); err != nil {
			log.Fatalf("Fatal: Deployment recovery failed: %v", err)
		}
		log.Println("Deployment recovery completed.")
		return
	}
	if *requireCleanDeployments && cfg.NativeAIO && upgrade.Pending(filepath.Join(filepath.Dir(cfg.filePath), "configflow-agent")) {
		log.Fatal("Native AIO Agent update requires completion or recovery; installation preflight left files and services unchanged.")
	}
	if err := routes.CheckDeployments(toRoutesConfig(cfg)); err != nil {
		log.Printf("Deployment check requires attention: %v. Files and services were left unchanged; inspect the journal and use -recover-and-exit to recover explicitly.", err)
		if *requireCleanDeployments {
			os.Exit(1)
		}
	} else {
		log.Println("Deployment check completed; no recovery required.")
	}
	if *checkDeployments || *requireCleanDeployments || *recoverOnly {
		return
	}

	// 2. 如果未注册，执行注册流程
	if cfg.AgentID == "" || cfg.Token == "" {
		if err := cfg.RegisterAgent(); err != nil {
			log.Fatalf("Fatal: Agent registration failed: %v", err)
		}
		// 注册成功后，配置已更新并保存
	} else {
		log.Printf("Agent already registered with ID: %s", cfg.AgentID)
	}

	// 3. 在后台启动心跳循环
	startDiskCleanup(cfg)
	StartHeartbeatLoop(cfg)
	routes.ReapplyReturnRouting(toRoutesConfig(cfg))
	routes.ReapplyLinuxGateway(toRoutesConfig(cfg))
	routes.StartDNSWatchdog(toRoutesConfig(cfg))

	// 4. 在前台启动 API 服务器 (这是一个阻塞操作)
	StartAPIServer(cfg)
}
