# OxiDNS Agent 部署

OxiDNS 使用 Go Agent 的 deployment protocol 1，与 MosDNS 保留为两个独立服务类型。旧 Shell Agent 不支持 OxiDNS 事务发布。

## 发布与回滚

1. 服务端从 Agent 绑定的配置快照生成配置，获取全部规则并计算文件摘要。
2. Agent 解包至隔离暂存目录，检查清单、路径和文件摘要。
3. 使用已安装核心执行 `oxidns check -c <暂存配置> -d <暂存目录>`。缺少功能的 minimal/standard 核心会在这里拒绝不兼容配置；特色功能建议 full 版本。
4. 检查待发布配置的 DNS 监听及同机上下游关系；明确冲突会在修改运行配置前拒绝发布。校验成功后停止服务、备份受管理文件、替换配置并启动服务。
5. 检查服务状态、实际 DNS A 查询；启用明文 HTTP API 时同时检查 `/api/readyz`，从配置中读取 Basic 认证。健康失败自动恢复旧配置及旧规则，再检查旧服务。

API 启用 TLS 时默认使用服务状态和实际 DNS 查询作为健康条件，不声称已验证 HTTPS 管理接口，也不跳过证书校验。需要额外验证 HTTPS 时，可设置证书名称与系统信任链均有效的 `health_url`；自签名证书需在主机安装可信 CA。mTLS 健康接口须另行提供可用检查入口。

管理 API 的用户名和密码不会被发送给不匹配的自定义健康 URL，原生校验错误也不回显包含凭据的核心输出。

动态规则和查询记录等文件使用 `state/`。该目录不进入下载包、发布清单或配置回滚；已有运行数据会保留。缓存 dump 也不作为规则文件发布。恢复配置不能撤销 RouterOS 上已经发生的外部变化，或已记录的查询历史。

### 发布前 DNS 检查

Go Agent `1.8.19-go` 起，Mihomo、MosDNS、OxiDNS 每次发布前检查最终待生效配置的 DNS TCP/UDP 监听。已确认属于当前受管核心的监听允许继续发布，其他进程占用或无法核实归属的占用会阻止发布。仅暂存不激活时也生成报告，之后点击激活会重新检查，避免沿用过期的端口状态。

Shell 同机模式与 Docker AIO 会读取另一核心实际配置，提示上下游地址/端口不一致及潜在回环；只有待发布配置自身或与对端组合后能够证明的无条件回环才阻止发布；对端配置已有的独立回环仅提醒，允许逐台发布修复。条件分流或多个入口存在具体循环引用时仅提醒；对端配置无法读取时提示尚未核实。常见缓存、扩展插件本身不触发警告，检测不遍历证明全部插件分流。启用 TUN 的一体化配置出现目标端口 53 劫持时提示可能绕过前置 DNS，不把它误判为监听端口占用。单服务部署不读取无关联的其他核心，也不因未启用 TUN 或尚无节点而拒绝配置。

实际解析仅探测当前已核实归属的 DNS 监听，使用 `health_dns_name`（默认 `example.com`），最多检测 8 个监听并限制总等待时间。新端口尚未启动时标为待生效；当前查询失败只提醒，允许发布修复配置。发布后的服务健康检查与失败回滚继续执行。检查不停止服务、不清缓存，不分析节点、订阅或完整路由；不承诺 ROS 及其他容器的流量路径已验证。

面板在发布进度和历史记录显示 `dns_checks` 与 `dns_checked_at`，错误及修复建议直接可见，警告不新增确认弹窗；旧 Agent 未返回报告时不会显示“通过”。安装前的 TUN、权限、IPv4 转发和端口环境检查保持原有逻辑。

### 手动回滚已成功发布的配置

Agent 页面“发布记录”中的“回滚到发布前”用于恢复最近成功发布前的备份。服务端入口为 `POST /api/agents/{agent_id}/deployments/{deployment_id}/rollback`，通过原部署状态接口轮询结果。已受理任务保留相同部署 ID，带 `manual_rollback: true`，状态从 `rolling_back` 进入 `rolled_back`；此时 `success: true`。自动失败回滚仍用 `success: false` 区分。

