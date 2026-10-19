# Linux 选择性网关隔离实验

这套实验用于验证真实 ConfigFlow API、Shell 安装器、双 Agent、Mihomo、OxiDNS、Linux 转发和失败恢复。**只能在专用、可销毁的 Debian 13 ARM64 / systemd 虚拟机中执行。不要在家庭网关、生产 Linux、开发机或已有服务的虚拟机上运行下列命令。**

本文描述复现步骤和验收条件，不代表某个版本已经通过测试。结论必须来自对应源码版本、二进制摘要和本次运行报告。

## 隔离边界

- QEMU 使用 user networking / SLIRP；如需 SSH，只将端口转发绑定到宿主机 `127.0.0.1`，例如网络参数片段 `-netdev user,id=net0,hostfwd=tcp:127.0.0.1:2222-:22`，另配对应虚拟网卡。宿主机不创建 TAP、bridge，不修改宿主路由、防火墙或 sysctl。
- 使用一次性虚拟磁盘；不挂载宿主生产配置、密钥目录或服务数据。将待测源码复制为客体内的独立副本，不直接运行共享的生产 checkout。
- 虚拟机内部通过 network namespace 隔离网关、客户端、路由器和探针。客体的实验 namespace 内会创建 bridge、veth 和供 `slirp4netns` 使用的 TAP；它们与宿主网络无关。
- `setup` / `cleanup` 在**客体初始 network namespace**运行。服务器和 driver 在 `cfgl-router` 运行；driver 将真实安装命令放入 `cfgl-gateway`。四个实验 systemd 服务也通过专属 drop-in 固定到 `cfgl-gateway`。
- 实验会安装、启动、停止、修改专用的 `mihomo`、`oxidns`、`configflow-agent-mihomo`、`configflow-agent-oxidns` 服务，并执行有意注入的失败。只有这些服务完全属于一次性实验 VM 时才可继续。

脚本执行前检查 Linux、root、hostname、root 私有 marker、当前 machine-id 和虚拟化类型。driver 额外检查 namespace、固定 API 地址、Profile、网络参数和 `/tmp` 工件路径。脚本不会替操作者创建授权 marker，也不接受生产 API 重定向或环境代理。这些检查不能把真实机器变成安全的实验环境。

## 客体准备

本节及后续 Shell 命令均在一次性 VM 的 root 会话中执行。先确认客体系统为 Debian 13 ARM64、PID 1 为 systemd，`systemd-detect-virt --vm` 返回 `qemu` 或相应 QEMU/KVM 标识；内核须提供 network namespace、nftables 和 `/dev/net/tun`。保留 VM 初始默认路由，不为本实验修改它。

安装依赖：

```sh
apt-get update
apt-get install -y python3 python3-venv python3-dev build-essential libyaml-dev \
  golang-go ca-certificates curl iproute2 nftables slirp4netns util-linux \
  tcpdump tar gzip coreutils diffutils procps
```

`iproute2` 提供 `ip` / `ss`，`util-linux` 提供 `nsenter`，`coreutils` 提供 `sha256sum` 等安装器依赖；DNS 证据采集需要 `tcpdump`。所有依赖安装仅限客体。

显式标记这台一次性 VM：

```sh
hostnamectl set-hostname configflow-gateway-lab
umask 077
python3 - <<'PY'
from pathlib import Path
marker = Path('/root/.configflow-disposable-lab')
marker.write_text('CONFIGFLOW_DISPOSABLE_LINUX_VM=1\nMACHINE_ID='
                  + Path('/etc/machine-id').read_text().strip() + '\n')
marker.chmod(0o600)
PY
```

marker 必须是 root 所有的普通文件、权限 `0600`，内容必须与本 VM 的 machine-id 完全匹配。不要复制另一台机器的 marker，也不要修改脚本绕过门禁。

将待测源码复制到 `/opt/configflow-lab/source`。副本应包含本次待测改动，但不包含生产 `.env`、数据库、配置仓库或 Agent 凭据。所有 harness 脚本、backend 与构建的 Agent 必须来自同一份待测源码。

