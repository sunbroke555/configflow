# MCP 服务

MCP 让支持这种连接方式的客户端调用 ConfigFlow 的管理工具。入口为与面板同地址的 `/mcp`，无需另装服务或开放新端口。

## 接入前准备

1. 确认客户端所在设备能打开 ConfigFlow。
2. 在「系统设置」生成并保存「访问令牌」。
3. 在 MCP 客户端中选择 **Streamable HTTP / HTTP** 连接，填写完整地址和认证信息。
4. 先调用 `get_overview` 或 `list_profiles` 确认连通，再进行修改。

| 项目 | 示例 |
| --- | --- |
| 服务地址 | `http://192.168.1.10:5010/mcp` |
| 请求头 | `Authorization: Bearer 你的访问令牌` |
| 不支持请求头的客户端 | 将 URL 填为 `http://192.168.1.10:5010/mcp?token=你的访问令牌` |
| 传输 | JSON-RPC 2.0，通过 HTTP POST 请求 |
| 支持的协议版本 | `2025-06-18`、`2025-03-26`、`2024-11-05` |

不同客户端的配置界面和文件格式不同，以其 HTTP MCP 接入方式填写以上字段。ConfigFlow 不提供 SSE 通道；GET、DELETE `/mcp` 返回 405。

## 认证与权限

| 服务端配置 | 可使用的凭证 |
| --- | --- |
| 未启用账号密码登录，也未设置访问令牌 | 无需凭证 |
| 未启用登录，但设置了访问令牌 | 正确的访问令牌 |
| 已启用账号密码登录 | 已设置的访问令牌，或登录取得的 JWT |

JWT 有效期为 24 小时；访问令牌没有同样的自动到期机制，修改后旧令牌失效。

**当前访问令牌同时用于配置链接和 MCP 管理。** 通过它可调用包括配置导出、重置、Agent 卸载等管理工具，不是只读订阅凭证。不要公开分享含令牌的配置链接；当前设置页没有独立的 MCP 专用令牌入口。

## 检查连接的示例

在能访问面板的终端中替换 URL 和令牌，执行下面的初始化请求。它只读取协议和服务信息：

```bash
CF_MCP_URL='http://192.168.1.10:5010/mcp'
CF_MCP_TOKEN='replace-with-your-config-token'
curl --fail-with-body --show-error "$CF_MCP_URL" \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $CF_MCP_TOKEN" \
  --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"connection-check","version":"1.0"}}}'
```

成功时返回 `result`，其中包含协议版本、服务信息和工具能力。服务无状态，不需要保存 SSE 会话。

## 工具范围

工具实际名称和参数以 `tools/list` 返回为准。常用工具如下：

| 功能 | 工具 |
| --- | --- |
| 配置空间 | `list_profiles`、`get_profile`、`manage_profile`、`clone_profile`、`bind_agent_profile` |
| 导入客户端配置 | `import_client_config`，支持 Mihomo、Surge、Loon、Shadowrocket |
| 订阅与节点 | `list_subscriptions`、`manage_subscription`、`fetch_subscription`、`get_subscription_nodes`、`list_nodes`、`manage_node` |
| 订阅聚合 | `list_aggregations`、`manage_aggregation`、`preview_aggregation` |
| 策略组与代理链 | `list_proxy_groups`、`manage_proxy_group`、`preview_proxy_group_regex` |
| 规则编排 | `list_rules`、`manage_rule`、`batch_add_rules`、`test_rule_match`、`find_duplicate_rules`、`reorder_items` |
| 共享规则库 | `list_rule_library`、`manage_rule_library`、`test_rule_library`、`get_rule_library_content`、`cache_rule_library` |
| 生成和预览 | `preview_config`、`generate_config`、`manage_custom_config` |
| DNS 设置 | `get_mosdns_settings`、`update_mosdns_settings`、`get_oxidns_settings`、`update_oxidns_settings`、`convert_mosdns_rule` |
| Agent | `list_agents`、`get_agent`、`manage_agent`、`get_agent_logs`、`get_agent_metrics`、`get_agent_install_info` |
| 系统、备份与日志 | `get_overview`、`get_settings`、`update_settings`、`manage_config_backup`、`run_backup`、`get_app_logs`、`manage_app_logs` |

**MCP 的生成、预览目标当前只有 `mihomo`、`surge`、`mosdns`、`oxidns`。** 页面和 HTTP API 另支持 Loon，不能因此向这些 MCP 工具传 `loon`。页面上其他功能也不一定已有同名 MCP 工具。

## 选对配置空间

- 独立配置工具可传 `profile_id`，例如 `home`。省略时使用系统当前默认配置空间，默认值不固定为字符串 `default`，也不跟随浏览器顶部选择。
- 订阅、节点、聚合、规则库、系统设置和全量备份仍是全局操作。
- Agent 推送使用它自己的绑定，调用前先核对绑定空间。
- 默认配置可在页面删除时选择接任者；MCP `manage_profile` 的删除操作当前不传接任参数，需要先在页面处理。仍绑定 Agent 的配置不能删除。

## 预览、保存与发布

| 操作 | 实际行为 |
| --- | --- |
| `preview_config` | 返回生成内容，不保存主配置文件；可能读取订阅或规则 |
| `generate_config`：Mihomo / Surge | 生成并保存文件，只返回结果摘要 |
| `generate_config`：MosDNS / OxiDNS | 准备完整 ZIP，不保存到服务端磁盘；文件需要在页面下载 |
| `manage_config_backup`，`scope=system` | 对整个系统导出、导入或重置 |
| `manage_config_backup`，`scope=profile` | 仅导入或导出单空间引用和独立参数，不支持重置 |
| Agent 配置推送 | 显式管理操作，最终是否生效需要查询发布结果 |

MCP 返回会过滤内部凭据，不能把返回文本当作可完整恢复设备身份的备份。恢复用途请在「系统设置」下载「导出全部数据」。

`manage_*` 的更新一般保留未提交字段，但列表和不同设置接口的替换规则应以工具参数为准。调整顺序用 `reorder_items`，其 `ids` 按指定顺序移到顶部或底部，其余保持相对顺序。共享资源被引用时，先解除引用再删除。

## 常见问题

| 现象 | 处理方法 |
| --- | --- |
| 401 | 检查令牌和认证头；JWT 可能已过期 |
| 405 或客户端等待 SSE | 选择 HTTP POST / Streamable HTTP，不使用 SSE 端点 |
| 未知工具或不支持的目标 | 重新读取 `tools/list`，核对当前实现的名称和参数 |
| 工具内容中出现「调用失败」 | 请求已到业务层，按返回原因检查资源、配置或设备 |
| 改了另一套配置 | 显式传 `profile_id`，检查 Agent 绑定和系统默认空间 |

## 实现参考

[协议处理](../../backend/mcp_server/server.py)、[工具与参数定义](../../backend/mcp_server/tools.py)、[认证](../../backend/mcp_server/auth.py)、[输出过滤](../../backend/common/config_export.py)。
