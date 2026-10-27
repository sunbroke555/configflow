# Docker OxiDNS HA 真机验收（2026-10-27）

验收对象为 PR #422 的 Docker Host + 独立 HA companion，以及 Macvlan 同容器 HA。使用仓库 `docker/Dockerfile.agent` 实际构建镜像；没有替换 Agent、keepalived、配置发布 API 或 DNS 查询为 mock。

## 环境与范围

所有构建、测试和 PVE 操作从测试服务器 `192.168.0.32` 发起。CT130、CT131 是同一台 PVE 实验宿主上的两个独立 Linux 实例，各自运行独立 dockerd；这覆盖真实内核、容器网络、keepalived 和服务生命周期，但不等于两台物理 NAS，也没有测试所有 NAS 型号或物理断电。

| 实例 | 用途 | 隔离 VLAN 91 地址 |
| --- | --- | --- |
| CT130 `cf-oxi-a-retest` | Docker 宿主 A | 192.168.79.21 |
| CT131 `cf-oxi-b-retest` | Docker 宿主 B | 192.168.79.22 |
| CT132 `cf-app-retest` | 临时 ConfigFlow、真实 UDP 上游、独立 DNS/ARP 观察器 | 192.168.79.30 |
| Macvlan 容器 A/B | 独立容器网络命名空间 | 192.168.79.81 / .82 |
| HA VIP | 两种模式先后复用 | 192.168.79.53 |

三台原来均停止，在修改前创建 `cf-docker-ha-20261027` 快照。CT130/131 临时从 384 MiB 增至 1024 MiB；CT132 为 1536 MiB。未修改生产 VM100、VM107、CT105、VM101，也没有修改 PVE 的网络、防火墙规则。已有非特权 LXC nesting 足够，不需要开放 privileged/keyctl 或放宽 AppArmor。

Docker 29.9.2 / Agent 1.8.47-go / OxiDNS 1.6.1 / keepalived 2.3.4。镜像 ID：`sha256:a5df4033c5711832479ed40075720b40ba6fcd9faa6c23169024139147c2fa4b`。Docker 静态安装包来自官方发布，SHA256 为 `9ac225999fd5e863f7694c728ef23d480616b1e5639328fa309befd4243d69eb`。两个 dockerd 采用独立数据目录、Unix socket、`bridge=none`，不管理宿主 iptables；专用 bridge 负例创建后即移除。

Host 核心容器没有额外 NET capability；companion 独立使用 NET_ADMIN/NET_RAW 和专用 Supervisor socket。Macvlan 使用固定容器 LAN IP、NET_ADMIN/NET_RAW、只读宿主 machine-id。实测 `ip -j -d link` 的 Macvlan 接口有 `info_kind=macvlan`、`link_netnsid=0`。Agent 能确认两宿主标识不同。

## 测量方法

CT132 每轮并行发送 3 个真实 UDP DNS 请求：两个节点和 VIP，目标间隔 100 ms，每个请求超时 300 ms。域名为 `sync.lab.invalid`，实际 UDP 上游分别返回 `198.51.100.10/.11/.20/.30/.70/.80`，可辨别初始配置、同步后配置和新发布配置。请求超时会拉长采样轮次，**失败轮数不能直接乘 100 ms 当作停机时长**。原始时间戳及各请求延迟见压缩 JSONL；按场景给出失败请求开始时间。

发布顺序同时核对 Docker/Supervisor 日志与外部 VIP 响应：备机新核心启动 → VIP 已返回新版答案 → 原主机核心停止。配置和规则文件均比较 SHA256；容器重建比较容器 ID、Agent ID、文件 SHA256，并继续使用原认证令牌访问真实 Agent。

## Host 结果

| 场景 | 结果 | 连续采样轮数 / VIP 失败 / 两节点同时不可答 |
| --- | --- | --- |
| 初始 fallback 不同，开启 HA 后同一份已保存配置快照及规则同步到两端 | 通过，两个节点及 VIP 均返回 .20 | 开启阶段未连续采样 |
| 从备机发起配置更新，有序迁 VIP 后重启原主 | 通过，新配置为 .30，日志顺序和文件哈希符合预期 | 472 / 0 / 0 |
| 强杀整个核心容器，companion 继续运行，备机接管；原容器重启恢复 | 通过，旧节点 VIP 被移除，Agent 身份保留 | 198 / 16 / 0 |
| 正常停止并删除核心容器，用原持久目录重建 | 通过，新容器 ID、原 Agent ID 与文件哈希 | 153 / 17 / 0 |
| 正常重启 companion | 通过，核心 PID、容器及配置未变化 | 176 / 1 / 0 |
| 手动更新规则，组发布完成 | 通过，两端文件一致，核心均未重启 | 196 / 0 / 0 |
| 强杀 companion 自身并恢复 | 复现已声明的残留边界；恢复后单 VIP、单 ARP | 272 / 0 / 0 |