```sh
install -d -m 700 /opt/configflow-lab/artifacts
python3 -m venv /opt/configflow-lab/venv
/opt/configflow-lab/venv/bin/python -m pip install --upgrade pip
/opt/configflow-lab/venv/bin/python -m pip install \
  -r /opt/configflow-lab/source/backend/requirements.txt

cd /opt/configflow-lab/source/backend/agents/go-agent
CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -trimpath \
  -o /opt/configflow-lab/artifacts/configflow-agent-linux-arm64 .

install -d -m 700 /tmp/configflow-gateway-lab-agents
install -m 755 /opt/configflow-lab/artifacts/configflow-agent-linux-arm64 \
  /tmp/configflow-gateway-lab-agents/configflow-agent-linux-arm64
sha256sum /opt/configflow-lab/artifacts/configflow-agent-linux-arm64
```

也可以在宿主使用符合 `backend/agents/go-agent/go.mod` 的 Go 工具链，以上述 `CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -trimpath` 参数交叉编译到专用临时路径，再将 ARM64 二进制复制到上述客体路径；交叉编译不执行实验网络脚本。必须保留构建源码版本和摘要。服务器提供下载的是 `/tmp` 中的**测试副本**，driver 会暂时破坏这个副本来验证真实 SHA-256 失败；不能将它指向正式发布产物。

若当前 Debian/Python 与锁定依赖不兼容，先解决并记录具体工具链或依赖变化，再开始实验；不得把安装失败当作网关功能测试结果。可记录环境信息及实际依赖：

```sh
uname -a
cat /etc/os-release
nft --version
go version
/opt/configflow-lab/venv/bin/python --version
/opt/configflow-lab/venv/bin/python -m pip freeze
```

## 建立拓扑并启动真实后端

在客体初始 namespace 中执行：

```sh
sh /opt/configflow-lab/source/scripts/tests/linux_gateway_lab_network.sh setup
```

`setup` 拒绝接管已有同名 namespace、实验状态或正在运行的核心服务。它在 `/var/lib/configflow-gateway-lab` 记录所有权、namespace 身份、drop-in 摘要与网络快照，并比较客体初始 namespace 在 setup 前后的网络状态；同时将客体原始 `/etc/nsswitch.conf` 的 SHA-256 保存为 `root-nsswitch.sha256`，用于 cleanup 时核验。

每个实验 namespace 都有独立的 `/etc/netns/<namespace>/resolv.conf` 和 `nsswitch.conf`。前者使用 SLIRP DNS `10.0.2.3`，后者复制客体 NSS 配置，仅将 `hosts:` 设置为 `hosts: files dns`，保留其他数据库项。`ip netns exec` 将这些文件映射到进程看到的 `/etc`。这样实验进程通过自己的 DNS 配置解析名称，避免 `libnss_resolve` 经客体共享的 AF_UNIX socket 请求 systemd-resolved，绕过 namespace 隔离。

systemd 服务不会通过 `ip netns exec` 启动，因此四个服务的实验 drop-in 同时设置网络 namespace 和两个只读文件映射：

```ini
[Service]
NetworkNamespacePath=/run/netns/cfgl-gateway
BindReadOnlyPaths=/etc/netns/cfgl-gateway/resolv.conf:/etc/resolv.conf
BindReadOnlyPaths=/etc/netns/cfgl-gateway/nsswitch.conf:/etc/nsswitch.conf
```

这些映射只在实验服务的运行环境中生效，不改写客体原始 `/etc/nsswitch.conf`。不要通过修改客体全局 NSS 或 systemd-resolved 设置来排除实验 DNS 故障。

| 角色 | 地址 / 用途 |
| --- | --- |
| `cfgl-router` | `192.0.2.1:8000`，测试 ConfigFlow API；通过 `slirp4netns` 出网 |
| `cfgl-gateway` | `lan0 = 192.0.2.2/24`，上游 `192.0.2.1`；运行两核心与两 Agent |
| `cfgl-client` | `192.0.2.100`，默认网关为 `192.0.2.2`；DNS 探针显式向该网关的端口 `53` 发起请求 |
| `cfgl-switch` | 仅实验 namespace 内的二层连接 |
| `cfgl-origin` | `203.0.113.2:1080` SOCKS5；`.10` / `.20` / `.30:18080` HTTP 探针 |

`.10` 是直连目标，`.20` 是所选 IP 规则目标；`proxy.lab.invalid` 经 fake-IP 解析后代理访问 `.30`。实验保留 `https://223.5.5.5/dns-query` 作为真实直连 DoH 上游，用 `example.com` 检查 OxiDNS 的 UDP/TCP DNS 回答。

