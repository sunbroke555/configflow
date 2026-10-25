# 客户端固定 IP：实现与隔离实测

设备名称与 DNS 策略仍按 IP 保存；本功能通过来源主路由的 DHCP 静态绑定保持地址，不做按 MAC 自动跟随。未勾选固定 IP 时不写路由器。

## 写入与删除边界

- 单个、批量和已添加设备共用预览/确认接口。预览令牌绑定配置空间、具体路由器连接、IP/MAC、DHCP 网段和稳定租约字段，10 分钟过期；执行前重新读取，状态变化即要求重新预览。
- 配置向导进行中后端拒绝 DHCP 写入，前端不显示固定入口。按发现设备的路由器写入，并使用现有发布/同步互斥锁。
- `client_fixed_ips` 仅保存路由器 ID、租约 ID、MAC 和归属等必要字段。缺省旧数据直接可读；克隆配置空间不复制路由器连接或固定绑定归属。
- 删除设备、清空名称和策略默认保留租约。只有明确预览确认后才删除 `cf-client:` 归属且 ID、IP、MAC、稳定字段全部匹配的对象。保留后重新添加设备仍可从路由器归属标记识别。
- 每个写入回读核验；中途失败只补偿仍能证明归属且没有外部修改的对象。批量逆序补偿；无法确认写入结果或回滚失败时明确提示检查路由器，不报告成功。

## 实验范围

从测试服务器 `192.168.0.32` 经 `ssh pve` 驱动，仅创建并使用本次新客体：

| 对象 | 配置 | 管理/客户端网段 |
| --- | --- | --- |
| VM140 | iKuai 4.0.314，2C/2G、e1000、cfik、onboot=0 | 管理 192.168.77.1；固定 MAC 客户端 192.168.77.100 |
| VM141 | 指定 CHR 7.24.5 镜像副本、cfgt、onboot=0 | 独立管理 10.241.141.1；VLAN 子接口 DHCP 10.97.71.0/24 |

开工前登记 `/root/cf-gwip-lab/fixed-ip-owner.json`，记录客体、地址、快照、拆除命令和最终状态。未操作 VM117、10.97.0.50、生产路由器或其他既有客体。

准备 CHR 管理地址时发现候选 10.97.0.71 的邻居 MAC 不属于本次 VM；两次连接被拒绝后停止该候选地址，改用本次独立管理网段并核对新 VM MAC。没有成功读取或修改该候选地址上的设备。后续所有管理操作均通过核对 MAC 的实验地址。

## RouterOS 7.24.5

通过真实 REST 请求确认：

- `POST /rest/ip/dhcp-server/lease/make-static`，请求 `{“.id”: “*…”}`，返回 `[]`；原租约 ID、server 和 address 保留。
- 无匹配动态租约时 `PUT` 新增，带 MAC、地址、明确 DHCP server 和唯一 `cf-client:` comment；按返回 ID 回读。客户端位于 VLAN 子接口网段，固定后续租仍为 10.97.71.149，DNS 为网段 10.97.71.2 / 10.97.71.3。
- 删除、删除后恢复原稳定配置、新 ID 回读；同 MAC 已有静态、同 IP 属其他 MAC、网段外、无 MAC、非 ConfigFlow 归属均拒绝覆盖/删除。
- 只读账号写入返回 HTTP 500 / `not enough permissions`，界面提示需要 write 等权限。
- 真机注入：新增 PUT 成功但丢响应、按 ID 回读失败、转换后 comment 写入失败、DELETE 成功后首次 GET 失败。分别验证只撤销本次新增/转换预留，以及再次回读确认已删除。
- `age`、`class-id`、`active-agent-*` 等运行字段不参与稳定配置比较；客户端续租不会使预览/删除误判外部修改。

**回滚限制：** 真机 `PATCH dynamic=true` 返回 400 `unknown parameter dynamic`，`make-dynamic` 返回 400 `no such command`。因此转换失败时删除本次静态预留，客户端下次续租恢复动态 DHCP；不能保证原 ID 或原 IP 原地恢复。预览和补偿错误均说明此限制。最初实验 DHCP hook 未清旧地址的问题已修正，正式静态续租验证使用修正后的客户端；不把先前的地址变化作为产品行为的因果证据。

