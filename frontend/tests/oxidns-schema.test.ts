// @vitest-environment node
import { describe, expect, it } from 'vitest';
import { oxidnsGlobalSchema, oxidnsPluginCatalog, oxidnsSchemaSource, oxidnsSchemaVersion, oxidnsUpstreamSchema, type OxidnsSchema } from '../src/lib/oxidnsSchema';

// Independent field inventory extracted from the official documentation headings
// and YAML examples at commit bd183f69348aa9c78c3b92bddbce28651d2bf66e.
// Arbitrary HTTP JSON payload keys are not OxiDNS configuration fields.
const documentedFields: Record<string, string> = {
  "_false": "",
  "_true": "",
  "adguard_rule": "files rules",
  "any_match": "",
  "arbitrary": "files rules short_circuit",
  "black_hole": "ips mode short_circuit",
  "cache": "cache_negative dump_file dump_interval ecs_in_key lazy_cache_ttl max_negative_ttl max_positive_ttl min_positive_ttl negative_ttl_without_soa short_circuit size",
  "client_ip": "",
  "client_ip_from_ecs": "",
  "cname": "",
  "cron": "jobs jobs[].executors jobs[].interval jobs[].name jobs[].schedule timezone",
  "debug_print": "msg",
  "domain_set": "exps files sets",
  "download": "downloads downloads[].dir downloads[].filename downloads[].url outbound socks5 startup_if_missing timeout",
  "drop_resp": "",
  "dynamic_domain_set": "batch_size bootstrap_rules flush_interval_ms path queue_size",
  "ecs_handler": "forward mask4 mask6 preset send",
  "env": "",
  "fallback": "always_standby primary secondary short_circuit threshold",
  "forward": "concurrent response_selection short_circuit upstreams upstreams[].addr upstreams[].bind_to_device upstreams[].bootstrap upstreams[].bootstrap_version upstreams[].dial_addr upstreams[].enable_http3 upstreams[].enable_pipeline upstreams[].idle_timeout upstreams[].insecure_skip_verify upstreams[].max_conns upstreams[].min_conns upstreams[].outbound upstreams[].port upstreams[].so_mark upstreams[].socks5 upstreams[].tag upstreams[].timeout",
  "forward_edns0opt": "codes",
  "geoip": "file selectors",
  "geosite": "file selectors",
  "has_resp": "",
  "has_wanted_ans": "",
  "hosts": "entries files short_circuit",
  "http_request": "async body content_type error_mode form headers headers.X-Client-IP headers.X-Qname insecure_skip_verify json max_redirects method outbound phase query_params query_params.qname query_params.source queue_size socks5 timeout url",
  "http_server": "cert enable_http3 entries entries[].exec entries[].json_api entries[].path idle_timeout key listen src_ip_header",
  "ip_selector": "cache cache.enabled cache.failure_ttl cache.size cache.ttl dnssec_policy max_parallel_probes max_wait outbound probe_methods probe_stagger probe_timeout selection_mode socks5 top_n",
  "ip_set": "files ips sets",
  "ipset": "mask4 mask6 set_name4 set_name6",
  "learn_domain": "answer_required async error_mode phase provider qtypes questions rule_kind success_only timeout",
  "mark": "",
  "metrics_collector": "name",
  "nftset": "ipv4 ipv4.mask ipv4.set_name ipv4.table_family ipv4.table_name ipv6 ipv6.mask ipv6.set_name ipv6.table_family ipv6.table_name mask4 mask6 set_name4 set_name6 table_family4 table_family6 table_name4 table_name6",
  "prefer_ipv4": "cache cache_ttl probe_executor",
  "prefer_ipv6": "cache cache_ttl probe_executor",
  "ptr_ip": "",
  "qclass": "",
  "qname": "",
  "qtype": "",
  "query_recorder": "batch_size cleanup_interval_hours flush_interval_ms memory_tail path queue_size reader_concurrency retention_days",
  "query_summary": "msg",
  "question": "",
  "quic_server": "cert entry idle_timeout key listen",
  "random": "",
  "rate_limiter": "burst mask4 mask6 qps",
  "rcode": "",
  "redirect": "files rules",
  "reload_provider": "",
  "resp_ip": "",
  "response": "additionals answers authentic_data authoritative authorities rcode short_circuit",
  "reverse_lookup": "handle_ptr size ttl",
  "ros_address_list": "address address_list4 address_list6 async cleanup_on_shutdown comment_prefix connect_timeout fixed_ttl max_ttl min_ttl password persistent persistent.files persistent.ips queue_capacity receive_timeout send_timeout tls username wait_timeout",
  "ros_route": "address async cleanup_on_shutdown comment_prefix connect_timeout conntrack_guard distance fixed_ttl gateway4 gateway6 max_ttl min_ttl password persistent persistent.files persistent.ips queue_capacity receive_timeout routing_table send_timeout tls username wait_timeout",
  "script": "args command cwd env env.FDNS_CLIENT_IP env.FDNS_MARKS env.FDNS_QNAME error_mode max_output_bytes timeout",
  "sequence": "[].exec [].matches",
  "sleep": "duration",
  "string_exp": "",
  "tcp_server": "cert entry idle_timeout key listen",
  "time": "periods periods[].end periods[].monthdays periods[].start periods[].weekdays timezone",
  "ttl": "fix max min",
  "udp_server": "entry listen",
  "upgrade": "asset backup_dir bundle cache_dir cleanup force github_token insecure_skip_verify no_restart outbound repository skip_webui socks5 timeout webui_dir",
};

