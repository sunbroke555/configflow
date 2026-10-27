# 单份 Compose 与原生 Agent 混合 HA 实测（2026-10-27）

CT130 原生 Shell Agent、CT131 独立 Docker daemon、CT132 ConfigFlow 与 DNS 观察器，均为同一 PVE 宿主上的隔离 Linux LXC；只使用 cfgt VLAN91。没有连接真实 NAS，也未测试物理断电。主机 boot identity 实际不同，没有伪造身份。

单份 `compose.yaml` 直接 `docker compose up -d`，没有 prepare.sh 或额外安装文件。Host 首装使用 Compose 5.6.1；Host 重建、macvlan 首装/重建、缺 machine-id 的 Host 身份卷首装均实际使用校验过官方 SHA256 的 Compose **2.20.0** 与 Docker **29.9.2**。未给 daemon 添加 API 兼容变量。

实际注册与心跳上线、HA 环境 ready、init 无网络及只读根目录、主服务/companion 只读身份挂载均通过。Host 身份卷分支验证三服务共享固定卷，只有 init 可写；同宿主 file/volume 身份不同仍被真实 boot evidence 拦截（HTTP 409）。宿主 resolv.conf、路由及可读 nft ruleset 在安装前后相同；iptables 工具不存在，未声称检查过其规则。

两模式均从原生/容器不同 fallback 与规则开始，启用后同步同一份已保存配置快照和规则。随后从 Docker 成员发起发布，以更改 worker_threads 触发实际重启，确认 **Docker 备机启动 → VIP 返回新配置答案 → 原生主机停止**，使用 DNS 应答完成时间与实际 systemd/Supervisor 日志比较。Host 两个间隔分别约 11.06 秒、4.60 秒；macvlan 为 9.74 秒、3.39 秒。

| 连续 VIP 探测阶段 | Host 样本/失败 | macvlan 样本/失败 |
| --- | ---: | ---: |
| 计划发布 | 353 / 0 | 335 / 0 |
| 停止原生 DNS 并恢复 | 129 / 17 | 158 / 15 |
| 故障前主动迁移 VIP | 89 / 0 | 109 / 0 |
| 强杀持有 VIP 的 Docker 核心容器并恢复 | 176 / 14 | 252 / 10 |

故障检测期间存在短暂 VIP 查询失败，不能据此承诺零中断。失败窗口按首个失败请求开始至最后失败请求完成计算，Host 两次故障为 **5.113 秒、4.213 秒**，macvlan 为 **4.512 秒、3.007 秒**；故障阶段所有成功答案均与发布后的预期 IP 一致，每个恢复阶段均有成功样本。Host 核心容器被强杀时独立 companion 保持运行。第三方观察器目标采样间隔 100ms、查询超时 300ms；超时会拉长实际间隔。

两种模式都将本轮后端会话人为置为过期，实测登记返回 401 后 `--force-recreate`，身份及配置/规则保持一致，未产生额外登记请求。此项在 HA 配对前执行；已配对时另测同容器 kill/start 恢复。

首轮 Host 发布只触发热更新，缺少重启事件；旧探针也缺每条查询完成时间，故不计入重启顺序通过。保留其原始样本，修正测试夹具并使用新答案/worker_threads 后得到上表结果。另一次辅助身份复用夹具创建同网段第二条 macvlan 网络被 Docker IPAM 拒绝，改为只运行 Host init 后验证成功；未计为产品失败。

完整功能矩阵使用 **1.8.47-go**（HA 新逻辑已在镜像中），镜像 ID `sha256:d9364ddd2915370bfd5ed5975177c9b7e309c5873496c1bd381ec3545505f4fb`。最终版本提示/常量调整为 **1.8.48-go**，另建镜像并仅补 Host 身份卷实际启动/上线/能力检查；不将新镜像冒认为完成全矩阵。JSON 分别记录二者 image ID、native binary SHA256，以及实际 runtime/generator/Go 源码 SHA256。

复现入口为 [实验驱动](../../scripts/tests/oxidns_compose_direct_mixed_lab.py)。仅在 `.32` 驱动：核对 CT130/131/132 身份、停止态及 cfgt，保存配置并创建独立快照；使用专用 Docker data/exec-root/socket 构建镜像和导出二进制，准备已有隔离 VLAN91/后端依赖。驱动的 `install-docker`、`prepare-backend` 后，依次执行 `install-host`、`expiry-host`、`enable-host`、`publish-host`、`faults-host`、`finish-host`，以及对应 macvlan 命令；补 `same-host-checks`、`host-volume-smoke`，最后 `export-evidence`、`cleanup`。Compose 私有文件和受保护注册凭据不进入仓库。

证据：[脱敏结果及来源](oxidns-compose-direct-mixed-2026-10-27.json)、[Host 原始 DNS](oxidns-compose-direct-mixed-host-2026-10-27.jsonl.gz)、[macvlan 原始 DNS](oxidns-compose-direct-mixed-macvlan-2026-10-27.jsonl.gz)、[首轮诊断原始 DNS](oxidns-compose-direct-mixed-first-diagnostic-2026-10-27.jsonl.gz)。原始 DNS 中每行 nodes 顺序为原生节点、Docker 节点、VIP，不含 Agent 凭据或主机身份原文。

根任务另外在测试服务器验证后端 241 项（安装/HA 238 项及版本升级 3 项）、Go HA 60 项与 upgrade 88 项（含子测试）、前端 29 项、类型检查、生产构建及真实浏览器 2 场景/4 份 YAML；这些不是本驱动重复运行的结果。

实际清理完成：[清理结果](oxidns-compose-direct-mixed-cleanup-2026-10-27.json)。CT130/131/132 均已回滚原磁盘与配置并停止，内存恢复为 384/384/1536 MiB，本轮快照已删除。专用 builder、隧道、镜像缓存和私有凭据目录已清除；测试服务器默认 Docker/docker.socket/containerd 仍 inactive，53 监听在清理前后不变，15800/15810 均 HTTP 200。生产实例未修改。
