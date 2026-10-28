# OxiDNS 精简 Compose 真机验收（2026-10-27）

25 项真实场景通过。Host 保留独立 HA companion，macvlan 将 Agent、OxiDNS、keepalived 放在一个容器内；两种模式均与另一台 Shell 安装的 OxiDNS 成功组成 HA。所有构建、测试与实验控制均从 `192.168.0.32` 执行。

## 版本与实验范围

- 产品源码：`9d2d77cd2b8e4992389bf3e1c31569268b1e0c5a`，基于 `upstream/main f8e9f2ef`；Agent `1.8.49-go`。
- 实际 Docker Engine `29.9.2`，实际 Compose `2.20.0`；官方 Compose 二进制 SHA256：`b49e358d11c198fa228fb7eca2a177affd8e1e33e06d29940668668482f797cd`。
- CT130 `cf-oxi-a-retest`：原生 Shell Agent/OxiDNS，`192.168.79.21`。
- CT131 `cf-oxi-b-retest`：独立 Docker daemon；Host 地址 `.22`、macvlan 容器地址 `.82`。
- CT132 `cf-app-retest`：隔离后端、DNS 上游和独立第三方探测器，`.30`。VIP 为 `192.168.79.53`，仅使用 `cfgt` VLAN91。
- 测试前核对三台 CT 名称、停止状态、网络与快照列表，再创建独立恢复快照。CT131 测试期间临时从 384 MiB 调至 1024 MiB。

测试镜像为本地 `cf-compose-compact:20261027`，Linux amd64。Shell 二进制与 Docker 二进制来自同一次镜像构建。**本轮没有将镜像发布到 Docker Hub，也没有把公开 `latest` 视为已支持新入口。**

构建器的 `docker image inspect .Id` 是索引摘要 `sha256:d2feeaf5ed855df6ec3332fd32f8b9dd7ecfe0bb6fd8ce2c8efd546d2284e99e`。两个实际容器的 `inspect .Image` 均为配置摘要 `sha256:3cbdb068e3e877aa2948eb10eaf353a0c5ac589e827d6ba7634cd3aa8ae9b130`。同一次 BuildKit exporter 的实际输出同时列出了这两个摘要及镜像 manifest，见 [构建输出摘录](results/oxidns-compose-compact-2026-10-27/build-export-excerpt.txt)。此证据来自构建时捕获的工具输出；清理后没有重新读取 OCI manifest。

## 部署与持久化

| 实际生成结果 | Host | macvlan |
|---|---:|---:|
| 旧 Compose 行数（自动身份卷方案） | 166 | 128 |
| 精简后行数 | 49 | 39 |
| 精简后字节数 | 1717 | 1239 |
| 常驻容器 | 2 | 1 |
| 外部部署文件 | 仅 compose.yaml | 仅 compose.yaml |

行数取自本轮实际生成的相同安装参数；macvlan 未设置 gateway，设置 gateway 的示例会多一行。没有 `prepare.sh`、init 容器或全局身份卷，三组数据使用目录 bind mount。

两种模式都真实验证了：

- 首次启动自动初始化、注册在线、HA 环境 ready；宿主 boot 证据只读挂载。Host 主容器没有额外 capability，companion 的 Agent 数据目录只读且不含安装密钥。
- 将安装会话真实置为过期，确认登记接口返回 HTTP 401；停止隔离后端后执行 `docker compose up -d --force-recreate`，仍复用原 Agent 身份、安装身份、配置和规则，恢复后端后没有新增登记请求。
- 分别替换、删除、软链接安装身份文件，均拒绝启动，没有重注册或重写归属记录；恢复原身份后正常启动。
- 同一 CT131 上两份不同安装身份的 Host/macvlan 实例，实际宿主 boot 证据相同，配对接口拒绝将其组成 HA。
- 部署前后宿主路由、nft 规则、`resolv.conf` 内容保持一致。实验 Docker 宿主机未提供 `iptables-save`/`ip6tables-save`，这些检查明确记录为不可用。

## 配置同步、发布与 DNS

开启 HA 前，Shell/Docker 分别使用不同上游，解析到 `198.51.100.10` / `.11`，规则文件也不同。开启后两端配置及规则哈希一致，解析到预期新地址。后续发布把 `worker_threads` 从 3 改为 4，确认发生真实核心重启，并验证：