fixture 将核心下载所需的 `github.com`、`release-assets.githubusercontent.com` 和 `objects.githubusercontent.com` 设为真实 IP / DIRECT，保留下载与诊断所需的直连路径。实验 SOCKS 只接受隔离目标；这些域名例外不会扩大所选 IP 接管范围。当前安装器只在核心二进制缺失、且安装状态允许补装时下载对应固定版本核心；已有完整标准布局核心使用本地二进制和配置校验，不重复从 GitHub 下载核心。Agent 仍从实验 ConfigFlow 下载并校验 SHA-256。DNS 转交证据使用随机代理子域名，防止缓存命中掩盖实际链路。

在独立客体终端前台启动服务器，保持该终端运行：

```sh
ip netns exec cfgl-router /opt/configflow-lab/venv/bin/python \
  /opt/configflow-lab/source/scripts/tests/linux_gateway_lab_server.py \
  --host 192.0.2.1 --port 8000 \
  --workspace /tmp/configflow-gateway-lab-server \
  --agent-binaries-dir /tmp/configflow-gateway-lab-agents
```

等待输出 `"ready": true`。测试服务器使用真实 Flask 应用及鉴权 API，在独立临时配置仓库中建立 `gateway-lab` Profile 和规则；不使用生产账户、配置仓库或后台 scheduler。fixture 初始不启用网关，由 driver 正式保存配置。

`/tmp/configflow-gateway-lab-server/manifest.json` 提供固定 API 地址、规则 ID、拓扑、测试凭据和 controller token 文件位置。目录权限为 `0700`，凭据文件为 `0600`。不要打印或分享 manifest 的原文。`--check-only` 仅做只读 fixture / onboarding 冒烟检查，不能代替下面的 Linux 安装与真实转发实验。

## 主流程与发布失败恢复

在另一个客体 root 终端运行主 driver：

```sh
ip netns exec cfgl-router /opt/configflow-lab/venv/bin/python \
  /opt/configflow-lab/source/scripts/tests/linux_gateway_lab_driver.py \
  --manifest /tmp/configflow-gateway-lab-server/manifest.json
```

默认每次创建独立的 `/tmp/configflow-gateway-lab-run-*` 输出目录。可用 `--output /tmp/configflow-gateway-lab-<新名称>` 指定一个**尚不存在**的目录；`--timeout` 默认 `1800`，表示每次安装子进程的最长秒数。不要并行执行多个 driver。

主 driver 依次验证：

1. 真实 onboarding 预览、保存、创建安装会话；重复创建请求应返回同一会话。
2. 暂时破坏测试 Agent 下载文件，执行真实安装命令并观察 checksum 失败；恢复文件后，用相同会话和命令重试。失败时核心文件和网络不得改变。
3. 完成全新安装、双 Agent 精确登记；核验磁盘中的 bootstrap 配置 `tun.enable=false`，且安装阶段没有 ConfigFlow 网关表或 journal。
4. 执行普通 AIO 安装选项，核验核心二进制、配置、unit 的内容与元数据、Agent ID 和网络状态保持不变。
5. 通过实际发布事务依次发布 Mihomo、确认健康，再发布 OxiDNS、确认健康。
6. 调用 gateway status / preview，确认只读；正式 apply 后执行直连、所选真实 IP、fake-IP 和 DNS 探针；调用 remove，比较应用前后的完整网络快照，并运行带 baseline 的撤销探针。
7. 在实验内移走安装器 ownership marker 来模拟已有标准核心，重命名安装会话后接入；marker 通过 `shutil.move` 移至报告目录，以兼容 `/tmp` 为独立 tmpfs。安装器使用完整的本地核心，发布前检查核心文件内容和元数据、原 Agent ID 均保留，再重复发布和转发验证。

主 driver 完整结束且已撤销 ConfigFlow 转发、两核心仍健康后，再运行独立的发布失败恢复用例：

```sh
ip netns exec cfgl-router /opt/configflow-lab/venv/bin/python \
  /opt/configflow-lab/source/scripts/tests/linux_gateway_lab_publish_retry.py \
  --manifest /tmp/configflow-gateway-lab-server/manifest.json
```

该用例用一次性 systemd `ExecStartPre` / `Restart=no` drop-in 让 OxiDNS 候选配置的启动真实失败，检查 Agent 回滚旧配置及健康恢复。清除失败条件后，在同一会话调用 `retry_failed=true`；检查只重试 OxiDNS，已成功的 Mihomo deployment ID、PID 和配置元数据不变。drop-in 的清理放在 `finally` 中；若进程被强杀或 VM 断电，仍须保留现场并检查残留。该用例不调用网关 apply / remove。

## 证据与验收

