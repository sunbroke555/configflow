# Linux 选择性网关实测结果 · 2026-10-19

最终代码在隔离的 Debian 13.7 ARM64 / systemd 虚拟机中通过安装、发布、真实转发、失败回滚与重试、撤销测试。完整主流程于北京时间 14:31:06–14:32:26 运行；随后在同一环境完成真实发布失败恢复。报告对应当前未提交的工作区源码，以源码和二进制 SHA256 标识，不代表某个已发布版本。

## 验收结果

| 场景 | 实际验证 | 结果 |
| --- | --- | --- |
| 全新安装 | 从无核心/Agent 文件的状态下载并校验真实产物，安装两个核心和 Agent，完成服务器登记 | 通过；安装结束 TUN 关闭，网络与安装前相同 |
| 下载失败重试 | 故意破坏服务器提供的测试 Agent 文件，使真实 SHA256 校验失败；恢复后重跑同一安装会话 | 通过；失败时未改核心与网络，同会话重试成功 |
| 普通模式 | 执行真实普通 AIO 安装菜单 | 通过；未启用转发，核心文件内容、权限、mtime、Agent ID、网络保持不变 |
| 双核心发布 | 真实 API 和 Agent 事务先发布并验证 Mihomo，再发布并验证 OxiDNS | 通过 |
| 只读检查 | status / preview 前后对比网络快照 | 通过；未提前应用转发 |
| 直连流量 | 客户端访问未选中 IP，联合检查目标日志、Mihomo controller 和 SOCKS 日志 | 通过；经 Linux SNAT 转发，Mihomo / SOCKS 无该连接 |
| 选择性接管 | 分别访问所选规则集的真实 IP、由 DNS 得到的 fake-IP | 通过；两者均有 Mihomo RuleSet 命中与 SOCKS 连接证据 |
| DNS 链路 | 客户端 UDP/TCP 请求 OxiDNS :53；随机代理域名触发 OxiDNS → 127.0.0.1:1053 | 通过；有双向 DNS 包，Mihomo :53 DNAT 计数没有增长 |
| 已有核心接入 | 移走实验安装器标记，按已有标准目录核心接入，并更改安装名称 | 通过；不重复下载核心，发布前核心文件与元数据、Agent ID 保留；再次发布和转发成功 |
| 发布失败与回滚 | 一次性 systemd ExecStartPre 让 OxiDNS 候选配置启动失败 | 通过；Agent 自动恢复旧 managed 文件及权限，旧核心与 DNS 恢复健康 |
| 同会话发布重试 | 清除故障后调用 retry_failed=true | 通过；仅创建新的 OxiDNS 任务；Mihomo 任务、PID、配置和 active-manifest 元数据不变 |
| 撤销 | 新装、已有核心两条流程都调用实际 remove API | 通过；恢复各自 apply 前的完整网络快照，自有表和 journal 消失 |
| 清理隔离环境 | 删除实验 namespace / drop-in，停止并禁用实验服务，关闭并销毁 VM | 通过；客体初始网络、NSS 文件及宿主默认路由/接口清单均保持不变 |

已有核心用例由本次实验的新装核心构造，覆盖标准目录接入，不代表所有第三方安装方式。最终复跑前清理并核验了实验专属安装文件；VM 的基础系统和依赖保留。完整复跑没有中途接续或跳过步骤。

## 实测发现并修复的问题

1. **nftables 序列化不一致。** nft 1.1.3 将 conntrack state 的集合比较序列化为 `==`，旧归属检查把自己创建的表误判为外部修改。现在兼容这一明确的等价表达式，并保留注释、handle、顺序和其他表达式检查；旧 Pending journal 也通过真实恢复验证。
2. **Mihomo 自动重定向抢占 OxiDNS DNS。** `dns-hijack: any:1053` 无法阻止所测核心生成的端口 53 DNAT。网关发布现在由目标 Agent 写入网关 LAN IPv4 `/32` 和 loopback 排除项，Mihomo DNS 绑定 `127.0.0.1:1053`；无网关标记的发布不进入该适配。真实包与计数器验证客户端 DNS 到达 OxiDNS。
3. **重试重复部署已经成功的核心。** 会话重试现在保留成功或尚未确定结果的任务，只为失败、已回滚或缺失的任务重新建 ID。真实故障后 Mihomo PID `15871` 在重试前后不变，OxiDNS 独立重试成功。
4. **已有完整核心仍依赖 GitHub 再下载。** 安装器现在直接校验完整已有核心，只下载缺少的核心；仍校验新下载核心的固定版本和 SHA256。最终已有接入约 1 秒完成，文件与身份保留。
5. **原生 OxiDNS 关闭 fallback 时的准备兼容。** 向导准备阶段接受已识别的标准关闭布局，再生成网关所需 fallback；保存后的就绪检查继续要求正确启用。自定义、不识别的链路仍拒绝自动改写。

