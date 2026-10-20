# ConfigFlow + RouterOS 安装指南

本指南使用 NAS 运行 ConfigFlow，在 RouterOS 上运行 Mihomo 和 OxiDNS。日常配置、设备分流和路由设置都在 ConfigFlow 页面中完成，核对后直接下发到设备。

安装完成后，客户端仍以 RouterOS 为网关，由 OxiDNS 处理 DNS，需要代理的流量交给 Mihomo。

**安装顺序：部署管理端 → 安装服务 → 发布服务配置 → 下发路由 → 单设备测试 → 全网启用。**

正文按页面操作介绍。容器安装命令和手动路由配置放在文末补充，需要时再查阅；已经下发成功的配置不要重复手动添加。

## 开始前

- NAS 已安装 Docker Compose；RouterOS 7 已安装 Container 软件包、启用容器功能，并有可写磁盘。
- 准备可用的代理订阅，以及可供 ConfigFlow 连接的 RouterOS 管理账号。
- 保存 RouterOS 配置备份，记录原来的 DHCP DNS。已有服务时，先保留原服务。
- 管理页面和 Agent 端口仅对可信内网开放，不直接映射到公网。

下面的 IP 都是**示例**，请按自己的网段替换，避开 DHCP 自动分配范围，并确认没有地址冲突：

| 设备 / 服务 | 示例地址 | 用途 |
|---|---|---|
| RouterOS | `192.168.50.254` | 网关、DHCP |
| NAS | `192.168.50.100:5010` | ConfigFlow 管理页面 |
| Mihomo | `192.168.50.28` | 代理服务；DNS 端口 `1053` |
| OxiDNS | `192.168.50.251` | 客户端 DNS；端口 `53` |
| 测试设备 | `192.168.50.50` | 首次验证，建议固定地址 |

本文以单个 `192.168.50.0/24` 内网为例。RouterOS 的 LAN 网桥使用 `bridge1`、存储目录使用 `disk1/configflow`，都需替换成实际名称。安装期间先不要修改全网 DNS。

## 1. 在 NAS 安装 ConfigFlow

新建一个 `configflow` 文件夹，在其中保存 `compose.yaml`。镜像地址无需修改，只需填写管理员用户名、随机密码和随机密钥：

```yaml
services:
  config-flow:
    image: thsrite/config-flow:latest
    ports:
      - "5010:80"
    volumes:
      - ./data:/data
    environment:
      DATA_DIR: /data
      ADMIN_USERNAME: 'replace-with-your-admin-user'
      ADMIN_PASSWORD: 'replace-with-a-random-password'
      JWT_SECRET_KEY: 'replace-with-at-least-32-random-chars'
    restart: unless-stopped
```

在该文件夹中执行：

```sh
docker compose config --quiet
docker compose pull
docker compose up -d
```

打开 `http://192.168.50.100:5010`，使用刚设置的账号登录。`data` 文件夹需要长期保留，Sub-Store 已内置，无需另装。

先在「系统 → 系统设置」将「服务域名」设为 `http://192.168.50.100:5010`，生成并保存「访问令牌」，确保容器能取得配置和规则。令牌同时授权 MCP，含令牌的配置链接不要公开分享。

## 2. 安装 Mihomo 和 OxiDNS 服务

在 RouterOS 上安装 Mihomo 和 OxiDNS 两个容器。服务镜像已包含 Agent，无需单独安装。

