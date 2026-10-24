# ConfigFlow 设备组第一步实施计划

状态：实施与验收完成，用户已确认提交、推送和开 PR。日期：2026-10-24。采用最长前缀归属和拦截优先，已先建立 golden 测试，再分阶段实施。最终结果见 [验收记录](device-groups-validation.md)。

## 基线与范围

- 任务通过 `cf-task new device-groups --prefix codex` 创建，worktree 为 `/Users/sunbroke/program/configflow-device-groups`，分支为 `codex/device-groups`。
- 已 fetch 并核实 `upstream/main` 为 `c6029930022db0a65aa56b72a61effec9cc2c51c`，其中已合入 #352。无需再叠加依赖分支。上游提交关键词查重无同类实现，PR 查重仅返回已合并的 #352。
- 主目录只用作源镜像、Python 和前端依赖来源，不修改、暂存、提交或 stash 其中的文件。提交、推送和开 PR 仍按本任务的明确要求先征询确认；发 PR 前运行 `cf-task sync` 并重跑相关验证。
- 实施仅覆盖方案第一步：设备组、组模式、独立策略路由来源、显式迁移、界面和身份告警、域名发现兼容。保留一张全局规则表。
- 不引入规则作用域、Mihomo 中段分段、整组拒绝、组私有 DNS、按组跳过 hosts/AdGuard 或安装向导兼容；不改 Go Agent。额外过滤场景统一称「加强过滤」。

## P0 已验证的形态与需修正的细节

这些实验先调用当前真实生成器，再在临时输出中注入第一步形态；不是已经完成的新设备组生成器。所有实验脚本、二进制和日志放在 `/private/tmp`，没有写入产品代码。

### OxiDNS

使用提供的官方 1.6.1 二进制，执行 `check -c <cfg> -d <dir>`、`start` 和真实 `dig -b <源地址>`。当前五个用例共 142 条查询通过：

1. 全部直连关闭拦截、全部代理开启拦截。
2. 全部直连开启拦截、全部代理关闭拦截。
3. 主动选择完整直连 DNS，包含本地缓存路径。
4. 旧设备策略经过真实 native applier 和 native generator 后，临时替换为迁移形态。
5. 把共享缓存错误放到分发前的反例：确认代理组读到了其他路径的本地应答。

前三种来源分别代表直连组、代理组、默认组；环回和本机 LAN 地址仅用作测试来源，不作为可保存的生产成员。验证内容包括四种 DNS 拦截、拦截优先、MATCH 之后的拦截不进入组段、两个查询顺序下的缓存隔离、默认直连链不使用缓存、hosts/AdGuard 全局生效，以及真实下发的 DNS watchdog 仍先执行。

**P0 发现并修正方案示例的控制流问题：** 组段尾部仅用 `goto proxy_dns_seq` 时，当前模板的远端链可能返回 `Return`，使主表里的 `jump cf_dg_dispatch` 继续执行后面的全局规则。真实内核已复现：组段没有复制的 `after.test` 仍被全局拒绝。

实施采用明确终止的组内兜底：代理组使用 `jump proxy_dns_seq` 后接 `accept`；完整直连使用 `jump china_dns` 后接 `accept`；默认直连进入本身已有 `accept` 的 `cf_dg_local`。只增加新组链的终止，不修改旧共享链和无组输出。原错误形态与失败日志已单独保留。

证据：`/private/tmp/configflow-device-groups-p0-oxidns/run.py`、`results.json`、各用例 `config.yaml/check.log/start.log/dig.log`；反例目录为 `goto-proxy-fallthrough-negative` 和 `cache-before-dispatch-negative`。当前基线已有的设备策略与逐条 DNS 真实内核测试另外运行通过：`21 passed`。这些测试没有覆盖任意自定义 native 文档。

### Mihomo

本机没有现成内核，已下载官方 Darwin arm64 v1.19.1 到临时目录。无组输出、组形态和三个运行变体共五份 `-t` 校验通过；实际运行验证了来源 `src` 匹配、DIRECT 与指定策略组路径、两种模式的拦截开关、MATCH REJECT 和其后规则不进入组段，以及用户已有 `sub-rules` 的保留。