完整 Host 连续记录含场景之间的稳定等待：6018 轮，VIP 失败 34，两节点同时不可答 0。另有一次开始连续采样前的 core-kill 冒烟，只记录接管/恢复成功，不计作零中断证据。

**Host companion 自身 SIGKILL 的边界：**旧宿主仍保留 VIP，同时备机取得 VIP；外部 ARP 观察到两条响应（`bc:24:11:e2:f5:57` 和 `bc:24:11:25:ed:96`）。重启 companion 后自动收敛为 CT130 独占 VIP，ARP 只剩一条，核心未重启。该场景 DNS 采样成功不能掩盖双 VIP；本次不承诺 guardian 强杀或 Docker daemon 崩溃时无残留，也没有把核心强杀能力扩大为 guardian 强杀能力。

真实拒绝验证：移除 companion 的 NET_ADMIN 后，capability `ready=false`，开启 HA 返回 HTTP 409；同一宿主上另一个真实 Agent 使用不同 LAN IP，仍因宿主 ID 相同返回 HTTP 409；普通 bridge 即使加 NET_ADMIN/NET_RAW，实际 capability 仍 `ready=false`（该负例仅验证 Agent 能力拒绝，没有提交后端变更）。两核心 PID/配置保持不变，临时 Agent、网络和 IP 均移除。

## Macvlan 结果

| 场景 | 结果 | 连续采样轮数 / VIP 失败 / 两节点同时不可答 |
| --- | --- | --- |
| 初始 fallback 不同，开启 HA 同步配置及规则 | 通过，两个节点及 VIP 均返回 .70 | 开启阶段未连续采样 |
| 从备机发起配置更新，有序迁 VIP 后重启原主 | 通过，新配置为 .80，日志顺序和文件哈希符合预期 | 446 / 0 / 0 |
| 强杀整个核心容器与 keepalived，备机接管；原容器重启恢复 | 通过，原容器网络命名空间回收，无旧宿主 VIP 残留，持久身份保留 | 294 / 12 / 0 |

完整 Macvlan 连续记录含稳定等待：861 轮，VIP 失败 12，两节点同时不可答 0。Host 计划配置发布 472 轮、规则发布 196 轮、Macvlan 计划配置发布 446 轮都未测到 VIP 请求失败；这个结果不包含且不抵消故障注入的失败。

外部失败请求开始时间（UTC，末次请求还可能等待至 300 ms 超时）：

| 故障场景 | 首次失败请求 | 末次失败请求 | 失败数 |
| --- | --- | --- | --- |
| Host 核心强杀/恢复 | 07:02:05.228586 | 07:02:09.741817 | 16 |
| Host 正常停止/重建 | 07:02:40.103068 | 07:02:44.917324 | 17 |
| Host guardian 正常重启 | 07:03:04.254119 | 07:03:04.254119 | 1 |
| Macvlan 核心强杀/恢复 | 07:19:25.939584 | 07:19:29.251009 | 12 |

证据包含 [Host JSON](oxidns-docker-ha-host-2026-10-27.json)、[Macvlan JSON](oxidns-docker-ha-macvlan-2026-10-27.json)、[Host 原始采样](oxidns-docker-ha-host-probe-2026-10-27.jsonl.gz)、[Macvlan 原始采样](oxidns-docker-ha-macvlan-probe-2026-10-27.jsonl.gz)。JSON 记录源码 SHA256、实际服务事件、文件哈希和逐场景失败时间；原始文件的未压缩 SHA256 也已保留。

## 内核对照

先用真实 keepalived 容器做四项独立对照：Macvlan 正常 stop 与 kill9 后，宿主不保留 VIP，外部 ARP 无响应；Host 正常 stop 后无残留，Host kill9 后保留 VIP 并继续响应 ARP。每项结束移除临时容器与测试 VIP；该结果是采用独立 companion 的原因与能力边界证据。详情见 [kernel JSON](oxidns-docker-ha-kernel-2026-10-27.json)。

## 复现

驱动脚本：[oxidns_docker_ha_lab.py](../../scripts/tests/oxidns_docker_ha_lab.py)，复用同目录 `oxidns_ha_sync_lab.py` 的隔离身份检查、HTTP 客户端、快照恢复和 DNS 探针。只允许从 Linux 测试服务器执行；`guard()` 检查三个固定 CT 的名称、cfgt 网络和预备快照。

前置步骤是核对三台实验 CT 均停止且无人使用、创建上述快照，再将 CT130/131 内存临时设为 1024 MiB 并启动三台；确认 eth0.91 和表中隔离地址存在。原生实验 Agent/OxiDNS/keepalived 必须停止，避免占用 53 端口。把此工作树源文件放至测试服务器 `~/configflow-docker-ha-lab/source`，官方静态 Docker 包放至同级 `docker-29.9.2.tgz`。

