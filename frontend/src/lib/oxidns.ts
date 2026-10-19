export interface OxidnsField {
  path: string
  label: string
  type?: 'text' | 'number' | 'password' | 'switch' | 'tls' | 'textarea' | 'list' | 'numbers' | 'upstreams' | 'lines'
  when?: string
  hint?: string
  documentation?: { href: string; label: string }
  placeholder?: string
  min?: number
  max?: number
  rows?: number
  wide?: boolean
  emptyHint?: string
  options?: Array<{ value: string; label: string }>
}

const number = (path: string, label: string, min = 1, hint?: string): OxidnsField => ({ path, label, type: 'number', min, hint })
const toggle = (path: string, label: string, hint?: string): OxidnsField => ({ path, label, type: 'switch', hint })
const text = (path: string, label: string, hint?: string): OxidnsField => ({ path, label, hint })
const select = (path: string, label: string, values: string[], hint?: string): OxidnsField => ({ path, label, hint, options: values.map(value => ({ value, label: value })) })

// Help follows the v1.6.1 runtime and ConfigFlow's generated execution paths.
const upstreamConcurrencyHint = '每次查询同时请求的上游数；核心最多使用 32 个，且不超过已配置的上游数。调大可增加竞速机会，也会增加 DNS 请求量。'

export const coreFields: OxidnsField[] = [
  text('listen', 'DNS 监听地址', '接收客户端 DNS 查询的 IP 与端口；UDP 与 TCP 共用，例如 0.0.0.0:53 监听所有 IPv4 网卡。'),
  { ...number('worker_threads', '工作线程数'), max: 256, hint: '处理异步任务的运行线程数。已验证模板为 2；增加线程可分担负载，也会增加调度开销，不能设为 0。' },
  toggle('tcp_enabled', '启用 TCP DNS', '在同一监听地址接收 TCP 查询，供客户端主动使用或 UDP 响应截断后重试。关闭后只提供 UDP DNS。'),
  toggle('prefer_ipv4', '优先 IPv4', '同一域名存在 A 记录时，对 AAAA 查询返回空成功响应，使客户端倾向 IPv4；IPv4 可用性未知时可能额外发起 A 查询。'),
  toggle('metrics_enabled', '轻量指标', '收集请求数量、耗时等汇总指标，供 API / WebUI 展示；不保存逐条查询历史。'),
  toggle('aggregate_rules', '合并相邻同策略规则', '将同类、相邻且转发目标相同的规则合并，减少规则插件数量，同时保留直连 / 代理的原始优先级顺序。'),
  { path: 'block_qtypes', label: '屏蔽查询类型', type: 'numbers', wide: true, hint: '对选定类型直接返回空成功响应。65 为 HTTPS，255 为 ANY；删除全部条目则不屏蔽。', documentation: { href: 'https://oxidns.org/dns-codes/#qtype-%E6%9F%A5%E8%AF%A2%E7%B1%BB%E5%9E%8B', label: '查询类型表' } },
  { path: 'default_forward', label: '未匹配时转发到', hint: '请求未命中域名、动态规则或响应 IP 规则时使用的最终上游。选择代理 DNS 时，按备用 DNS 设置决定是否使用回退。', options: [{ value: 'forward_remote', label: '代理 DNS' }, { value: 'forward_local', label: '直连 DNS' }] }
]