RouterOS 故障注入在真实 driver 请求边界丢弃响应或拒发指定请求，**不是实际网络故障或设备断电**。真实响应样本：`backend/testdata/routeros_fixed_ip_7245.json`。

## iKuai 4.0.314

先从新 VM 的真实 Web UI 和 HTTPS 请求确认契约，再编写适配器：

- `dhcp_static/add` 使用 interface、ip_addr、mac、gateway、dns1、dns2、comment、tagname、enabled、hostname。tagname 必填且 1–15 字符，适配器使用独立随机标记；comment 写 `cf-client:<名称>`。成功返回顶层 `rowid`，不是 `results.id`。
- **dns1/dns2 空字符串实机接受。** 静态分配后客户端重新获取同一 192.168.77.100，DNS 为 DHCP 网段的 192.168.77.2 / 192.168.77.3；现有 `read_dhcp_dns` 的 blockers 为空，没有独立 DNS 阻断。
- `show TYPE=static_total,static_data` 才返回静态配置；`total,data` 实测返回租约视图，缺少 DNS/comment，不能用于核验。读取使用完整分页，按 ID 核对。
- `del` 请求 `{"id":"1"}`，成功 code=0，回读 static_total=0。支持删除失败/批量失败时恢复原稳定字段并记录新 ID。
- `hostname` 实测随 DHCP 客户端报文更新，即使 add 提交空值也可能立即返回非空值，因此与 termname、ip_addr_int 一样视为运行字段；带非空主机名及续租改名后的写入、核验、删除均通过。
- 真实验证同 MAC、同 IP 异 MAC、网段外、无写权限、非本项目归属保护。缺少必填字段、重复 MAC/IP 返回 3001；受限账号可读，add/del 返回 1002，并显示需要修改权限。
- 通过真实 HTTPS 转发代理注入新增成功但丢响应、按 ID 回读失败、删除后首次回读失败。新增失败清除本次绑定；删除回读瞬时失败再次核对已消失后报告成功，不额外重建。

请求/响应、必填字段与权限错误测试数据：`backend/testdata/ikuai_fixed_ip_40314.json`。实验凭据、会话和密钥不进入仓库。

## 完整 HTTP 流程与界面

同一个临时配置空间同时接入上述两台新路由器，真实调用 `/api/client-devices`：

1. 双路由器发现正确 IP/MAC/来源，资格判断通过。
2. 批量预览完全无写入；无效确认令牌被拒绝。
3. 确认后分别写正确来源、回读并持久化两个绑定 ID；重复执行不覆盖现有静态绑定。
4. 批量预览删除、回读确认不存在，仅清理固定元数据。
5. 单独操作 iKuai 不打开 RouterOS 连接。
6. 默认删除设备保留真实静态绑定；重新添加后可按实时归属标记预览删除。
7. 注入本地配置持久化失败，确认实际新建 iKuai 静态分配被撤销。

UI 在测试服务器用 headless Chromium 验证 1440px 桌面和 390px 手机，所有 API 使用明确标注的模拟数据。验证单个/批量默认不勾选、禁用原因、预览确认、取消、已固定标记、默认保留删除，零浏览器异常；12 张截图人工检查。

## 证据与未覆盖边界

- 测试服务器：`/home/sunbroke/configflow-fixed-ip-evidence/`。
- 本机脱敏副本：`/private/tmp/configflow-fixed-ip/`。
- 主要文件：`ikuai-api-discovery.json`、`ikuai-adapter-results.json`、`ikuai-adapter-requests.json`、`ikuai-hostname-probe.json`、`routeros-*.json`、`two-router-http-integration.json`、`ui/report.json` 和 UI PNG。
- 自动化回归包括确认令牌/过期、跨配置空间/多路由器、并发修改、回滚、克隆隔离，以及 OxiDNS、Mihomo、Surge、Loon、MosDNS 和路由器期望输出逐字节不变。
- 不覆盖其他 RouterOS/iKuai 固件版本、实际设备断电或持续网络中断、生产拓扑、MAC 随机化和按 MAC 自动跟随 IP。路由器 API 无条件写入事务，无法完全消除外部管理器在最终核对与写入之间的竞争；检测到外部变化时保留现状并报告。