不要只看 Shell 退出码或页面“发布成功”。主 driver 和发布重试各自产生一份 `report.json`；只在其顶层 `success=true`、预期 case 都有对应证据且 cleanup 验证通过时，才能为这次环境与源码给出通过结论。本版 setup 创建的完整实验还应在 cleanup 返回中看到 `initial_namespace_unchanged=true` 和 `initial_nsswitch_unchanged=true`。后者为 `null` 表示旧实验没有保存原始 NSS 摘要，不能据此证明 NSS 文件未变。

必须区分三种时间点：

| 快照 | 应检查的边界 |
| --- | --- |
| 安装前 | 初始核心与 gateway namespace 网络基线 |
| 仅安装、尚未发布 | TUN 关闭；无 ConfigFlow 网关 journal / 自有转发表；与安装前网络一致 |
| 发布后、尚未 apply | 允许出现 Mihomo 自身 TUN 路由 / nft 规则或 sysctl 变化；仍无 ConfigFlow 自有转发表 / journal |

remove 的比较基准是**发布完成、apply 之前**的快照。不能把 Mihomo 自身的网络行为算成 ConfigFlow 转发已启用，也不能强制把发布前原本为 `1` 的 `ip_forward` 改为 `0`。报告记录 `pre_apply_ip_forward`，撤销探针按该 baseline 判断。

探针同时检查客户端请求、HTTP 目标收到的源地址、Mihomo controller 连接及 SOCKS 服务记录：直连应无 Mihomo / SOCKS 连接，所选 IP 与 fake-IP 应都有对应证据，并验证预期 SNAT 源地址。DNS 证据包含 `example.com` 的 UDP/TCP 真实地址回复、OxiDNS 到本机 `1053` 的请求/回复、Mihomo 端口 53 DNAT 计数未增长及 PCAP。缓存可能复用已有 DoH 结果，因此不要求每次重复探针都建立新的 `223.5.5.5:443` 连接。

辅助快照和探针命令如下；标签使用字母、数字、连字符或下划线：

```sh
sh /opt/configflow-lab/source/scripts/tests/linux_gateway_lab_network.sh snapshot manual-before
sh /opt/configflow-lab/source/scripts/tests/linux_gateway_lab_network.sh snapshot manual-after
sh /opt/configflow-lab/source/scripts/tests/linux_gateway_lab_network.sh compare manual-before manual-after both

sh /opt/configflow-lab/source/scripts/tests/linux_gateway_lab_network.sh probe active \
  --controller-token-file /tmp/configflow-gateway-lab-server/controller-token
sh /opt/configflow-lab/source/scripts/tests/linux_gateway_lab_network.sh probe removed \
  --baseline ACTUAL_PRE_APPLY_LABEL
```

`ACTUAL_PRE_APPLY_LABEL` 必须替换为本次 driver 保存的真实标签，不能使用安装前快照。`active` 仅在已应用转发时执行，`removed` 仅在正式 remove 后执行。上述额外探针用于排查，不替代 driver 的前后比较。

## 清理与报告导出

失败时先保留报告、服务日志、部署 journal 和网络快照。不要用 `nft flush ruleset`、手动覆盖 sysctl 或直接删除 journal 来“修好”测试；这会销毁归属和恢复证据。若已 apply 而用例中途退出，先用实验 API 的正常 remove 流程验证恢复，或保留现场供排查。

停止 driver / 重试用例和前台测试服务器后，在**客体初始 namespace**执行：

```sh
sh /opt/configflow-lab/source/scripts/tests/linux_gateway_lab_network.sh cleanup
```

cleanup 校验 namespace 与 drop-in 所有权，停止并禁用四个实验服务，移除它创建的网络资源、drop-in 及 `/etc/netns/<namespace>/` 下的 `resolv.conf` / `nsswitch.conf`。随后比较客体初始 namespace 的 setup 前 / cleanup 后状态，并用 `root-nsswitch.sha256` 核验客体原始 `/etc/nsswitch.conf`，将结果写入 `root-nsswitch-after-cleanup.log`。摘要不一致会直接失败，须保留现场排查；不能覆盖原始摘要使其通过。

核心文件和证据保留以便检查，实验状态标记为已清理。cleanup 不会把客体恢复成适合再次“全新安装”的空环境；下一轮请使用新的 VM 或干净快照，不通过删除 guard / ownership 文件绕过检查。

需要保留和审查的证据位于：