export const upstreamFields: OxidnsField[] = [
  { path: 'local_dns', label: '直连 DNS', type: 'upstreams', wide: true, placeholder: 'udp://223.5.5.5:53', emptyHint: '未配置条目，使用默认直连上游。', hint: '用于直连分流和响应 IP 探测。留空使用 223.5.5.5 与 119.29.29.29 的 UDP DNS；可逐条展开参数或切换 YAML 编辑。' },
  number('local_concurrent', '直连并发数', 1, upstreamConcurrencyHint),
  { path: 'remote_dns', label: '代理 DNS / 外部 Fake-IP 上游', type: 'upstreams', wide: true, placeholder: 'udp://192.0.2.53:1053', emptyHint: '未配置条目，使用默认代理上游。', hint: '用于代理分流；Fake-IP 需由外部上游提供，名称本身不会启用网络代理。留空使用 Cloudflare / Google DoH；域名上游可配置 bootstrap 或 dial_addr 避免解析环路。' },
  number('remote_concurrent', '代理并发数', 1, upstreamConcurrencyHint),
  number('remote_ttl', '代理响应 TTL（秒）', 0, '将代理 DNS 分支的响应 TTL 固定为此值，控制客户端缓存时长；0 保留原 TTL。备用分支的响应不做此改写。'),
  { path: 'fallback_dns', label: '备用 DNS', type: 'upstreams', wide: true, emptyHint: '未配置备用地址，不使用代理 DNS 回退。添加有效上游后自动启用，直连 DNS 不变。', hint: '添加有效上游后自动启用回退；删除最后一个地址会关闭回退，代理分流只使用代理上游，直连 DNS 不变。仍有地址时继续使用剩余上游，不会继承直连 DNS。已有 SERVFAIL、NXDOMAIN 等 DNS 响应不保证触发备用。' },
  number('fallback_concurrent', '备用并发数', 1, `启用代理 DNS 回退时生效。${upstreamConcurrencyHint}`),
  number('fallback_threshold', '触发备用等待时间（毫秒）', 1, '启用代理 DNS 回退时，主分支等待超过此时长后启用备用竞争；主分支执行失败或没有响应时可提前启用。500 毫秒即 0.5 秒。'),
  toggle('always_standby', '同时请求备用上游', '仅在启用代理 DNS 回退时生效。开启后主备同时查询，但备用结果需等阈值到达或主分支失败后才能采用；关闭时按需启动备用。')
]

export const cacheFields: OxidnsField[] = [
  toggle('cache_enabled', '启用主缓存', '缓存直连分支的 DNS 响应，命中时直接返回，减少上游查询；代理 / Fake-IP 分支不使用此主缓存。'),
  number('cache_size', '主缓存条目数', 1, '主缓存最多保存的条目数。增大可容纳更多查询结果，同时增加内存占用；不是字节数。'),
  number('cache_lazy_ttl', 'Lazy cache 总保留时长（秒）', 0, '0 关闭 lazy cache，保留普通缓存。300 表示从写入起至少保留 300 秒，并非原 TTL 再加 300 秒；原 TTL 更长时保留原 TTL。过期命中会后台刷新。'),
  toggle('probe_cache_enabled', '启用国内探测缓存', '独立缓存国内探测响应，返回主序列后继续判断 IP 分流；不启用 lazy cache。'),
  number('probe_cache_size', '探测缓存条目数', 1, '国内探测缓存的容量，与主缓存独立计数；增大可减少重复探测，也会增加内存占用。'),
  toggle('cache_dump_enabled', '缓存持久化', '定期保存主缓存，以便重启后恢复；会增加磁盘写入。v1.6.1 恢复时不扣除停机时长，默认关闭。'),
  text('cache_dump_file', '缓存文件', '主缓存的持久化文件，使用相对于 OxiDNS 工作目录的路径，例如 ./state/cache.dump；不能使用绝对路径或 ../。'),
  number('cache_dump_interval', '持久化间隔（秒）', 1, '主缓存写入磁盘的周期，仅在主缓存和持久化都启用时生效。间隔越短，写盘越频繁。')
]

