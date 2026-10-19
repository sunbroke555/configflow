# Linux 选择性网关安装向导浏览器验收（2026-10-19）

状态：**本报告声明的浏览器与 Linux 实测范围已通过，实验服务和网络清理完成**。新装、已有标准核心接入、发布失败回滚与重试、选择性转发及撤销、普通 Shell AIO 安装均有真实执行证据；Docker 只检查页面命令生成，未部署。此前 [API / VM 端到端报告](LINUX_GATEWAY_E2E_2026-10-19.md) 的通过结果不算作本轮浏览器覆盖。

本报告保留当时的源码摘要、Agent `1.8.20-go` 测试构建和界面截图。整理 PR 时已将网关改动单独移植到更新的 `upstream/main`（`6c0dd26`），保留此后合入的普通安装短链接、可选核心安装、用户 DNS 保留和 DNS 跟随能力，并将正式网关最低版本调整为 `1.8.25-go`。移植后另运行前后端与 Agent 回归；本报告不将历史 VM / 浏览器实测冒充为 PR 最终提交的重新实测。发布用证据中的本机个人路径和宿主网关地址已脱敏，不影响实验网络快照。

移植后的最终回归：前端 10 个文件、251 项测试通过，typecheck 与 build 通过；Python 网关 / 安装 / 撤销 286 项、部署 / 转换 / 缓存 233 项通过，安装器严格版本检查最后复跑 9 项通过（各组有重叠，不相加）；Go `go test ./...` 与 Linux ARM64 静态构建通过。Shell 卸载覆盖 systemd / OpenRC、单 / 双 Agent、有效 / 损坏 / 悬空恢复记录及停止服务时的竞争变化，共 20 项通过，也包含在 Python 286 项中。新补的旧 Agent 发布门槛和 DNS 联动忙碌时的原子拒绝另经独立代码复核。

本轮使用真实登录、产品页面、Flask API、Agent、Mihomo、OxiDNS 和 Linux 转发命令。安装会话由浏览器创建，发布、失败重试、转发预览与应用由页面按钮触发。终端只执行页面生成的安装命令、注入受控故障，以及采集只读快照和探针；未运行主 driver 代替向导操作。

## 环境与版本

| 项目 | 本轮值 |
| --- | --- |
| 客体 | 一次性 QEMU VM，Debian 13.7 ARM64，Linux `6.12.113+deb13-arm64` |
| 系统工具 | Python `3.13.5`，nftables `1.1.3`，slirp4netns `1.2.1`，systemd |
| Agent | `1.8.20-go`，ARM64；SHA-256 `2260ebdff871c227856534beb7df09954729fd88e1dcb30b3a7407e077ea48ab` |
| Mihomo | `v1.19.32`；SHA-256 `c80df2ca5159567804da883401e0118908fc1155e05df2baf3dbd4d3ecfc3754` |
| OxiDNS | `1.6.1`；SHA-256 `df6e341909906eff183a1d6e507be7821007e421a9494acc24c97c2a21eca5bd` |
| 源码基线 | Git HEAD `79c3b91d142d9c729940bd68eff36e5def543e12` 加当前工作区改动；不是干净提交 |
| 后端源码归档 SHA-256 | `ceae3a0eab1a665860b008b549f826b4d27f09dcfd7deccaeac2026a83c204e5`，448 个文件 |
| 测试 Profile | `gateway-lab`，界面名称 `Disposable gateway laboratory` |
| 后端 | `cfgl-router` namespace 内的 `http://192.0.2.1:8000`，独立 `/tmp` 配置仓库，无生产账户与后台 scheduler |
| 浏览器入口 | `http://127.0.0.1:13001/login` |
| API 转发 | 前端 `/api` → 宿主 `127.0.0.1:18000` → SSH → 客体 loopback relay → `cfgl-router:192.0.2.1:8000` |

宿主前端和 SSH 转发只绑定 `127.0.0.1`；客体 relay 同样只绑定 loopback。实验未给宿主添加网关路由，未使用生产配置或真实账号。fixture 初始没有核心和 Agent，网关模式关闭。实验环境摘要保存于本轮私有目录中的 `environment.json`、`guest-environment.txt`、`build-manifest.json` 和 `topology.json`。