- `/tmp/configflow-gateway-lab-run-*/report.json`、HTTP JSON、安装日志、核心元数据、探针结果。
- `/var/lib/configflow-gateway-lab/snapshots/`、`root-before.json`、`root-after-cleanup.json`、`root-nsswitch.sha256`、`root-nsswitch-after-cleanup.log`、fixture 日志和 DNS PCAP。
- 本次源码版本 / patch、Agent SHA-256、系统与依赖版本；另记录实际下载的核心版本。

driver 对 JSON 中常见 token、密码、Authorization 和安装命令字段进行脱敏，但**不能直接打包整个 `/tmp` 或实验目录对外分享**。原始 manifest、token 文件、配置仓库、安装脚本、服务日志、URL 路径中的安装 token、controller secret 和 PCAP 可能仍含凭据或敏感内容。导出时只复制所需证据，人工检查并替换秘密；私有原始证据保持 `0700` / `0600`，审核后销毁整个一次性 VM。

## 覆盖范围限制

这里限定 Debian 13、ARM64、systemd、IPv4、固定 namespace 拓扑和 nftables。它不证明 amd64、Alpine/OpenRC、实体路由器、复杂已有防火墙、IPv6、真实上游代理供应商或其他安装目录均可用。已有标准核心用例从本实验已安装核心构造，不能覆盖所有第三方部署方式。

后端和 Agent 使用真实请求、安装器和系统命令；SOCKS/HTTP 目标是隔离探针服务。核心下载及真实 DoH 需要外网可达，下载源或 DNS 故障应与产品逻辑失败分开记录。本套 API driver 不点击前端向导，前端交互与窄屏视觉验证应另行执行。

## 通过浏览器操作真实安装向导

本节用于单独验证前端操作路径，描述准备和验收方法，不代表浏览器测试已经通过。使用新的实验 VM 或干净快照，按前文启动拓扑、探针和测试后端，保留 fixture 初始的普通模式。**本轮由浏览器预览、保存配置并创建安装会话，不先运行主 driver，也不使用 driver 的准备、发布或应用接口代替页面操作。** 可以继续用只读快照和网络探针核验页面操作的实际结果。

浏览器运行在开发机，测试后端仍位于 VM 的 `cfgl-router`，地址为 `192.0.2.1:8000`。先在 VM 初始 namespace 准备一个仅监听 `127.0.0.1:18000` 的临时 TCP relay；每条连接通过 `ip netns exec cfgl-router socat STDIO TCP:192.0.2.1:8000` 转入后端，客体需额外安装 `socat`。本次实验可将 relay 进程交给单独的 `cfgl-browser-relay` transient systemd unit 管理。不要让 relay 绑定 `0.0.0.0`，也不要给开发机添加实验网段路由。

在开发机使用本次专用 VM 的 SSH 配置启动本地转发，替换下例 SSH 配置路径并保持进程运行：

```sh
ssh -F /path/to/this-lab/ssh_config -N \
  -L 127.0.0.1:18000:127.0.0.1:18000 gateway-lab
```

从仓库根目录启动独立前端入口，前端依赖应已安装：

```sh
node scripts/tests/linux_gateway_lab_frontend.mjs --api-port 18000 --port 13001
```

[前端启动器](../../scripts/tests/linux_gateway_lab_frontend.mjs) 默认先把产品 `src`、静态资源、入口 HTML、Vite 配置与包信息复制到本次 `/tmp` 工作目录，使用这份固定源码快照；仅 `node_modules` 通过符号链接复用已有依赖。Vite root、配置文件和 `@` 别名均指向快照，并设置 `hmr: false`、`watch: null`。测试文件、报告、生产 `.env` 不进入快照，共享 checkout 的后续编辑不会更新当前页面，也不会触发向导整页刷新。不要在运行期间升级共享依赖。

启动器仅在当前进程中覆盖本地监听和 API 代理，不修改产品 `frontend/vite.config.mts`，不加载开发者 `.env`。它只绑定 `127.0.0.1:13001`，将所有同源 `/api` 请求转发到 `http://127.0.0.1:18000`。端口被占用时直接失败，不会自动换端口。需要其他端口时同时调整 SSH 转发和 `--api-port`；`--port` 决定浏览器入口，二者必须不同。