在测试服务器启动专用 builder daemon，使用独立数据目录和 socket，不改共用 Docker 服务。构建并保存镜像：

```sh
# 在测试服务器 ~/configflow-docker-ha-lab 中执行；Docker socket 指向专用 builder。
sudo docker -H unix://$PWD/builder/docker.sock build --network=host --platform=linux/amd64 \
  -f source/docker/Dockerfile.agent -t cf-oxidns-docker-ha:20261027 source
sudo docker -H unix://$PWD/builder/docker.sock save -o app-image.tar cf-oxidns-docker-ha:20261027
sudo chown sunbroke:sunbroke app-image.tar
```

内核对照镜像为 Alpine 加 `iproute2 keepalived`，保存为 `kernel-image.tar`；`load-kernel-image` 同时创建每台 Docker 宿主的专用 Macvlan 网络。脚本 `install-hosts` 安装隔离静态 daemon 并创建 systemd transient unit；`prepare-backend` 创建随机私有凭据、临时数据库、HTTP 服务和 UDP 上游，依赖 CT132 已有 `/opt/cf-retest/venv`。后续全部动作都有对应命令：

```sh
cd ~/configflow-docker-ha-lab/source
python scripts/tests/oxidns_docker_ha_lab.py install-hosts
python scripts/tests/oxidns_docker_ha_lab.py load-kernel-image
python scripts/tests/oxidns_docker_ha_lab.py kernel-probe
python scripts/tests/oxidns_docker_ha_lab.py prepare-backend
python scripts/tests/oxidns_docker_ha_lab.py load-app-image
python scripts/tests/oxidns_docker_ha_lab.py prepare-host
python scripts/tests/oxidns_docker_ha_lab.py enable-mode
python scripts/tests/oxidns_docker_ha_lab.py start-probe
python scripts/tests/oxidns_docker_ha_lab.py test-publish
python scripts/tests/oxidns_docker_ha_lab.py test-core-kill
python scripts/tests/oxidns_docker_ha_lab.py test-recreate
python scripts/tests/oxidns_docker_ha_lab.py test-guardian-restart
python scripts/tests/oxidns_docker_ha_lab.py test-rules
python scripts/tests/oxidns_docker_ha_lab.py test-guardian-boundary
python scripts/tests/oxidns_docker_ha_lab.py finish-probe
python scripts/tests/oxidns_docker_ha_lab.py disable-mode
python scripts/tests/oxidns_docker_ha_lab.py test-rejections
python scripts/tests/oxidns_docker_ha_lab.py remove-containers
python scripts/tests/oxidns_docker_ha_lab.py prepare-macvlan
python scripts/tests/oxidns_docker_ha_lab.py enable-mode
python scripts/tests/oxidns_docker_ha_lab.py start-probe
python scripts/tests/oxidns_docker_ha_lab.py test-publish
python scripts/tests/oxidns_docker_ha_lab.py test-core-kill
python scripts/tests/oxidns_docker_ha_lab.py finish-probe
python scripts/tests/oxidns_docker_ha_lab.py disable-mode
python scripts/tests/oxidns_docker_ha_lab.py remove-containers
python scripts/tests/oxidns_docker_ha_lab.py export-evidence
python scripts/tests/oxidns_docker_ha_lab.py cleanup
```

导出脱敏 JSON 报告与 `.jsonl.gz` 后再删除测试服务器任务目录（含临时凭据、镜像、socket 和 builder 数据）。不能把 private.json 导出到仓库。

## 实际清理

已完成并核实，见 [cleanup JSON](oxidns-docker-ha-cleanup-2026-10-27.json)：

- CT130、CT131、CT132 均回滚本轮快照，磁盘和配置恢复，删除 `cf-docker-ha-20261027` 快照，最终均为 `stopped`。
- CT130/131 内存恢复为 384 MiB，CT132 为原来的 1536 MiB。Docker 数据、容器、临时地址、临时服务、`/opt` 权限变动随快照恢复；负例临时容器、bridge 与 `.81` 宿主地址在回滚前已移除。
- 测试服务器专用 builder 服务已为 `inactive`，端口 19284 隧道已关闭；整个 `~/configflow-docker-ha-lab` 目录已删除，包括随机凭据、数据库副本、源码副本、镜像、builder 数据和 socket。
- 测试服务器新增的通用 Docker/Buildx/containerd/runc/libpathrs 软件包保留，供后续远端构建使用；没有留下运行的任务 daemon。

此外，Agent 在测试服务器完成 `GOOS=linux GOARCH=arm64 CGO_ENABLED=0` 交叉构建，生成静态 AArch64 ELF，产物已清理。该项仅验证编译，不是 ARM NAS 运行验收。

这是远端实际执行的验收记录，不代表 GitHub CI 已报告通过。
