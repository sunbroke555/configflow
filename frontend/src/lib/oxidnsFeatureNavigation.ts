/** Presentation categories only: changing a category never changes native YAML. */
import { optionHelp } from './optionCopy';

export const oxidnsFeatureCategories = [
  {
    id: 'runtime',
    label: '运行设置',
    help: optionHelp('需要调整服务性能、查看日志或开启网页管理时使用。', '管理工作线程、日志和 API / WebUI，不改变客户端查询入口。手机、电脑使用的 DNS 地址与协议，应在监听服务中设置。'),
  },
  {
    id: 'listeners',
    label: '监听服务',
    help: optionHelp('希望手机、电脑能通过所选协议访问 OxiDNS 时设置。', '定义接收查询的地址与端口，支持 UDP、TCP、DoT、DoH 和 DoQ。还需指定处理入口；加密协议按要求准备证书和私钥。'),
  },
  {
    id: 'upstreams',
    label: 'DNS 上游',
    help: optionHelp('需要指定向哪些 DNS 服务器询问域名结果时使用。', '可分别创建直连、远程和备用查询实例，设置地址、并发与连接参数。新增实例还需在分流流程中引用，名称本身不会让连接自动走代理。'),
  },
  {
    id: 'cache',
    label: '缓存',
    help: optionHelp('希望复用查询结果、减少等待，或记录 IP 对应域名。', '管理普通响应缓存、过期结果复用和反向查询缓存。探测响应也可使用独立缓存实例；新增插件需在执行链中引用，不会自动接管全部查询。'),
  },
  {
    id: 'routing',
    label: '分流规则',
    help: optionHelp('需要决定哪些查询走直连、代理、备用或拦截时使用。', '按顺序组合匹配条件和处理动作，可设置主备切换、固定应答或域名重定向。条件只负责判断，最终处理方式由命中后的动作决定。'),
  },
  {
    id: 'resources',
    label: '规则资源',
    help: optionHelp('多个匹配条件需要共用域名或 IP 列表时设置。', '管理域名、IP、GeoSite、GeoIP 和 AdGuard 规则资源，也可载入 include 文件。资源供执行链引用，不会单凭列表名称自动决定查询出口。'),
  },
  {
    id: 'features',
    label: '特色功能',
    help: optionHelp('需要测速选址、查询历史、路由器联动或自动维护。', '集中设置 IP 优选、来源网段信息、查询记录，以及系统和 RouterOS 联动。下载、升级和定时任务也在这里；各项需按实际权限和依赖配置。'),
  },
  {
    id: 'network',
    label: '网络出站',
    help: optionHelp('多个上游或下载任务需要共用同一解析器和代理。', '先定义带名称的出站配置，再让 DNS 上游及 HTTP 请求引用。可统一设置目标域名如何解析、连接经过哪个代理，避免逐项重复填写。'),
  },
  {
    id: 'advanced',
    label: '扩展配置',
    help: optionHelp('使用新版或第三方功能，现有参数目录尚未识别时使用。', '支持添加和编辑自定义顶层字段、插件类型及 YAML。未知字段和值类型会保留，但需按目标核心文档填写，界面无法替代运行环境兼容性检查。'),
  },
] as const;

export type OxidnsFeatureCategoryId = typeof oxidnsFeatureCategories[number]['id'];

const globalCategories = new Map<string, OxidnsFeatureCategoryId>([
  ['runtime', 'runtime'],
  ['log', 'runtime'],
  ['api', 'runtime'],
  ['include', 'resources'],
  ['network', 'network'],
]);

// Keep this inventory explicit: a new catalog plugin should be consciously
// classified rather than silently inheriting an unrelated visual category.
const pluginTypesByCategory: Partial<Record<OxidnsFeatureCategoryId, readonly string[]>> = {
  listeners: ['udp_server', 'tcp_server', 'http_server', 'quic_server'],
  upstreams: ['forward'],
  cache: ['cache', 'reverse_lookup'],
  routing: [
    'sequence', 'fallback', 'hosts', 'arbitrary', 'response', 'redirect',
    'black_hole', 'drop_resp', 'sleep',
    'qname', 'question', 'qtype', 'qclass', 'client_ip', 'ptr_ip', 'resp_ip',
    'cname', 'rcode', 'has_resp', 'has_wanted_ans', 'mark', 'env', 'time',
    'random', 'rate_limiter', 'string_exp', 'any_match', '_true', '_false',
  ],
  resources: ['domain_set', 'dynamic_domain_set', 'geosite', 'adguard_rule', 'ip_set', 'geoip'],
  features: [
    'client_ip_from_ecs', 'ecs_handler', 'forward_edns0opt', 'ttl',
    'ip_selector', 'prefer_ipv4', 'prefer_ipv6', 'query_summary',
    'query_recorder', 'metrics_collector', 'debug_print', 'learn_domain',
    'http_request', 'script', 'ipset', 'nftset', 'ros_address_list', 'ros_route',
    'upgrade', 'download', 'reload_provider', 'reload', 'cron',
  ],
};

const pluginCategories = new Map<string, OxidnsFeatureCategoryId>(
  Object.entries(pluginTypesByCategory).flatMap(([category, types]) =>
    types.map(type => [type, category as OxidnsFeatureCategoryId] as const),
  ),
);

export function getOxidnsGlobalCategory(key: string): OxidnsFeatureCategoryId {
  return globalCategories.get(key) ?? 'advanced';
}

export function getOxidnsPluginCategory(type: string): OxidnsFeatureCategoryId {
  return pluginCategories.get(type) ?? 'advanced';
}
