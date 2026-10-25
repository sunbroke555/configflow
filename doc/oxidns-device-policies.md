# 客户端设备与 OxiDNS DNS 策略

入口：**当前配置 → 客户端设备**。也可以在 OxiDNS 面板点「客户端设备」，此时固定打开该 Agent 绑定的配置空间。

1. 从 OxiDNS 查询记录中选择客户端，或手动输入 IPv4 / IPv6 地址及 CIDR 网段，例如 `192.168.50.0/24`、`2001:db8::/64`。
2. 填写名称，按需选择 DNS 策略「默认」「智能分流」或「直连」。每一行修改后自动保存。
3. 名称立即生效，不需要发布。DNS 策略保存为草稿，勾选要发布的 OxiDNS 后点「发布」。发布沿用完整配置的校验、备份、应用和失败回滚流程，使用 Agent 支持的热重载或重启方式。

| DNS 策略 | 行为 |
| --- | --- |
| 默认 | 不单独设策略：落在已设策略的网段内时沿用该网段，否则保持默认分流。 |
| 智能分流 | 沿用当前配置的域名、IP 和默认分流规则；可作为「直连」网段中的例外。IPv4 地址同时是上游路由「策略路由」的来源，改动后需在上游路由页重新发布。 |
| 直连 | 保留公共查询记录、查询类型过滤、Hosts 和广告过滤，随后使用本地 DNS 上游，跳过分流及代理解析。 |

名称和 DNS 策略都清空后，设备从列表移除。名称在本配置空间共用：Mihomo 连接、数据统计和 OxiDNS 查询记录都按它显示。

例如 `192.168.50.50` 设为智能分流，`192.168.50.51` 设为直连。国内外如何划分仍由现有规则决定；代理侧需要先配置 Mihomo DNS，本地上游需要配置运营商或其他直连 DNS。设备策略不会自行配置 Mihomo、RouterOS 或 DHCP。

## 网段与优先级

- 规则重叠时优先匹配最具体的范围：单个 IP 优先于网段，小网段优先于大网段，与添加顺序无关。
- 例如 `192.168.50.0/24` 设为直连，`192.168.50.50` 设为智能分流，则 `.50` 使用现有分流规则，其余匹配该网段的设备直连。
- 智能分流也可以作为网段例外：`192.168.0.0/16` 直连、`192.168.50.0/24` 智能分流时，后者优先。
- 移除一条策略后，设备会落入剩余的更大网段策略；没有其他匹配才恢复默认分流。
- `192.168.50.51/24` 自动规范为 `192.168.50.0/24`。`/32` 和 `/128` 规范为单个地址，等价条目不能重复添加。
- IPv4 映射的 IPv6 地址及 `/96`–`/128` 网段会规范为 IPv4；更短的映射前缀不接受。IPv4 和 IPv6 分别匹配，`0.0.0.0/0`、`::/0` 可分别覆盖该地址族。

## 作用范围

- 上游路由「策略路由」的「来源设备」只读显示这些设备，并标出参与策略路由的智能分流 IPv4；在那里点「在客户端设备中管理」回到本页修改，改完后需在上游路由重新保存并预览发布。

- 设备属于 **配置空间**。从导航进入时使用侧栏选中的配置空间；从 OxiDNS 面板进入时使用该 Agent 绑定的配置空间，不切换全局空间。
- 同一配置空间的 OxiDNS 共用这份草稿。从导航进入时默认勾选所有在线的 OxiDNS，从 OxiDNS 面板进入时只勾选该设备；未勾选的设备保持现有配置。
- 发布的是该配置空间的完整已保存配置，包括其他已保存的改动。
- 核心离线或查询记录未开启时，可以手动添加 IP。已有客户端列表来自查询记录，不是 DHCP 设备清单。
- 每行单独保存：只有这一行在别处被改过时才会要求重新载入，修改其他设备或其他 OxiDNS 设置不影响。发布时如果草稿已在别处改变，会停止发布并要求重新载入。

## 设备发现（读取主路由）