只允许当前配置方案下最新且仍在运行清单中的成功部署。Agent 在停止服务前检查操作锁、备份摘要、实际配置路径及当前文件摘要。较旧发布、缺失备份、正在升级或被外部任务改写的配置/规则会拒绝回滚并说明原因。若每日规则更新任务已改写规则，需先处理该差异；不能用旧发布记录覆盖未知更改。

回滚恢复受管理文件及发布前的服务启动状态，并对原本运行的服务重新执行健康检查。备份中的旧服务无法恢复健康时状态为 `rollback_failed`，保留恢复记录并阻止新的发布；Agent 启动时只检查并提示，不会自动停止核心或重试恢复；修复原因后需显式处理恢复记录。成功回滚会清除 ConfigFlow 的“当前方案已同步”标记，因为恢复的文件可能来自接入 ConfigFlow 之前。

完整发布不接受外部 `include` 配置。请把插件写入主配置；`files`、`file`、`persistent.files` 引用的输入文件必须在包中。TLS 证书和 WebUI 静态资源由主机或镜像单独提供。

## 本机安装

Agent 安装页面选择 OxiDNS，默认核心配置路径 `/etc/oxidns/config.yaml`、服务名 `oxidns`、二进制 `oxidns`。使用官方便携安装布局时，请把配置路径改为实际路径，例如 `/opt/oxidns/config.yaml`。核心服务的 `-d` 工作目录必须与配置文件所在目录一致；systemd 安装和每次发布均检查此条件。官方 Debian 包采用 `/etc/oxidns` 配置、`/var/lib/oxidns` 工作目录的分离布局，需先调整核心服务与资源布局再接入，安装器会明确拒绝直接覆盖该布局。OpenRC 或自定义命令由管理员保证同目录运行。

systemd 检查也会核对 `-c`/`--config` 实际文件与 Agent 的 `config_path`，避免同目录中的两个文件造成“推送成功但服务仍使用旧配置”。相对配置路径按 OxiDNS 的实际工作目录解析；省略 `-c` 时按 `config.yaml` 检查。

单服务安装沿用 `configflow-agent` 系统服务。检测到另一核心的 Agent 配置时，会拒绝覆盖；同机管理 Mihomo 和 OxiDNS 请使用下述同机安装模式。Agent 端口与 DNS 核心端口应避免冲突。

### 单服务同时安装核心

在 Agent 安装弹窗选择 Shell →「单服务」及 Mihomo / OxiDNS，可勾选「同时安装对应核心」。默认不勾选，沿用接管已有服务的方式；勾选后生成一条完整下载成功才执行的命令，以 root 在目标 Linux 机器运行，支持 systemd / OpenRC。命令默认使用 curl，可切换为 wget，先下载到私有临时文件，成功后执行并清理。目标机器须已有所选下载工具和 mktemp；若 curl、wget 都没有，请先安装其中一个。环境检测和其余依赖补装在脚本中进行，失败会提示修复方法。终端选择「安装或升级」后，先运行对应核心安装步骤，成功后继续安装 Agent；选择取消或卸载时不执行核心安装。MosDNS 暂不提供此选项。

勾选时使用标准布局：核心程序 `/usr/local/bin/mihomo` 或 `/usr/local/bin/oxidns`，配置 `/etc/<核心名>/config.yaml`，服务名为核心名，自动识别服务管理器。配置路径及服务设置位于默认收起的「服务管理高级配置（可选）」中；勾选时暂不可编辑，取消勾选后恢复原来填写的内容。已有自定义布局应取消勾选并填写实际路径；接口也会拒绝将核心安装与不匹配的接管路径或服务命令组合。

新装 Mihomo 的 DNS 为 `1053`，保留 TUN 自动检测与开启逻辑；新装 OxiDNS 的 DNS 为 `53`，使用公共 DNS 上游，可独立运行。已有核心的程序、配置和服务会保留，跳过安装不代表已确认其健康。核心步骤失败时，不继续安装 Agent；核心步骤成功后，若 Agent 安装失败，已安装核心会保留，可修复 Agent 问题后重试。卸载 Agent 也不卸载核心。

该选项通过 `/api/agents/install-script?install_core=true` 生成组合脚本，预览与实际下载使用相同参数；未传或传 `false` 时仅安装 Agent。面板生成命令前检查返回脚本的接口标记；旧服务端不支持该选项时会提示更新。核心程序仍从固定版本的官方发行页下载并校验 SHA256，命令没有打包核心二进制。

