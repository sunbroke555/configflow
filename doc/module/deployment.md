# Docker / NAS 部署指南

> 新手请先阅读[新版操作手册](../guide/install.md)，包含选项建议、操作结果和当前页面说明。本页保留作为补充资料。

本文适用于普通 Docker 主机。RouterOS Container 请使用 [专门安装指南](../../docs/CONFIGFLOW_AGENT_ROUTEROS_INSTALL.md)。

镜像包含前端、Python 后端、Sub-Store 和节点测速组件。准备好 Docker 与 Compose 后，按下面四步操作。

## 1. 准备目录和账号

```bash
mkdir -p configflow
cd configflow
```

在这个目录创建 `compose.yaml`。将下面密码和密钥替换为自己的随机值，密钥建议至少 32 位；用户名也可修改。

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
      ADMIN_USERNAME: admin
      ADMIN_PASSWORD: "replace-with-a-strong-password"
      JWT_SECRET_KEY: "replace-with-at-least-32-random-characters"
    restart: unless-stopped
```

`5010` 是浏览器访问的端口，右侧 `80` 保持不变。端口冲突时只改左侧，例如 `5011:80`。

`./data` 相对于部署文件所在目录解析。NAS 用户可以换成明确的绝对路径，例如 `/volume1/docker/configflow/data:/data`，路径必须按自己的设备填写。

## 2. 启动并打开页面

在部署文件所在目录执行：

```bash
docker compose config --quiet
docker compose pull
docker compose up -d
docker compose ps
```

| 部署位置 | 访问地址 |
| --- | --- |
| 当前电脑 | `http://localhost:5010` |
| NAS / 服务器 | `http://设备IP:5010`，例如 `http://192.168.1.10:5010` |

使用部署文件中的账号和密码登录。只有用户名、密码都设置完整才开启登录验证；本例已开启。

## 3. 填写设备能访问的地址

进入「系统 → 系统设置」：

1. 「服务域名」填写完整地址，例如 `http://192.168.1.10:5010`。其他设备不能使用浏览器自己的 `localhost`。
2. 生成并保存「访问令牌」，之后通过页面复制配置 URL。
3. 按 [第一次使用](../guide/first-run.md) 添加资源并生成配置。

访问令牌同时授权 MCP 管理工具，含令牌的订阅链接需要像密码一样保管。

Sub-Store 默认按需启动，空闲约 5 分钟后停止；第一次转换需要等待就绪。在「第三方依赖」可检查、更新或切换为常驻模式。无需另外开放容器内部的 `3001` 端口。

## 自定义 Web 端口与 Host 网络

「系统设置 → 服务配置 → Web 监听端口」修改的是 **ConfigFlow 容器内的 HTTP 监听端口**，默认 `80`。填写 `1–65535` 之间的整数（不含后端 `5001` 和 Sub-Store 的内部端口，默认 `3001`），点击「应用」并确认；端口占用或应用失败时保留原端口。成功后通过页面提供的直连参考地址访问，端口改变后可能需要重新登录。

- **Bridge：**普通部署继续用 `5010:80`，只改左侧即可调整浏览器访问端口。如果在 Web 中修改了容器端口，必须同时修改映射右侧，例如监听 `8088` 时使用 `5010:8088`，再重建容器。
- **Host：**容器与 Linux 宿主机共用网络，`ports` 不生效；Web 设置直接调整宿主机的监听端口。
- **反向代理：**修改监听后需要同步调整代理的上游端口。页面提供的是 HTTP 直连参考地址，原 HTTPS 域名是否改变取决于反向代理设置。

从现有 Bridge 部署迁移时，可先在 Web 保存要使用的监听端口，再将部署文件中的 `ports` 两行替换为 `network_mode: host` 并重建容器，最后通过新端口访问。保存后旧映射可能立即失效，继续完成 Compose 修改即可。

Host 首次启动时若默认端口 `80` 已占用，可通过环境变量指定端口，例如在原有 `environment` 中增加：

```yaml
      CONFIGFLOW_WEB_PORT: "8088"
```

重建后访问 `http://设备IP:8088`。环境变量优先于 Web 保存值，并使页面中的端口设置只读；要改由 Web 管理，可先将数据目录中的 `web-listener.json` 写为 `{"port":8088}`，移除这个环境变量后重建容器，再从页面修改。保留原有账号、密钥和数据挂载。