`GET /api/client-devices/discovery?profile=<配置空间>`（或 `?agent=<OxiDNS Agent>`，与 `GET /api/client-devices` 相同的配置空间解析和登录要求）实时读取该配置空间中所有 RouterOS 与 iKuai 4.x 主路由，并发进行，每台最多约 20 秒。结果不保存，也不新增数据表：每次打开或刷新页面都重新读取。

```jsonc
{
  "success": true,
  "profile_id": "office",
  "state": "ok",            // ok | partial（部分路由器失败）| failed（全部失败）| unsupported（没有 RouterOS / iKuai）
  "message": "",            // unsupported 时为「需要在当前配置接入 RouterOS 或 iKuai 才能发现设备」
  "checked_at": "2026-10-25T10:46:34+08:00",
  "sources": [              // 每台路由器一条
    {"id": "router_x", "name": "主路由", "type": "routeros", "ok": true, "error": "",
     "notes": ["账号缺少 test 权限，无法确认静态 IP 设备是否在线"],
     "policy_routing": true}  // 仅 RouterOS：已启用按来源策略路由（智能分流 IPv4 设备参与）
  ],
  "devices": [              // 每条已保存的行：{ip, name, mode} 加上下列字段
    {"ip": "192.168.0.133", "name": "摄像头", "mode": "direct",
     "presence": "online", "basis": "lease", "mac": "E0:BA:AD:94:F2:48", "hostname": "C6c_EZVIZ",
     "sources": ["arp", "lease"], "last_seen": 523, "router_id": "router_x"}
  ],
  "discovered": [           // 路由器看到、但没有自己那一行的 IPv4 地址，按地址排序
    {"ip": "192.168.0.147", "mac": "16:F7:C5:8A:16:A4", "hostname": "iPhone", "sources": ["arp", "lease"],
     "presence": "online", "basis": "arp", "last_seen": 286, "router_id": "router_x",
     "network": {"ip": "192.168.0.0/24", "name": "家里", "mode": "smart"}}   // 覆盖它的最具体已保存网段，没有则为 null
  ]
}
```

- `presence`：`online` 在线、`recent` 近期见过（路由器记得它但无法确认）、`offline` 离线、`unknown` 未知；已保存的网段行为 `network`，IPv6 行为 `unknown`。
- `basis` 说明依据：`arp`（ARP 状态）、`lease`（DHCP 续租时间）、`probe`（经路由器主动确认）、`terminal`（iKuai 终端监控）、`stale`（只有未确认的 ARP 记录或租约）、`absent`（所有路由器都读取成功但没有该地址）。
- `sources`：`arp`、`lease`（DHCP 租约）、`online`（iKuai 在线终端）。`last_seen` 是距上次 DHCP 通信的秒数，未知为 `null`。
- 已保存的单个 IPv4 没有出现在任何路由器中：所有路由器读取成功时为 `offline`，否则为 `unknown`。
- `discovered` 不含路由器自己的地址、路由器管理地址和本配置空间 Agent 主机的地址，也不含只有失败 ARP 记录（无 MAC、无租约）的地址。

### RouterOS 判断规则

只看局域网：启用的 DHCP 服务所在接口；没有 DHCP 服务时，改用配置了静态私有地址（RFC1918）的接口，排除动态地址（例如 DHCP 获取地址的 WAN）和接口列表 `WAN` 的成员。WAN 侧大量 `failed` ARP 记录不会出现。

1. ARP 状态 `reachable` / `delay` / `probe`：在线。
2. 租约 `bound` 且 20 分钟内续租过：在线（默认租期 30 分钟，在线客户端约每 15 分钟续租）。
3. ARP `failed` / `incomplete`：离线。
4. 租约错过续租（超过租期一半再加 5 分钟，至少 20 分钟）：离线。静态租约显示 `waiting` 而主机使用静态 IP 的情况不算错过续租。
5. 只剩 `stale` / `permanent` ARP 记录（RouterOS 中 stale 记录不会过期，常见于静态 IP 的虚拟机），或租约较新但没有 ARP：经路由器主动确认。在该接口上发 ARP ping（`POST /rest/ping {"address","count":"2","interval":"300ms","arp-ping":"yes","interface"}`，屏蔽 ICMP 的主机也会应答）；接口不支持时改用普通 ping，并再读一次 ARP 表：`reachable` 为在线，`failed` / `incomplete` 为离线。已保存的地址优先，每次最多确认 32 台、6 个并发、单次请求 4 秒。
6. RouterOS 的 `/ping` 需要用户组带 `test` 权限；没有时返回 HTTP 500 `not enough permissions (9)`，此时不再继续，这些设备显示为「近期见过」，并提示「账号缺少 test 权限，无法确认静态 IP 设备是否在线」。超时或超过上限的设备同样显示为近期见过。读取和确认都不修改路由器配置。