实际 `/connections` 返回 `rule=SubRules`、`rulePayload=(RULE-SET,cf_dg_set_a3,src)`，不包含组段名称。用当前 Go 的精确失败日志正则解析了 19 条真实日志，均保留了来源 provider 信息。因此域名发现应按受管 provider 条件识别，不能只搜 `cf_dg_seg_*`；无需改 Agent 协议。

建议最低稳定内核为 **v1.19.1**：官方 [v1.18.8](https://github.com/MetaCubeX/mihomo/releases/tag/v1.18.8) 引入 RULE-SET 来源匹配，[v1.19.1](https://github.com/MetaCubeX/mihomo/releases/tag/v1.19.1) 引入 inline rule provider，本次已在 1.19.1 验证组合。当前安装脚本固定 1.19.32，不能据此假定历史 Agent 均满足版本要求。版本查询在目标检查/发布边界通过现有接口执行，以 capability 传入生成检查；转换器不联网。已知低版本在生成阶段报错，未知或离线目标保留草稿并明确标示尚未验证，发布前完成版本检查。没有目标 Agent 的订阅/下载注明最低版本要求，不声称核验了接收方内核；其他既有功能要求更高版本时采用更高下限。

实验策略组选 DIRECT 作为出站替身，验证的是选组与分段行为，不代表验证了外部代理服务器。

证据：`/private/tmp/configflow-device-groups-p0-mihomo/evidence.json`、`probe.py`、`runtime-result.json`、`failure-regex-result.json`。

### RouterOS 与基线证据

迁移公式直接与当前 `ros_policy.smart_sources` 做 Python 列表 `==` 比较，包含 CIDR 字符串、分段边界和顺序。3 个固定嵌套案例和 seed=20261024 的 12,000 个随机案例全部一致；随机范围为 `192.168.0.0/24` 内的 /24 至 /32。该实验尚不等于迁移持久化、IPv6 或真实路由器验收。

按用户授权，在 `192.168.0.6` 创建隔离 CHR 7.22.3（VMID 9710），三个测试桥均无物理上联、无宿主 IP，未修改既有路由器虚拟机或网络。使用当前真实 `compute_desired/build_plan/RouterOSDriver.apply` 部署 fake-IP 路由与 NAT 保护，再用 `inspect_desired/ros_policy.build` 生成和部署来源策略包。10 条 TCP 和 3 条 DNS 的稳定复测全部通过：

- 选中真实 IP 目的网段：只有来源内客户端进入代理观测端；同网段与跨网段均保留真实源 IP。
- 未选中目标：即使在来源内也走普通出口，组模式不会自动接管这些连接。
- fake-IP：来源外的同 /24 客户端也可保留真实 IP；跨网段客户端即使在来源内，也可能因普通 fake-IP NAT 保护只覆盖网关 /24 而被改写为路由器 IP。
- DNS：同网段直连和跨网段直连均能看到真实来源；同网段 DNS guard hairpin 劫持后，观测端看到路由器 IP。
- 在同一 CHR 内将 4 条来源按迁移前/拟迁移后重写并读回，条目与顺序完全一致。

Linux network namespace 中的固定应答端点用于观测路由和来源，不代替 OxiDNS/Mihomo 的实际组行为；后者由上述独立真实内核试验验证，实现后还要从新生成器重跑组合验收。首次即时 TCP 探针未命中、未改规则重试后通过的记录已保留，原因未完全确定，不将其定为已证实产品缺陷；正式验收须在启用状态回读后稳定复测。DNS 夹具曾因应答器未绑定 DNS 别名而超时，修正临时夹具后对服务端 peer 日志逐条断言通过。

证据：`/private/tmp/configflow-device-groups-p0-routeros/compare_sources.py`、`result.json`、`chr-source-migration.json`、`chr-probes.json`、`chr-summary.json` 和实际生成包。实验结束后已删除自建 VM 9710、专用磁盘、五个 namespaces、三个桥、观测进程和远端任务目录；`cleanup-result.json` 确认无任务网络残留，原有 VM 和网络接口身份/归属保持一致。既有 ros-chr VM 101 未改动。

已保存四组输入的 OxiDNS 模板/native 和 MosDNS 原始输出共 12 份，以及一份 Mihomo 输出和 SHA256。当前已有 OxiDNS 逐字节回归测试运行结果为 `12 passed`。证据目录为 `/private/tmp/configflow-device-groups-p0-baselines` 和 Mihomo 实验目录。确认计划后首先将固定输入与完整输出纳入仓库 golden 测试，再改生成器。

## 数据结构与统一校验

新增纯模型模块 `backend/common/device_groups.py`，让两个引擎、RouterOS、IP 查询和预览共用一份编译结果。建议结构：

```json
{
  "device_groups": {
    "version": 1,
    "groups": [
      {
        "id": "dg_7f3a1c02d8e94b61",
        "name": "直连设备",
        "note": "由设备策略迁移",
        "members": ["192.168.0.0/25"],
        "mode": "direct",
        "keep_blocking": false,
        "dns": "follow"
      },
      {"id": "default", "name": "默认组", "members": ["192.168.0.101"]}
    ]
  },
  "policy_routing_sources": {
    "include": ["192.168.0.0/24", "192.168.0.101"],
    "exclude_groups": ["dg_7f3a1c02d8e94b61"]
  }
}
```

- 旧配置允许字段缺省；需要完整默认值的位置使用 `null`，不用空列表或 `{}` 猜测是否已迁移。新建无旧策略空间首次启用时显式建立模型；有旧策略必须走升级事务。独立来源不是设备组的派生字段；迁移事务同时初始化二者。
- 组对象与来源对象分别判断是否启用。来源未启用时继续走旧策略推导和原 revision；来源对象即使 include 为空也代表显式的新模式。仅默认组或仅 follow 组不产生无意义的引擎插件。
- `default` 唯一、固定末尾、不可删除，没有 mode/policy/dns/keep_blocking。其他 ID 使用随机十六进制，标签只用稳定 ASCII ID，不受组改名影响。
- 成员只存规范化 IP/CIDR；设备备注统一存 `device_notes`。组备注不代替设备备注。
- 复用 `normalize_client_range`，校验等价重复、跨组相同 CIDR、非法地址、/0、环回、fake-IP、显式基础设施地址；宽网段可以包含基础设施，但生成时必须扣除。IPv4-mapped 地址归一到 IPv4。
- 最长前缀优先，与展示顺序无关。**逐成员减去其他组更具体的成员，再按组合并**；不能先合并同组网段后减例外，否则 `direct /24 ⊃ default /25 ⊃ direct /26` 会丢掉最后重新进入的 /26。
- 编译同时产出「归属网段」和「有效网段」。有效网段在归属网段上扣除已登记的路由器、相关 Agent 和网关地址；引擎用有效网段，RouterOS 排除用归属网段。默认组的完整归属包含未归组地址，不能误当成只有显式成员。
- `exclude_groups` 可引用 default；只在 include 覆盖范围内求其补集，避免先展开无关的整个地址空间。成员总数最多 1024，有效网段和来源展开保留 4096 条上限；两个地址族分开运算。
- `policy_routing_sources.include` 与组成员采用不同限制：必须能保留旧 smart 的 /0 和 IPv6 条目，RouterOS 仍只生成 IPv4。否则无法实现无损迁移。
- proxy 模式只允许已启用策略组；不接受内置策略或独立节点。删除/停用被引用策略组应阻断并列依赖，同 ID 改名在同一事务更新引用。不能默默改成 follow。

`config_repository.py` 的 `_validate_document/_validate_profile` 同时用于读取，因此只作纯校验；写事务在 `_document(write=True)` 最终闸口统一规范化、互斥检查和引用清理，确保普通保存、增量事务、共享资源事务、导入、复制、全量恢复、MCP 和旧接口均经过。

旧空间读取不迁移、不写回新字段。启用组后两处 `device_policies` 必须都为空，整对象保存或 template/native 切换也不能让旧策略复活。删除组时同时清理 exclude 引用，并在预览中展示策略路由影响。

基础设施快照只使用可核实的登记/网络检测地址，不在生成器里临时 DNS 解析。hostname、多网卡或缺失网络信息应明确标示未确认项，不能声称已排除未知地址。相关地址与 Agent 绑定纳入新功能的预览/保存/发布 fingerprint；旧配置空间的 revision 计算保持原样。

## 分阶段实施与验收

| 阶段 | 实施内容 | 进入下一阶段的条件 |
|---|---|---|
| P1A 兼容基线 | 固定无组输出与旧设备策略输出；覆盖 OxiDNS 两模式、Mihomo、MosDNS，并补 Surge/Loon；先落 golden 测试，禁止通过更新期望掩盖变化 | 全部 golden 与相关现有测试通过 |
| P1B 模型 | 新模型、最长前缀、两种网段、基础设施快照、仓储统一写闸口、策略组引用和删除清理、revision | 全部写入入口、跨空间、边界和事务回滚测试通过 |
| P2A 引擎 | 共享拦截编译；OxiDNS 模板/native 受管补丁；Mihomo 顶部分段；不支持格式仅外部警告；域名发现识别 | 无组逐字节不变，新增形态测试和真实内核回归通过 |
| P2B 来源与迁移 | 新 RouterOS 来源读取及同步 fingerprint；显式升级规划、预览、执行和兼容接口 | 嵌套迁移逐条等价，MemoryROS 来源列表一致，迁移失败原子回滚 |
| P3 界面与发布 | 设备组页、IP 查询、升级预览、来源编辑、活动汇总、身份告警、受影响 Agent 顺序发布 | 前端/类型检查、375px、深浅主题与端到端场景通过 |
| 收尾 | 从当前最新 upstream 同步；全量后端、前端、类型和 Go；再跑真实内核；自查 diff、形成中文 PR 草稿 | 测试全绿；提交/推送/开 PR 前获得确认 |

每阶段结束保持其相关测试和已经建立的回归全绿；实现后最终运行所有用户指定命令。若同步引入上游变化，单独复核基线差异来源，不能把功能回归混入刷新快照。

### P2A 的生成约定

1. 新增共享拦截编译函数，对已解析共享规则库、实际启用的 `rule_configs` 只遍历一次，遇到第一条已启用 MATCH 停止；共享库停用的引用不参与。DNS 动作复用现有 rule_dns 的有效值，parked 状态不参与。输出 OxiDNS 域名拦截记录与 Mihomo REJECT/REJECT-DROP 记录，并保留 rule id 和顺序供预览。
2. 两层分别认定：DNS 动作为 refused/nxdomain/nodata/blackhole 的域名部分；Mihomo 策略为 REJECT/REJECT-DROP。排除 MATCH、其后行、停用规则及 DNS 应答 IP 拦截；混合 Classical 只取可用域名部分进入 DNS 组段。
3. 当前 OxiDNS provider 按相邻 route 聚合，而旧 `_selected` 本身没有 MATCH 截止。不能简单复制一个混有 MATCH 后条目的聚合 provider。保留来源映射；整份 provider 都合法时复用，否则为合法子集生成受管 provider，使用已有格式和 `route_exec`，不复制被截止条目、不新增上游。
4. OxiDNS 在现有设备策略钩子的位置插入 `jump cf_dg_dispatch`，位于 hosts/AdGuard 等全局前缀后、head 自定义匹配和所有缓存前；扩展 `_prefix_end` 对新钩子的识别。每个非 follow 且有效网段非空的组生成 ip_set 和段，使用前述明确终止的兜底。`cf_dg_local` 与旧 `cf_device_direct` 插件逐项一致，不缓存、不触发 ros_*；direct_full 是主动选项。
5. native 复用受管补丁的完整性、位置、重名与额外引用检查；补丁可重复应用、替换和清理。按所选模式校验 forward_local、proxy_dns_seq 或 china_dns 等实际依赖链存在、类型和调用结构可兼容；缺失时拒绝并解释，不补造用户模板链。还要核对分发前的已识别前缀实际引用的插件，不能只凭 custom_hosts 等标签跳过，若有缓存或无法证明安全的自定义调用则拒绝组补丁并说明原因。新旧两套钩子不得同时存在。keep_blocking=true 用 `OxidnsRoutingLocked` 拒绝，包括从模板切换到 native 或整对象导入的路径；不重写 native 识别器来偷偷开放该能力。现有 `apply_common_settings` 也会通过 prepare_editor 转成 native，这类保存必须提前显示相同限制，不能静默关闭组拦截或生成不同步副本；普通规则 DNS 在模板模式下的编辑仍保持模板路径。
6. Mihomo 只在主表最前面插入 `SUB-RULE,(RULE-SET,cf_dg_set_<id>,src),cf_dg_seg_<id>`。inline ipcidr 使用有效网段；段尾只为 MATCH,DIRECT 或 MATCH,策略组。保持全局规则后缀和用户 sub-rules。底稿解析后、managed providers 覆盖前检查 cf_dg_/cf_scope_ 冲突，同时检查规则集命名；不嵌套 SUB-RULE。
7. MosDNS、Surge、Loon 不改序列化输出，警告走预览/生成响应或现有反馈通道，不能往下载文本插入注释导致字节变化。只有实际存在组配置时提示尚不支持。
8. 域名发现保留统计展示，仅将受管组 SubRules 的贡献排除出 uncovered/failing/pending 建议；同域名的默认组 Match 仍可正常产生建议。用户自定义 SubRules 不一概忽略。

### P2B 的 RouterOS 与迁移约定

新版来源 = include − exclude_groups 的 IPv4 **归属网段** − 网关地址。未迁移时保留当前 smart_sources、source_revision、draft_revision 和自动同步控制值。新版 source_revision、工作区 draft_revision 和 `rule_sync.controls` 使用同一来源 fingerprint；组名、备注或 DNS 上游变化不应误报来源改变。

显式升级分为预览与执行，两者使用同一纯规划函数：

1. 锁定目标配置空间快照，读取旧策略和当前有效设备备注。direct 行创建一个「直连设备」组，mode=direct、keep_blocking=false、dns=follow。
2. 位于 direct 范围内的 smart 例外进入 default.members；全部 smart 行进入 include；不建「智能分流」组。有直连组时加入 exclude_groups。
3. 保存迁移前有效显示的设备备注；同时清空 `oxidns.device_policies` 和 `oxidns.template_settings.device_policies`。模板保持模板模式，不调用会隐式转 native 的旧写入流程；native 在完整性检查通过后原子替换钩子/插件。
4. 预览列出新增组、显式例外、规范化/阻断项、来源列表前后逐条对照、OxiDNS/Mihomo 实际生成差异、位于 direct 范围的基础设施与样本占比、需发布的 Agent。
5. 预览明确三项行为：直连 DNS 链保持原样；Mihomo 新增连接层 DIRECT；基础设施查询改按全局处理。native 手工链冲突或旧 direct 含新模型禁止的显式地址时阻断迁移，保留旧数据，不自动丢弃成员。
6. 执行重新核验内容 revision、基础设施/Agent 绑定和预览 fingerprint，重新规划后在一个仓储事务内落盘。共享规则或策略组变化也使旧预览失效；事务内不请求远端。重复执行不创建第二个组。
7. 迁移只保存草稿，不写 RouterOS 已发布 manifest，不直接改变运行中来源。模板模式迁移后可一次性提示打开拦截，但不自动打开；native 不显示该入口。
8. 旧设备策略 GET 保留一个版本，迁移后返回 migrated=true、editable=false 和只读投影摘要；proxy 无法被 smart/direct 完整表达，必须标明。旧 PUT 返回 409 并指向新页。
9. 旧 policy_workspace 真正修改 policies 时返回 409；只改路由器字段仍可保存。新工作区编辑 include/exclude_groups，不再用 DNS mode 表达 RouterOS 来源。

## API 与界面

建议新增 `backend/routes/device_groups.py`：

- `GET/PUT /api/profiles/<id>/device-groups`：模型、来源、备注、revision、capabilities、归属/有效网段、基础设施、受影响 Agent、告警。PUT 写仓储事务，返回规范化结果。
- `POST .../device-groups/preview`：草稿生成差异、实际复制的拦截及 MATCH 后排除项、策略路由增减、受影响样本与发布目标。
- `POST .../device-groups/migration/preview` 和 `POST .../device-groups/migration`：显式升级，不能信任客户端提交的规划结果。
- `GET .../device-groups/lookup?ip=`：胜出组/模式、命中成员、覆盖关系、基础设施排除原因与策略路由状态；第一步不返回规则作用域信息。
- `GET .../device-groups/activity`：跨本空间 OxiDNS Agent 合并 IP/count，逐 Agent 返回可用、离线、需升级、recorder 关闭或失败状态。短时缓存、限定并发与数量，采集后再验绑定；无 last_seen，不把重复观测声称为唯一请求。

新页 `/device-groups` 使用 `PageHeader`、`SectionCard`、`.cf-table/.cf-mobile-records`；默认组固定最后。编辑 Sheet 桌面 560px、手机全屏，模式控件用 Segmented；代理组只列启用策略组。复用 `OxiDNSClientSelect` 并支持聚合数据。保存前预览、退出保护、删除影响、成功/失败/待激活反馈统一使用 `@/lib/feedback` 和现有发布组件。

活动汇总复用 `oxidns_monitor.clients()`，Agent 至少 1.7.1-go，每台最多 10,000 条样本。显示「样本内 N 次查询」「样本内未发现查询」「无查询记录」，不把无记录断言为未生效。基础设施有样本时客观展示数量/比例；没有有效样本时不显示比例。DNS guard 开启常驻说明；NAT 复用真实有序规则评估，未检查或无法确认不能宣称保留了源 IP。现有 `assess_bypass` 固定检查网关所在 /24，不能用其 synced 状态证明跨网段成员安全；新增按组有效网段、fake-IP/所选真实目的路径调用 `preserves_sources` 的只读诊断，保留旧调用默认行为，不自动扩展 NAT 规则。通过用户触发的独立诊断或现有差异检查获取，返回 checked_at/state/reason 和检查范围，使用有界短缓存；过期、失败与未检查均可见，不能用 last_probe 代替，不在保存事务中检查远端。

直接可见的当前警告包括：退出策略路由；全部代理未覆盖来源或连接来源可能被 NAT；IPv6 路径限制及地址轮换；仅 Mihomo 部署的 DNS 限制；完整参数拦截锁；换组后 DNS TTL/重新联网的延迟。RouterOS/选择性网关的全部代理必须显示「只有进入 Mihomo 的连接才能按组处理；直接访问 IP 或未被接管的目标可能不经过 Mihomo」，完整 TUN 按实际接管范围诊断，不套用选择性网关结论。fake-IP 来源外是否被 NAT 以实际规则评估，不能一律断言。静态租约建议、全局动态分类/fake-IP 池、自定义 listener 绕过主表等原理放帮助。正常选项遵守 optionCopy 的 15–25 字简述、50–100 字适用条件/工作原理说明；不把必须执行的操作藏进问号。

发布复用现有 Agent 事务：先全部受影响 Mihomo，再 OxiDNS，失败、未知或待激活时暂停后续。发布目标与快照固定到实际绑定空间，不因全局空间切换而换目标；新 API 补入 scopedPath 的请求追踪。RouterOS 需要重新发布的状态单独可见，不自动把路由器发布混进保存事务。

## 涉及文件

以下符号和行号已按 `c602993` 重新定位；实施时仍以实际代码为准。

| 范围 | 现有文件与落点 | 预计新增 |
|---|---|---|
| 模型/仓储 | `backend/common/config_repository.py` PROFILE_FIELDS:170、_document:681、_update_profile_locked:914、_validate_profile:1981；`common/config.py`；`common/device_notes.py`；`common/oxidns_settings.py` | `common/device_groups.py`、`common/device_group_migration.py`、共享拦截编译模块 |
| 引擎 | `backend/converters/oxidns.py` generate:261、最终旧补丁:470；`common/oxidns_device_policy.py` patch_document:101；`common/oxidns_common_routing.py` _prefix_end:72/受管资源清理；`converters/mihomo.py` providers:746/规则:753；`common/rule_dns.py` route_exec；`routes/generate.py`、`agents/deployment_bundle.py` 生成/发布边界 | `common/oxidns_device_groups.py` 等独立补丁模块 |
| 路由器 | `backend/routers/ros_policy.py` subtract:104、smart_sources:116、inspect_desired:136；`policy_workspace.py`；`rule_sync.py` controls:35；`routes/routers.py` policy-settings:490 | 新来源编译 helper，避免把归属算法耦合到路由驱动 |
| 接口与依赖 | `backend/routes/agents.py` 旧策略接口:1859；`routes/proxy_groups.py` 修改/依赖:171–236；`routes/oxidns.py` 整对象/模板切换；`routes/config.py` 兼容导入；蓝图注册入口 | `routes/device_groups.py`、`agents/device_group_activity.py` |
| 发现/身份 | `backend/utils/domain_discovery.py` summarize:56；`domain_discovery_service.py` load_domains:392；复用 `agents/oxidns_monitor.py` clients:591、`routers/routeros_nat.py` assess_bypass | 受管分段识别与活动缓存/告警辅助函数 |
| 前端入口 | `frontend/src/navigation.ts`、`router/index.ts`、`lib/icons.ts`、`api/index.ts`、`views/Rules.vue`、`views/OxiDNSMonitor.vue` | `views/DeviceGroups.vue`、设备组编辑/预览/IP 查询组件及公共 optionCopy 文案 |
| 旧前端兼容 | `components/agents/OxiDNSDevicePolicies.vue`、`OxiDNSClientSelect.vue`、`components/routers/PolicySourceEditor.vue`、`PolicyRoutingPanel.vue`、`composables/usePolicyRoutingWorkspace.ts` | 按迁移状态切换新来源和只读摘要，复用发布逻辑 |
| 文档 | `doc/oxidns-device-policies.md`、相关 RouterOS/DNS 说明；按需要更新 DESIGN.md 的新页面约定 | 设备组使用与迁移说明，不新增第二步承诺 |

若某个既有入口只需共用仓储校验，不为凑覆盖而逐个重复写校验。保持新增功能分模块，避免扩大旧生成路径改动。

## 测试清单

1. **逐字节 golden**：无字段/null、无实际分发组、旧 smart/direct、模板/native、缓存开关、规则动作/指定上游、DNS watchdog 的下发路径；Mihomo 自定义底稿/providers/sub-rules、MosDNS 实际路由、Surge/Loon 输出。不支持格式警告不改变配置文件字节。
2. **模型**：IPv4/IPv6/mapped、两种三层嵌套及多层重入、相同 CIDR 冲突、默认组显式例外、基础设施落入宽网段、空有效网段、fake-IP、自定义 fake-ip-range、1024/4096 边界、无效 proxy policy、策略组改名/删除/停用、default 的来源排除。
3. **仓储**：全部保存/导入/复制/恢复/MCP 路径，同事务清理，跨空间隔离，读不迁移，旧模板不复活，非法写入不落盘，基础设施/绑定变化使旧预览失效。必要时调整新版受保护发布快照，旧空间 revision 不变。
4. **拦截/引擎**：两层共享认定但独立结果；四种 DNS 拦截；REJECT/REJECT-DROP；停用 MATCH 不截断、启用 MATCH 截断且本身不复制；聚合 provider 跨截止点污染；Classical 混合规则；优先级遮挡；IP 拦截不进入 DNS 段；无整组 reject；head 自定义 cache 仍在分发后。
5. **native**：补丁稳定/幂等、去旧加新、受管插件修改/移动/重名/额外引用拒绝、目标链缺失/类型不符在保存/迁移/生成拒绝、前缀标签被换成缓存或不透明调用时拒绝、切换与导入 keep_blocking 拒绝、失败事务回滚、规则路由识别不把新钩子当自定义行。
6. **RouterOS**：两种三层嵌套、五层交替、随机模型、IPv6 忽略、网关/路由器/Agent 在 direct 网段或 smart 例外内；来源列表逐条/顺序完全相等；MemoryROS 发布前后 src address-list 内容一致；旧 revision 不变、新 fingerprint 不受 DNS 设置影响、自动规则同步能检测新来源变化、退出来源预览告警。
7. **迁移/API**：预览零写入、完整差异与阻断原因、模式不被偷偷切换、备注显示一致、两处旧策略清空、双钩子互斥、重复迁移、CAS/共享资源/绑定冲突、旧 GET 只读/PUT 409、旧 router-only 请求可用、导出导入复制往返。
8. **活动/发现/发布**：跨 Agent 样本合并、上限/部分失败/过旧/离线/recorder 关闭、同分母比例、缓存与改绑隔离；真实 SubRules provider 形态及失败日志字段；组 DIRECT 失败不产生建议、默认组贡献仍产生建议；先 Mihomo 后 OxiDNS、目标改绑/离线/待激活/重试不重复发布。
9. **前端与视觉**：表单/模式/锁定/删除/覆盖、长名称/IPv6/长错误/空态/失败重试、未保存保护；375px 无页面横滚、44px 触区、Sheet 全屏/正文滚动/底栏可见；深浅主题和 clay/kraft/sky、键盘与读屏、无动效/减少动效。Vitest 之外使用真实浏览器检查。
10. **真实内核最终验收**：实现后直接使用新生成器输出，不再临时注入。OxiDNS check/start/dig 验证不同组、默认例外与未归组、开关、双向查询顺序缓存隔离、前缀和明确终止；Mihomo 1.19.1 与当前安装版本做 -t 和来源运行验证。隔离 RouterOS 验证来源/NAT/目标路由/DNS guard，不以 mock 结果代替实机。

预计新增 `backend/test_device_groups.py`、`test_device_group_migration.py`、`test_device_groups_api.py`、`test_device_group_activity.py`、生成 golden 和真实核心专项；扩展仓储、旧策略、rule_dns、RouterOS、rule_sync、domain_discovery 现有测试。前端新增设备组专项并扩展已有 policy-workspace、policy-editors、oxidns-client-select、navigation 测试。

最终命令（在任务 worktree 根目录执行）：

```sh
export OXIDNS_TEST_BINARY=/Users/sunbroke/program/configflow-handoff/device-groups/ref/oxidns-bin/oxidns
/Users/sunbroke/program/configflow/.venv/bin/python -m pytest -q -p no:cacheprovider backend
(cd frontend && npx vitest run)
(cd frontend && npx vue-tsc --noEmit)
(cd backend/agents/go-agent && go test ./...)
```

## 对方案第 9 节的处理建议

| 问题 | 本次建议 |
|---|---|
| 1 基线 | 已从合入 #352 后的最新 main 新建；实施和开 PR 前继续用 cf-task sync |
| 2 第二步 spike | 本次不实现，不把其未知语法引入第一步 |
| 3 两层同源 | 共用组编译与一次拦截认定，保留 provenance；真实两核分别验收 |
| 4 归属顺序 | 采用最长前缀；组顺序只影响展示；默认例外参与同一算法 |
| 5 拦截优先 | 采用方案建议；预览显示复制项、被截止项及可能覆盖原非拦截允许项 |
| 6 全局副作用 | 默认直连不用 ros_*；follow 组仍共享动态分类、fake-IP 池，明确说明 |
| 7 身份丢失 | 只按可证据化样本/NAT评估告警，不保证所有设备真实来源可见；不改 DNS guard 的监听端口架构 |
| 8 native | 支持受管组补丁；keep_blocking 一律锁定，迁移不自动打开 |
| 9 可观测性 | 接受 SubRules 展示，识别受管来源 provider；不引入 rematch 和更高版本依赖 |
| 10 不支持格式降级 | 第一步输出原样并警告；第二步如何省略 only 规则留到其计划确认 |
| 11 组默认 DNS | 留后续，本次不增加 default_dns 或组私有上游 |
| 12 忽略/单设备/入站 | 未归组走默认；单设备用单成员组；不做按入站绑定 |

建议本次确认包括上述第 4/5 条、1.19.1 版本下限、原示例兜底的明确终止修正，以及迁移遇新模型非法地址时阻断而保留旧配置。确认后从 P1A 开始。
