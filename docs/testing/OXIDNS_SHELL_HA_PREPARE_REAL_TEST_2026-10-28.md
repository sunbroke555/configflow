# Shell OxiDNS 高可用一键安装：多系统真实验证

执行时间：2026-10-27 23:35 至 2026-10-28 00:06（Asia/Shanghai），随后回收实验资源。

## 结论与范围

Debian 12、Alpine 3.22、Arch Linux、Fedora 43 均执行了真实 API 生成的完整安装脚本，自动安装 OxiDNS、Go Agent 与缺失的 HA 依赖，Agent 在线，外部 UDP/TCP DNS 查询成功。重复安装保留 Agent 身份、运行参数、核心配置、规则文件和已有 keepalived 配置。未配对时没有新增 VIP，keepalived 未启动、未设置开机自启，临时 systemd mask 已移除。

这是运行在 PVE 上的 **AMD64 非特权 LXC** 实测，PVE 内核为 `7.0.14-24-pve`。没有将其表述为实体 NAS 或 ARM 实测；Ubuntu、RHEL、SUSE 也没有单独真机覆盖。本轮检查安装准备，不重新测试已存在的 VIP 切换功能。

| 实际系统 | 初始化系统 | keepalived | 脚本下载工具 | 结果与前置 |
|---|---|---|---|---|
| Debian GNU/Linux 12 / systemd 252.39 | systemd | 2.2.7 | 模板已有 wget | 自动补装、新装、重跑、自定义目录保留通过 |
| Alpine Linux 3.22.0 | OpenRC | 2.3.1 | 模板已有 wget | 自动补装完整 iproute2、keepalived 与 keepalived-openrc，新装与重跑通过 |
| Arch Linux rolling / systemd 262.1 | systemd | 2.4.3 | 模板已有 curl | 临时模板先完成正常系统升级并释放 resolved stub 的 53 端口，随后自动补装、新装与重跑通过 |
| Fedora Linux 43 / systemd 258.3 | systemd | 2.2.8 | 模板已有 curl | 默认 resolved 端口冲突先安全拒绝；仅实验节点释放 stub 后新装与重跑通过 |

验证的产品提交为 `974b2e4a`，最终源码逐文件哈希另附。四系统核心均为 **OxiDNS 1.6.1 (full)**，Agent 均为 **1.8.49-go**。Agent 在测试服务器从本分支 Go 源码静态构建，通过隔离后端的真实下载入口获取；OxiDNS 走生成脚本正常下载、校验及原生服务安装流程。没有手动给测试节点安装 HA 依赖。

## 验证方法与通过项目

测试由 `192.168.0.32` 驱动 PVE，只操作本次创建的 CT190–193，以及先建立唯一快照保护的既有实验 CT132。五个节点只连接 `cfgt` 实验网络。临时放行仅包含这些实验 IP 的互访及后端回复；没有开放生产网访问。

调用实际 `/api/agents/install-command`，请求 `type=oxidns`、`install_core=true`、`prepare_dns_ha=true`；从返回的短链接下载，逐字比较下载内容与 API 返回脚本，再无 TTY 执行完整脚本。没有提取函数来代替真实安装。每份脚本和实际后端源码都记录 SHA-256。

1. **全新安装**：缺失 keepalived 时自动补装；Alpine 原 BusyBox ip 不支持 JSON，安装后 `ip -j -4 addr show` 正常；原生服务可用，OxiDNS 和 Agent 成功启动。
2. **外部解析**：从另一实验节点直接查询各节点的 UDP/53 与 TCP/53，`example.com` 返回 `rcode=0` 且答案数大于 0；不是仅检查进程或监听端口。
3. **依赖失败停止**：在空 Debian 的单次执行中注入 apt-get 退出 42。完整安装脚本失败退出，未写入 `/etc/oxidns`、Agent 目录或核心二进制，未注册 Agent，临时 keepalived mask 清理完成。实际包管理器和系统文件未被替换。
4. **重复安装**：比较前后 Agent ID/token、名称、端口、配置路径、服务管理参数；比较核心配置、规则文件及已有 keepalived 配置的 SHA-256，均保留，重跑后 DNS 与 Agent 在线状态仍正常。
5. **自定义部署**：将 Debian 的核心目录移至 `/srv/cf-shell-ha-custom`，同步原生服务工作路径与已有 Agent 的配置路径，再执行默认一键 HA 安装请求。原部署和身份保留，没有生成第二套 `/etc/oxidns`。
6. **尚未配对**：地址只有本机实验 IP 和回环地址，没有 VIP。keepalived 没有运行、没有启用开机启动；systemd 临时 mask 不再存在。新安装包的示例配置被安全处理，重跑时已有操作者配置保持不变。
7. **53 端口冲突**：Fedora 默认 systemd-resolved，以及升级后的 Arch resolved 占用 53 时，脚本先停止，原 DNS 仍 active，核心和 Agent 尚未写入。成功路径由实验准备显式设置 `DNSStubListener=no` 后单独执行，产品没有关闭系统 DNS。