const documentedPlugins = {
  "server": [
    "udp_server",
    "tcp_server",
    "http_server",
    "quic_server"
  ],
  "executor": [
    "sequence",
    "fallback",
    "forward",
    "cache",
    "hosts",
    "arbitrary",
    "response",
    "redirect",
    "client_ip_from_ecs",
    "ecs_handler",
    "forward_edns0opt",
    "ttl",
    "ip_selector",
    "prefer_ipv4",
    "prefer_ipv6",
    "black_hole",
    "drop_resp",
    "reverse_lookup",
    "query_summary",
    "query_recorder",
    "metrics_collector",
    "debug_print",
    "sleep",
    "learn_domain",
    "http_request",
    "script",
    "ipset",
    "nftset",
    "ros_address_list",
    "ros_route",
    "upgrade",
    "download",
    "reload_provider",
    "reload",
    "cron"
  ],
  "matcher": [
    "qname",
    "question",
    "qtype",
    "qclass",
    "client_ip",
    "ptr_ip",
    "resp_ip",
    "cname",
    "rcode",
    "has_resp",
    "has_wanted_ans",
    "mark",
    "env",
    "time",
    "random",
    "rate_limiter",
    "string_exp",
    "any_match",
    "_true",
    "_false"
  ],
  "provider": [
    "domain_set",
    "dynamic_domain_set",
    "geosite",
    "adguard_rule",
    "ip_set",
    "geoip"
  ]
};

function at(schema: OxidnsSchema, path: string): OxidnsSchema | undefined {
  let current: OxidnsSchema | undefined = schema;
  for (const key of path.match(/\[\]|[^.\[\]]+/g) ?? []) {
    current = key === '[]' ? current?.items : current?.properties?.[key] ?? current?.additionalProperties;
  }
  return current;
}
function visit(schema: OxidnsSchema, fn: (item: OxidnsSchema) => void): void {
  fn(schema);
  for (const child of Object.values(schema.properties ?? {})) visit(child, fn);
  if (schema.items) visit(schema.items, fn);
  if (schema.additionalProperties) visit(schema.additionalProperties, fn);
}