### 一键安装两个核心

在 Agent 安装弹窗选择 Shell →「Mihomo + OxiDNS 同机」，保持「同时安装 Mihomo 与 OxiDNS 核心」未勾选，展开「仅安装核心」，选择安装两个核心、仅 Mihomo 或仅 OxiDNS，再生成对应命令。以 root 在目标 Linux 机器粘贴执行这一行命令；无需填写 Agent 名称。默认提供 curl 命令，可切换为 wget；先完整下载脚本，下载成功后才执行，结束时清理私有临时文件。若两个下载工具都没有，请先通过系统包管理器安装 curl 或 wget；缺少 mktemp 时先安装 coreutils。其余环境检测与必要依赖补装由脚本处理。该脚本只安装所选核心，不安装 Agent，也不向面板注册。

| 安装选择 | 新建核心的初始 DNS 配置 |
| --- | --- |
| 两个核心（默认） | OxiDNS `53` → Mihomo `127.0.0.1:1053` → 公共 DNS；已有核心会跳过，仅补装缺少的核心 |
| 仅 Mihomo | Mihomo `1053` → 公共 DNS，不安装或操作 OxiDNS |
| 仅 OxiDNS | OxiDNS `53` → 公共 DNS，可独立运行，不依赖或操作 Mihomo |

**已有 Mihomo、需要补装与它协作的 OxiDNS 时，保持默认的两个核心选项即可。** 现有 Mihomo 的程序、配置和服务保持原样，需要自行确认它已提供 `127.0.0.1:1053` DNS。若现有服务占用了 `53`，先调整其监听设置再安装 OxiDNS；脚本不会自动迁移现有 DNS。单独安装 OxiDNS 时的公共上游为 `223.5.5.5`、`119.29.29.29`，后续可在面板修改上游。已有 OxiDNS、后来补装 Mihomo 时，也会保留原来的 OxiDNS 上游；要组成同机链路，需自行将其上游设置为 `127.0.0.1:1053`。核心安装完成后，再使用原有的 Agent 安装入口接入。