启动输出包含 `login_url`、PID、快照 SHA-256 以及 `/tmp/configflow-gateway-lab-frontend-*/manifest.json` 路径（macOS 会规范化为 `/private/tmp/...`）。该目录为 `0700`，manifest 为 `0600`，记录测试代理目标、原始源码目录、快照目录与摘要、启动时间和关闭状态；同目录 `source-manifest.json` 记录每个快照文件的相对路径、字节数和 SHA-256，Vite 缓存也放在这里。浏览器打开本次输出的登录地址，默认是 `http://127.0.0.1:13001/login`。后端尚未连通时页面可以加载，但接口请求会失败，此时不能判定登录或向导功能异常。准备期间的版本请求、代理连接或 Axios 错误单独记录，正式验收检查后端 ready 之后的日志。

需要验证新的产品改动时，先保存本轮阶段与证据，明确结束当前前端进程，再重新执行启动命令生成新快照，最后手动刷新浏览器。仅刷新页面仍会使用旧快照；不能把旧版本的操作结果归入新摘要。保持相同前端端口和 API 端口可保留浏览器同源登录状态，已保存的安装会话由后端恢复；未保存的表单需要重新填写。原快照和摘要保留供核对，不自动同步或覆盖。

共享 checkout 有并发改动、只需核验一个获授权文件时，可在停止旧进程后从其工作目录派生快照，只刷新明确列出的文件。例如：

```sh
node scripts/tests/linux_gateway_lab_frontend.mjs --api-port 18000 --port 13001 \
  --snapshot-from /private/tmp/configflow-gateway-lab-frontend-PREVIOUS \
  --refresh-file src/components/generate/LinuxGatewaySettings.vue
```

`--snapshot-from` 指向含 `manifest.json` 的旧工作目录，不是 `frontend` 子目录。启动器先核验复制结果与旧摘要一致，再从当前 checkout 复制指定的 `src/` 文件；可重复 `--refresh-file`，但不接受目录或父路径跳转。新 manifest 记录 `parent_snapshot_sha256` 和 `refreshed_files`。核验新旧 `source-manifest.json` 的差异确实只有授权文件后，再手动刷新浏览器；其余源码继续使用旧快照。

使用 **VM 测试后端 manifest 中的 `username` / `password`** 正常填写登录表单。凭据只存在于该临时 fixture，不使用开发机或生产账户，不在报告中打印 manifest 原文、密码或 token，也不要用预写 localStorage token 跳过登录验证。登录后通过界面的配置空间选择器切换到 **Disposable gateway laboratory**（ID `gateway-lab`）；新浏览器标签的初始空间可能是 `default`。

通过 **Agent → 添加 Agent** 检查普通安装默认选中、原有 Shell / Docker 设置仍可使用，再明确选择 Linux 选择性网关，按页面引导填写并继续。实验设备参数为 `lan0`、`192.0.2.2`、`192.0.2.0/24` 和上游 `192.0.2.1`；IP 规则候选为 `LAB selected IPv4`，域名规则候选为 `LAB direct domains` / `LAB proxy domains`。向导中的 ConfigFlow 服务地址应从后端设置填入 **`http://192.0.2.1:8000`**，供 VM 中的安装器、核心和 Agent 使用；不能替换为浏览器的 `127.0.0.1:13001` 或 SSH 转发端口。

安装命令必须来自本轮浏览器创建的会话。将页面命令安全保存为 VM 内权限 `0600` 的临时脚本，在 `cfgl-gateway` 中执行；它会操作真实核心和 Agent。回到同一个浏览器向导继续登记检查、顺序发布、只读转发预览、显式应用、撤销和后续管理。记录普通模式、校验错误、规则选择、刷新恢复、失败重试等实际走过的路径；未通过浏览器操作的路径不能用单元测试或 API driver 的通过结果代替。

归档截图限于阶段状态、规则选择、网络预览、应用与撤销结果、普通模式等不含秘密的界面。**截图不得包含一键安装命令、安装 token、登录凭据或 controller secret。** 截图前收起或避开命令区域，不把剪贴板内容、完整请求 URL 或带 token 的浏览器地址一并保存。

结束时先通过页面撤销已应用的 ConfigFlow 转发并核验快照，再退出测试账户。停止前端可在启动终端按 Ctrl-C，或向**本次输出记录的 PID**发送 `SIGTERM`；启动器关闭监听、删除自己的 Vite 缓存，并在 manifest 写入 `status: stopped`、关闭时间与原因。保留这份 PID 记录作为清理证据。随后停止本次 SSH 转发、VM 的 `cfgl-browser-relay` 和测试后端，再按前文运行网络 cleanup、导出脱敏证据并销毁一次性 VM。不要停止其他开发服务或删除其他任务的临时目录。
