# OxiDNS 配置与发布

ConfigFlow 将 **MosDNS** 和 **OxiDNS** 作为两个独立生成目标。它们共享规则仓库，但分别保存上游、规则选择、缓存和特色功能设置。修改 OxiDNS 不会改写 MosDNS 设置。

模板按 OxiDNS **v1.6.1 full** 的配置语义验证；精简构建可能缺少查询记录、IP 优选或 RouterOS 等插件。不同版本的额外参数请以 [OxiDNS 官方文档](https://oxidns.org/) 和目标二进制的 `oxidns check` 为准。

## 第一次配置，先做这几步

1. 先选择要使用的配置空间，在规则库准备直连和代理所需规则。
2. 打开「配置生成 → OxiDNS → 设置」，从「常用设置」填写直连 / 代理 DNS 上游。使用 Mihomo 上游时，填写 OxiDNS 能访问的实际 IP 和 DNS 端口。
3. 保存草稿，生成预览并检查错误；备用 DNS 留空表示不使用回退，不会自动补成直连 DNS。
4. 手动部署下载完整 ZIP；远程管理选择同空间的 OxiDNS Agent 发布，等待最终成功。
5. 想看查询历史时，再开启管理 API 和查询记录，重新发布后进入「总览 → OxiDNS 面板」。

| 操作 | 是否改变运行设备 |
| --- | --- |
| 切换常用设置、完整参数或 YAML | 否，三个视图编辑同一份草稿 |
| 保存草稿 | 否，只保存面板数据 |
| 生成预览 / 下载 ZIP | 否，手动部署仍需将完整包放到目标机 |
| 发布并激活 Agent 配置 | 是，校验和健康检查成功后生效 |
| 更新规则 | 更新已部署规则内容，不下发其他草稿 |

## 使用流程

1. 在「当前配置 → 配置生成」选择 **OxiDNS**，打开参数设置。
2. 在“常用设置”调整核心、分流规则、DNS 上游、缓存、特色功能、WebUI / 日志与高级匹配；“完整参数”按功能分类编辑全部插件，“YAML”编辑同一份文档。三种入口共用一次保存。
3. 原模板中的选项已合并到常用设置，不再有独立模板工具。“填入已验证性能参数”会直接更新当前草稿，最后统一保存。表单读取当前原生文档的实际值；省略的参数显示未配置，不沿用旧模板值。
4. 点击“保存草稿”保存当前编辑。参数问题会按字段位置提示，可先保存，修正后才能预览、完整下载或发布。预览显示最终 YAML；下载返回 `oxidns-config.zip`，包括 `config.yaml`、已转换的订阅规则、自定义 hosts 和启用的 AdGuard 文件。
5. 在 Agent 中选择类型为 OxiDNS、配置空间相同的目标进行发布。Agent 先准备完整文件、校验，再切换并检查服务；失败时走既有事务回滚流程。

### 统一配置与旧版本迁移

打开旧配置时，以原来生效的内容为准：快捷模板生成当前原生文档，已使用完整配置的文档保留原文。另一份未生效的完整配置草稿保留为“旧草稿备份”，可下载查看；打开、切换视图或编辑都不会自动保存。

常用设置按用户实际修改的字段更新当前原生配置；未修改的参数、未知字段、手工插件、附加文件和仍被引用的资源都保留。若执行链、规则来源或值类型无法安全映射到简化表单，该项显示“自定义配置”，可直接跳转完整参数编辑。连续输入按顺序同步到草稿；未完成的数字输入或 YAML 语法错误会保留在编辑器并阻止保存，参数语义问题则允许保存为草稿。取消恢复已保存配置。点击“保存草稿”不会自动发布。

备用 DNS 留空表示不使用代理 DNS 回退，不再继承直连 DNS。常用设置中删除最后一个备用地址，会自动把代理处理链改为仅使用代理 DNS；重新填写有效备用地址时自动接回回退链，没有额外开关。完整参数中删除标准 `forward_fallback` 的最后一个上游或整个插件，也会同步清理其备用链；填写该标准插件的上游时会接回回退。国内直连配置不受影响。自动联动只处理可识别的 ConfigFlow 标准链，自定义调用或外部 include 引用会提示处理；直接编辑 YAML、打开已有原生配置及修改无关字段不会自动改写执行链。

常用设置与完整参数共用后端的功能依赖处理。完整参数中的“添加 / 恢复功能”按标准功能选择主缓存、探测缓存、查询记录、IP 优选、RouterOS、动态域名、Hosts、AdGuard 或规则更新，由后端恢复原参数或使用默认参数，并补齐所需插件和调用；必填项仍需填写后才能发布。删除这些标准插件时，会同步处理对应调用和受管资源。直接添加使用标准 tag 和类型的插件定义，也会联动对应功能，并保留本次填写的参数。备用 DNS 需要填写有效地址才接回回退链，添加空占位或辅助节点不会恢复已清空的地址。

“添加自定义实例”用于额外实例，不推测它应插入哪条处理链；可在完整参数内编辑序列的执行条目或引用字段，无需切换 YAML。插件改名同步修改明确引用。仍被自定义链共用的插件不会被直接删除，表单会定位剩余引用，允许继续编辑、重试或撤销未同步修改。同步完成前禁止保存和发布，切换配置后会忽略旧请求结果。

主缓存、探测缓存和缓存持久化关闭后，其未完成的子参数不再阻塞保存；重新开启时继续显示原输入和错误。关闭标准规则自动更新时，会停放无其他调用者的下载、重载和执行序列，保留参数供恢复使用；空的更新超时按未设置处理。自定义定时任务或其他调用者仍使用的组件会保留。

### 条目编辑与 YAML

DNS 上游、自定义匹配、Hosts、查询类型及特色功能中的列表默认使用条目编辑，可添加、删除和调整顺序；各区块可以切换 YAML 批量编辑。上游可展开填写 bootstrap、dial_addr 和连接流水线参数，未在表单中展示的参数会保留。

高级配置可添加全局参数和自定义插件，插件参数支持对象、列表、文本、数字、布尔值与空值，亦可切换完整 YAML。切换模式保留字段、类型和顺序，条目编辑后的 YAML 注释与排版可能重新生成。YAML 语法或根节点结构错误会保留在编辑器并阻止保存；可解析的参数错误允许保存草稿，必须修正后才能发布；在设置页切换标签不会清除错误草稿，取消并重新打开则恢复已保存设置。

Hosts 每个条目填写 `域名规则 IP [更多 IP]`，例如 `domain:service.test 192.0.2.10 2001:db8::10`。IPv4、IPv6 和多个地址可放在同一条；同一个域名规则的后行会覆盖前行。明确的旧式 `IP 域名 [别名]` 仍可导入，无法识别的输入会按行号报错。AdGuard 自定义规则保留整段文本编辑，以保留其规则语法和注释。

订阅 URL 只返回 YAML，不能替代首次部署所需的完整 ZIP。手动部署时，解压到 OxiDNS 的工作目录后先执行：

```sh
oxidns check -c /etc/oxidns/config.yaml -d /etc/oxidns
```

`-d` 必须与实际服务的工作目录一致。当前原生 Agent 要求工作目录等于配置文件所在目录；官方 Debian 布局常为配置放在 `/etc/oxidns`、工作目录为 `/var/lib/oxidns`，此时原生 Agent 安装/发布会明确拒绝，需要先将核心服务的 `-d` 与配置目录对齐。Docker Agent 已使用一致目录。API TLS 证书、RouterOS CA 和 WebUI 静态资源需要在目标机安装；配置包不包含二进制或这些外部资源。Docker Agent 镜像提供的 WebUI 目录为 `/usr/share/oxidns/webui`。

## 已验证模板

| 项目 | 默认值 | 作用 |
| --- | --- | --- |
| Worker 线程 | 2 | 可按目标机器 CPU 调整，不能为 0 |
| 国内 / 国外 / 备用并发 | 2 / 1 / 2 | 各 forward 的上游竞速并发 |
| 主缓存 | 10240 条 | 用于国内直连响应，`short_circuit: true` |
| Lazy cache | 300 秒 | 允许在总保留期内返回过期正响应并后台刷新 |
| 探测缓存 | 2048 条 | 只缓存国内探测结果，不启用 lazy |
| 缓存持久化 | 关闭 | 可独立开启；升级或变更策略后应考虑旧缓存有效性 |
| Provider 合并 | 开启 | 只合并相邻、策略相同的规则，保留直连/代理顺序 |
| 轻量指标 | 开启 | 不默认启用逐条查询日志 |
| DNS 服务 | UDP + TCP，端口 53 | TCP 用于截断响应回退等场景 |

Lazy TTL 不是“原 TTL 再加 300 秒”。v1.6.1 的总保留期为 `max(原始 TTL, lazy_cache_ttl)`；例如原 TTL 60 秒时，可能在第 60–300 秒返回过期响应；原 TTL 600 秒不会额外延长。负响应不会按正响应的 stale 规则续用。

国内探测使用独立子序列：

```yaml
- tag: local_probe_cache
  type: cache
  args:
    size: 2048
    short_circuit: false
- tag: local_probe
  type: sequence
  args:
    - exec: $local_probe_cache
    - matches: ['!has_resp']
      exec: $forward_local
    - exec: return
```

主序列通过 `jump local_probe` 调用，随后继续按响应 IP 分流。不要改成 `goto local_probe` 或将探测缓存设为 `short_circuit: true`；那会改变父序列继续分类的行为。探测缓存不保存最终的代理/Fake-IP 响应。

### 分流与扩展匹配

规则按照配置空间中的顺序执行：先域名规则，再对未命中请求进行国内探测和 IP 分类，最后走默认转发。普通规则和订阅规则使用同一顺序。混合 classical 订阅拆分为域名、IP 两部分；正则中的逗号保留原义。

机器标签和规则文件名使用稳定的 ASCII 标识，中文名称、重复名称或带路径符号的名称不会成为 OxiDNS tag 或不安全的文件路径。

高级匹配可引用模板提供的 `china_dns`、`remote_dns`、`proxy_dns_seq` 等序列。需要绕过常规缓存、按真实上游响应返回的专用域名，可增加域名条件并执行 `goto remote_dns`；实际是否为真实 IP 取决于该上游。OxiDNS v1.6.1 没有原生 Fake-IP 插件，现有 Fake-IP 应继续使用外部上游。

## 特色功能

特色功能默认关闭，可分别启用；开启后会增加相应资源消耗。

| 功能 | 模板行为与可配置项 |
| --- | --- |
| 查询记录 `query_recorder` | 放在主入口，记录 SQLite 查询历史；可调整保留天数、队列、批量写入和读并发，默认 7 天 |
| IP 优选 `ip_selector` | 只接入国内真实 IP 分支；默认后台探测、`top_n: 0` 只排序，位于缓存之前，缓存保留原始应答 |
| 动态域名集 | 可通过 OxiDNS API 管理，支持初始规则与直连/代理目的地；持久文件放在 `state/`；显式域名规则优先于学习结果 |
| 自动学习 `learn_domain` | 可选，仅在明确命中 IP 分流规则的分支学习，不对未分类的默认结果学习；默认只学习成功、有 answer 的 A/AAAA 完整域名 |
| AdGuard DNS 规则 | 支持内联文本和 HTTP(S) 订阅，使用 `question` 匹配以保留 `dnstype`；可选 NXDOMAIN、NODATA、空地址或 REFUSED |
| RouterOS address-list | 仅处理国内真实 IP 响应；支持凭据、TLS、队列、异步和 TTL 参数 |
| RouterOS route | 同上，需要预先存在的路由表以及 IPv4/IPv6 网关；插件不会创建路由表、routing rule 或默认路由 |
| 原生规则更新 | 生成 `cron` → `download` → `reload_provider` 维护链，支持时区、时间表达式和下载出站设置 |

AdGuard 支持的是 DNS 规则子集，不等同于完整 AdGuard：URL/路径、页面元素、`dnsrewrite`、`$client`、`$ctag` 等不受支持。

动态规则的初始 `bootstrap_rules` 由 OxiDNS 处理。`state/` 中的学习文件、查询数据库和缓存 dump 不纳入配置包，避免配置发布覆盖运行数据。需要清空学习结果时，应通过 OxiDNS 的动态 provider API 操作。

### 原生定时更新的边界

原生下载对**每个文件**使用临时文件后替换，单项失败会保留该项旧文件并继续其他项目；随后 provider reload 读取新旧混合的文件集。它不是整套规则的原子事务，也不提供 Agent 配置发布的整包回滚保证。更新内容无法解析时可能导致 provider reload 失败，应检查 OxiDNS 任务状态和日志。

默认关闭此功能。Agent 的手动「更新规则」和已有规则集内容自动同步也会调用这条原生维护链，可以与它配合使用，不必因此关闭原生定时更新。已有 systemd timer 或其他外部更新脚本时，应避免它们与核心同时写同一组文件。需要整套预校验、健康检查和失败回滚时使用 Agent 发布流程。

Agent `1.8.38-go` 起，已发布的原生下载规则内容刷新后，仍可继续「跟随 Mihomo DNS」和手动回滚；主配置及其他受管文件的检查保持严格。**回滚会恢复发布前备份的规则版本**，具体范围见 [发布与回滚](AGENT_OXIDNS.md#手动回滚已成功发布的配置)。

### 手动更新规则内容

OxiDNS 面板顶部和 Agent 卡片提供「更新规则」。需要 Agent `1.8.30-go` 或更新版本、已启用的本机 HTTP 管理 API，以及支持手动 download 和 provider reload 接口的 OxiDNS 核心。标准配置先在「常用设置 → 特色功能」开启「原生规则定时更新」并发布一次，建立规则下载与热重载链；之后修改已引用的共享规则集内容，可以直接点击「更新规则」。未配置维护链、核心版本不支持或 API 不可用时，页面会给出原因。

Agent 读取实际部署的配置，确认核心正在运行该版本后，调用原生下载接口；全部下载成功后，逐个热重载对应 provider。页面显示下载与重载数量，并等待最终结果。更新只处理已有规则文件，不推送整套配置、不重启核心、不清空 DNS 缓存，也不改变定时计划。新增或移除规则集、修改单条内嵌规则、策略和顺序，可使用下述规则自动同步或手动发布配置。

下载未全部成功时，本次不会继续热重载；已写入的文件不会自动还原。热重载部分失败时，页面明确报告失败，不把已受理或部分完成当作成功。任务与 Agent 发布、重启共用服务操作锁；原生定时任务仍使用核心自身的并发保护。离开页面后任务在 Agent 继续，返回可查询当前结果；Agent 重启会丢失内存中的任务记录，不能据此认定更新成功。

### 按设备自动同步 ConfigFlow 规则

在对应 Agent 卡片开启「规则库自动同步」或「单条规则自动同步」。两个开关分别保存，默认关闭；需要 Agent `1.8.30-go` 或更新版本，并先成功手动发布一次配置作为同步基线。尚无基线、设备离线或不能热加载时，页面会显示原因，不会将等待状态当作同步完成。

规则库开关同步已启用、已引用的共享规则集内容及新增引用；单条规则开关同步当前配置选用的内嵌规则。只向共享规则库添加未引用条目不会触发同步。已有规则集内容变化通过核心原生下载与 provider reload 更新，要求运行配置具有可识别的下载、重载维护链。新增引用或内嵌规则等结构变化会基于成功发布的配置生成只修改规则范围的候选，再通过原生配置 reload 应用，不携带 DNS 上游等其他未发布草稿。

正常自动同步不会以重启服务作为热加载失败的替代方案；不能安全合并的自定义原生分流图会阻止自动同步，需手动检查并发布。候选已经切换后若健康检查失败，恢复旧配置时可能重启旧服务。此功能只检测 ConfigFlow 内保存的变化，不轮询远端 GitHub 或规则订阅内容；远端 URL 未变而内容更新时，继续使用原生定时更新或「更新规则」。完整流程见 [Agent 自动同步说明](AGENT_OXIDNS.md#每台-agent-的规则自动同步)。

### 自动同步的增量下载

Agent `1.8.35-go` 起，规则自动同步会在能定位已部署下载清单时，只下载有变化的文件，并热重载引用这些文件的 provider。旧 Agent、无法定位的文件或不支持的清单会退回全量规则更新。

手动「更新规则」和原生定时更新仍是全量，不要把增量下载理解为所有更新入口都只请求一个文件。

## 清理运行中的 DNS 缓存

在「总览 → OxiDNS 面板」选择设备，点击「清理缓存」，确认设备名称和将清理的缓存实例。需要 Agent `1.8.36-go` 或更新、已部署的明文 HTTP 管理 API，以及支持 flush 的核心。

此操作按实际部署配置里的 `type: cache` 发现全部缓存，实例改名也能识别。它不发布草稿、不重启核心、不删除查询历史或学习文件；清理后新查询需要重新访问上游。发布、重启或恢复期间会提示忙碌，部分失败会列出已清理的实例，不能据此认为全部完成。

## API / WebUI 和日志

API 默认关闭。启用后填写监听地址：`127.0.0.1:9199` 只供本机访问，`0.0.0.0:9199` 可供 IPv4 局域网访问。用户名和密码须同时填写；填写后生成 HTTP Basic Auth。WebUI 目录需指向已安装的静态资源，启用 API 本身不会下载 WebUI。

API TLS 证书与私钥必须成对配置。启用 TLS 时，Agent 自动健康检查使用进程状态和实际 DNS 应答，不自动探测管理 API，避免对自签证书、域名证书或 mTLS 做不安全的跳过验证；显式填写的 HTTPS 健康地址仍按系统信任库验证。RouterOS TLS 则为独立的 `args.tls` 对象；对象存在即启用 API-SSL，`ca` 与 `insecure: true` 不能同时使用。

日志默认仅标准输出、`info` 级别；可配置日志文件和轮转。查询历史属于单独的 SQLite 功能，不依赖日志文件开关。

## 高级 YAML

主编辑器的 YAML 就是当前配置；修改会同步到常用设置和完整参数。未知字段、插件和原生值类型保留。保存草稿仅要求安全、可解析的对象 YAML 与合法的附件元数据；重复 tag、参数类型/范围及必填项错误由统一规则提示并阻止发布。`include` 可以引用“附加文件”中的配置，完整包会展开并校验。

全局扩展与自定义插件直接通过完整参数或 YAML 编辑；不再维护独立的“基础配置扩展”输入。`include` 与附加文件仍可在同一配置中编辑。

自定义插件的外部规则 `files`、`file` 或 RouterOS `persistent.files` 必须包含在完整包中。通常建议使用共享规则仓库或对应 provider 的内联 `exps` / `ips`；仅填写本机路径不能自动上传该文件。

## 校验与发布边界

常用设置、完整参数和 YAML 调用同一个后端校验器。规则目录位于 `backend/schemas/oxidns/`，按仓库收录的 OxiDNS 1.6.1 插件定义和全局参数检查；前端不再维护另一套参数语义规则。检查不补写默认值、不清空合法 `{}` / `[]` / `null`，不删除未知字段。未知插件/扩展字段显示警告；核心明确拒绝未知字段的结构会报告错误。目标核心版本或扩展不同的情况，仍需以实际校验和运行结果为准。

保存草稿不依赖 Agent 在线或参数校验成功。发布时先检查最终配置，再由目标 Agent 在暂存目录执行已安装核心的 `oxidns check`；任一步失败均不替换运行配置。分步暂存后再次激活也会重跑当前核心检查，避免沿用旧环境下的通过结果。服务器同时记录与发布包及规则版本绑定的校验结果；旧版本暂存、缺少结果或规则已更新的候选需重新推送校验。启动后仍执行健康检查，失败自动回滚。

官方 1.6.1 的 `check` 只检查 YAML、全局结构、插件类型和依赖，**不初始化插件，也不检查所有插件参数**。例如负数缓存容量和空的 forward/http_server args 可能通过核心 check，因此发布需要统一规则补漏，不能只相信退出码 0。见 [官方 check 说明](https://oxidns.org/cli/tools/#check)。

规则覆盖静态配置参数，无法提前证明外部服务连通、证书内容、目标权限、环境变量展开后的动态值或未来自定义插件全部可运行；动态占位符会保留并标为待目标环境确认。部署成功仍以核心启动及健康检查结果为准。新增官方参数需要更新一次后端规则及对应测试，已有三个编辑入口同时生效。

## 接口与 MCP

- `GET /api/oxidns/settings`：返回当前配置空间的完整默认化设置。
- `POST /api/oxidns/settings`：保存完整草稿，验证 YAML/附件结构；返回 `{success, settings, draft: true}`。
- `POST /api/oxidns/editor`：准备统一编辑草稿，不保存；返回 `{settings}`。
- `POST /api/oxidns/render`：渲染传入的未保存 `{settings, base_url?}`，不保存；返回 `{config}`，与生成预览的 `{content}` 不同。
- `POST /api/oxidns/validate`：接收 `{settings, base_url?}`，不保存；返回 `{valid, issues, schema_version, core_check_required}`，每条问题含字段路径、源文件、级别。
- `POST /api/oxidns/common-settings`：从当前 `settings.native_config` 读取常用设置及可编辑能力，不保存。
- `POST /api/oxidns/common-settings/apply`：接收 `settings` 与按字段路径组织的 `changes`，返回更新的草稿及投影，不保存。
- `POST /api/oxidns/native-settings/apply`：完整参数的依赖联动接口，接收 `settings`、候选 `native_config`、可选的 `renames` 映射及 `standard_add` 标准功能 tag 列表；返回更新的草稿、常用设置投影和联动提示，不保存。引用冲突返回可定位到表单的字段路径。
- `POST /api/generate/oxidns/preview`：返回 `{content}` YAML。
- `POST /api/generate/oxidns`：下载完整 ZIP。
- 显式配置空间路径：`/api/profiles/<id>/oxidns/settings`、`/api/profiles/<id>/generate/oxidns[/preview]`。
- MCP 支持 `get_oxidns_settings`、`update_oxidns_settings` 和现有生成/预览工具中的 `oxidns` 目标。

设置存储在 profile 的 `oxidns` 字段。旧配置导入后自动补齐缺省值；脱敏导出会去除 API/RouterOS 等凭据以及高级 YAML 中可能包含的秘密。

## 实现参考

[统一草稿与接口](../backend/routes/oxidns.py)、[默认值与结构校验](../backend/common/oxidns_settings.py)、[参数校验](../backend/common/oxidns_validation.py)、[include 展开与资源处理](../backend/common/oxidns_native.py)、[生成器](../backend/converters/oxidns.py)、[增量规则同步](../backend/agents/rule_sync.py)、[缓存清理](../backend/agents/go-agent/routes/oxidns_cache.go)。

## 验证记录

本实现使用 OxiDNS v1.6.1 full 进行 10 组原生配置校验，覆盖核心模板、查询记录、IP 优选、动态域名与学习、RouterOS 两类集成、API Basic Auth、AdGuard 与原生规则更新。

Go Agent 的可选真实二进制回归 `TestOxiDNSNativeTransactionalDeployment` 已在 Linux amd64 + OxiDNS v1.6.1 通过：

- 完整包真实 `oxidns check`、启动、Basic Auth readyz 和 DNS 应答检查。
- 原生未知插件导致校验失败，运行中的配置和服务保持可用。
- 新配置启动成功但 DNS 返回 REFUSED，健康检查失败后自动恢复旧配置、旧规则和正常 DNS。
- 新增失败规则被移除，动态域名持久文件内容保留。
- 成功发布后手动回滚，恢复发布前配置并保留运行数据；旧发布记录的回滚请求被拒绝。

此测试使用独立临时目录和 loopback 临时端口，不操作系统 OxiDNS 服务。可在 Go Agent 目录运行：

```sh
OXIDNS_TEST_BINARY=/path/to/oxidns go test ./routes -run '^TestOxiDNSNativeTransactionalDeployment$' -count=1 -v
```

未设置环境变量时此测试跳过；普通单元测试不要求安装 OxiDNS。

## 参考

- [全局配置与 tag 规则](https://oxidns.org/configuration/global/)
- [解析与缓存](https://oxidns.org/plugin-reference/executor/resolution/)
- [IP 优选与响应处理](https://oxidns.org/plugin-reference/executor/response/)
- [查询记录与学习](https://oxidns.org/plugin-reference/executor/observability/)
- [域名 provider 与 AdGuard 子集](https://oxidns.org/plugin-reference/provider/domain/)
- [RouterOS 集成](https://oxidns.org/plugin-reference/executor/integrations/)
- [维护与定时任务](https://oxidns.org/plugin-reference/executor/maintenance/)