按文末 [补充一：容器安装](#container-install) 完成安装，再回到这里继续。可以使用 WinBox，也可以使用命令行；任选一种，不要重复创建。路由和设备分流在后续步骤通过 ConfigFlow 下发，不在此时手动添加。

回到 ConfigFlow 的 **系统 → Agent**，应看到 `mihomo-main`、`oxidns` 两条在线记录。Agent 自动注册，无需手工填写 Token。

> Agent 管理端口是 `8080 / 8082`，不是 DNS 端口。RouterOS 容器使用各自的 IP，不需要 Docker 端口映射。

## 3. 发布服务配置

### Mihomo：设置代理配置

1. 在「共享资源 → 订阅来源」添加订阅并手动更新一次，或在「共享资源 → 节点库」导入节点。
2. 在「系统 → 配置空间」新建并选择空间，在「策略组」选择资源，在「策略规则」设置去向；新增规则在顶部，调整后保存并将 `MATCH` 放最后。
3. 在「系统 → Agent」中，将 Mihomo Agent 绑定到该配置空间。
4. 检查以下设置并保存。

| 设置 | 值 |
|---|---|
| DNS 启用 | 是 |
| DNS 监听 | `0.0.0.0:1053` |
| DNS 模式 | `fake-ip` |
| fake-IP 网段 | `100.127.0.0/17` |
| Mixed Port | `7890` |
| TUN、自动路由 | 启用 |

fake-IP 网段不能与已有 LAN、VPN 网络重叠。

### OxiDNS：统一处理客户端 DNS

将 OxiDNS Agent 绑定到**同一配置空间**，在 OxiDNS 配置页面设置：

| 设置 | 值 |
|---|---|
| UDP / TCP 监听 | 均为 `0.0.0.0:53` |
| 直连上游 | 可用的公共 DNS，例如 `https://223.5.5.5/dns-query` |
| 代理上游 | `192.168.50.28:1053` |
| 设备策略 | 先将测试设备 `192.168.50.50` 设为「智能分流」 |

使用页面生成配置，无需手写配置文件。代理域名交给 Mihomo 处理，直连域名使用公共 DNS。

分别为 Mihomo 和 OxiDNS Agent 推送配置；若显示等待激活，点击 **应用并启动**。等发布成功、服务状态正常后，再继续。只保存草稿不会使设备上的配置生效。

## 4. 接入 RouterOS 并下发路由

### 添加路由器

先确认 ConfigFlow 能通过内网访问 RouterOS 管理页面，并按 [补充二：管理账号与连接](#router-access) 准备专用账号。

切换到**绑定这两个 Agent 的配置空间**，打开 **上游路由 → 添加路由器**：

| 字段 | 填写内容 |
|---|---|
| 名称 | 自定，例如「主路由」 |
| 类型 | RouterOS |
| 管理地址 | RouterOS 的管理地址，例如 `https://192.168.50.254`；端口非默认时一并填写 |
| 用户名、密码 | 为 ConfigFlow 准备的 RouterOS 账号 |
| 校验 HTTPS 证书 | 建议开启，并使用有效证书 |
| 代理网关（mihomo Agent） | `mihomo-main` |
| 自动配置回流 | 本文同网段部署开启 |
| 使用静态路由的 IP 规则集 | 留空，fake-IP 网段会自动包含 |
| 使用策略路由的 IP 规则集 | 按需选择 Telegram、Twitter、Google 等所用的 **IP 规则集** |
| DHCP DNS 服务器（OxiDNS Agent） | **暂选「不关联」**，单设备测试通过后再设置 |

保存后点击 **探测**，确认显示「可连接」。保存和探测都不会下发路由。

### 设置需要分流的设备

点击这台路由器的 **策略路由**，在 **来源设备** 中确认测试设备 `192.168.50.50` 已设为「智能分流」。这里与当前配置空间的 OxiDNS 设备策略共用设置。

- 需要代理分流的设备选择「智能分流」。
- 不需要这项分流的设备可设为「直连」。
- 初次安装只添加测试设备，不要直接填整个内网。

在 **规则集与网关** 中确认代理网关为 `mihomo-main`，将 Telegram、Twitter、Google 等需要按目标 IP 分流的规则集放入 **使用策略路由的 IP 规则集**；**使用静态路由的 IP 规则集** 保持留空。

这里的来源分流仅创建 IPv4 策略；更具体的直连来源会作为例外，Mihomo 网关本身会排除。IPv6 设备策略仍属于 OxiDNS，不会因此创建 RouterOS IPv6 策略路由。直连来源仍遵循 RouterOS 主路由表，不能用它绕过已有手动静态路由。

这样，fake-IP 路由由 ConfigFlow 自动添加；所选 IP 规则集只对列为「智能分流」的设备生效，无需自己创建策略路由和路由表。域名规则继续在 Mihomo、OxiDNS 中使用，不用放进这里。

### 预览并确认发布

1. 在 **发布预览** 中选中本次使用的 OxiDNS Agent。
2. 点击 **保存并预览**。
3. 核对来源设备、所选规则集、Mihomo 网关；确认本次 **DHCP DNS** 为跳过，不会提前影响全网。
4. 没有阻止发布的提示后，点击 **确认发布**。
5. 等待 OxiDNS 和 RouterOS 都显示发布成功。

若页面提示已有分流规则冲突或 FastTrack 影响分流，先按提示核对，不要直接删除原来的防火墙配置。发布失败或结果待确认时，先查看结果，不要反复提交。

发布后，可在 **已下发对象** 中查看策略配置，在 **静态路由** 中检查 fake-IP 路由。显示同步成功后，仍需完成下面的上网测试。

## 5. 先测试一台设备

测试设备的网关仍使用 RouterOS，只把 DNS 改为 `192.168.50.251`。关闭该设备上的其他代理或 VPN，以及浏览器的安全 DNS，然后检查：

- 直连网站和代理网站都能正常打开。
- 选用了 Telegram 等 IP 规则集时，对应应用也能正常连接。
- OxiDNS 查询记录中能看到测试设备自己的 IP。
- Mihomo 中能看到代理连接。

如有已设为「直连」并发布的设备，也检查其普通上网，确认没有被所选 IP 规则集带入代理。设备自己的代理、VPN，以及已有手动路由仍可能影响结果。

<details>
<summary>可选：用命令检查 DNS 和代理连接</summary>

在测试设备上执行（需安装 `dig` 和 `curl`）。示例域名应分别命中你的代理规则和直连规则：

```sh
dig @192.168.50.251 www.google.com A +short
dig +tcp @192.168.50.251 www.google.com A +short
dig @192.168.50.251 www.baidu.com A +short
dig +tcp @192.168.50.251 www.baidu.com A +short
dig @192.168.50.28 -p 1053 www.google.com A +short
```

智能分流设备查询代理域名时应得到 `100.127.0.0/17` 内的 fake-IP，直连域名应得到真实 IP。直接查询 Mihomo 时，代理域名也应得到该网段内的地址。

将下面变量替换为实际返回的 fake-IP：

```sh
FAKE_IP='replace-with-returned-fake-ip'
curl --noproxy '*' --resolve "www.google.com:443:${FAKE_IP}" \
  --max-time 15 -sS -o /dev/null -w '%{http_code}\n' \
  https://www.google.com/generate_204
```

预期返回 `204`，不要跳过证书检查。

</details>

## 6. 全网启用

单设备测试通过后，在**同一配置空间**完成以下设置：

1. 编辑 RouterOS 接入设置，将 **DHCP DNS 服务器（OxiDNS Agent）** 选为 `oxidns`，保存；此时尚不会修改全网 DNS。
2. 打开 **策略路由 → 来源设备**，添加其他需要使用的设备或网段，选择「智能分流」或「直连」。需要长期按设备分流时，建议在 DHCP 中为这些设备固定 IP。
3. 在 **发布预览** 中选中 OxiDNS 发布目标，点击 **保存并预览**。
4. 确认 DHCP 网段是预期的 `192.168.50.0/24`、DNS 将设为 `192.168.50.251`，并核对设备范围和其他变更。
5. 点击 **确认发布**，等待所有目标显示成功。DHCP DNS 只更新包含所选 Mihomo 网关的唯一已启用网段；找不到或存在多个候选时会阻止，不会新建 DHCP 网段。写入会替换原 DNS 列表，不保留旧备用地址。
6. 让客户端断开重连或续租，检查实际获得的 DNS，再次测试直连网站、代理网站和相关应用。

**只调整 DNS，客户端网关始终保留 RouterOS 地址。** 手动指定 DNS 的设备需单独修改。若存在自定义 DHCP DNS 选项，先按页面提示处理；多个内网网段不要直接套用本文示例。

确认上网正常后，在 WinBox 的 **Container** 中为 Mihomo 和 OxiDNS 两个容器开启 **Start On Boot**。普通安装到这里即可；有些设备会忽略 DHCP 下发的 DNS，再按需设置下面的可选功能。

<a id="dns-guard"></a>

### 可选：强制局域网 DNS 走 OxiDNS

如果设备自己指定了 `8.8.8.8` 等外部 DNS，可让 RouterOS 把它经过路由器的普通 DNS 查询转给 OxiDNS。**该功能默认关闭，仅支持 RouterOS。** 请先完成单设备测试，再开启：

1. 编辑 RouterOS 接入设置，先选择 **DHCP DNS 服务器（OxiDNS Agent）**。
2. 开启 **强制局域网 DNS 走 OxiDNS**，按下表选择附加选项。
3. 保存后进入「发布预览」，点击 **保存并预览**，核对入口网段、OxiDNS 地址、放行设备和将增删的规则。
4. 点击 **确认发布**，等待成功。只保存不会下发规则；测试设备手动指定外部 DNS 后，再检查查询是否到达 OxiDNS。

| 选项 | 默认值 | 怎么选 |
| --- | --- | --- |
| 强制局域网 DNS 走 OxiDNS | 关闭 | 开启后转发对应 LAN 入口的 IPv4 UDP / TCP 53 查询；目的地为路由器自身的查询不在此范围 |
| 拦截加密 DNS（853 端口） | 关闭 | 按需拦截 TCP / UDP 853；不会把加密内容转换成普通 DNS |
| OxiDNS 故障时自动暂停 | 开启 | 每 10 秒检查 OxiDNS 的 TCP 53 能否连接，失败时暂停 DNS 转发规则，恢复后重新启用 |
| 放行设备 | 空 | 每行填写一个 IPv4 或网段，最多 64 项；OxiDNS 和已选 Mihomo 网关自动放行 |

注意以下范围和限制：

- **只作用于识别到的一个 LAN 入口。** 系统根据通往 OxiDNS 的主路由表直连路由确定网段和接口；多网段 / VLAN 不会自动全部覆盖，无法唯一识别时会阻止发布。
- **不会拦截 IPv6 或 HTTPS 加密 DNS（DoH，通常为 443 端口）。** 严格指定主机名的 Android「私人 DNS」在拦截 853 后可能直接失败，需要改为自动或关闭，再测试。
- **“自动暂停”不是备用 DNS。** 它只检查 TCP 端口连通性，不验证 DNS 回答；暂停时仅关闭 53 转发规则，不恢复 DHCP 原值、不更换客户端 DNS，也不解除可选的 853 拦截。仍直接使用故障 OxiDNS 的设备需要另行恢复 DNS。
- **被转发的查询可能显示为路由器 IP。** 为保证同网段回包经过路由器，会同时设置源地址转换；OxiDNS 按设备分流时需核对实际查询来源。正常直接访问同网段 OxiDNS 的设备不经过这条转发链。
- **还要核对已有的来源策略路由。** 若手动指定的 DNS 地址也落在已选代理 IP 规则集内，这类查询可能沿策略送往 Mihomo，不能仅凭开启此开关就认定已由 OxiDNS 回答。进阶说明见 [路由设计中的已知限制](ROUTER_INTEGRATION_DESIGN.md#46-强制-dnsrouteros)；RouterOS 的处理顺序见 [官方 Packet Flow](https://help.mikrotik.com/docs/spaces/ROS/pages/328227/Packet+Flow+in+RouterOS)。

关闭功能后，仍需保存、预览并确认发布，才会移除它管理的规则。系统不自动删除已有的手工 DNS 规则；受管规则被外部改动时会阻止覆盖，先按提示核对。

<a id="restore-dns"></a>

#### 停用强制 DNS 并恢复原来的 DNS

只想停止强制转发、继续通过 DHCP 使用 OxiDNS 时，关闭强制 DNS 开关，保持 DHCP DNS 关联，正常预览并确认发布即可。

需要恢复安装前的 DNS，或 OxiDNS 已离线时，按下面顺序操作。**只改 DHCP DNS，仍可能被尚未移除的强制 DNS 规则转回 OxiDNS。**

1. 编辑 RouterOS 接入设置，将 **DHCP DNS 服务器（OxiDNS Agent）** 改为「不关联」，确认 **强制局域网 DNS 走 OxiDNS** 已关闭，保存。取消关联不会自动恢复路由器上的旧 DNS 值。
2. 打开「策略路由 → 发布预览」，取消勾选全部 OxiDNS 发布目标，再点「保存并预览」。确认提示为「本次只发布 ROS」，且变更中包含撤销强制 DNS，然后点击「确认发布」并等待成功。若已无受管强制 DNS 对象、预览也无变更，则无需重复发布。OxiDNS Agent 离线时，保留 DHCP DNS 关联或仍勾选该发布目标都会阻止发布。
3. 按 [补充三：手动设置或恢复 DHCP DNS](#手动设置或恢复-dhcp-dns) 将 DNS 改回安装前记录的值；手动指定 DNS 的设备也要单独恢复。
4. 让客户端重新连接或续租，核对实际 DNS，再测试解析和上网。已有手工 DNS 规则需另行核对。

## 日常使用

- **修改服务配置**：在对应配置空间保存并发布到 Agent。
- **修改分流设备、IP 规则集或网关**：在「上游路由」修改，保存并预览，确认后发布；只保存不会改变现网。
- **更新远程 IP 规则集**：首次成功发布后，可在「规则同步」中设置自动同步。它不代替 Mihomo、OxiDNS 自身的配置发布。
- **备份**：保留 NAS 的完整 `data`、RouterOS 配置、各服务配置及 Agent 身份目录，不要手工覆盖受管运行配置。

## 遇到问题

| 现象 | 先检查 |
|---|---|
| Agent 没出现或离线 | 容器是否运行、NAS 地址是否填写正确、管理端口是否被防火墙拦截 |
| RouterOS 探测或发布失败 | 管理地址、账号权限和页面错误提示；可连接不代表账号具有写入权限 |
| 预览提示冲突，无法发布 | 先核对现有路由、分流和 FastTrack 设置，不要重复添加或直接删除旧规则 |
| DNS 能解析，但代理网站打不开 | 路由是否发布成功、Mihomo 是否正常运行、代理节点是否可用 |
| 只有部分设备不生效 | 设备实际 DNS 和 IP、来源设备策略是否已发布、浏览器安全 DNS / 其他代理 |
| 开启强制 DNS 后仍有设备绕过 | 检查是否走同一 IPv4 LAN 入口、是否在放行范围，以及是否使用 IPv6 或 DoH；详见上方可选功能 |
| 开启 853 拦截后设备无法解析 | 检查是否启用了严格的「私人 DNS」，先改为自动或关闭再测试 |
| 发布失败或结果待确认 | 查看本次发布结果，核实设备状态后再操作；Agent 配置可在发布记录中使用「回滚到发布前」 |
| 全网启用后无法上网 | 开启过强制 DNS 时，先按 [停用与恢复](#restore-dns) 撤销对应规则；再恢复原 DHCP DNS，让客户端重新获取租约 |

需要恢复 DHCP DNS 时，按 [补充三：手动配置](#manual-network) 操作。在 ConfigFlow 中把 DHCP DNS 改为「不关联」只会停止管理，不会恢复原值。

更多说明：[Agent 部署与回滚](../backend/agents/go-agent/docs/DEPLOYMENT.md)、[OxiDNS 监控配置](AGENT_OXIDNS.md)、[通用 Docker 部署](../doc/module/deployment.md)。

---

<a id="container-install"></a>

## 补充一：容器安装

本节只负责安装服务。完成后回到正文第 2 步，确认 Agent 上线，再继续发布配置和下发路由。

### 准备镜像

在有 Docker 的电脑或 NAS 上执行以下两条命令，下载最新 Agent 镜像并只导出目标架构。默认适用于 x86 RouterOS；ARM64 设备将**两处** `linux/amd64` 都改为 `linux/arm64`。`docker save --platform` 需要 Docker API 1.48+，详见 [Docker 镜像导出说明](https://docs.docker.com/reference/cli/docker/image/save/#platform)：

```sh
docker pull --platform linux/amd64 thsrite/config-flow-agent:latest
docker save --platform linux/amd64 -o configflow-agent.tar thsrite/config-flow-agent:latest
```

两个服务共用 `configflow-agent.tar`。下面任选一种方式上传镜像并创建服务；IP、网桥和磁盘路径需按实际环境替换。

### 选择配置方式

<details>
<summary>方式一：命令行（SSH 或 RouterOS Terminal）</summary>

**① 备份并准备上传目录**

以下 `routeros` 命令在 RouterOS 的 SSH 会话或 Terminal 中执行，不能在 NAS 的 Linux Shell 中执行。

```routeros
/export file=cf-before-install
/ip dhcp-server network print detail
/file print
```

记录当前 DNS，确认 `disk1` 是实际磁盘。新建安装目录；已有该目录时跳过这条命令：

```routeros
/file add name="disk1/configflow" type=directory
```

回到保存镜像 TAR 的**电脑或 NAS**，连接 SFTP。用户名、地址和 SSH 端口按实际填写：

```sh
ROS_USER='replace-with-routeros-user'
ROS_HOST='192.168.50.254'
ROS_SSH_PORT='22'
sftp -P "$ROS_SSH_PORT" "$ROS_USER@$ROS_HOST"
```

进入 `sftp>` 后执行。第一条将配置导出保存到本地，第二条上传镜像：

```text
get cf-before-install.rsc
put configflow-agent.tar disk1/configflow/configflow-agent.tar
bye
```

**② 创建网络、目录和容器**

回到 RouterOS。下面脚本仅用于新装，确认没有同名 `cf-main`、`cf-dns` 对象或旧服务目录后再执行；不要覆盖已有安装。**先修改开头的参数，再将包括最外层 `{ }` 的整段一起粘贴**，保证变量在同一作用域中。

先检查现有对象；名称或目录已被使用时停止，不能直接执行新装脚本：

```routeros
/container print detail
/interface veth print detail
/container mounts print detail
/container envs print detail
/file print
```

```routeros
{
    :local lanBridge "bridge1"
    :local gateway "192.168.50.254"
    :local serverUrl "http://192.168.50.100:5010"
    :local mainIP "192.168.50.28"
    :local dnsIP "192.168.50.251"
    :local prefix "/24"
    :local base "disk1/configflow"
    :local imageFile ($base . "/configflow-agent.tar")

    /interface veth add name=cf-main address=($mainIP . $prefix) gateway=$gateway
    /interface veth add name=cf-dns address=($dnsIP . $prefix) gateway=$gateway
    :foreach name in={"cf-main";"cf-dns"} do={
        /interface bridge port add bridge=$lanBridge interface=$name
    }

    :foreach dir in={"main-config";"main-state";"dns-config";"dns-state"} do={
        /file add name=($base . "/" . $dir) type=directory
    }
    /container mounts add list=cf-main-config src=($base . "/main-config") dst=/etc/mihomo
    /container mounts add list=cf-main-state src=($base . "/main-state") dst=/opt/configflow-agent
    /container mounts add list=cf-dns-config src=($base . "/dns-config") dst=/etc/oxidns
    /container mounts add list=cf-dns-state src=($base . "/dns-state") dst=/opt/configflow-agent

    /container envs add list=cf-main key=SERVER_URL value=$serverUrl
    /container envs add list=cf-main key=SERVICE_TYPE value="mihomo"
    /container envs add list=cf-main key=AGENT_NAME value="mihomo-main"
    /container envs add list=cf-main key=AGENT_IP value=$mainIP
    /container envs add list=cf-main key=AGENT_PORT value="8080"

    /container envs add list=cf-dns key=SERVER_URL value=$serverUrl
    /container envs add list=cf-dns key=SERVICE_TYPE value="oxidns"
    /container envs add list=cf-dns key=AGENT_NAME value="oxidns"
    /container envs add list=cf-dns key=AGENT_IP value=$dnsIP
    /container envs add list=cf-dns key=AGENT_PORT value="8082"

    /container add file=$imageFile name=cf-main hostname=cf-main \
        interface=cf-main envlist=cf-main \
        mountlists=cf-main-config,cf-main-state \
        root-dir=($base . "/main-root") start-on-boot=no
    /container add file=$imageFile name=cf-dns hostname=cf-dns \
        interface=cf-dns envlist=cf-dns \
        mountlists=cf-dns-config,cf-dns-state \
        root-dir=($base . "/dns-root") start-on-boot=no
}
```

如中途报错，先检查已经创建的对象，不要整段重跑。

**③ 等待解压并启动**

```routeros
/container print detail where name~"^cf-"
```

确认镜像解压完成、容器为 `stopped` 后，逐条启动；每次检查对应容器变成 `running` 再启动下一个：

```routeros
/container start [find where name="cf-main"]
/container start [find where name="cf-dns"]
/container print detail where name~"^cf-"
```

目录创建命令见 [RouterOS Files 文档](https://help.mikrotik.com/docs/spaces/ROS/pages/2555971/Files)。若设备版本不支持 `/file add type=directory`，可通过 SFTP 的 `mkdir` 创建同名目录后跳过对应建目录命令。

</details>

<details>
<summary>方式二：WinBox 手动配置</summary>

在 **Files → Backup** 保存配置备份并下载到本地；在 **IP → DHCP Server → Networks** 记录原来的 DNS。

在 **Files** 中创建 `disk1/configflow`，上传 `configflow-agent.tar`。然后依次填写以下四组参数；**每个容器使用独立目录**。

**① 网络接口**

在 **Interfaces → VETH** 添加以下接口，Gateway 都填 `192.168.50.254`。随后在 **Bridge → Ports** 将两个接口加入 LAN 网桥 `bridge1`。

| Name | Address |
|---|---|
| `cf-main` | `192.168.50.28/24` |
| `cf-dns` | `192.168.50.251/24` |

**② 持久化目录**

在 Files 中创建下表的 Src 目录，在 **Container → Mounts** 添加对应挂载。容器的 Root Dir 使用下一张容器表中的独立路径。

| List | Src（RouterOS 目录） | Dst（容器内目录） |
|---|---|---|
| `cf-main-config` | `disk1/configflow/main-config` | `/etc/mihomo` |
| `cf-main-state` | `disk1/configflow/main-state` | `/opt/configflow-agent` |
| `cf-dns-config` | `disk1/configflow/dns-config` | `/etc/oxidns` |
| `cf-dns-state` | `disk1/configflow/dns-state` | `/opt/configflow-agent` |

**③ 环境变量**

在 **Container → Envs** 中逐项添加。表头是 List 名称，每行是 Key 及对应的 Value：

| Key | `cf-main` | `cf-dns` |
|---|---|---|
| `SERVER_URL` | `http://192.168.50.100:5010` | `http://192.168.50.100:5010` |
| `SERVICE_TYPE` | `mihomo` | `oxidns` |
| `AGENT_NAME` | `mihomo-main` | `oxidns` |
| `AGENT_IP` | `192.168.50.28` | `192.168.50.251` |
| `AGENT_PORT` | `8080` | `8082` |

`SERVER_URL` 必须是容器能访问的 NAS 地址，不能填 `localhost`。这里使用单服务模式，不需要再填写 `ENABLE_MIHOMO` 等开关。

**④ 容器**

在 **Container** 中添加两个容器，File 都选择 `disk1/configflow/configflow-agent.tar`：

| 字段 | Mihomo | OxiDNS |
|---|---|---|
| Name / Hostname | `cf-main` | `cf-dns` |
| Interface | `cf-main` | `cf-dns` |
| Root Dir | `disk1/configflow/main-root` | `disk1/configflow/dns-root` |
| 环境变量列表（Envlist / Envlists） | `cf-main` | `cf-dns` |
| Mountlists | `cf-main-config,cf-main-state` | `cf-dns-config,cf-dns-state` |

保留默认入口命令。等待镜像解压结束后逐个点击 **Start**，状态应变为 `running`。安装验收通过后，再为两个容器开启 **Start On Boot**。

</details>

<a id="router-access"></a>

## 补充二：管理账号与连接

在 WinBox 中完成以下准备，已有可用设置时不必重复创建：

1. 在 **IP → Services** 中确认网页管理服务可供 NAS 访问。推荐 HTTPS：启用 `www-ssl` 并配置有效证书；仅使用 HTTP 时，RouterOS 需为 7.9 或更高版本，启用 `www`。ConfigFlow 中填写对应的协议和端口。
2. 在 **System → Users → Groups** 创建专用用户组，勾选 `read`、`write`、`api`、`rest-api`。
3. 在 **System → Users** 创建专用账号，使用随机密码并选择该用户组；将允许来源限制为 NAS 的实际访问地址。
4. 确认路由器防火墙允许 NAS 访问所用管理端口，不要为此向公网开放。

使用自签名证书时，应先解决证书信任问题。确需关闭「校验 HTTPS 证书」时，仅在可信内网使用，并确认管理地址无误。

完成后回到正文第 4 步添加路由器并探测。

<a id="manual-network"></a>

## 补充三：手动配置

以下用于不采用页面下发的环境，或恢复原来的 DHCP DNS。**按正文下发成功后，无需再手动添加路由。** 不要在 WinBox 中直接改动已由 ConfigFlow 管理的路由和策略。

### 手动添加 fake-IP 路由

不使用页面下发时，可手动添加以下 fake-IP 路由。它只负责 fake-IP 访问，不能代替 Telegram 等 IP 规则集的按设备分流。手动路由使用表中的备注，不要使用 `cf`、`cf-` 或 `cf:` 开头的备注。

<details>
<summary>WinBox：手动添加 fake-IP 路由</summary>

先在 **IP → Routes** 中检查，已有相同路由时核对下一跳和路由表，不要重复添加：

| Dst. Address | Gateway | Routing Table | Comment |
|---|---|---|---|
| `100.127.0.0/17` | `192.168.50.28` | `main` | `manual-main-fake-ip` |

网段和地址必须与 Mihomo 配置一致。已有其他分流规则的网络，还需确认实际使用的路由表能到达对应 Mihomo。

</details>

<details>
<summary>命令行：检查并添加 fake-IP 路由</summary>

在 RouterOS Terminal 或 SSH 会话中先检查：

```routeros
/ip route print detail where dst-address="100.127.0.0/17"
```

确认已有条目的网关和路由表正确后执行。脚本只在缺少对应路由时添加，不覆盖已有条目：

```routeros
:if ([:len [/ip route find where dst-address="100.127.0.0/17" and routing-table="main"]] = 0) do={
    /ip route add dst-address=100.127.0.0/17 gateway=192.168.50.28 \
        routing-table=main comment="manual-main-fake-ip"
}
```

</details>

### 手动设置或恢复 DHCP DNS

<details>
<summary>WinBox：设置客户端使用的 DNS</summary>

在 **IP → DHCP Server → Networks** 中打开目标网段：

- **DNS Servers** 填写 OxiDNS 地址：`192.168.50.251`。
- **Gateway** 保持原来的 RouterOS 地址。
- 需要恢复时，将 **DNS Servers** 改回安装前记录的值。

检查 DHCP 自定义 DNS 选项和设备单独选项没有覆盖这里的设置。保存后让客户端重新连接或续租，核对实际获得的 DNS。

</details>

<details>
<summary>命令行：设置或恢复 DHCP DNS</summary>

先检查目标网段及自定义选项：

```routeros
/ip dhcp-server network print detail
/ip dhcp-server print detail
/ip dhcp-server option print detail
/ip dhcp-server option sets print detail
```

替换为实际网段和 DNS，确认仅匹配目标网段后执行：

```routeros
/ip dhcp-server network set [find where address="192.168.50.0/24"] \
    dns-server=192.168.50.251
```

需要恢复时，将 `dns-server` 改回安装前记录的原值；原来为空时设置为 `dns-server=""`。然后让客户端重新连接或续租。

</details>

### 命令行开启容器自动启动

安装验收通过后，在 RouterOS 检查每个名称仅对应一个运行中的容器，再执行：

```routeros
/container print detail where name~"^cf-"
/container set [find where name="cf-main"] start-on-boot=yes
/container set [find where name="cf-dns"] start-on-boot=yes
```

## 可选进阶：备用 Mihomo

需要代理服务冗余时，可自行部署备用 Mihomo，并根据实际网络手动完成以下配置：

- 为备用 Mihomo 准备独立 IP、配置空间和持久化目录，fake-IP 网段不能与现有实例或网络重叠。
- 手动配置备用 fake-IP 路由及所需的回流设置，确认流量能够到达备用实例并正常返回。
- 备用路由使用独立备注，例如 `manual-backup-fake-ip`，不要使用 `cf`、`cf-` 或 `cf:` 开头的备注。
- 同一台 RouterOS 的自动路由下发仍只由原配置空间负责；不要在备用配置空间向它发布路由配置或接管备用路由。
- 确认备用代理链路可用后，再按需设置 OxiDNS 的代理备用上游，不要用公共 DNS 代替备用代理。
- 分别验证正常访问、主实例停用后的新请求，以及主实例恢复后的访问；每次重新下发路由后，也检查手动备用路由仍然存在。

备用实例不会自动接管已缓存的主实例 fake-IP 或已有连接；只有一个 OxiDNS 时，DNS 服务本身也不具备冗余。

## 实现与命令参考

- 页面和发布顺序：[上游路由页面](../frontend/src/views/Routers.vue)、[设备分流工作区](../frontend/src/components/routers/PolicyRoutingPanel.vue)、[发布协调](../backend/routers/release.py)。
- 路由器写入与 DHCP 范围：[RouterOS REST 驱动](../backend/routers/routeros.py)、[DHCP DNS 选择与校验](../backend/routers/dhcp_dns.py)。
- 可选的 DNS 转发、放行与故障暂停：[强制 DNS 规则](../backend/routers/dns_guard.py)。
- 镜像环境变量与持久化：[Agent 镜像](../docker/Dockerfile.agent)、[单服务启动脚本](../docker/docker-agent-entrypoint-service.sh)。
- RouterOS 命令以目标版本为准，参考 [Container](https://help.mikrotik.com/docs/spaces/ROS/pages/84901929/Container)、[Files](https://help.mikrotik.com/docs/spaces/ROS/pages/2555971/Files)、[REST API](https://manual.mikrotik.com/docs/developer-guides/rest-api/)；镜像按架构导出参考 [Docker image save](https://docs.docker.com/reference/cli/docker/image/save/#platform)。

本文核对了 ConfigFlow 的页面、驱动、镜像参数和官方命令说明，不能代替目标 RouterOS 版本上的安装及单设备网络验证。