| 实验角色 | 地址及用途 |
| --- | --- |
| 网关 | `cfgl-gateway`，`lan0 = 192.0.2.2/24`，上游 `192.0.2.1` |
| 客户端 | `cfgl-client`，`192.0.2.100` |
| SOCKS 出口 | `cfgl-origin`，`203.0.113.2:1080`，节点 `LAB-SOCKS`，策略 `LAB-PROXY` |
| HTTP 探针 | 直连 `203.0.113.10`，所选 IP `203.0.113.20`，代理域名 `proxy.lab.invalid` → `203.0.113.30`，端口均为 `18080` |
| fake-IP | `198.18.0.0/16` |
| Controller | `192.0.2.2:9090`，随机秘密保存在私有文件，不进入报告 |

## 固定前端快照

本轮开始时共享 checkout 的测试和其他组件编辑触发 Vite 整页刷新，使未保存的向导状态消失。开发服务日志有与浏览器重连时间对应的 `page reload tests/oxidns-settings.test.ts`、`page reload src/components/generate/OxidnsSettings.vue` 等记录。这是测试开发服务的 watch 行为，不能据此判断产品会自动刷新，也没有证据归因于浏览器本身。

独立 [前端启动器](../../scripts/tests/linux_gateway_lab_frontend.mjs) 已改为复制固定源码到 `/tmp`，Vite root、配置与 `@` 别名均指向快照；关闭 HMR 和文件监听。测试文件、报告、`.env` 不进入快照，仅只读复用已有 `node_modules`。后续共享 checkout 编辑不会更新这份页面；需要显式结束进程、重新取快照并手动刷新浏览器，才会加载新代码。

| 快照 | SHA-256 | 用途 |
| --- | --- | --- |
| 修复前 | `98580731548b240926ff058ab75089e503dd450adc593cb470ce4faa797a2c20` | 完成新装命令生成、真实安装和发布故障复现，并稳定复现回看第 3 步后无法返回发布步骤的问题；原进程已结束 |
| 主流程验收版本 | `dc335b29f11192ff4cdc3399d24001c8b3eeb2643aeedfbe62e44220c34d35bf` | 验证新装会话恢复、发布重试、转发与撤销；已有接入和普通模式在此版本完整操作，修复后的填写与规则说明也在已有接入流程复验 |
| 管理文案核对版本 | `a659bfea796559355a056d339e5539f75b952a126080556fc5efad9e17235b97` | 从上一快照派生，只更新 `src/components/generate/LinuxGatewaySettings.vue` 三段文案 |

主流程快照启动于 `2026-10-19T06:53:26.675Z`，PID `54098`，共 313 个文件；管理文案核对版启动于 `2026-10-19T07:10:45.393Z`，PID `57540`。逐文件比较证明新版本仅指定组件改变，向导及其他 312 个文件没有引入并发改动。两版均核验 Vue 模块编译与 API 代理，最后均通过 SIGTERM 正常关闭并清理缓存。

已归档 [主流程 manifest](results/linux-gateway-browser-2026-10-19/frontend-main-manifest.json)、[逐文件摘要](results/linux-gateway-browser-2026-10-19/frontend-main-source-manifest.json) 和 [管理文案 manifest](results/linux-gateway-browser-2026-10-19/frontend-management-manifest.json)、[逐文件摘要](results/linux-gateway-browser-2026-10-19/frontend-management-source-manifest.json)。原 `/private/tmp/configflow-gateway-lab-frontend-*` 工作目录已删除，manifest 中的原路径只用于溯源。

## 浏览器覆盖进度

以下“已验证”仅限本轮实际执行并回报的操作；页面状态与真实系统结果分别取证。