describe('OxiDNS official configuration coverage', () => {
  it('pins the source and covers every documented plugin exactly once', () => {
    expect(oxidnsSchemaVersion).toBe('1.6.1');
    expect(oxidnsSchemaSource.commit).toBe('bd183f69348aa9c78c3b92bddbce28651d2bf66e');
    expect(new Set(oxidnsPluginCatalog.map(plugin => plugin.type)).size).toBe(65);
    for (const [category, types] of Object.entries(documentedPlugins)) {
      expect(oxidnsPluginCatalog.filter(plugin => plugin.category === category).map(plugin => plugin.type).sort()).toEqual([...types].sort());
    }
  });
  it('offers all official fields including nested examples', () => {
    for (const [type, fields] of Object.entries(documentedFields)) {
      const plugin = oxidnsPluginCatalog.find(plugin => plugin.type === type);
      expect(plugin, type).toBeDefined();
      for (const path of fields.split(' ').filter(Boolean)) expect(at(plugin!.args, path), `${type}.args.${path}`).toBeDefined();
    }
  });
  it('covers shared resolvers, API TLS, CORS, logs and includes', () => {
    const paths = [
      'runtime.worker_threads', 'log.level', 'log.file', 'log.rotation.type', 'log.rotation.max_files',
      'network.outbound.default', 'network.outbound.profiles.remote.resolver.nameservers[].addr',
      'network.outbound.profiles.remote.resolver.nameservers[].dial_addr',
      'network.outbound.profiles.remote.resolver.ip_version', 'network.outbound.profiles.remote.resolver.timeout',
      'network.outbound.profiles.remote.resolver.proxy', 'network.outbound.profiles.remote.proxy.socks5',
      'api.http.listen', 'api.http.ssl.cert', 'api.http.ssl.key', 'api.http.ssl.client_ca',
      'api.http.ssl.require_client_cert', 'api.http.auth.type', 'api.http.auth.username', 'api.http.auth.password',
      'api.http.cors.allowed_origins', 'api.http.webui.root', 'api.http.webui.index', 'include',
    ];
    for (const path of paths) expect(at(oxidnsGlobalSchema, path), path).toBeDefined();
    expect(oxidnsGlobalSchema.properties).not.toHaveProperty('plugins');
  });
  it('preserves native argument shapes and avoids invalid lazy cache defaults', () => {
    const get = (type: string) => oxidnsPluginCatalog.find(plugin => plugin.type === type)!.args;
    expect(get('sequence').type).toBe('array');
    expect(get('sequence').items?.properties?.matches.type).toBe('array');
    expect(get('qtype').type).toBe('array');
    expect(get('random').items?.type).toBe('string');
    expect(get('string_exp').type).toBe('string');
    expect(get('reload').type).toBe('null');
    expect(get('cache').properties?.lazy_cache_ttl.default).toBeUndefined();
    expect(get('cache').properties?.min_positive_ttl.help).toContain('不写入');
    expect(get('ip_selector').properties?.reorder_only.help).toContain('dnssec_policy');
    expect(get('upgrade').properties?.allow_prerelease).toBeDefined();
    expect(Object.keys(oxidnsUpstreamSchema.properties ?? {})).toHaveLength(17);
    expect(get('forward').properties?.upstreams.items).toBe(oxidnsUpstreamSchema);
  });
  it('provides help for every field and type-correct suggested defaults', () => {
    const check = (schema: OxidnsSchema) => {
      expect(schema.help.trim().length).toBeGreaterThan(0);
      expect(schema.label?.trim().length).toBeGreaterThan(0);
      if (schema.default !== undefined) {
        if (schema.type === 'null') expect(schema.default).toBeNull();
        else if (schema.type === 'array') expect(Array.isArray(schema.default)).toBe(true);
        else expect(typeof schema.default).toBe(schema.type);
        if (schema.enum) expect(schema.enum).toContain(schema.default);
      }
    };
    visit(oxidnsGlobalSchema, check);
    for (const plugin of oxidnsPluginCatalog) {
      expect(plugin.help.length).toBeGreaterThan(0);
      expect(new URL(plugin.documentation).hostname).toBe('oxidns.org');
      visit(plugin.args, check);
    }
  });
});