export const apiFields: OxidnsField[] = [
  toggle('api.enabled', '启用 API / WebUI', '开启管理接口；填写静态文件目录后可在同端口访问 WebUI。两者共用下方账号，生成的运行配置会包含登录凭据。'),
  text('api.listen', 'API 监听地址', '127.0.0.1:9199 仅供本机访问；0.0.0.0:9199 供局域网访问。Docker 桥接网络还需映射此端口。'),
  text('api.username', '用户名', 'API 与 WebUI 共用的 Basic Auth 用户名，需与密码同时填写；两项留空表示不启用身份验证。'),
  { path: 'api.password', label: '密码', type: 'password', hint: 'API 与 WebUI 共用的登录密码，需与用户名同时填写。密码会写入生成的运行配置，分享配置时请移除凭据。' },
  { ...text('api.webui_root', 'WebUI 静态文件目录', 'OxiDNS 所在机器上的前端文件目录；留空仅启用 API。Docker 镜像通常为 /usr/share/oxidns/webui，相对路径以 OxiDNS 工作目录为基准。'), wide: true },
  text('api.ssl.cert', 'API TLS 证书路径', '启用 HTTPS 使用的证书文件路径，必须同时设置私钥；两项留空使用 HTTP。文件需在 OxiDNS 运行环境中可读。'),
  { path: 'api.ssl.key', label: 'API TLS 私钥路径', type: 'password', hint: '与 TLS 证书配套的私钥文件路径，填写路径而非密钥内容；文件需在 OxiDNS 运行环境中可读。' },
  select('log.level', '日志级别', ['off', 'error', 'warn', 'info', 'debug', 'trace'], '从 error 到 trace 逐渐增加详细程度；off 关闭日志。日常可用 info，debug / trace 会增加日志量。'),
  text('log.file', '日志文件', '留空仅输出到服务日志；填写文件路径后同时写入标准输出和该文件。路径需在 OxiDNS 运行环境中可写。'),
  select('log.rotation', '日志轮转', ['never', 'minutely', 'hourly', 'daily', 'weekly'], '仅对日志文件生效：never 不轮转，其余分别按分钟、小时、天、周生成新日志文件。'),
  number('log.max_files', '保留日志文件数', 0, '轮转时保留的历史日志文件数量；0 不自动删除。仅设置日志文件并启用轮转时有意义。')
]

const rosFields = (name: string): OxidnsField[] => [
  text(`${name}.args.address`, 'RouterOS API 地址', 'OxiDNS 连接的 RouterOS 管理地址，例如 192.168.0.1:8728；TLS API 通常为 8729，需与 RouterOS 实际设置一致。'),
  text(`${name}.args.username`, '用户名', 'RouterOS API 登录账号，需要读取和维护目标地址列表或路由的权限。'),
  { path: `${name}.args.password`, label: '密码', type: 'password', hint: 'RouterOS API 登录密码，用于连接与重连；会保存在生成的运行配置中。' },
  { path: `${name}.args.tls`, label: '使用 TLS', type: 'tls', hint: '通过 API-SSL 加密连接 RouterOS；关闭时使用明文 API。开启后请填写对应的 TLS API 端口。' },
  { ...text(`${name}.args.tls.server_name`, 'TLS 服务器名称', '用于校验证书的服务器名称；留空从 API 地址推断。以 IP 连接但证书签发给域名时可填写该域名。'), when: `${name}.args.tls` },
  { ...text(`${name}.args.tls.ca`, 'TLS CA 文件路径', '留空使用系统证书；配置 CA 时不能跳过证书验证。'), when: `${name}.args.tls` },
  { ...toggle(`${name}.args.tls.insecure`, '跳过 TLS 证书验证', '连接仍加密，但不验证对端证书身份；不能与 CA 文件同时使用。'), when: `${name}.args.tls` },
  toggle(`${name}.args.async`, '异步写入', '开启后 DNS 只投递任务，由后台写入 RouterOS；关闭后等待一次写入尝试，可能增加 DNS 响应延迟。'),
  number(`${name}.args.min_ttl`, '最小 TTL（秒）', 0, '写入 RouterOS 的动态条目至少保留此时长；较小的 DNS TTL 会被提升。0 表示不抬高 TTL，不能大于最大 TTL。'),
  number(`${name}.args.max_ttl`, '最大 TTL（秒）', 0, '限制动态条目的保留时长，较大的 DNS TTL 会被截短。通常应设为正数；0 会使动态租约立即到期，不代表永久保留。'),
  number(`${name}.args.queue_capacity`, '队列容量', 1, '入口队列和重试积压各自允许的不同 IP 数；同一 IP 会合并。满载时丢弃新增 IP，不阻塞 DNS 响应。'),
  text(`${name}.args.comment_prefix`, '注释前缀', '写入 RouterOS 条目的归属标记，用于识别与清理。不能含分号或等号；不要手工修改受管条目的归属注释。'),
  toggle(`${name}.args.cleanup_on_shutdown`, '正常停止时清理临时条目', '正常关闭或应用重载时，清理本插件管理的条目。关闭后保留 RouterOS 状态，可减少重启时的策略空窗。')
]