| 场景 | 当前结果 | 对应证据 |
| --- | --- | --- |
| 登录与选择实验 Profile | 已进入真实安装向导；未预写 token 跳过登录 | 浏览器操作记录；私有凭据不归档 |
| 新设备填写、选规则、保存并创建会话 | 已由页面生成本轮安装命令 | 会话 `c6366135eece424eb454f36f91e1c53d` |
| Agent 下载校验失败 | 已验证安装命令失败，核心与 Agent 文件、两个 namespace 网络均不变，无转发 journal；下载文件已恢复 | `checksum-failure-report.json` |
| 同会话安装重试 | 同一浏览器命令成功，双 Agent 在线，bootstrap TUN 关闭，无转发表 / journal，网络与初始状态一致 | `fresh-install-report.json` |
| OxiDNS 真实发布失败与回滚 | 真实启动失败；旧配置字节和权限恢复，新规则文件清理，健康恢复；故障条件已移除 | `publish-failure-rollback-report.json` |
| 页面点击发布重试 | 已发布；只为失败 OxiDNS 创建新任务，成功 Mihomo 的任务、PID 和文件保持不变 | `browser-publish-retry-report.json` |
| 已完成步骤回看 | 修复版允许第 3 → 4 步；发布后回看第 3 步仍可返回第 5 步 | [`before-navigation.json`](results/linux-gateway-browser-2026-10-19/before-navigation.json)、[`after-navigation.json`](results/linux-gateway-browser-2026-10-19/after-navigation.json) |
| 转发预览 | 页面预览成功；gateway / 客体初始 namespace 全网络快照均未改变 | 浏览器及只读快照回报；[预览截图](results/linux-gateway-browser-2026-10-19/new-transfer-preview.png) |
| 显式应用与恢复向导 | 页面应用成功；关闭后重新打开恢复第 5/5 步及 active 状态 | [应用截图](results/linux-gateway-browser-2026-10-19/new-transfer-active.png) |
| 375px 完成页 | `scrollWidth / clientWidth = 375 / 375`，无横向溢出；12 个可见按钮均为 44px 高；接入与撤销说明滚动可达 | 浏览器测量；[客户端说明截图](results/linux-gateway-browser-2026-10-19/mobile-client-guidance.png) |
| 直连、所选 IP、fake-IP、UDP/TCP DNS 探针 | 新装与已有接入均通过，含 controller、SOCKS、HTTP 和 DNS 包证据 | [`browser-new-active-report.json`](results/linux-gateway-browser-2026-10-19/browser-new-active-report.json)、[`browser-existing-active-report.json`](results/linux-gateway-browser-2026-10-19/browser-existing-active-report.json) |
| 页面撤销及网络完整恢复 | 两次均恢复各自发布后 / apply 前的完整快照，客体初始 namespace 不变，无 journal | [`browser-new-removed-report.json`](results/linux-gateway-browser-2026-10-19/browser-new-removed-report.json)、[`browser-existing-removed-report.json`](results/linux-gateway-browser-2026-10-19/browser-existing-removed-report.json) |
| 已有标准核心接入全流程 | 页面创建新会话、原命令接入、发布、预览、应用、流量与撤销全部通过 | [`browser-existing-install-report.json`](results/linux-gateway-browser-2026-10-19/browser-existing-install-report.json)、[发布报告](results/linux-gateway-browser-2026-10-19/browser-existing-publish-report.json)、[接入说明](results/linux-gateway-browser-2026-10-19/existing-network-guidance.png) |
| 普通模式默认与 Shell AIO 安装 | 普通单服务默认已在 UI 验证；页面 AIO 原始命令真实执行，选择保留已有配置，不创建网关会话、不改变核心或网络 | [`browser-ordinary-report.json`](results/linux-gateway-browser-2026-10-19/browser-ordinary-report.json)、[默认选项](results/linux-gateway-browser-2026-10-19/ordinary-default.png) |
| 普通模式 Docker | 页面生成 Docker 命令成功，普通模式保持勾选、命令不含网关路径；未执行容器部署 | [Docker 入口截图](results/linux-gateway-browser-2026-10-19/ordinary-docker.png)、[`final-browser-checks.json`](results/linux-gateway-browser-2026-10-19/final-browser-checks.json) |
| 三色 × 深浅主题及辅助显示设置 | 六种组合均检查；关闭纹理、静止动效后仍可操作 | `theme-*.png`、`mobile-active-light.png`、`existing-active-dark-kraft.png`、`existing-publish-dark-sky.png` |
| 管理页三段文案 | 页面已读到普通脚本只接已有核心 / 安装向导补装核心、向导准备两项服务 / 管理页不改写 OxiDNS，并核对完整原值恢复指南 | [`final-browser-checks.json`](results/linux-gateway-browser-2026-10-19/final-browser-checks.json)、[管理恢复指南](results/linux-gateway-browser-2026-10-19/management-restore-guidance.png) |
| 手机网络帮助、规则空态与草稿退出 | 375px 无横向溢出，按钮至少 44px；继续编辑保留草稿，放弃后恢复原已有接入会话、未新建会话；搜索空结果不丢选项 | [测量记录](results/linux-gateway-browser-2026-10-19/mobile-rule-metrics.json)、[网络帮助](results/linux-gateway-browser-2026-10-19/mobile-network-help.png)、[规则空态](results/linux-gateway-browser-2026-10-19/mobile-rules-empty.png)、`final-browser-checks.json` |
| 退出登录、前端与 VM 清理 | 实际 UI 退出回到登录页并显示成功提示；剪贴板已清空、viewport 已恢复、测试标签已关闭；前端、隧道、VM 服务和实验网络均已清理 | [前端清理报告](results/linux-gateway-browser-2026-10-19/frontend-cleanup-report.json)、[VM 清理报告](results/linux-gateway-browser-2026-10-19/vm-cleanup-report.json)、[最终清理报告](results/linux-gateway-browser-2026-10-19/final-cleanup-report.json) |

