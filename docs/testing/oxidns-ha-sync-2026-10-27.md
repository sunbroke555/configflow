# OxiDNS HA 配置与规则同步真机验收

2026-10-27，在测试服务器 `192.168.0.32` 驱动 PVE 隔离实验。使用真实 Flask HTTP API、两台原生 OxiDNS、Go Agent、systemd 和 keepalived，未替换发布、健康检查或 HA 接口。

## 环境与边界

| 角色 | 实例 | 实验地址 | 软件 |
|---|---|---|---|
| 初始主机 | CT130 `cf-oxi-a-retest` | `192.168.79.21` | OxiDNS 1.6.1 (full)、本任务源码编译 Agent 1.8.45-go |
| 初始备机 | CT131 `cf-oxi-b-retest` | `192.168.79.22` | 同上 |
| 独立 ConfigFlow 与 DNS 探针 | CT132 `cf-app-retest` | `192.168.79.30:18080` | 本任务后端源码、独立数据目录与随机测试凭据 |
| VIP | keepalived | `192.168.79.53:53` | 原生 VRRP |

三台实例原本处于停止状态，来自此前保留的实验环境。每台先创建本次专用 `cf-ha-sync-20261027` 快照；实验使用独立 `/opt/cf-ha-sync` 目录，不读取原 ConfigFlow 账户或配置。原 `/opt` 属于旧实验 UID 501，keepalived 安全预检正确拒绝执行其下的程序；本轮只在快照保护的两台 DNS 容器内临时恢复 root 所有权。

实验接口是 `cfgt` 桥上的 VLAN 91，桥没有物理端口；已有实验防火墙阻断向生产 RFC1918 网段转发。没有操作生产 VM100、VM107、CT105、VM101，也没有调整 PVE 网桥、防火墙或生产路由。

## 验收内容

脚本：[oxidns_ha_sync_lab.py](../../scripts/tests/oxidns_ha_sync_lab.py)。报告：[oxidns-ha-sync-2026-10-27.json](oxidns-ha-sync-2026-10-27.json)。报告保留每个阶段的发布摘要、主备文件 SHA-256、实际 DNS 应答和 systemd 停止/启动时间线。原始 DNS 采样另存为 [gzip JSONL](oxidns-ha-sync-dns-2026-10-27.jsonl.gz)，解压后的 SHA-256 与报告一致。

- 初始主备分别应答 `198.51.100.10`、`198.51.100.11`，`config.yaml` 和 `rules/lab.txt` 均不同。开启 HA 后，两台使用同一份已保存配置快照及附加规则，先完成配置同步，再启用 VIP。
- 从主机、备机分别发起完整发布，均更新两台的配置和规则；真实 DNS 应答跟随新配置变化。
- `restart=false` 时两台只暂存，在线文件摘要与 PID 不变；从备机激活仍按先备后主执行。
- 在备机实际启动阶段重启 ConfigFlow，检查持久任务续跑、两台均只重启一次。
- 给备机注入一次真实 `ExecStartPre` 失败：Agent 回滚旧配置，主机的 PID、文件摘要和服务状态不变，主机没有停止事件。移除故障后重新发布成功。
- 延迟备机启动后并发提交：同一请求重复提交可复用；从另一成员发起竞争发布被拒绝，两台均不会重复重启。
- 实际停止主机 OxiDNS，确认备机取得 VIP、回答 DNS；启动主机后检查恢复健康。

探针以 100ms 为目标间隔，并行查询主机、备机与 VIP，单次超时 300ms；若本轮超过间隔，则完成后进入下一轮，同时采集 systemd 事件。以“备机已恢复并通过健康检查后才更新主机”为通过条件，不把单个 accepted HTTP 响应当成部署完成。

## DNS 连续性边界

串行发布避免两台同时停止，但不保证 VIP 零查询失败：主机停止到监听恢复之间，以及真实故障后 VRRP 选举期间，VIP 仍可能短暂不能应答。首次启用时，在两台配置同步完成并选出持有者前，VIP 不存在属于预期。

完整 9 场景全部通过，共采样 **1088 轮**，其中“两节点 DNS 同时不可用”样本为 **0**；systemd 事件也没有发现两台核心同时处于停止状态。

| 阶段 | 样本数 | VIP 查询失败数 |
|---|---:|---:|
| 首次同步并启用 VIP | 95 | 32 |
| 从主机发布 | 95 | 2 |
| 从备机发布 | 103 | 2 |
| 暂存后从备机激活 | 143 | 2 |
| 发布中重启 ConfigFlow 并续跑 | 146 | 2 |
| 备机启动失败并回滚 | 127 | 0 |
| 失败后重新发布 | 96 | 2 |
| 重复及并发请求 | 127 | 2 |
| 主机真实故障与恢复 | 66 | 16 |

阶段之间的检查间隙也持续采样，因此表中样本总数小于 1088。失败数不能直接换算为连续中断时长。重复提交和另一端竞争提交的 HTTP 状态分别为 `202`、`202`、`409`；每台服务均只重启一次。后台重启续跑阶段，备机启动后约 2.64 秒才停止主机；备机故障阶段主机没有任何停止事件。

采样不能证明采样间隙绝无中断；因此同时保留系统服务时间线，不将采样结果宣传为零丢包。

## 重现

所有命令都在测试服务器执行。先核对三个实例的专用测试身份、`cfgt` 网络和停止状态，再为它们创建上述恢复快照并启动。准备 Go Agent：

```sh
cd backend/agents/go-agent
go build -trimpath -o /tmp/configflow-ha-sync-agent .
```

从任务仓库根目录：

```sh
python scripts/tests/oxidns_ha_sync_lab.py prepare
python scripts/tests/oxidns_ha_sync_lab.py test
python scripts/tests/oxidns_ha_sync_lab.py edge-tests
python scripts/tests/oxidns_ha_sync_lab.py cleanup
```

`prepare` 使用真正的 `/api/agents/register` 接口取得一次性 Agent 凭据，管理 API 的脱敏值不能用作 Agent 令牌。私有凭据仅写测试服务器和容器的限制权限文件，不进入报告。更新源码可用 `update-source`；完整重跑用 `reset` 回滚原快照、保留快照并重新启动，再运行 `prepare`。

`cleanup` 先停止本轮 HTTP 隧道，再停止三台容器、回滚原始快照并删除本次快照，恢复原数据、权限与停止状态。导出报告后再移除本轮私有临时目录。