export const featureGroups: Array<{ key: string; title: string; hint: string; fields: OxidnsField[] }> = [
  { key: 'query_recorder', title: '查询记录', hint: 'SQLite 查询历史、执行路径与耗时。启用后增加磁盘写入；数据存放于 state/，部署保留。', fields: [
    text('query_recorder.args.path', '数据库路径', 'SQLite 查询记录文件，必须使用 state/ 下的相对路径；部署时保留该目录中的运行数据。'),
    number('query_recorder.args.retention_days', '保留天数', 1, '保留最近多少天的查询记录；到期后由定时清理任务删除。删除后文件可能不会立即缩小，空闲空间可被复用。'),
    number('query_recorder.args.queue_size', '写入队列', 1, '等待后台写入数据库的记录容量，用于缓冲查询高峰；容量越大，可能占用的内存越多。'),
    number('query_recorder.args.batch_size', '每批条数', 1, '每次批量写入 SQLite 的记录数量；较大批次可减少写入次数。'),
    number('query_recorder.args.flush_interval_ms', '刷新间隔（毫秒）', 1, '后台批量提交记录的时间间隔；较短可更快看到新记录，也会增加写入频率。'),
    number('query_recorder.args.memory_tail', '内存最近记录数', 0, '内存中保留的最近记录数量，供实时流回放；更多记录占用更多内存。v1.6.1 中设为 0 仍至少保留 1 条。'),
    number('query_recorder.args.cleanup_interval_hours', '清理间隔（小时）', 1, '检查并删除超过保留天数的记录的周期；到期记录可能保留到下一次清理。'),
    number('query_recorder.args.reader_concurrency', '读取并发数', 1, 'WebUI / API 同时执行 SQLite 历史与统计查询的上限，用于限制读取产生的线程和内存开销。')
  ] },
  { key: 'ip_selector', title: 'IP 优选（实验性）', hint: '只应用于直连的真实 IP 响应；代理 / Fake-IP 分支不探测。后台模式配合 top_n=0 仅调整顺序。', fields: [
    select('ip_selector.args.selection_mode', '探测模式', ['background', 'first_success', 'best_within_budget'], 'background 先返回结果、后台测速；first_success 采用首个探测成功的 IP；best_within_budget 在等待预算内比较延迟。后两者可能增加响应时间。'),
    number('ip_selector.args.top_n', '保留 IP 数量', 0, '0 保留全部地址，仅按探测结果排序。'),
    { path: 'ip_selector.args.probe_methods', label: '探测方法', type: 'list', wide: true, hint: '例如 tcp:443、tcp:80、ping 或 none。TCP 测量连接耗时；ping 依赖系统支持与权限，无法执行时保留 DNS 结果。' },
    select('ip_selector.args.dnssec_policy', 'DNSSEC 策略', ['reorder_only', 'skip'], '请求带 DNSSEC DO 标记或响应含相关签名时，reorder_only 仅重排、不裁剪地址；skip 完全跳过优选，保护签名记录完整性。'),
    number('ip_selector.args.probe_stagger', '探测间隔（毫秒）', 0, '同一 IP 的多种探测方法错峰启动的间隔；0 表示不等待，并非周期测速频率。'),
    number('ip_selector.args.probe_timeout', '探测超时（毫秒）', 1, '单次 IP 探测允许等待的最长时间；超时会记为该次探测失败。'),
    number('ip_selector.args.max_wait', '等待预算（毫秒）', 1, '一次前台 IP 优选最多等待多久；用于控制 first_success 和 best_within_budget 增加的响应延迟。'),
    number('ip_selector.args.max_parallel_probes', '最大探测并发', 1, '整个 IP 优选插件同时执行的探测数量上限，限制大量冷查询造成的连接与资源开销。'),
    toggle('ip_selector.args.cache.enabled', '缓存探测结果', '复用 IP 探测评分，减少重复测速。此处缓存的是测速结果，与 DNS 响应主缓存独立。'),
    number('ip_selector.args.cache.size', '探测结果缓存条数', 1, '测速评分缓存的目标容量；更多条目可减少重复探测，同时增加内存占用。'),
    number('ip_selector.args.cache.ttl', '探测成功缓存（秒）', 1, '成功测速的评分保留时长；较长可减少探测，但对网络变化的反应较慢。'),
    number('ip_selector.args.cache.failure_ttl', '探测失败缓存（秒）', 1, '失败评分的保留时长；较短可更快重新尝试恢复的 IP，也会增加重复探测。')
  ] },
  { key: 'dynamic_domain_set', title: '动态域名集与学习（实验性）', hint: '可通过 OxiDNS API 更新域名；启用学习后从已归类的响应学习，可能固定后续分流结果。运行数据保留在 state/。', fields: [
    text('dynamic_domain_set.args.path', '动态规则文件', '保存 API 添加或自动学习的域名，必须使用 state/ 下的相对路径；文件不存在时创建，已有文件在启动时读取。'),
    { path: 'dynamic_domain_set.route', label: '动态规则转发到', hint: '命中动态域名集时使用的上游；显式域名规则优先。自动学习也只收录通过响应 IP 规则分到此目标的域名。', options: [{ value: 'direct', label: '直连 DNS' }, { value: 'proxy', label: '代理 DNS' }] },
    { path: 'dynamic_domain_set.args.bootstrap_rules', label: '初始规则', type: 'list', wide: true, hint: '仅在动态规则文件不存在时写入。full:example.com 精确匹配；domain:example.com 同时匹配子域名。已有文件不会被覆盖。' },
    number('dynamic_domain_set.args.queue_size', '写入队列', 1, '自动学习待写入任务的队列容量，用于缓冲高峰；增大可能增加内存占用。'),
    number('dynamic_domain_set.args.batch_size', '每批条数', 1, '后台累计到此数量时批量追加规则，未满时按刷新间隔写入。'),
    number('dynamic_domain_set.args.flush_interval_ms', '刷新间隔（毫秒）', 1, '后台将已学习规则追加到文件的时间间隔；较短可更快落盘，也会增加磁盘写入。'),
    toggle('dynamic_domain_set.learn', '启用自动学习', '从响应 IP 分流结果中学习域名，下次可直接按域名分流。只学习查询域名，不学习 CNAME 目标；可能固定后续分流结果。'),
    select('dynamic_domain_set.learn_args.phase', '学习阶段', ['after', 'before'], 'after 在后续处理完成后检查响应再学习；before 提前学习查询域名，不检查是否成功或含答案。'),
    select('dynamic_domain_set.learn_args.questions', '学习问题范围', ['first', 'all'], 'first 仅检查请求中的第一个 DNS 问题；all 检查全部问题。常见 DNS 请求只有一个问题。'),
    { path: 'dynamic_domain_set.learn_args.qtypes', label: '学习查询类型', type: 'list', wide: true, hint: '只学习这些查询类型，例如 A、AAAA；用于避免把无关类型的查询加入动态分流，至少保留一个类型。' },
    select('dynamic_domain_set.learn_args.rule_kind', '学习规则类型', ['full', 'domain'], 'full 只匹配学到的完整域名；domain 还匹配其下级子域名，影响范围更大。'),
    toggle('dynamic_domain_set.learn_args.success_only', '仅学习成功响应', '仅在 after 阶段生效，要求响应码为 NOERROR；before 阶段不检查此条件。'),
    toggle('dynamic_domain_set.learn_args.answer_required', '要求响应中含答案', '仅在 after 阶段生效，要求响应的 Answer 区非空，避免学习空答案；before 阶段不检查此条件。'),
    toggle('dynamic_domain_set.learn_args.async', '异步学习', '开启后规则入队即继续 DNS 处理；关闭后等待动态规则写入完成，可能增加响应时间。'),
    select('dynamic_domain_set.learn_args.error_mode', '学习失败行为', ['continue', 'stop', 'fail'], 'continue 记录错误并继续；stop 终止当前执行链；fail 返回执行错误，可能影响 DNS 响应。'),
    text('dynamic_domain_set.learn_args.timeout', '学习超时', '同步学习等待写入的时间上限，例如 1s 或 500ms；仅关闭异步学习时生效。')
  ] },
  { key: 'adguard', title: 'AdGuard DNS 过滤', hint: '支持 DNS 规则子集；不支持网页元素过滤、URL 路径、dnsrewrite 或 $client。规则文件包含在完整下载包中。', fields: [
    { path: 'adguard.content', label: '自定义规则', type: 'textarea', wide: true, rows: 5, placeholder: '||ads.example.com^\n@@||allowed.example.com^', hint: '每行一条 DNS 过滤规则，例如 ||ads.example.com^ 拦截域名，@@||allowed.example.com^ 放行；自定义规则与订阅规则一起使用。' },
    { path: 'adguard.urls', label: '规则订阅', type: 'list', wide: true, hint: '每项填写一个 HTTP(S) 规则文件地址，规则会加入完整下载包；启用原生定时更新后可自动刷新。' },
    select('adguard.mode', '拦截响应', ['nxdomain', 'nodata', 'null', 'refused'], 'nxdomain 表示域名不存在；nodata 返回空成功响应；null 对 A / AAAA 返回 0.0.0.0 / ::；refused 表示拒绝查询。')
  ] },
  { key: 'ros_address_list', title: 'RouterOS 地址列表', hint: '只写入直连分支的真实 A / AAAA 地址。需要 RouterOS API 权限及已有的地址列表策略。', fields: [
    ...rosFields('ros_address_list'),
    text('ros_address_list.args.address_list4', 'IPv4 地址列表', '将直连响应中的 A 记录写入此 RouterOS address-list；留空不处理 IPv4，需在 RouterOS 侧配置使用该列表的策略。'),
    text('ros_address_list.args.address_list6', 'IPv6 地址列表', '将直连响应中的 AAAA 记录写入此 RouterOS address-list；留空不处理 IPv6，需有对应的 IPv6 策略。')
  ] },
  { key: 'ros_route', title: 'RouterOS 主机路由', hint: '只为直连分支的真实 IP 创建主机路由。路由表和网关须先在 RouterOS 中配置。', fields: [
    ...rosFields('ros_route'),
    text('ros_route.args.routing_table', '已有路由表', '受管主机路由写入的 RouterOS 路由表名称；插件不会创建路由表、路由规则或默认路由，需提前配置。'),
    text('ros_route.args.gateway4', 'IPv4 网关', 'IPv4 主机路由使用的 RouterOS 网关表达式；留空忽略 IPv4 地址，两个地址族的网关至少填一个。'),
    text('ros_route.args.gateway6', 'IPv6 网关', 'IPv6 主机路由使用的 RouterOS 网关表达式；留空忽略 IPv6 地址，两个地址族的网关至少填一个。'),
    number('ros_route.args.distance', '路由距离', 1, '写入受管路由的 distance；相同目标前缀下，较小的距离通常具有更高优先级，需与已有路由策略协调。')
  ] },
  { key: 'rule_update', title: '原生规则定时更新', hint: '在设备上定时下载并热重载规则，与 ConfigFlow 规则库自动同步独立；关闭后仍可在 Agent 页面手动更新。下载失败的文件保留旧版，不提供整批回滚；Agent 手动更新会在全部下载成功后才热重载。请勿同时使用外部脚本更新同一规则目录。', fields: [
    text('rule_update.args.schedule', 'Cron（五段）', '依次为分、时、日、月、星期；例如 30 3 * * * 表示按下方时区每天 03:30 更新。'),
    text('rule_update.args.timezone', '时区', '执行 Cron 使用的 IANA 时区，例如 Asia/Shanghai；用于确定定时任务的实际触发时间。'),
    text('rule_update.args.timeout', '下载超时', '手动与定时更新共用。单个规则下载允许等待的时长，例如 60s 或 2m；文件按顺序下载，总耗时可能超过单次超时。'),
    text('rule_update.args.outbound', '出站配置标签', '手动与定时更新共用。引用高级配置 network.outbound.profiles 中的标签，复用其解析器与代理；留空使用 OxiDNS 默认出站。'),
    text('rule_update.args.socks5', 'SOCKS5 代理', '手动与定时更新共用。规则下载专用代理，例如 192.168.0.1:1080 或 用户名:密码@主机:端口；填写后覆盖出站配置的代理，留空沿用出站配置。')
  ] }
]

export const applyVerifiedOxidnsTemplate = (settings: Record<string, any>) => Object.assign(settings, {
  worker_threads: 2, aggregate_rules: true, metrics_enabled: true,
  local_concurrent: 2, remote_concurrent: 1, fallback_concurrent: 2,
  fallback_threshold: 500, always_standby: false, remote_ttl: 1,
  cache_enabled: true, cache_size: 10240, cache_lazy_ttl: 300, cache_dump_enabled: false,
  probe_cache_enabled: true, probe_cache_size: 2048
})