## 实验中发现并修复的问题

首轮 Debian 和 Alpine 的最小 root PATH 不包含 `/usr/local/bin`。核心已经安装启动，但联合脚本父 shell 误判没有核心。修复后从干净系统重新运行完整新装，四系统最终通过。最终测试使用重新启动的隔离后端及新生成的短链接，没有复用修复前的 Python 模块或下载快照。

Arch 模板还存在两类实验前置问题：PVE 内核不支持 pacman 下载沙箱使用的 Landlock；模板的仓库索引与签名钥匙环陈旧。因此只在临时 CT 内设置 `DisableSandbox`、运行标准 `pacman-key --init` / `--populate archlinux` 和 `pacman -Syu`，保留包签名验证，没有修改 PVE 或产品安装器。这属于实验容器适配，不能据此声称任意旧 Arch 环境都无需维护就能安装。

Alpine 的包没有创建 keepalived 默认配置；尚未配对时 `rc-service keepalived status` 会先报告配置不存在。因此实验直接核对 OpenRC started 状态、runlevel 和进程是否存在，未为通过断言创建或启动 VIP 配置。

## 证据与复现

- [实验驱动](../../scripts/tests/oxidns_shell_ha_prepare_lab.py)：运行在测试服务器，包含资源所有权检查、真实下载/执行、DNS 验证、配置保留检查和回收。
- [场景结果与生成脚本 SHA-256](results/oxidns-shell-ha-prepare-2026-10-28/report.json)：保留探索轮和最终轮，最终源码对应 `final-product-source-verified`，四系统成功场景为 `full-install-and-idempotent-repeat-*`。
- [实际版本、OS、最终在线及 DNS 结果](results/oxidns-shell-ha-prepare-2026-10-28/metadata.json)。
- [最终源码 SHA-256](results/oxidns-shell-ha-prepare-2026-10-28/source-hashes.json)：包含安装输出包装器、Agent/核心生成器、API 路由和 shell 模板；与隔离后端文件逐项核对。
- [浏览器 Shell 安装入口](results/oxidns-shell-ha-prepare-2026-10-28/oxidns-shell-ha-entry-browser.json)及 [Docker Host/macvlan 回归](results/oxidns-shell-ha-prepare-2026-10-28/oxidns-docker-compose-compact-browser.json)。浏览器记录由主任务独立执行。

- [测试预览 Agent 下载检查](results/oxidns-shell-ha-prepare-2026-10-28/preview-agent-downloads.json)：amd64、arm64、armv7 的真实 HTTP 下载均为 200 且字节等于构建文件；amd64 与本次四系统实测二进制相同。ARM 仅构建和下载验证，没有运行验证。

报告不保存安装口令、注册 token、登录凭据或完整私人日志。页面继续使用 curl 短命令；本次无 curl 的基础模板采用已有 wget 获取同一短链接，未声称这些模板预装 curl。

## 回收与既有服务

回收结果见 [cleanup.json](results/oxidns-shell-ha-prepare-2026-10-28/cleanup.json)；测试服务器既有服务检查见 [server-health.json](results/oxidns-shell-ha-prepare-2026-10-28/server-health.json)。

CT190–193 和本次下载的三个模板已删除；CT132 已回滚到实验前快照，配置逐字一致并恢复 stopped，临时快照已删除；PVE 原 nft 表逐字恢复。`192.168.0.32:15800` 和 `:15810` 的 `/login` 均返回 HTTP 200；默认 docker.service、docker.socket、containerd.service 与已知基线一致，均 inactive。本轮未启动它们。

取回脱敏证据后，本次独立测试目录、临时登录凭据、私人原始日志和实验 Agent 二进制均已删除；预览部署使用独立保留的二进制副本。