**Docker 备机启动完成 → VIP 返回新配置的解析结果 → 原 Shell 主机出现 Stopping 事件。**

排序使用每次 DNS 查询的 `completed_at`，没有把查询开始时间当作解析成功时间。发布结束后两端文件哈希再次一致。

CT132 每轮并发查询两台节点和 VIP，目标间隔 100 ms、超时 300 ms；连续探测协议为 UDP。成功样本必须为 RCODE 0 且包含实验预期地址。故障恢复还要求观察到恢复时刻之后的成功样本；各阶段样本非空。另在安装后、开启 HA 后和发布后，从 CT132 实际验证 TCP 53（节点及已存在的 VIP），均返回对应预期地址。

| 场景 | Host 样本/失败 | Host 失败样本窗口 | macvlan 样本/失败 | macvlan 失败样本窗口 |
|---|---:|---:|---:|---:|
| 正常发布（需要重启） | 350 / 0 | 0 s | 365 / 0 | 0 s |
| Shell 核心停止与恢复 | 131 / 17 | 5.115 s | 158 / 15 | 4.514 s |
| 计划切到 Docker，准备进程故障 | 90 / 0 | 0 s | 108 / 0 | 0 s |
| Docker 内仅 OxiDNS 进程停止与恢复 | 244 / 12 | 3.610 s | 266 / 14 | 4.211 s |
| 计划切到 Docker，准备容器故障 | 88 / 0 | 0 s | 107 / 0 | 0 s |
| 整个活动 Docker 容器被 KILL 与恢复 | 175 / 12 | 3.610 s | 283 / 11 | 3.309 s |

进程故障使用 `supervisorctl stop oxidns`，确认容器及 Agent 仍运行；整容器故障另用 `docker kill --signal=KILL`，两者分开记录。Host 整容器故障时独立 companion 仍存活。两种模式均实际转移 VIP、恢复正确解析，并保持原 Agent 身份。

完整连续样本分别为 Host 1800 条、macvlan 1796 条；故障阶段合计失败 41 / 40 条，所有记录阶段的“两台节点同时查询失败”样本数均为 0。失败样本窗口按首个失败查询开始至最后一个失败查询完成计算，是本轮探测观察值。正常发布本轮未观察到失败，不能据此保证零中断；故障接管存在上表所示短暂失败。

## 浏览器与清理

[浏览器报告](results/oxidns-compose-compact-2026-10-27/oxidns-docker-compose-compact-browser.json)记录了 1440px 浅色 Host、375px 深色 macvlan 两种布局，下载内容与接口响应一致、复制内容一致、单机快捷入口及旧迁移入口可达、无身份文件表单、无额外部署文件、无横向溢出。实际预览为 `http://192.168.0.32:15810`，入口资源 `index-GWEgHwyA.js`。

[清理记录](results/oxidns-compose-compact-2026-10-27/lab-cleanup.json)确认三台 CT 已恢复测试前磁盘和完整配置，均停止，本轮快照已删除；内存恢复为 384 / 384 / 1536 MiB。隔离 builder、隧道、私密凭据、镜像缓存与整个实验目录均已删除。默认 `docker`、`docker.socket`、`containerd` 仍 inactive；测试服务器 53 端口监听与测试前一致，15800/15810 均 HTTP 200，15810 仍无需账号密码。

这是同一台 PVE 上三个隔离 LXC 实验主机内的真实 Docker、Shell、VRRP/VIP 和网络解析测试；不是物理 NAS、ARM 设备或物理断电测试。未修改生产设备。

## 可复核证据

- [脱敏实验报告、摘要与源码哈希](results/oxidns-compose-compact-2026-10-27/lab-report.json)
- [Host 原始探测 JSONL.gz](results/oxidns-compose-compact-2026-10-27/probe-host.jsonl.gz)
- [macvlan 原始探测 JSONL.gz](results/oxidns-compose-compact-2026-10-27/probe-macvlan.jsonl.gz)
- [真机驱动](../../scripts/tests/oxidns_compose_compact_mixed_lab.py)

导出前在测试服务器检查上述报告与原始样本不含本轮管理员密码、JWT 密钥、登记密钥、Agent token 或 INSTALL_KEY。完整 Compose、私密 API 响应和未经脱敏的服务日志未入库。驱动只允许三台指定 CT，执行需要事先准备的本轮快照及隔离目录；不能直接指向生产机器。