### iKuai 4.x 判断规则

接口形式已在 iKuai 4.0.314 上核对（测试数据 `backend/testdata/ikuai_clients_40314.json`）：列表用 `show`，参数 `{"TYPE":"data,total","limit":"<起始>,<条数>"}`，返回 `{"data":[...],"total":N}`，每页 1000 条。

- `monitor_lanip`（终端监控）中的地址为在线。名称取备注 `comment`、终端名 `termname`、主机名 `hostname` 中第一个非空值；只有 DHCP 客户端有 `hostname`，静态 IP 设备没有这个字段。终端监控只列出最近有流量经过路由器的设备（4.0.314 实测：客户端停止流量后 100–120 秒从列表消失，租约仍在），来源提示「按 2 分钟内经过路由器的流量判断在线」。
- `dhcp_lease` 只用于发现设备；不在终端监控中的地址为离线。`last_seen` 取距上次分配或续租的秒数（`end_time - start_time - timeout`，只用路由器自己的时钟；续租时 `start_time` 会更新）。读取失败只记为提示。
- `lan` 的 `ip_mask`（如 `192.168.77.1/255.255.255.0`）是路由器自己的局域网地址，不会出现在 `discovered` 中；读取失败时忽略。
- 错误码（4.0.314 实测，很多错误响应也带 `"Result":10000`，不能据此判断为 3.x）：
  - `1001`：账号没有该功能的读取权限，`Label` 为功能名（如「终端监控IPv4」）；受限账号调用不存在的接口也返回 1001。
  - `1002`：账号没有执行（修改）权限。
  - `1003 Access denied`：固件没有这个接口（管理员账号调用不存在的 `func_name`）。
  - `1005`：登录时账号或密码错误。
  - `1008`：会话已过期，ConfigFlow 会重新登录一次再重试。
  - `3001`：action 或参数无效。
- 受限账号需要读取权限：终端监控IPv4（`monitor_lanip`，必需）和 DHCP 租约（`dhcp_lease`，权限提示中的功能名为「IPv4」）；`lan` 可选。

## 网络条件

设备需通过 OxiDNS 查询，并且 OxiDNS 能看到真实客户端来源 IP。可以由路由器 DHCP 统一下发 OxiDNS 地址，建议固定 DHCP 租约。若所有查询先由路由器转发，OxiDNS 可能只看到路由器的地址。

此功能控制 DNS 解析，不会撤销 RouterOS 全局真实 IP 代理路由，也不会拦截设备自行使用 DoH 或硬编码目标 IP。IPv4 与 IPv6 地址分别配置；地址变化后需要更新策略。

## 原生配置兼容

无需修改 OxiDNS 核心。ConfigFlow 使用原生 `client_ip` 匹配器，维护 `cf_device_policy` / `cf_device_direct` 两个插件和主处理链中的一个入口。直连使用独立执行链，不复用代理响应缓存，也不触发分流用的 RouterOS 动态规则。

仅对可识别的主处理链开放编辑。执行链来自 include、入口不一致、插件命名冲突或设备策略片段被手动改动时，页面会给出原因并停止自动修改；其他原生字段和附加文件保留。删除最后一条直连策略会移除专用插件及入口。

可选真实核心验收：设置 `OXIDNS_TEST_BINARY` 指向本机 OxiDNS，运行 `backend/test_oxidns_device_policy_core.py`。测试仅启动临时本地 DNS 上游与核心，以 IPv4 / IPv6 客户端验证单 IP、网段、智能分流例外、嵌套网段和全地址族规则下的同域名查询互不串流。