发布重试的实际任务为：Mihomo `72092f3803214352a6be119de40eed36` 保持，PID `4039` 保持；OxiDNS 从失败任务 `ad8fd1c820264aa18ff69cbc22f5dcc2` 改为成功任务 `8df8d465368f4ffb856cb11d109ae440`，成功后 PID `4673`。这些是部署任务 ID，不含安装凭据。

新装和已有接入的流量探针均证明：未选中的直连 IP 没有 Mihomo 连接或 SOCKS 记录；所选真实 IP 与 fake-IP 请求均命中 `LAB-PROXY`，且有 HTTP 目标、controller 和 SOCKS 三方记录。客户端 UDP/TCP 53 查询均由 OxiDNS 回答，随机代理域名出现 OxiDNS → `127.0.0.1:1053` 请求与响应，返回 `198.18.0.0/16` 内地址；Mihomo 的 53 端口 DNAT 计数未增长。已有接入重复探针使用缓存时不强求新建 DoH TLS 连接。

已有核心命令耗时 1.11 秒，核心字节、权限、属主、mtime、PID 与 Agent ID 均保持不变；被动 curl 记录只有实验 ConfigFlow 地址，无 GitHub 下载。普通 Shell 原始命令耗时 2.18 秒，交互选择 `1` 保留现有配置，网关会话数保持 `2 → 2`，核心 / 配置 / PID / Agent ID / nft / 路由 / sysctl 和监听保持不变，无转发 journal。普通 Agent 服务自身更新属于原有安装行为，不宣称所有 Agent 进程都未重启。

普通命令使用浏览器的 `window.origin`，因此客体内临时建立仅网关 namespace loopback 可见的 `127.0.0.1:13001` relay；页面命令未修改。relay 在执行后已关闭，监听比较以关闭后为准，宿主网络未改变。普通安装之后，两 Agent 保存的回连地址就是该临时 URL；关闭 adapter 后心跳变为 `0/2` 是测试 URL 生命周期导致的预期离线，不是核心启动或转发故障。核心健康、文件与网络不变的实测结果仍然成立；本轮不以关闭适配器之后的离线状态宣称 Agent 长期连通。

## 本轮前端修复与测试

- 步骤导航按安装会话已达阶段计算可访问范围，回看已完成的安装步骤后仍可回到发布或启用页面。
- 撤销后显示独立结果，指导恢复客户端原网关、DNS、IPv6 与安全 DNS；说明撤销转发不会卸载核心，也不会自动关闭已发布的 Mihomo TUN。
- 安装与接入说明明确标准目录、已有核心保留边界、双 Agent 登记后发布顺序，以及修改安装信息和继续最近会话的作用。
- 375px 步骤导航只在当前步骤显示名称，保留步骤编号与至少 44px 的操作目标。
- 生成工作台的管理入口指向 Agent 安装向导，并补充版本要求和恢复客户端原设置的说明。

最终修复后，主验收任务于本地时间 `14:53:28` 在 `frontend` 执行以下六文件测试：

