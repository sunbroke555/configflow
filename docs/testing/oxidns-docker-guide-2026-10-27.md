# Docker OxiDNS 安装引导真机验收（2026-10-27）

使用真实认证后端、正式 `/api/agents/docker-oxidns-ha-guide` 生成结果、真实 Docker/Compose 和官方源码镜像执行。没有替换 API 响应或 Docker 命令为 mock。结构化证据、退出码、容器 ID、数据哈希与分阶段源码 SHA256 见 [验收 JSON](oxidns-docker-guide-2026-10-27.json)；可复现驱动见 [oxidns_docker_guide_lab.py](../../scripts/tests/oxidns_docker_guide_lab.py)。

## 环境

全部构建与测试从 `192.168.0.32` 驱动，PVE 操作通过该服务器的专用 SSH 通道。CT130 是隔离 Linux Docker 宿主，CT132 是临时 ConfigFlow、真实 UDP 上游和观察器，位于 cfgt/VLAN 91 的 `192.168.79.21`、`.30`；Macvlan 容器使用 `.81`。CT131 没有启动或修改。未操作生产 VM100/VM107/CT105/VM101。

CT130/132 原来均停止，先创建独立快照 `cf-docker-guide-20261027`。CT130 临时从 384 MiB 调至 1024 MiB，CT132 保持 1536 MiB。测试服务器及 CT130 使用专用 dockerd 数据目录/socket，不修改共用 Docker 服务或 PVE 网络、防火墙。

软件为 Docker 29.9.2、Compose 5.6.1、Agent 1.8.47-go、OxiDNS 1.6.1。镜像由本任务 `docker/Dockerfile.agent` 实际构建，ID 为 `sha256:1cac73f79381e941c9c8710df2911abfd435af3398074f44e644d39658fb3e91`，构建基线提交 `104e7ae7ce3e4416ae32fba37375ae8df68e549a`。本轮没有修改 Go 或镜像运行时；新引导、登记和续装逻辑来自报告中记录哈希的 Python 模块及生成命令。

这次覆盖 Linux Docker 的安装引导和迁移行为，没有重跑 VIP 接管矩阵，也没有在 DSM/QNAP 专有系统或物理 NAS 上运行，不代表断电验收。运行时 HA 的两独立 Docker daemon 验证见 [上一轮报告](oxidns-docker-ha-2026-10-27.md)。

## 实际结果

| 场景 | 结果与证据 |
| --- | --- |
| Host 全新安装，Agent 数据目录不存在 | 正式命令执行时才登记 1 个设备；管理员认证及全局 Agent 注册密钥均开启。未预种身份，命令不含管理员 JWT、密码或全局注册密钥；真实 Agent 心跳 online，HA capability ready。 |
| Macvlan 全新安装 | 正式 helper 经新 Macvlan 网络完成登记，固定 IP 交给主容器；同样只登记 1 个设备，真实心跳与 capability 正常。 |
| bind 目录旧容器迁移及回滚 | 保留原 Agent ID/token 哈希、配置、规则、缓存和身份标记；原容器保留为 `-pre-ha`，备份文件存在。执行生成 rollback 后恢复原容器 ID、身份和数据。 |
| Docker 命名卷旧容器迁移及回滚 | 同上；实际命名卷保持原位，未转为空 bind 目录。 |
| 原环境包含字面 `$` | 迁移后实际容器与真实 `docker compose run --no-deps` 均保留原值，未把 `$dollar` 当变量展开。Compose `config` 的转义输出不作为运行时值判据。 |
| 旧容器迁移中断 | 观察真实 Docker rename 事件后临时移走专用 daemon socket，后续真实 Docker 调用失败；恢复 socket 后执行原生成 rollback，成功恢复原容器 ID、身份、配置及状态文件。 |
| 宿主身份创建与恶意路径 | 首次创建成功；重复创建、已有文件、软链接及悬空软链接均拒绝且不覆写。包含引号、`$()`、分号和 `#` 的合法绝对路径按字面创建，注入标记不存在；相对路径、根目录、非规范路径、冒号、逗号、换行返回 HTTP 400。系统 `/etc/machine-id` 未变化。 |
| 最终 Host 部分失败续装 | companion 启动事件后使专用 socket 暂时不可用，主容器尚未创建且登记设备为 offline。恢复 socket、使用相同设置重新生成命令后成功继续，companion 原 CID 保留，原登记 Agent 转为真实 online，设备数不增加。 |
| 最终 Macvlan 重生成续装 | 对同一个实际安装现场分别在主容器运行中、停止后重新生成并执行；容器 ID、网络 ID、Agent ID/token、配置均不变，无新增设备。停止状态的原容器被启动。 |

JSON 的 8 条顶层记录合并了同一项中的子场景。前两项 v2 新装记录发生在安全续装增强之前，其“重复完整安装拒绝”仅是当时行为；**最终行为由 v3 续装记录取代**：只有本次记录的容器与网络可以继续使用。当前脚本中的通用新装验收已更新为要求 owned-resource 重复执行成功且 CID 不变。

## 真机发现并修复的问题

1. 首轮新装中，容器及 HA 环境正常，但真实心跳持续 HTTP 401。原因是全局 JSON 脱敏器把新 enrollment 响应中的 token 替换为 `[REDACTED]`，随后被写入 Agent 配置。修复仅保留成功 enrollment 响应的那个受限 Agent token；使用全新名称和完全空的数据目录重验后，真实心跳上线。没有手工替换失败容器的 token 来充当成功验收。
2. Macvlan 首次安装成功，但重新执行时 helper 试图再次使用主容器已占用的固定 IP，实际 Docker 返回 125。最终修复使已记录主容器的身份校验使用只读挂载和 `network none`；在原失败现场继续验证运行中、停止后的两种续装均成功，没有通过重建现场掩盖冲突。

