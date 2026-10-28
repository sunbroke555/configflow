[文档首页](../README.md)

# 安装 ConfigFlow 管理面板

本章先把浏览器里的管理页面装好。完成后，你能登录、保存配置，并让其他设备连接它。此时家里的上网方式还没有改变。

ConfigFlow 管理面板负责保存和分发设置；**Mihomo、OxiDNS 等“核心”才是实际处理连接或 DNS 查询的程序**。核心所在设备还需要一个 **Agent（设备上的小助手）**，负责接收配置、检查运行状态。面板与核心可以放在同一台机器，也可以分开放。

本页目录：

- [开始前准备](#prepare)
- [用 Docker Compose 安装](#compose)
- [在 NAS 容器管理器中安装](#nas)
- [第一次打开后的设置](#initial-settings)
- [选择网络方式和修改端口](#network)
- [确认安装成功](#verify)
- [停止、更新与退回](#maintain)
- [下一步](#next)

<a id="prepare"></a>
## 开始前准备

准备以下内容：

| 需要准备 | 怎么选 |
| --- | --- |
| 一台能运行 Docker 的服务器或 NAS | 安装并启动 Docker；使用命令行时还需 Docker Compose。NAS 的“项目／Compose”功能也可以。 |
| 这台设备的局域网 IP | 例如 `192.168.1.10`。建议在主路由中为它保留地址，避免重启后变化。 |
| 一个长期保存数据的目录 | 例如 NAS 的 `/volume1/docker/configflow/data`。这是示例，需换成自己的实际路径。 |
| 一个未被占用的访问端口 | 本章使用 `5010`；已有服务占用时可以改成 `5011`。 |
| 管理账号、密码和登录密钥 | 自己设置，不要原样使用示例里的占位文字。 |

下面的命令在**准备运行面板的设备**上执行。浏览器可以在另一台电脑或手机上打开。

<a id="compose"></a>
## 用 Docker Compose 安装

### 1. 建立部署目录

```bash
mkdir -p configflow
cd configflow
```

在这个目录创建 `compose.yaml`，填入下面内容。先改账号、密码和密钥，再启动。

```yaml
services:
  config-flow:
    image: thsrite/config-flow:latest
    container_name: config-flow
    ports:
      - "5010:80"
    volumes:
      - ./data:/data
    environment:
      DATA_DIR: /data
      TZ: Asia/Shanghai
      ADMIN_USERNAME: "your-admin-name"
      ADMIN_PASSWORD: "replace-with-your-own-password"
      JWT_SECRET_KEY: "replace-with-at-least-32-random-characters"
    restart: unless-stopped
```

<a id="deployment-parameters"></a>
### 2. 看懂需要修改的地方

| 设置 | 本例值／默认行为 | 什么时候修改 | 修改后的结果与生效时间 |
| --- | --- | --- | --- |
| `image` | `thsrite/config-flow:latest` | 普通安装使用这个官方面板镜像；需要固定版本时使用已发布的具体标签或摘要 | 拉取镜像、重新创建容器后生效。它不是 Agent 镜像。 |
| `ports` | `5010:80` | 访问端口冲突时，只把左边 `5010` 改成其他空闲端口 | 左边是设备对外端口，右边是容器内端口。重新创建后按新端口打开浏览器。 |
| `volumes` | `./data:/data` | NAS 或迁移旧实例时，改成自己的实际数据目录 | 左边的文件保存在设备上，右边 `/data` 保持不变。更新时必须继续使用同一个目录。 |
| `ADMIN_USERNAME`、`ADMIN_PASSWORD` | 仓库示例默认不启用；本例已填写，建议保留登录保护 | 首次安装时设置自己的账号和密码 | 两项都非空才启用登录。只设置一项不会启用。修改后需重新创建容器。 |
| `JWT_SECRET_KEY` | 本例要求自行填写 | 首次安装设置足够长的随机字符串 | 用于保护登录会话。更新容器时保留原值；更换后原登录会话失效。 |
| `TZ` | `Asia/Shanghai` | 希望服务使用其他时区时 | 重新创建容器后生效。 |
| `restart` | `unless-stopped` | 普通安装保留 | Docker 启动后会按此策略恢复容器；你主动停止的容器需自行启动。 |

`./data` 是部署文件所在目录下面的 `data`，不是浏览器所在电脑的目录。已有实例不要随意换目录，否则会像新安装一样显示空配置。

一般不需要另外设置 `CONFIGFLOW_HTTP_THREADS`（默认 `8`，允许 `1–64`，控制服务同时处理请求的数量）或 `LOG_LEVEL`（默认 `INFO`，控制日志详细程度）。`LOG_LEVEL=DEBUG` 会启用开发调试模式，不建议普通部署长期开启。修改环境变量后需重新创建容器。

### 3. 启动

在 `compose.yaml` 所在目录执行：

```bash
docker compose config --quiet
docker compose pull
docker compose up -d
docker compose ps
```

这几条命令依次检查部署文件、下载镜像、创建并启动容器、查看状态。若报错，先处理报错，不要继续改家里的网关或 DNS。

### 4. 打开管理页面

在浏览器访问 `http://设备IP:5010`，例如：

```text
http://192.168.1.10:5010
```

使用刚设置的账号密码登录。只有浏览器和容器运行在同一台电脑上时，才可以用 `http://localhost:5010`；其他设备需要实际 IP。

<a id="nas"></a>
## 在 NAS 容器管理器中安装

NAS 不同系统的按钮名称不完全相同，按功能找到对应入口即可。

1. 打开 NAS 的容器管理器，选择“项目”“Compose”或“从 YAML 创建”。
2. 建立一个独立的 ConfigFlow 项目，粘贴上面的部署内容。
3. 将 `./data:/data` 改成 NAS 上的实际目录，例如 `/volume1/docker/configflow/data:/data`。确认这个目录可写。
4. 设置账号、密码、密钥；检查左侧访问端口 `5010` 没有被其他项目占用。
5. 点击管理器中的部署／启动，等容器状态变为运行。
6. 用 `http://NAS的IP:5010` 打开面板。

如果管理器只有“创建容器”，没有 Compose，逐项填写：

| 容器管理器字段 | 填写内容 |
| --- | --- |
| 镜像 | `thsrite/config-flow:latest` |
| 网络 | 默认 bridge／桥接 |
| 端口 | 主机 TCP `5010` → 容器 TCP `80` |
| 目录挂载 | NAS 的数据目录 → `/data`，可读写 |
| 环境变量 | 上例 `environment` 中的各项 |
| 启动命令 | 使用镜像默认值，不另填 |

NAS 部署和命令行部署二选一，不要为同一数据目录同时启动两个面板容器。

<a id="initial-settings"></a>
## 第一次打开后的设置

1. 进入「系统 → 系统设置 → 常规」。
2. 在「服务配置 → 服务域名」填写**其他设备能访问到的完整地址**，例如 `http://192.168.1.10:5010`。移开输入焦点后保存。
3. 如需保护配置下载链接，在「访问令牌」输入自己的令牌，或点击生成按钮。之后从页面复制完整配置链接，不要手工漏掉令牌。

![服务配置中的服务域名与访问令牌](../img/guide/panel-service-settings.jpg)

图中是当前真实界面的隔离演示：`192.168.1.10:5010` 是示例地址，请换成你的地址。截图使用独立开发服务，所以 Web 监听端口显示“不支持”；官方 Docker 容器里的端口设置按下一节操作。服务域名与监听端口始终是两项不同设置。

| 选项 | 默认／建议 | 设置后的作用 | 不设置时 |
| --- | --- | --- | --- |
| 服务域名 | 容器中新实例可能未保存；页面可显示当前浏览器地址。建议填可从其他设备访问的地址 | 生成订阅、规则和安装链接时使用它；不会改监听端口 | 从 `localhost` 打开时，生成的地址可能只有本机能用，其他设备安装或取规则会失败 |
| 访问令牌 | 默认空；需要限制配置链接访问时生成 | 下载链接需带令牌；此令牌也授权 MCP 管理操作，应像密码一样保管 | 配置令牌保护关闭；不要把它与网页登录账号混为一项 |
| Web 监听端口 | 容器内默认 `80`；普通 bridge 安装先保持 | 改的是容器实际监听端口，详见下一节 | 继续使用 `5010:80` 这样的端口映射 |

内置 Sub-Store 是转换订阅用的程序，默认按需启动，空闲约 5 分钟后休眠。首次转换需要等待启动；看到“休眠中”不代表故障。无需另装 Sub-Store，也无需对外开放其内部 `3001` 端口。

<a id="network"></a>
## 选择网络方式和修改端口

### 普通安装先选 bridge

| 网络方式 | 适合谁 | 端口怎么改 | IPv6 要看什么 |
| --- | --- | --- | --- |
| bridge（桥接，Compose 未指定网络时的普通用法） | 只需要面板正常访问，希望与宿主机其他服务分开 | 改 `5010:80` 左边；一般不用动 Web 设置里的端口 | Docker 网络需具备所需 IPv6 能力；改端口不会给容器增加 IPv6 地址 |
| host（主机网络） | Linux 上希望直接使用宿主机网络，包括已有的 IPv6 网络能力 | `ports` 映射不生效，使用 Web 监听端口或启动环境变量 | 宿主机本身要有可用 IPv6 地址、路由，相关防火墙也需允许访问 |

### Host 首次安装

将上例的 `ports` 两行删除，改为：

```yaml
    network_mode: host
```

宿主机的 `80` 已有服务时，在 `environment` 中增加一个确认空闲的端口：

```yaml
      CONFIGFLOW_WEB_PORT: "8088"
```

重新部署后访问 `http://设备IP:8088`。Host 共享主机端口，还需检查面板内部后端 `5001`、内置 Sub-Store 默认 `3001` 是否冲突。不要把面板的 Host 设置与 Agent 容器的网络设置混在一起：它们是不同容器。

### 从 Web 修改监听端口

1. 进入「系统设置 → 服务配置 → Web 监听端口」。
2. 填写 `1–65535` 的整数；不要使用内部后端端口 `5001`、Sub-Store 使用的端口或其他已占用端口。
3. 点击「应用」，阅读当前连接可能断开的提示，再点击「应用新端口」。
4. 成功后按新端口访问。页面给的是 HTTP 直连参考地址；经过 HTTPS 反向代理时，先更新代理的后端地址，不必把原 HTTPS 域名改成该参考地址。
5. 如果设备访问 ConfigFlow 的完整地址也变了，另外更新「服务域名」以及已有 Agent 使用的服务地址，并更新客户端保存的配置下载链接。修改监听端口不会自动替这些地方换地址；仍使用同一反向代理域名时，只需按实际情况更新代理后端。

| 当前情况 | 接下来做什么 |
| --- | --- |
| Host 模式 | 使用宿主机 IP 和新端口；可能需要重新登录 |
| Bridge 模式 | 同时把映射右侧改为新端口，例如容器改为 `8088` 后使用 `5010:8088`，然后重新创建容器 |
| 端口显示只读 | `CONFIGFLOW_WEB_PORT` 已明确指定端口。先用环境变量管理；需要交回 Web 管理时，按下面的退回方法处理 |
| 保存时提示端口占用 | 新端口不会应用，选择其他空闲端口后重试 |
| 应用失败 | 系统会尝试恢复原配置。若明确提示“无法确认原端口已恢复”，按下方环境变量方法重建容器恢复访问，不要假定原端口仍然可用 |
| 连接断开，结果未确认 | 分别检查原地址与新端口，不能只凭连接断开判断成功或失败 |
| 源码运行或旧镜像显示不支持 | 该功能需要带端口管理组件的 Docker 镜像；先更新镜像 |

Web 保存的端口位于实际数据目录的 `web-listener.json`，不包含在页面 JSON 导出和 WebDAV 备份中。若要移除 `CONFIGFLOW_WEB_PORT` 后继续使用当前端口，先在此文件保存对应值，例如 `{"port":8088}`，再移除环境变量、重新创建容器。单纯移除环境变量，会恢复使用以前的 Web 保存值；从未保存过时回到 `80`。

IPv6 访问地址需加方括号，例如 `http://[2001:db8::10]:8088`，示例地址需换成自己的地址。主机支持 IPv6 时，面板会同时监听 IPv4 与 IPv6；内核禁用 IPv6 时仅监听 IPv4。

<a id="verify"></a>
## 确认安装成功

1. 从另一台局域网设备打开面板，确认不是只有宿主机自己能访问。
2. 刷新页面后能正常登录，系统设置仍然保留。
3. 确认数据挂载指向准备好的实际目录。
4. 对照「服务域名」，确认准备安装 Agent 的设备也能打开这个地址。

打不开时，查看 `docker compose logs --tail=100 config-flow`，或 NAS 管理器里的容器日志。按顺序检查容器是否运行、IP、对外端口、挂载权限和防火墙。`5001` 是容器内部后端端口，不是本章让浏览器访问的端口。

<a id="maintain"></a>
## 停止、更新与退回

| 要做的事 | 操作 | 结果 |
| --- | --- | --- |
| 暂停面板 | `docker compose stop` | 停止面板，保留数据；它不会替你停止其他机器上的代理或 DNS 核心 |
| 再启动 | `docker compose start` | 按原容器设置启动 |
| 只是重启 | `docker compose restart` | 不读取新环境变量，也不下载新镜像 |
| 应用 Compose 中改过的端口或环境变量 | `docker compose up -d` | 按新设置重新创建必要的容器，数据挂载必须保持原路径 |
| 更新镜像 | 先备份，再运行 `docker compose pull`、`docker compose up -d` | 使用新镜像；Agent 与核心的更新另行管理 |
| 改端口后无法访问 | 在 Compose 设 `CONFIGFLOW_WEB_PORT` 为已确认空闲的端口；bridge 同时改匹配的端口映射，再 `up -d` | 用明确的启动值恢复访问，再核对原设置 |

更新前，先从「系统设置」导出全部数据；如需完整保留运行文件，再正常停服并复制**整个实际数据目录**。不要在运行中单独复制数据库文件。JSON 导出不含端口文件和监控历史，数据备份边界见 [部署与备份技术说明](https://github.com/thsrite/configflow/blob/main/doc/module/deployment.md#备份与升级)。

更新后配置看似丢失时，先检查是否挂载了另一个空目录，保留原文件，不要立即重置。需要退回镜像时，应使用事先记录的旧版本及对应数据备份；不要盲目让旧版本打开已经迁移的数据。

<a id="next"></a>
## 下一步

- 第一次把一台 Linux 配成分流设备：继续 [首次使用向导](first-run.md)。
- 已经有 Mihomo／OxiDNS，或要用 Docker、RouterOS 部署核心：继续 [安装与接入设备](agent-install.md)。
- 面板已经安装，准备做两台 DNS 互为备用：先完成基础服务，再看 [OxiDNS 高可用](oxidns-ha.md)。

实现依据：[面板 Compose](https://github.com/thsrite/configflow/blob/main/docker/docker-compose.yml)、[登录启用条件](https://github.com/thsrite/configflow/blob/main/backend/common/auth.py)、[Web 端口设置](https://github.com/thsrite/configflow/blob/main/frontend/src/components/settings/WebPortSettings.vue)、[端口管理](https://github.com/thsrite/configflow/blob/main/backend/utils/web_listener.py)。