```sh
NODE_OPTIONS=--no-experimental-webstorage npm test -- \
  tests/linux-gateway-settings.test.ts \
  tests/gateway-install-wizard.test.ts tests/gateway-install-entry.test.ts \
  tests/agent-shell-aio-install.test.ts tests/agent-settings-entry.test.ts \
  tests/gateway-onboarding-api.test.ts
```

结果为 **6 个文件、69 个相关测试通过；最后文案变更后管理页 13 项复跑与 build 通过**，由执行命令的主验收任务及负责 build 的 agent 确认。13 项与原 69 项有重叠，不相加。这与准备阶段另一次 5 文件、69 测试的鉴权 / 向导测试也不是同一组；本报告的主回归采用上述六文件结果。管理页三段文案在定点快照中另经浏览器核对，不宣称之后重新执行了全部网络流程。

本机 Node `26.4.0` 的默认 webstorage 与 jsdom 的 localStorage 有冲突，因此显式关闭 Node 的实验 webstorage；这属于测试运行环境适配，未修改产品登录逻辑。

独立 harness 已通过 `node --check scripts/tests/linux_gateway_lab_frontend.mjs`；后端 fixture 与发布重试脚本通过 Python `py_compile`。启动器已验证严格 loopback 绑定、独立快照、编译模块、API 代理、指定单文件更新及非法参数拒绝，以及 SIGTERM 后关闭监听、清理缓存并更新 manifest。

## 复现入口与证据管理

完整准备、登录、刷新快照与清理说明见 [实验指南的浏览器章节](LINUX_GATEWAY_LAB.md#通过浏览器操作真实安装向导)。前端启动命令为：

```sh
node scripts/tests/linux_gateway_lab_frontend.mjs --api-port 18000 --port 13001
```

服务地址仍填写客体可达的 `http://192.0.2.1:8000`；不能将浏览器的 loopback 入口写入安装配置。切换端口须同时调整 SSH 转发和启动参数。浏览器负责建立会话及有状态操作，终端只执行本轮页面提供的命令，不运行 `linux_gateway_lab_driver.py` 顶替 UI。

本轮私有证据原根目录为 `/tmp/configflow-browser-linux-e2e.be_hngzp`，**审阅后已删除**；可归档结果位于 [results/linux-gateway-browser-2026-10-19](results/linux-gateway-browser-2026-10-19/)。只归档经过检查的阶段状态、规则、网络预览、应用 / 撤销和普通模式截图。**不归档包含一键安装命令、token、密码或 controller secret 的截图**，不复制 `*.private.json`、`admin-token`、SSH 私钥或完整配置仓库到公共结果。

准备期 `06:40–06:42 UTC` 的版本请求 / Axios 连接错误来自后端尚未接通；旧快照 `06:48:39 UTC` 的 `Missing initializer` 出现在 macOS `/tmp` 与 `/private/tmp` 路径规范化修复之前。最终主流程快照从 `06:53 UTC` 开始验收后，浏览器未新增 error / warn。准备期问题单独记录，不能混作最终产品运行错误。

## 最终清理

[前端清理报告](results/linux-gateway-browser-2026-10-19/frontend-cleanup-report.json) 确认 PID `57540` 已结束、宿主 `127.0.0.1:13001` 不再监听，最后两份快照的 manifest 和逐文件摘要已校验后归档，本轮 8 个前端临时工作目录均已删除，仓库依赖目录保留。

[最终环境报告](results/linux-gateway-browser-2026-10-19/final-cleanup-report.json) 与 [VM cleanup](results/linux-gateway-browser-2026-10-19/vm-cleanup-report.json) 均为 `success=true`：客体初始网络前后完全一致，`/etc/nsswitch.conf` SHA 校验通过；四个测试服务停止并禁用，后端与 relay 停止，5 个实验 namespace 移除，SSH 转发关闭，QEMU 正常退出。宿主 API `18000` 与 VM SSH `58119` 不再监听，宿主默认路由和接口列表未改变。审阅完成后，私有 VM 工作区及其中的磁盘、密钥、凭据和命令文件已删除，最终报告记录 `private_workspace_deleted=true`；仓库仅保留脱敏证据。

本实验仅覆盖 Debian 13 ARM64、systemd、IPv4 和上述隔离拓扑，不扩大为其他架构、第三方核心目录、IPv6 或复杂防火墙的兼容性结论。