Web 保存值位于 `/data/web-listener.json`，随数据挂载保留，不包含在 JSON 导出、WebDAV 备份或配置导入中。若改端口后无法访问，可在 Compose 设置 `CONFIGFLOW_WEB_PORT` 为确认可用的端口并运行 `docker compose up -d`；Bridge 还需匹配端口映射。恢复页面后核对「服务域名」、客户端订阅链接和已有 Agent 的服务地址，这些地址不会随监听端口自动改写。

支持 IPv6 的主机会同时监听 IPv4 与 IPv6；IPv6 地址访问格式为 `http://[IPv6地址]:端口`。内核禁用 IPv6 时仅监听 IPv4。Host 使用宿主机的网络能力，宿主机本身仍需具备 IPv6 地址、路由和相应防火墙放行。Bridge 也支持 IPv6，但容器出站 IPv6 需配置 Docker 网络，见 [Docker IPv6 文档](https://docs.docker.com/engine/daemon/ipv6/)。修改 Web 端口本身不会给容器分配 IPv6 地址。

本设置仅适用于包含端口管理组件的 Docker 镜像。源码运行或旧镜像会显示不可用，需更新镜像。Python 后端仍使用 `5001`，内置 Sub-Store 使用 `3001`；Host 部署时也需确保这些端口可用。

## 4. 认识数据目录

当前配置格式为 schema 5，主要目录如下。部分目录按需创建，不要求首次启动就全部出现。

```text
data/
├── configflow.sqlite3   # 系统、共享资源和全部配置空间
├── web-listener.json    # Web 监听端口（页面修改后创建，不在 JSON 导出中）
├── config.json          # 仅升级前的旧格式副本（若存在），新版本不再写入
├── configflow.sqlite3.corrupt-*  # 数据库损坏时保留的原文件（仅在出现损坏时）
├── shared/              # 共享订阅和规则缓存
├── profiles/<id>/       # 每个配置的生成文件、Provider 和转换规则
├── sub-store/           # 内置 Sub-Store 数据及在线更新组件
├── migrations/          # 迁移/导入快照（含数据库备份）与删除配置的保留数据（含 deleted-profile.json）
├── metrics/             # Agent 主机最近 24 小时的监控历史（每台 Agent 一个 .jsonl 文件）
└── app.log              # 服务日志，路径可由 LOG_FILE 调整
```

Agent 主机监控图表（CPU、内存、磁盘和流量，最近 24 小时）的数据先保存在内存中，每台 Agent 最多保留 4000 个点。「系统设置 → 定时任务 → 监控历史保存」按间隔把新增数据追加写入磁盘，可选 5、10、30 或 60 分钟，默认 30 分钟；文件明显大于内存数据时自动整理。`docker compose stop` / `restart` 等正常停止（包括 `LOG_LEVEL=DEBUG` 调试模式）会先保存再退出；意外断电或强制结束最多丢失一个间隔的数据。写入失败（如磁盘已满、没有权限）会显示在该任务的上次运行结果中。这些文件位于数据目录的 `metrics/`（容器内为 `/data/metrics/`），更新镜像、重建容器后仍在；它们不在 JSON 导出和 WebDAV 备份里，停服复制整个数据目录时会一并保留。旧版本把监控历史写在容器内的 `/opt/configflow/data/metrics/`（源码运行时为 `backend/data/metrics/`）。升级后首次启动时，如果旧目录仍在，会自动移入 `metrics/`；若 `metrics/` 中已有同一 Agent 的历史，以 `metrics/` 中的为准，旧文件原样保留。只有源码运行和容器内原地自动更新（不重建容器）会保留旧目录；`docker compose pull && docker compose up -d` 等更新镜像、重建容器的方式会先丢弃容器内的旧目录，这次升级后监控历史从空开始，此后不再丢失。删除 Agent 时同时删除它的监控历史；恢复备份或重置后不再存在的 Agent，其监控历史在下次保存时删除。

旧实例还可能保留 `system.json`、旧 `profiles/<id>/config.json` 等文件。不要只看根配置就清理旧数据；迁移和恢复需要保留完整目录。

## 常用操作

所有命令均在部署文件所在目录执行。

| 操作 | 命令 |
| --- | --- |
| 查看状态 | `docker compose ps` |
| 查看最近日志 | `docker compose logs --tail=100 config-flow` |
| 持续查看日志 | `docker compose logs -f config-flow`，按 Ctrl+C 退出查看 |
| 停止 | `docker compose stop` |
| 启动 | `docker compose start` |
| 重启 | `docker compose restart` |
| 修改环境变量后重新创建容器 | `docker compose up -d` |

`restart` 不会应用部署文件中新改的环境变量，也不会拉取新镜像。

## 备份与升级

1. 在「系统设置」导出全部数据，并记录当前镜像版本或摘要。
2. 停服后复制整个实际挂载的数据目录。服务运行时不要只复制 `configflow.sqlite3`，可能复制到写到一半的状态；运行中请使用导出或 WebDAV 备份。
3. 确认备份成功，再更新镜像。

本例使用相对路径 `./data`：

```bash
docker compose stop
cp -a ./data "./data-backup-$(date +%Y%m%d-%H%M%S)"
```

确认复制完成后执行：

```bash
docker compose pull
docker compose up -d
docker compose logs --tail=100 config-flow
```

`latest` 是可变化的标签，不代表已核实包含某个修复。重要实例应记录可回退的版本或摘要，并先用数据副本验证。

Agent 最近一次心跳的状态（在线时间、服务状态、当前监控指标）只保存在内存里，不写入配置，也不在 JSON 导出和 WebDAV 备份里。服务重启后，各 Agent 在下一次心跳（通常 30–60 秒内）前显示为离线；版本号在变化时已写入配置数据库，会随备份保留。监控历史曲线（最近 24 小时）按上文「监控历史保存」的间隔写入磁盘，不随每次心跳写入。旧版本留下的 `runtime.sqlite3` 会在升级后首次启动时读入并删除。

旧版多配置必须完整备份 `system.json`、`profiles/`、`.bak` 和缓存，不能只备份根 `config.json`。完整 JSON 导出也不包含主机上所有运行文件。迁移边界和恢复方法见 [升级、备份与恢复](../design/multi-config-management.md#升级备份与恢复)。

仓库的 Compose 文件位于 `docker/docker-compose.yml`；它的 `./data` 指向 `docker/` 下的数据目录。改用它之前，明确指定原数据路径。

## 不使用 Compose 时

以下是另一种独立部署方式，与上面的 Compose 二选一。先进入准备保存数据的目录，并替换三个变量：

```bash
CF_ADMIN_USER='admin'
CF_ADMIN_PASSWORD='replace-with-a-strong-password'
CF_JWT_SECRET='replace-with-at-least-32-random-characters'

docker run --pull=always -d \
  --name config-flow \
  -p 5010:80 \
  -v "$(pwd)/data:/data" \
  -e ADMIN_USERNAME="$CF_ADMIN_USER" \
  -e ADMIN_PASSWORD="$CF_ADMIN_PASSWORD" \
  -e JWT_SECRET_KEY="$CF_JWT_SECRET" \
  thsrite/config-flow:latest
```

后续重新创建容器时保留原账号、密钥、端口和绝对数据路径。Compose 会把这些设置保存到文件中，更便于维护。

## 常见问题

| 现象 | 检查方法 |
| --- | --- |
| 容器启动失败 | 查看日志，检查 `5010` 是否被占用以及挂载目录权限 |
| 本机能打开，其他设备打不开 | 使用主机实际 IP，检查主机防火墙和端口；容器内部 `5001` 不是本例对外端口 |
| 更新后配置变空 | 先检查 `docker inspect config-flow` 中的 Mounts 是否仍指向原数据目录，保留现有文件并查看迁移日志 |
| Sub-Store 显示休眠 | 按需模式下属正常状态；转换任务会唤醒，失败时在第三方依赖查看原因 |
| 旧版还有 sub-store 服务 | 当前镜像内置组件，旧独立服务及 `SUB_STORE_URL` 环境变量不再使用 |
| 需要详细日志或调整线程 | 参阅 [README 部署参数](../maintenance.md#部署参数)；`DEBUG` 会启用开发调试模式 |

## 实现参考

[部署示例](../../docker/docker-compose.yml)、[镜像构建](../../docker/Dockerfile)、[Nginx 端口与代理](../../docker/nginx.conf)、[配置存储](../../backend/common/config_repository.py)、[Sub-Store 运行模式](../../backend/utils/sub_store_runtime.py)。