实验工具也修复了跨文件系统移动标记、空 controller 连接列表和 namespace 内 NSS 隔离。`libnss_resolve` 的全局 socket 会绕过 namespace DNS，最终工具为每个 namespace 绑定独立 `hosts: files dns`，不改客体初始 NSS。实验工具问题与产品修复分开记录；最终结果使用修复后的完整工具运行。

## 环境与证据

- QEMU 11.1.2，ARM64 HVF，Debian 官方 cloud image，Linux `6.12.113+deb13-cloud-arm64`，nftables `1.1.3`，Python `3.13.5`。
- Agent `1.8.20-go`、Mihomo `1.19.32`、OxiDNS `1.6.1`，均为实际运行的 Linux ARM64 二进制。
- QEMU 仅使用 user networking，SSH 只绑定宿主 `127.0.0.1`；客户端、网关、路由器、代理/HTTP 探针分别在客体 network namespace 中。宿主未创建 TAP / bridge，未执行转发或防火墙命令。
- 测试使用临时 ConfigFlow 仓库、账户和凭据，未接入现有服务。虚拟磁盘、启动 seed 和临时 SSH 密钥已删除。为运行实验安装的 QEMU 工具保留在开发机中。
- 后端与 Shell 副本摘要与工作区一致；以最终 Go 源码重新交叉编译得到的 Agent SHA256 与实测二进制相同：`2260ebdff871c227856534beb7df09954729fd88e1dcb30b3a7407e077ea48ab`。

可复现步骤见 [隔离实验说明](LINUX_GATEWAY_LAB.md)。脱敏证据保存在 [结果目录](results/linux-gateway-2026-10-19/)：

- [完整主流程](results/linux-gateway-2026-10-19/main-report.json)：新装、普通模式、已有接入、真实流量、DNS 与两次撤销。
- [真实发布失败和重试](results/linux-gateway-2026-10-19/publication-retry-report.json)、[Agent 回滚状态摘要](results/linux-gateway-2026-10-19/rollback-evidence.json)。
- [环境和二进制摘要](results/linux-gateway-2026-10-19/environment.json)、[源码摘要](results/linux-gateway-2026-10-19/source-sha256.json)、[官方镜像校验](results/linux-gateway-2026-10-19/debian-image-verification.txt)。
- [客体清理验证](results/linux-gateway-2026-10-19/network-cleanup.json)、[宿主验证](results/linux-gateway-2026-10-19/host-safety.json)，以及安装、普通模式、两次 apply 前/remove 后和客体清理前后的网络快照。

报告中的 `/tmp`、`/var/lib` 日志和 PCAP 路径是已销毁 VM 内的原始路径；保留在 JSON 的解析包记录、连接证据和快照可以直接审阅。未导出凭据、安装脚本或完整运行配置。DNS 上游使用真实 DoH，重复探针可能命中缓存，因此 TLS 新包数为 0 不代表 DNS 请求未成功。

## 补充回归与范围

Go Agent 的 `go test ./...` 通过；另完成网关 DNS/发布边界、nft 序列化、Pending 恢复和篡改保护针对性回归。核心安装 39 个用例（原 37 个加两个混合已有/缺失核心用例）、普通 AIO 安装/API 52 个用例均通过；网关生成、安装会话、onboarding、Agent bridge 和 OxiDNS 设置相关回归也通过。Shell 语法、Python 编译和 `git diff --check` 均通过。

本次真实环境覆盖 **Debian ARM64、systemd、IPv4**。未实机验证 amd64、OpenRC、IPv6、复杂已有防火墙或第三方核心目录；代理/HTTP 目标是隔离测试服务。本轮使用 API 驱动真实安装流程，未点击浏览器安装向导，也未新增前端视觉验收结论。