初始错误样本保留到快照回滚。报告中的 `first_verified_source_sha256`、`v3_host_source_sha256` 与 `final_source_sha256` 区分各阶段，避免把早期行为标成最终代码行为。

## 复现

只在测试服务器运行。准备三个实验身份中的 CT130/132，确认均停止且无人使用；CT131保持原样。将任务源码复制到 `~/configflow-docker-guide-lab/source`，官方静态 Docker 29.9.2 归档放于同级 `docker-29.9.2.tgz`。测试服务器安装 Docker/Buildx/Compose；创建专用 builder socket `~/configflow-docker-guide-lab/builder/docker.sock`，使用独立数据目录、`bridge=none`、`iptables=false`。

```sh
# 测试服务器执行，Dockerfile、二进制和依赖均实际构建。
cd ~/configflow-docker-guide-lab
sudo docker -H unix://$PWD/builder/docker.sock build --network=host --platform=linux/amd64 \
  -f source/docker/Dockerfile.agent -t cf-oxidns-docker-guide:20261027 source
sudo docker -H unix://$PWD/builder/docker.sock save -o app-image.tar cf-oxidns-docker-guide:20261027
sudo chown sunbroke:sunbroke app-image.tar
cd source
python scripts/tests/oxidns_docker_guide_lab.py prepare-guests
python scripts/tests/oxidns_docker_guide_lab.py install-docker
python scripts/tests/oxidns_docker_guide_lab.py prepare-backend
python scripts/tests/oxidns_docker_guide_lab.py load-image
python scripts/tests/oxidns_docker_guide_lab.py prepare-legacy-bind
python scripts/tests/oxidns_docker_guide_lab.py test-migration-bind
python scripts/tests/oxidns_docker_guide_lab.py test-new-host
python scripts/tests/oxidns_docker_guide_lab.py test-new-macvlan
python scripts/tests/oxidns_docker_guide_lab.py test-identity
python scripts/tests/oxidns_docker_guide_lab.py test-migration-volume
python scripts/tests/oxidns_docker_guide_lab.py test-partial-rollback
python scripts/tests/oxidns_docker_guide_lab.py test-resume-host
python scripts/tests/oxidns_docker_guide_lab.py test-resume-macvlan
python scripts/tests/oxidns_docker_guide_lab.py export-evidence
# 等使用此真实后端的浏览器验收结束，再运行：
python scripts/tests/oxidns_docker_guide_lab.py cleanup
```

CT132 使用已有的 `/opt/cf-retest/venv`。生成命令以 Bash 原样执行，仅配置实验专用 `DOCKER_HOST` 和静态 Docker 所在 PATH。镜像参数指定实际构建的标签；旧容器 fixture 才预存其真实原身份，新装由正式受限 enrollment helper 完成。完整生成命令可能含临时安装凭据，仅以 0600 保存在测试目录；仓库只导出脱敏报告和命令哈希。

`resume-existing-macvlan` 是本次从已捕获 IP 冲突现场继续验收的专用入口；全新复现使用 `test-resume-macvlan` 即同时覆盖运行和停止状态，不需要制造已修复的冲突。

## 实际清理

浏览器验收结束后已实际完成，见 [cleanup JSON](oxidns-docker-guide-cleanup-2026-10-27.json)：

- CT130/132 已回滚本轮快照、删除 `cf-docker-guide-20261027` 快照，最终均停止。CT130 内存恢复 384 MiB，CT132 保持 1536 MiB；磁盘、配置、原生服务、Docker 数据、安装日志、宿主身份测试文件和 `/opt` 权限一并恢复。
- CT131 全程未启动或修改，最终仍停止。
- 专用 builder 已 inactive，19285 隧道端口已关闭；测试服务器整个 `~/configflow-docker-guide-lab` 已删除，包含镜像、源码副本、临时 DB/凭据、完整生成命令与故障注入文件。
- 本轮新增的通用 `docker-compose 5.6.1` 软件包保留，供后续远端测试使用；没有保留任务 daemon。

## 同轮界面与回归验证

最终远端验证为后端 244 项、前端 130 项、类型检查和生产构建全部通过。浏览器连接上述真实认证后端完成 8 项，0 个页面错误，覆盖 375/1440 宽度、浅色/深色、三种强调色、关闭纹理和动效，以及 Host/Macvlan 引导、复制、HTTP 400 后保留草稿并修正、同一 Agent 配对阻断。

capability 使用真实 API：前序检查曾取得在线实例 HTTP 200；最终检查刻意选择已停止的原 Agent，实际 HTTP 502 仍保留可理解的失败状态，并验证同一 Agent 不得配对。没有把网络失败伪装成全部成功。浏览器/Vite 进程与浏览器临时目录已由主任务清理。

[浏览器结果 JSON](results/oxidns-docker-guide-2026-10-27/browser.json) · [桌面引导](results/oxidns-docker-guide-2026-10-27/desktop-guide.png) · [手机身份步骤](results/oxidns-docker-guide-2026-10-27/mobile-identity.png) · [桌面迁移](results/oxidns-docker-guide-2026-10-27/desktop-migration.png) · [手机真实检查](results/oxidns-docker-guide-2026-10-27/mobile-live-check.png)

这些是测试服务器实际执行结果，不是宣称 GitHub CI 已报告通过。
