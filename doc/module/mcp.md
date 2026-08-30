# MCP 服务

ConfigFlow 内置 MCP（Model Context Protocol）服务端，把平台的全部业务能力以工具的形式暴露给外部 MCP 客户端（Claude Desktop、Claude Code 等）。接上之后，可以直接用自然语言完成订阅管理、规则调整、策略组编排和配置生成。

## 端点

| 项目 | 说明 |
|------|------|
| 地址 | `http://<你的 ConfigFlow 地址>/mcp` |
| 传输 | Streamable HTTP（JSON-RPC 2.0 over POST） |
| 会话 | 无状态，不需要维持 SSE 通道 |
| 协议版本 | 2025-06-18（同时兼容 2025-03-26、2024-11-05） |

MCP 服务与主应用同进程运行，不需要额外部署或额外端口。

## 认证

复用 ConfigFlow 既有的凭证，不需要单独申请密钥。按当前部署的认证配置，行为如下：

| 部署情况 | MCP 需要的凭证 |
|----------|----------------|
| 未设置 `ADMIN_USERNAME` / `ADMIN_PASSWORD`，也未设置配置令牌 | 无需凭证 |
| 设置了配置令牌 | 该配置令牌 |
| 已启用账号密码登录 | 配置令牌，或登录后获得的 JWT |

令牌支持两种带法，按客户端支持情况任选其一：

- 请求头：`Authorization: Bearer <令牌>`
- 查询参数：`http://<地址>/mcp?token=<令牌>`

推荐用配置令牌接入：它不会过期，适合长期保持的 MCP 连接（JWT 有效期只有 24 小时）。

> ⚠️ **请注意配置令牌的权限范围**
>
> 配置令牌同时也是**订阅链接令牌** —— 「配置生成」页会把它拼进订阅 URL
> （`.../api/config/mihomo?token=<配置令牌>`），你复制到 Mihomo / Surge 等客户端的
> 那串链接里就带着它。
>
> 而 MCP 工具可以导出整份配置（含各订阅的明文地址）、重置系统、卸载 Agent。
> 因此 **持有订阅链接 = 持有管理员权限**。
>
> 这意味着：**订阅链接不要分享给他人**。如果确实需要把订阅链接发给别人，
> 请不要再用同一个令牌接入 MCP。

## 客户端配置

### Claude Code

```bash
claude mcp add --transport http configflow http://<你的地址>/mcp \
  --header "Authorization: Bearer <配置令牌>"
```

### Claude Desktop

在 `claude_desktop_config.json` 中加入：

```json
{
  "mcpServers": {
    "configflow": {
      "type": "http",
      "url": "http://<你的地址>/mcp",
      "headers": {
        "Authorization": "Bearer <配置令牌>"
      }
    }
  }
}
```

若客户端不支持自定义请求头，把令牌写进 URL 即可：`http://<你的地址>/mcp?token=<配置令牌>`。

## 可用工具

工具按功能域组织，读操作以 `list_` / `get_` 开头，写操作以 `manage_` 开头（通过 `action` 参数区分增删改）。

### 订阅与节点

| 工具 | 说明 |
|------|------|
| `list_subscriptions` | 列出所有订阅源 |
| `manage_subscription` | 增删改订阅 |
| `fetch_subscription` | 拉取订阅最新内容并刷新节点缓存 |
| `get_subscription_nodes` | 获取订阅下已解析的节点 |
| `list_nodes` / `manage_node` | 手动节点的查询与增删改 |
| `list_aggregations` / `manage_aggregation` | 订阅聚合的查询与增删改 |
| `preview_aggregation` | 预览聚合产出的节点与数量 |

### 规则

| 工具 | 说明 |
|------|------|
| `list_rules` / `manage_rule` | 规则与规则集的查询与增删改 |
| `batch_add_rules` | 按同一类型和策略批量添加规则 |
| `test_rule_match` | 测试域名/IP 命中哪条规则、走哪个策略组 |
| `find_duplicate_rules` | 扫描重复的规则条目 |
| `list_rule_library` / `manage_rule_library` | 规则仓库条目的查询与增删改 |
| `test_rule_library` | 测试规则集 URL 的连通性 |

### 策略组与配置

| 工具 | 说明 |
|------|------|
| `list_proxy_groups` / `manage_proxy_group` | 策略组的查询与增删改 |
| `preview_proxy_group_regex` | 预览筛选正则会匹配到哪些节点 |
| `preview_config` | 生成配置内容并返回，不写盘 |
| `generate_config` | 生成配置并保存（MosDNS 为打包下载，不落盘） |
| `manage_custom_config` | 读写自定义配置片段 |
| `manage_config_backup` | 导出 / 导入 / 重置整份配置 |

### MosDNS、Agent 与系统

| 工具 | 说明 |
|------|------|
| `get_mosdns_settings` / `update_mosdns_settings` | MosDNS 各分区配置的读写 |
| `list_agents` / `get_agent` / `manage_agent` | Agent 的查询、重启、推送配置、升级、卸载 |
| `get_agent_logs` / `get_agent_metrics` | Agent 日志与监控数据 |
| `get_overview` | 系统概览统计 |
| `get_settings` / `update_settings` | 系统设置的读写 |
| `run_backup` | 立即备份或测试 WebDAV 连通性 |
| `get_app_logs` | 读取服务端日志 |

## 使用要点

- **改完配置记得生成**：修改订阅、规则或策略组后，需要调用 `generate_config` 才会写入 Mihomo / Surge 订阅链接对应的配置文件（MosDNS 订阅链接为实时生成，无需此步）。
- **更新是增量的**：`manage_*` 的 `update` 只需给出要改的字段，其余字段会自动保留。
- **先看再改**：`preview_config`、`preview_proxy_group_regex`、`preview_aggregation` 都不会改动数据，适合在落盘前确认效果。

## 对话示例

> 帮我看下 ConfigFlow 现在的状态

> 把 github.com 和 githubusercontent.com 加到「国外流量」策略组

> 检查一下规则里有没有重复条目

> 预览一下当前的 Mihomo 配置，确认没问题后生成

## 排查

| 现象 | 原因与处理 |
|------|-----------|
| 客户端提示 401 | 未带凭证或凭证不对，检查配置令牌是否与「配置生成」页中一致 |
| 接入一段时间后开始 401 | 用的可能是 JWT（24 小时过期），改用配置令牌可长期有效 |
| 客户端提示不支持的传输 | 本服务只支持 POST，请确认客户端使用的是 Streamable HTTP 而非 SSE |
| 工具返回「调用失败」 | 属于业务失败（如 Agent 不在线、订阅地址不可达），错误信息中会带上原因 |