脚本支持 systemd / OpenRC 以及 amd64、arm64、armv7。它从官方 GitHub 发行页下载所需的固定版本 [Mihomo v1.19.32](https://github.com/MetaCubeX/mihomo/releases/tag/v1.19.32) 或 [OxiDNS v1.6.1 full](https://github.com/svenshi/oxidns/releases/tag/v1.6.1)，并按脚本内固定的 SHA256 校验；下载或校验失败时停止，不执行不完整或未经校验的文件。

缺少 `tar`、`gzip`、`sha256sum` 等必要工具时自动补装，支持 Debian/Ubuntu 的 apt-get、Alpine 的 apk、Fedora/RHEL 系列的 dnf/yum、Arch 的 pacman 和 openSUSE 的 zypper。仅在缺工具时安装对应依赖，不执行整机升级，也不自动安装或更换服务管理器。包管理器失败或安装后工具仍缺失时停止并提示处理方法；新增依赖包不会随核心安装失败而卸载。现有 curl/wget 继续使用其证书和自定义信任设置，TLS 验证失败时停止并提示检查系统时间、ca-certificates 和自定义 CA，不关闭证书验证。

| 项目 | 默认布局 |
| --- | --- |
| 核心程序 | `/usr/local/bin/mihomo`、`/usr/local/bin/oxidns` |
| 配置文件 | `/etc/mihomo/config.yaml`、`/etc/oxidns/config.yaml` |
| 系统服务 | 本次新装的 `mihomo` / `oxidns` 启动并加入开机启动，已有服务保持原样 |
| DNS 入口 | OxiDNS TCP/UDP `53`，同机模式转发到 `127.0.0.1:1053`，单独安装则使用公共上游 |
| Mihomo DNS | TCP/UDP `1053`，初始上游为 `223.5.5.5`、`119.29.29.29` |
| TUN | 首次安装检查设备和权限，可用时开启；不可用时关闭并给出原因和修复方法 |
| 代理 | 不创建代理监听或代理节点，初始流量按 `MATCH,DIRECT` 直连 |

仅对本次新装的 Mihomo 检查 TUN：不仅检查路径存在，还检查 `/dev/net/tun` 是正确的字符设备、可读写，以及当前进程具备 `CAP_NET_ADMIN` 网络管理权限。通过后，初始配置使用 `stack: mixed`、`auto-route: true`、`auto-detect-interface: true`、`auto-redirect: false`，设备名为 `configflow-tun`，DNS 劫持目标为 `any:1053` 和 `tcp://any:1053`；Mihomo 启动时会自动配置路由。无法确认可用时继续安装，但保持 `tun.enable: false`，并显示具体原因及开启步骤。服务启动后还会确认 TUN 网卡实际创建；若未创建，则关闭初始配置中的 TUN 并重新启动 DNS 服务，提示修复方法。

初始配置可用于 DNS 解析和直连，不代表已配置代理分流；接入 Agent 后可从 ConfigFlow 发布所需规则、节点及 TUN 配置。作为旁路网关时，还需在实际转发流量的 LXC / VM 内持久开启 IPv4 转发，脚本会检查并提醒，但不写入 `/etc/resolv.conf`、IPv4 转发、防火墙或 PVE 设备设置；TUN 启用后核心会配置路由，并可能配置该网卡的系统 DNS。Agent 弹窗中的自定义核心路径仅用于接管现有安装，不改变该独立脚本的默认布局。

若 TUN 未开启，请按安装输出修复设备与权限：PVE LXC 的设备直通在 **PVE 宿主机**设置，容器内检查 `/dev/net/tun` 及网络管理权限；普通 Linux 主机需检查 TUN 驱动与设备权限。修复后在现有 `/etc/mihomo/config.yaml` 的 `tun` 段将 `enable` 改为 `true`，再重启 Mihomo 服务。已接入 ConfigFlow 时，可在「Mihomo 基础配置」修改同一设置并保存发布。**发布使用面板草稿中的 TUN 设置，不继承安装时的检测结果**；若安装时关闭了 TUN 且尚未修复，首次发布前也需将草稿中的 `tun.enable` 设为 `false`。重复运行核心安装命令会保留已有配置，不会重新决定 TUN 开关。

安装前检查 root 权限、系统架构、服务管理器、必要工具，以及本次新装核心所需的端口：Mihomo `1053`、OxiDNS `53`。所需核心下载和配置校验完成后才落盘；失败时只撤销本次创建的文件和服务，不覆盖或停止已有核心。若回滚无法停止服务或取消开机启动，则保留恢复所需文件并提示手动处理。所选核心都已存在时直接跳过，不下载或安装依赖，也不重启服务；“跳过”不表示已有服务已通过健康检查。仅在预检失败、尚未创建核心资源时，明确提示未修改核心，不再显示已执行回滚。

安装后检查本次新装服务的状态。systemd 日志进入系统日志；OpenRC 日志保存在 `/etc/mihomo/mihomo.log`、`/etc/oxidns/oxidns.log`，启动失败会先显示最近日志。目标机有 `dig` 时验证本次新装核心端口的 UDP/TCP 解析；未安装 `dig` 时明确提示尚未验证实际解析，并给出查询命令。TUN 环境检测与 DNS 查询不能证明完整的网关转发、代理分流或重启后状态，这些仍需在目标网络验证。按已安装核心手动验证：

```sh
dig @127.0.0.1 -p 53 example.com
dig @127.0.0.1 -p 53 example.com +tcp
dig @127.0.0.1 -p 1053 example.com
dig @127.0.0.1 -p 1053 example.com +tcp
```

脚本下载入口为 `/api/agents/core-install-script`，使用固定布局且不包含 Agent 身份信息。已下载脚本可用 `sh install-cores.sh --core mihomo` 或 `sh install-cores.sh --core oxidns` 单独安装；省略参数或传 `--core both` 为同机协作模式。面板生成命令前检查脚本接口标记，旧版不支持选择核心时会提示更新。其命令与 `/api/agents/install-script` 的 Agent 安装命令相互独立。

### Shell 同机安装 Mihomo + OxiDNS

在 Agent 安装弹窗选择 Shell 的「Mihomo + OxiDNS 同机」模式，可勾选「同时安装 Mihomo 与 OxiDNS 核心」，用一条命令先安装或补齐两个核心，再安装两个独立 Agent。默认不勾选，继续接管已安装的核心；此时也可展开「仅安装核心」生成独立命令。两种方式均支持 systemd 和 OpenRC。

勾选时使用 `/etc/mihomo/config.yaml`、`/etc/oxidns/config.yaml`，核心程序位于 `/usr/local/bin`，服务名为 `mihomo` 和 `oxidns`；高级配置暂时锁定，取消勾选后恢复原值。新装核心的 DNS 链路为 OxiDNS `53` → Mihomo `127.0.0.1:1053`，Mihomo 沿用 TUN 自动检测。已有核心和配置会跳过并保留；已有 Mihomo 需确认 DNS 监听 `1053`，已有 OxiDNS 的上游不会被自动迁移。两个 Agent 的端口不能占用 `53` / `1053`，也不能相同。

组合命令完整下载后再执行，通过 `type=aio&install_core=true` 生成，默认使用 curl，可切换 wget；面板生成前校验脚本接口标记。在终端选择安装后才执行核心安装；取消或卸载只处理原有 Agent 流程。核心安装失败时不进入 Agent 安装，后续 Agent 安装失败则保留已完成的核心。Agent 接入前会验证两个核心的服务配置，并检查是否存在未完成的发布；任一检查失败时，不停止现有 Agent。

运行安装或卸载脚本期间，请暂停从面板发起新的配置发布和 Agent 升级；脚本的状态预检不代替跨进程的完整操作锁。

| 用途 | 默认端口 |
| --- | --- |
| OxiDNS DNS（UDP / TCP） | 53 |
| Mihomo DNS（UDP / TCP） | 1053 |
| Mihomo Agent | 8080 |
| OxiDNS Agent | 8082 |

DNS 端口分工与 Docker 同时启用 Mihomo、OxiDNS 时一致。两套 Agent 端口可在安装弹窗修改；未勾选同时安装核心时，核心配置路径、服务名和可执行文件也可分别设置。两个 Agent 分别以 `<名称>-mihomo`、`<名称>-oxidns` 注册。

- 两套 Agent 使用独立的 `configflow-agent-mihomo` / `configflow-agent-oxidns` 系统服务、`/opt/configflow-agent/mihomo` / `/opt/configflow-agent/oxidns` 目录和二进制，升级及远程卸载互不删除对方的文件。重跑同机脚本会保留对应 Agent 身份。
- 安装脚本保留现有核心配置。首次接入先在 ConfigFlow 发布 Mihomo 配置，再发布 OxiDNS 配置：Mihomo Agent 将 DNS 监听调整到 `0.0.0.0:1053`，将 TUN 的 DNS 劫持目标调整为 `any:1053` 和 `tcp://any:1053`，避免抢占 OxiDNS 的 53 端口。Agent 发布时沿用面板草稿中的 DNS 和 TUN 开关。
- OxiDNS 的监听和上游沿用用户配置。使用常用设置时保留 DNS 监听 `53`；需要 Mihomo 处理远程解析时，将远程上游改为 `127.0.0.1:1053`。Mihomo 上游不应再指回 OxiDNS，以免形成循环。自定义 OxiDNS YAML 不会被强制改写。
- TUN 可继续使用，但 `/dev/net/tun`、网络管理权限及路由需要在运行核心的系统中准备。承担网关转发的 LXC / VM 需在该系统持久开启 IPv4 转发；安装脚本不会修改 PVE、RouterOS 或系统转发设置。OxiDNS 上游是否经过 TUN 仍由 Mihomo 路由与规则决定，端口适配不等于上游绕行配置。

已有单服务 Agent 时，同机安装会明确拒绝共享布局；先卸载原 Agent（保留核心及核心配置），再运行同机脚本。保留原 Agent 时不要直接混用两种安装方式。运行当前安装脚本前，需要更新 ConfigFlow 并提供 `1.8.19-go` 或更高版本的 Go Agent 二进制。

### 安装前环境检测

从 `1.8.19-go` 起，Shell 单服务及同机安装都会在停止旧 Agent、替换其程序和配置之前进行环境检测。同机模式一次检查两个 Agent，输出「通过 / 提醒 / 失败」及对应处理方法；出现失败时退出安装。检测不会自动开放权限、修改转发设置、停止占用端口的程序或更改 PVE 配置。

| 检查 | 判定与处理 |
| --- | --- |
| TUN 设备 | 检查 `/dev/net/tun` 是否为可访问的 TUN 字符设备，读取驱动能力；当前 Mihomo 配置通过顶层 `tun` 或 `listeners` 启用 TUN 时，明确不可用会阻止安装。未启用 TUN 时提示，不阻止 DNS / 普通代理用途。 |
| 网络权限 | 检查网络管理能力和可见的核心服务权限限制；不把 root 身份直接当作 TUN 可用。不能确认服务启动环境时明确提示。 |
| IPv4 转发 | 显示当前 `net.ipv4.ip_forward`，检查可见的持久化配置。关闭或存在覆盖风险时提示如何修复；纯 DNS 或本机代理不强制开启。作为旁路网关时需先完成提示中的转发设置。 |
| 端口占用 | 联合核对 Agent 的 TCP 端口及 DNS 的 UDP/TCP 监听；同机 Mihomo 按发布后的 `1053` 检查，OxiDNS 按实际配置读取。确认是本实例已有进程监听时允许重装，外部或无法确认归属的占用会阻止安装。 |

检测以当前核心配置为依据，不会提前启用 TUN，也不会创建测试网卡或修改路由。PVE LXC 缺少 TUN 时，提示在 **PVE 宿主机**检查模块、为容器添加 `/dev/net/tun` 设备；IPv4 转发在实际承担路由的 **LXC / VM 内**检查和持久化。设备存在、能力检查通过不代表已验证完整流量链路；安装后仍应验证 DNS、网关流量与重启后的状态。

端口检查区分监听地址及协议，不把同一端口上的所有进程一概判为冲突。已有 Mihomo 使用 `53`、同机部署将迁往 `1053` 时给出迁移提示：先发布 Mihomo，再发布 OxiDNS。对于其他程序占用，会提示定位进程及调整监听或 Agent 端口，不自动停止系统 DNS。检查是当前状态的快照，运行安装脚本期间仍应避免其他安装、发布和升级操作。

安装后也可以重新检查同机环境：

```sh
/opt/configflow-agent/mihomo/configflow-agent \
  -config /opt/configflow-agent/mihomo/config.json \
  -environment-peer-config /opt/configflow-agent/oxidns/config.json \
  -check-environment
```

只检测单服务时使用对应 Agent 的实际程序和配置路径，并省略 `-environment-peer-config`。检查只读取状态；存在失败项时退出码为 `1`。

修复提示依据：[Linux TUN/TAP 驱动](https://kernel.org/doc/html/v5.19/networking/tuntap.html)、[PVE 设备直通参数](https://github.com/proxmox/pve-docs/blob/master/generated/pct.1-synopsis.adoc)、[systemd sysctl 配置优先级](https://www.freedesktop.org/software/systemd/man/257/sysctl.d.html)。持久化配置只能证明发现了设置，不能保证之后没有其他服务覆盖，需在维护时重启验证。

## Docker

统一镜像新增 `SERVICE_TYPE=oxidns` 和 `ENABLE_OXIDNS=true`，Agent 默认端口 `8082`。示例见 [docker-compose-oxidns.yaml](../docker/docker-compose-oxidns.yaml)。使用新增功能前必须构建或拉取包含本次适配的新 Agent 镜像。

- OxiDNS full 核心固定官方 `v1.6.1` 多架构镜像摘要，支持 `linux/amd64` 和 `linux/arm64`。
- `/etc/oxidns` 持久化配置、规则、事务备份与 `state/`；`/opt/configflow-agent` 持久化 Agent 身份。
- 默认映射 DNS UDP/TCP 53 及 Agent 8082；管理 API 9199 需要配置认证、监听地址和端口映射。
- WebUI 文件在 `/usr/share/oxidns/webui`，在 OxiDNS API 设置中填写该路径。
- Docker 同时启用 Mihomo 与 OxiDNS 时，Mihomo DNS 调整至 1053。MosDNS 与 OxiDNS 默认均使用 53，同一容器安装入口会拒绝同时启用二者；分别用实例与不同端口部署。
- 核心日志写入 `/var/log/supervisor/oxidns.log` 与 `oxidns.err.log`。原生运行优先读取配置中的日志文件，否则读取 OxiDNS systemd journal。

若已有外部规则更新定时任务，选择由外部任务或 OxiDNS 原生定时更新负责规则，避免两个任务同时写同一组文件。

## 日常 DNS 监控

左侧导航「数据统计」下方的「OxiDNS 监控」进入监控页：单台 OxiDNS Agent 直接打开，多台时先选择要查看的实例。Agent 卡片中的「DNS 监控」也可直达；原有 CPU、内存等主机监控入口保持不变。需要 ConfigFlow 服务端与 Go Agent 均更新到支持此功能的版本，Agent 至少为 `1.7.0-go`。当前适配 OxiDNS `v1.6.1` full 核心的 HTTP API。

- **运行概览**：DNS 请求、近期请求速率、平均响应时间、失败请求、处理中请求，以及各缓存和 DNS 监听服务的统计。更新至 `1.8.14-go` 后支持下述排行、上游统计与重置。
- **缓存命中率**：`(fresh + stale 命中数) / 缓存查找次数`，不是命中数占全部 DNS 请求的比例；可以按缓存实例查看。旧 Agent 的累计指标从核心或插件启动后计数，重启或重载可能清零。新面板按显示的统计起点计算。请求速率由相邻两次采样计算，首次采样或计数重置时显示 `—`。
- **查询记录**：支持域名、客户端、查询类型、响应码、状态、匹配器标签及时间范围筛选。每页最多 50 条，使用核心游标翻页；不显示核心未提供的总条数。详情包含 DNS 应答、耗时及执行轨迹。
- **解析来源**：列表显示本地 DNS、远程 DNS、备用 DNS、缓存等来源。更新 ConfigFlow 与 Agent 至 `1.8.10-go` 后，Agent 按需订阅核心现有的查询记录推送，在内存中缓存来源摘要并随列表返回；无需修改或升级 OxiDNS 核心。连接持续接收新记录，不按固定时间断开；仅在连接异常、结束或连续 45 秒没有记录及心跳时重连。每个记录器最多缓存 2000 条摘要，同时最多订阅 4 个记录器，多页面共享订阅；最后一次列表请求后约 60 秒释放订阅及缓存。核心实例或配置变化会清理缓存。2000 条只限制来源缓存，不限制历史列表；被淘汰、断线漏收、推送不可用或旧 Agent 缺少摘要的记录仍通过详情补查，每页最多 4 个并发请求，浏览器缓存最近 200 条结果。
- **IPv4 优先（抑制 AAAA）**：解析来源分类 `ipv4_preferred` 表示 IPv4 优先策略抑制了最终的 AAAA 应答。仅在记录包含单个 AAAA 查询、响应码为 `NOERROR`、Answer 数量明确为 0，且执行轨迹中对应 quick `prefer_ipv4` 节点具有匹配的相邻 `entered` → `stop` 摘要时识别；普通空应答、NXDOMAIN 或仅出现插件标签不足以判定。此分类不表示探测期间未访问上游 DNS。列表直接返回这一新分类需要更新 ConfigFlow，并将 Agent 升级至 `1.8.12-go` 或更高版本；`1.8.10-go` 提供前述来源缓存能力，但不包含此分类。无需升级或修改 OxiDNS 核心。
- **刷新**：默认每 10 秒刷新，可暂停。页面隐藏或离开后停止轮询；历史页不会被自动跳回第一页。连接失败时保留上次概览并标明采样时间。

### 排行、上游与统计重置

ConfigFlow 与 Go Agent 均需更新，Agent 至少 `1.8.14-go`；无需修改 OxiDNS 核心。老 Agent 的原有监控和查询历史仍可使用。

- **客户端 / 请求域名排行**：复用 OxiDNS `query_recorder` 的聚合 API，展示前 10 项。每项排行仅覆盖统计起点之后、仍在核心保留期内的最近 10,000 条匹配记录；达到上限会标明，不能视为全部历史累计。多问题 DNS 请求可能贡献多个域名计数。页面关闭不影响核心记录，刷新时重新聚合，不在 Python 中另存一份查询日志。
- **被拦截域名排行**：只在实际配置能证明是 ConfigFlow 标准广告规则匹配后直接进入黑洞应答并结束查询时统计。普通 NXDOMAIN、REFUSED、IPv4 优先抑制的 AAAA 不视为广告拦截；自定义执行链无法确认时显示原因，不凭响应码猜测。此排行的 10,000 条上限针对匹配拦截条件的记录，样本数可以与全部请求排行不同。
- **上游服务器**：按转发组与具体上游分开，显示请求次数、失败次数和平均响应时间。请求次数为上游尝试数，并发竞速时一次 DNS 查询可能产生多次尝试；平均时间是已完成尝试的总耗时除以次数，包含成功及失败 / 超时，取消但未完成的尝试不计入平均。没有完成样本时显示 `—`。地址中的 URL 用户名、密码、查询参数和片段会在展示前隐藏。
- **重置统计**：只为当前 Agent 保存新的面板统计起点和计数基线，所有浏览器共享，ConfigFlow 重启后仍保留。查询历史、DNS 缓存、规则和服务运行状态不变；缓存条目数与处理中请求等实时状态继续显示当前值。平均响应时间和命中率从原始计数差重新计算，不对平均值直接相减。
- 首次启用新版统计时开始一个新统计周期；核心实例、实际配置或注册连接目标变化时自动开始新周期，避免旧配置中的匹配标签影响新排行。检测到单个插件计数下降时也会持久开启新周期；采样缺项会注明统计不完整，不把未知数据填成零。

统计起点使用 Agent 所在主机的采样时间，避免 ConfigFlow 与 DNS 主机时钟不同导致筛选错位。基线单独保存于数据目录的 `dns-monitor/`，只在创建或重置周期时原子更新，不写入配置快照、不生成额外备份。重置需要已登录的管理权限；核心 API 桥仍仅执行读取操作。

### 统计契约的隔离回归

在 `backend/agents/go-agent` 目录运行 `go test ./...`。如有本机可执行的官方 OxiDNS `v1.6.1` full 核心，还可运行以下真实核心回归；它只创建临时目录与回环监听，验证超过一页查询的排行、标准广告拦截、具体上游指标和运行配置身份，不访问已部署的 DNS 服务：

```sh
OXIDNS_TEST_BINARY=/path/to/oxidns go test ./routes -run '^TestOxiDNSNativeDashboardRankings$' -count=1 -v
```

### 启用条件

1. 在 OxiDNS 配置中启用管理 HTTP API，监听本机 IP；`0.0.0.0`/`[::]` 会转换为本机回环地址访问。推荐监听 `127.0.0.1:9199` 并配置 Basic 认证。无需为了 ConfigFlow 监控将管理 API 暴露到公网，Agent 与核心需共享网络空间。
2. 查询历史另外需要 `query_recorder` 插件。进入对应配置空间的「配置生成 → OxiDNS → 设置」，启用查询记录并推送；执行链中的记录器应位于缓存前面，以记录缓存命中的请求。未启用记录器时仍可查看概览，查询页会给出配置提示。
3. Agent 从实际 `config_path` 读取监听地址、认证和插件标签。仅支持直接写在主配置中的设置；不解析外部 `include` 或环境变量占位符。当前监控桥接不支持 TLS/mTLS 管理 API，也不会跳过证书校验。

监控数据经已登录的 ConfigFlow → 已注册 Agent → 本机核心读取。浏览器不会收到核心认证凭据。Agent 接口仅允许固定的健康、指标和查询记录 / 聚合统计读取路径，不提供配置写入、缓存清空或服务重启能力。ConfigFlow 的重置接口只更新本地面板计数基线。查询历史可能包含内网域名和客户端地址，仅向已认证的 ConfigFlow 管理员提供，响应禁止缓存。

查询记录保存在核心自己的 `state/` 中，保留期限由 OxiDNS 设置决定；记录器异步写入，压力过大时可能丢弃记录，不能当作完整审计日志。解析来源根据 ConfigFlow 标准插件标签和完整执行轨迹归纳，不代表转发组内最终获胜的具体 DNS 服务器；没有轨迹显示「未记录」，无法识别的自定义执行链显示「未识别」。缓存来源只识别标准缓存命中轨迹，不能仅凭缓存插件执行结束就判定命中；NXDOMAIN 也不会被当成广告拦截。
