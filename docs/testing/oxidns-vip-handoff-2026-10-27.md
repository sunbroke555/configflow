# OxiDNS 发布前主动迁移 VIP 真机验收

在测试服务器 `192.168.0.32` 驱动 PVE 隔离实验，使用真实 OxiDNS 1.6.1、Go Agent 1.8.46、Flask HTTP API、systemd 与 keepalived。没有替换发布、VIP 迁移或 DNS 健康检查接口。

## 环境

| 角色 | 实例 | 实验地址 |
|---|---|---|
| 配置中的主机 | CT130 `cf-oxi-a-retest` | `192.168.79.21` |
| 配置中的备机 | CT131 `cf-oxi-b-retest` | `192.168.79.22` |
| 独立 ConfigFlow 与探针 | CT132 `cf-app-retest` | `192.168.79.30` |
| VIP | 原生 keepalived | `192.168.79.53:53` |

复用的三台容器在开始前均为 stopped、onboot 0，没有遗留测试快照。分别创建 `cf-vip-handoff-20261027` 快照后启动；本轮目录为 `/opt/cf-vip-handoff`，测试服务器使用 `/tmp/configflow-vip-handoff*`，HTTP 隧道使用 `127.0.0.1:19283`。旧实验账户与本轮随机凭据相互独立。

使用既有 `cfgt` VLAN 91。桥没有物理接口，既有 nft 规则阻断向生产 RFC1918 网段转发。没有改动生产 VM100、VM107、CT105、VM101，也没有调整 PVE 网络、防火墙或路由。

## 场景与证据

脚本：[oxidns_vip_handoff_lab.py](../../scripts/tests/oxidns_vip_handoff_lab.py)，复用已参数化的 [公共实验 fixture](../../scripts/tests/oxidns_ha_sync_lab.py)。旧脚本默认路径和旧报告保持不变。

1. 从配置主机发起发布。
2. 从配置备机发起发布。
3. 真实停止 VIP 持有者核心，确认另一台接管，再在角色反转状态发起发布。
4. 暂停备机 keepalived 进程，保留其 DNS 与 Agent，导致迁移超时；确认旧持有者 PID、配置、服务状态完全不变且没有停止事件。
5. 移除迁移故障后重新发布。
6. 两台 `restart=false` 暂存不修改在线文件/PID/VIP，从备机激活后完成迁移与发布。
7. 让位记录持久化后停止 ConfigFlow，重启让位节点 Agent；确认同 operation_id 保留、VIP 能回答新配置，再恢复 ConfigFlow 完成任务。
8. 重复提交同请求并从另一台提交竞争请求，确认不会重复重启或并行发布。
9. 手动持久让位期间停止接管端 DNS，确认原节点恢复优先级并接管；对端恢复后继续让位，DELETE 后恢复正常选举。

配置故意不提供原生热重载 API，并改变 `runtime.worker_threads`，保证主要发布场景确实执行服务重启。每个成功发布都要求时间线满足：**备机 Started → VIP 返回新配置应答 → 旧持有者 Stopping**，并检查两台核心没有重叠停止。

探针使用与上一轮相同的 100ms 目标间隔、300ms 单次超时，并行查询两台节点与 VIP；耗时超过间隔时等当前轮结束再开始下一轮。额外记录 keepalived 选举、内核 VIP 地址变更以及 systemd 启停日志。采样不能证明间隙中完全没有丢包，故障注入造成的查询失败也不能算作正常发布中断。

第一轮前六项通过，在第七项发现验收脚本用 `systemctl start` 恢复已被回收的临时 unit；已改成用原命令重建 unit，随后从原始快照完整重跑。该问题不涉及产品进程恢复逻辑。

## 实测结果

最终 9 场景全部通过。详细证据见 [JSON 报告](oxidns-vip-handoff-2026-10-27.json)，[原始 DNS 采样 gzip](oxidns-vip-handoff-dns-2026-10-27.jsonl.gz) 解压后的 SHA-256 为 `23307f51b9304a7bfb4a9e0d054609a1a0641bdbed01b77e62edab6b0971dae4`。报告记录最终产品源码和测试脚本摘要、实际二进制摘要及软件版本。

| 场景 | 样本数 | VIP 查询失败数 |
|---|---:|---:|
| 从主机发布 | 191 | 0 |
| 从备机发布 | 185 | 0 |
| 故障接管后角色反转发布 | 109 | 0 |
| 迁移失败保护旧持有者 | 367 | 0 |
| 迁移失败后重试 | 194 | 0 |
| 暂存后从备机激活 | 206 | 0 |
| ConfigFlow 与 Agent 中断恢复 | 274 | 0 |
| 重复及竞争请求 | 227 | 0 |
| 持久让位期间主动停止对端 DNS 并恢复 | 243 | 14 |

前八项共 **1753 轮、0 次 VIP 查询失败**。上一轮同样探针设置下，每次普通发布曾观察到 2 次 VIP 查询失败；本轮普通发布与激活均未观察到失败。七次成功更新都满足迁移先于旧持有者停止，普通场景的首次新 VIP 应答提前 **1.00–2.34 秒**；暂停后台的恢复场景提前 **11.81 秒**。systemd 日志没有两台核心重叠停止。

全轮共 **2553 轮，VIP 查询失败 30 次，两节点同时不可用 0 次**。这 30 次均来自主动故障注入：角色反转发布之前主动停止主 DNS 的阶段间隙有 **16 次**，最后一项停止接管端 DNS 有 **14 次**。角色反转前的间隙共 40 轮，单列在报告 `intentional_holder_failure_gap`；其他阶段之间也持续采样，因此表格样本数小于全轮总数。失败样本数不直接等于连续中断时长，不能把本轮描述为全程零中断。

## 实际清理结果

已回滚 CT130、CT131、CT132 的 `cf-vip-handoff-20261027` 原始快照，随后删除本轮快照；重新查询确认三台均为 `stopped`、`onboot: 0`。原数据、配置和目录权限已恢复，本轮服务、让位记录与测试凭据随快照恢复移除。

已确认 HTTP 隧道 `127.0.0.1:19283` 关闭。导出证据后，测试服务器的 `/tmp/configflow-vip-handoff`、`/tmp/configflow-vip-handoff-source`、`/tmp/configflow-vip-handoff-agent` 全部删除，私有测试凭据已清除。实际复核结果见 JSON 的 `cleanup` 字段。生产实例、PVE 网络和防火墙均未改动。

## 重现

所有命令都在测试服务器执行，先核对实例身份、隔离网络和停止状态，为三台创建本次恢复快照并启动，再准备 Go 二进制：

```sh
cd backend/agents/go-agent
go build -trimpath -o /tmp/configflow-vip-handoff-agent .
```

从仓库根目录运行：

```sh
python scripts/tests/oxidns_vip_handoff_lab.py prepare
python scripts/tests/oxidns_vip_handoff_lab.py test
python scripts/tests/oxidns_vip_handoff_lab.py cleanup
```

`reset` 会恢复原快照、保留该快照供本轮再次使用，并重新启动三台。测试凭据仅进入限制权限的临时文件，不进入验收报告。
