"""References and quick-setup syntax audited against OxiDNS 1.6.1 factories.

Only known syntax is rejected. Future plugin/expression names are preserved with
warnings. This pass never opens provider files, executes commands or resolves DNS.
"""
import re

_ENV = re.compile(r'(?<!\$)\$\{[^}]+\}')
_DOMAIN = {'domain_set', 'dynamic_domain_set', 'geosite', 'adguard_rule'}
_IP = {'ip_set', 'geoip'}
_RULES = {'qname', 'cname', 'client_ip', 'resp_ip', 'ptr_ip', 'question', 'reload_provider'}
_QUICK = set('''_true _false has_resp has_wanted_ans qtype qclass rcode qname cname client_ip
resp_ip ptr_ip question mark random env string_exp time rate_limiter forward cache black_hole
ttl sleep ecs_handler forward_edns0opt client_ip_from_ecs ipset nftset ip_selector prefer_ipv4
prefer_ipv6 download upgrade reload reload_provider drop_resp query_summary debug_print metrics_collector'''.replace('+', '').split())
_IGNORED = {'drop_resp', 'reload', 'prefer_ipv4', 'prefer_ipv6', 'ecs_handler'}


def _tokens(value):
    return [token for raw in (value if isinstance(value, list) else [value]) if isinstance(raw, str)
            for token in re.split(r'[\s,]+', raw.strip()) if token]


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _uint(raw, maximum=4294967295):
    if not isinstance(raw, str) or not re.fullmatch(r'\+?[0-9]+', raw):
        return False
    digits = raw.lstrip('+').lstrip('0') or '0'
    limit = str(maximum)
    return len(digits) < len(limit) or (len(digits) == len(limit) and digits <= limit)


def _uint_value(raw):
    return int(raw.lstrip('+').lstrip('0') or '0')


def collect_reference_issues(document, plugin_schemas, *, dependency_graph=None):
    """Collect diagnostics, optionally exposing the same parsed dependency edges."""
    from backend.common.oxidns_validation import validate_schema, format_error, dns_code_valid
    from backend.common.oxidns_plugin_constraints import collect_plugin_issues

    issues, registry, edges = [], {}, {}
    plugins = _mapping(document).get('plugins', [])
    if not isinstance(plugins, list):
        return issues

    def issue(path, message, code='reference', severity='error'):
        issues.append({'path': list(path), 'message': message, 'code': code, 'severity': severity})

    for index, plugin in enumerate(plugins):
        tag = _mapping(plugin).get('tag')
        if not isinstance(tag, str):
            continue
        if tag in registry:
            issue(['plugins', index, 'tag'], '插件 tag 重复，必须全局唯一', 'duplicate-tag')
        else:
            registry[tag] = (index, plugin)
            edges[tag] = []

    def reference(owner, raw, path, kind, exact=None, capability=None):
        if not isinstance(raw, str) or _ENV.search(raw):
            return
        tag = raw.strip()
        if not tag:
            issue(path, '插件引用不能为空')
            return
        target = registry.get(tag)
        if target is None:
            issue(path, '引用的插件 tag 不存在')
            return
        _, target_plugin = target
        target_type = target_plugin.get('type')
        schema = plugin_schemas.get(target_type) if isinstance(target_type, str) else None
        if owner in edges:
            edges[owner].append((tag, path))
        if not schema:
            issue(path, '目标是尚未识别的插件类型，由目标核心确认其能力', 'unknown-reference', 'warning')
            return
        if schema.get('x-kind') != kind:
            issue(path, '引用目标必须为 %s 类型插件' % kind, 'reference-kind')
        elif exact and target_type != exact:
            issue(path, '引用目标必须为 %s 插件' % exact, 'reference-type')
        elif capability == 'domain' and target_type not in _DOMAIN:
            issue(path, '引用的 Provider 必须支持域名匹配', 'reference-capability')
        elif capability == 'ip' and target_type not in _IP:
            issue(path, '引用的 Provider 必须支持 IP 匹配', 'reference-capability')

    def provider_rules(owner, kind, args, path):
        for token in _tokens(args):
            if token.startswith('$') and len(token) > 1:
                reference(owner, token[1:], path, 'provider',
                          capability='domain' if kind in ('qname', 'cname') else
                          'ip' if kind in ('client_ip', 'resp_ip', 'ptr_ip') else None)

    def converted(owner, kind, args, path):
        schema = plugin_schemas.get(kind)
        if schema:
            for problem in validate_schema(args, schema, path):
                issues.append({**problem, 'path': list(path)})
        pseudo = {'tag': owner, 'type': kind, 'args': args}
        for problem in collect_plugin_issues(pseudo, 0, document):
            problem = dict(problem)
            # A quick expression has no nested YAML fields; point to its input.
            problem['path'] = list(path)
            issues.append(problem)
        if kind in _RULES:
            provider_rules(owner, kind, args, path)

    def short_flags(tokens, path):
        result = False
        while tokens and (tokens[-1] == 'short_circuit' or tokens[-1].startswith('short_circuit=')):
            token = tokens.pop()
            if token == 'short_circuit':
                result = True
            elif token.split('=', 1)[1] not in ('true', 'false'):
                issue(path, 'short_circuit 只能使用 true 或 false', 'quick-setup')
            else:
                result = token.endswith('=true')
        return result

    def quick(owner, kind, param, path, expected):
        schema = plugin_schemas.get(kind)
        if schema is None:
            issue(path, '尚未识别的快捷表达式，请由目标核心确认', 'unknown-expression', 'warning')
            return
        if schema.get('x-kind') != expected:
            issue(path, '此处需要 %s 表达式' % expected, 'expression-kind')
            return
        if kind not in _QUICK:
            issue(path, '该插件不支持快捷表达式，请定义插件后使用 $tag 引用', 'quick-setup')
            return
        raw = param or ''
        tokens = raw.split()
        if kind in _IGNORED or kind in ('query_summary', 'debug_print', 'metrics_collector'):
            return
        if kind in ('_true', '_false', 'has_resp', 'has_wanted_ans'):
            if raw.strip():
                issue(path, '此匹配器不接受参数', 'quick-setup')
            return
        if kind in _RULES or kind in ('qtype', 'qclass', 'rcode', 'mark', 'random', 'string_exp', 'ttl'):
            if not raw.strip():
                issue(path, '快捷表达式缺少必填参数', 'quick-setup')
                return
            converted(owner, kind, raw, path)
            return
        if kind == 'env':
            converted(owner, kind, tokens, path)
            return
        if kind == 'forward':
            short_flags(tokens, path)
            converted(owner, kind, {'upstreams': [{'addr': item} for item in tokens]}, path)
            return
        if kind == 'cache':
            if any(item not in ('short_circuit', 'short_circuit=true', 'short_circuit=false') for item in tokens):
                issue(path, 'cache 快捷参数仅支持 short_circuit、short_circuit=true/false', 'quick-setup')
            return
        if kind == 'black_hole':
            short = short_flags(tokens, path)
            tokens = _tokens(tokens)
            modes = {'nxdomain', 'nodata', 'null', 'custom', 'refused'}
            mode = tokens.pop(0).lower() if tokens and tokens[0].lower() in modes else None
            args = {'ips': tokens, 'short_circuit': short}
            if mode is not None:
                args['mode'] = mode
            converted(owner, kind, args, path)
            return
        if kind == 'sleep':
            error = format_error('duration', raw)
            if not raw.strip() or error:
                issue(path, error or 'sleep 必须填写时长', 'quick-setup')
            return
        if kind == 'forward_edns0opt':
            if any(not _uint(token, 65535) for token in _tokens(raw)):
                issue(path, 'EDNS0 代码必须为 0–65535 的整数', 'quick-setup')
            return
        if kind == 'client_ip_from_ecs':
            converted(owner, kind, _tokens(raw), path)
            return
        if kind == 'time':
            if len(tokens) != 1 or '-' not in tokens[0]:
                issue(path, 'time 快捷参数必须是单个 HH:MM-HH:MM 时间段', 'quick-setup')
                return
            start, end = tokens[0].split('-', 1)
            converted(owner, kind, {'periods': [{'start': start, 'end': end}]}, path)
            return
        if kind == 'rate_limiter':
            args = {}
            for index, key in enumerate(('qps', 'burst', 'mask4', 'mask6')):
                if len(tokens) <= index:
                    break
                try:
                    if key != 'qps' and not _uint(tokens[index]):
                        raise ValueError()
                    args[key] = float(tokens[index]) if key == 'qps' else _uint_value(tokens[index])
                except ValueError:
                    issue(path, 'rate_limiter 快捷参数需为 qps burst mask4 mask6 数字', 'quick-setup')
                    return
            converted(owner, kind, args, path)
            return
        if kind == 'ip_selector':
            args = {}
            if tokens and tokens[0] in ('first_success', 'best_within_budget', 'background'):
                args['selection_mode'] = tokens.pop(0)
            if tokens:
                args['probe_methods'] = tokens
            converted(owner, kind, args, path)
            return
        if kind == 'ipset':
            args = {}
            for token in tokens:
                parts = token.split(',')
                if len(parts) != 3 or parts[1] not in ('inet', 'inet6') or not _uint(parts[2], 255):
                    issue(path, 'ipset 快捷格式为 set,inet|inet6,mask', 'quick-setup')
                    continue
                suffix = '4' if parts[1] == 'inet' else '6'
                args['set_name' + suffix], args['mask' + suffix] = parts[0], _uint_value(parts[2])
            converted(owner, kind, args, path)
            return
        if kind == 'nftset':
            args = {}
            for token in tokens:
                parts = token.split(',')
                if (len(parts) != 5 or parts[0] not in ('ip', 'ip6', 'inet')
                        or not parts[1] or not parts[2] or not _uint(parts[4], 255)):
                    issue(path, 'nftset 快捷格式为 family,table,set,type,mask', 'quick-setup')
                    continue
                if parts[3] not in ('ipv4_addr', 'ipv6_addr'):
                    issue(path, '未知 nftset 地址类型，核心会忽略此条目', 'ignored-parameter', 'warning')
                    continue
                args['ipv4' if parts[3] == 'ipv4_addr' else 'ipv6'] = {
                    'table_family': parts[0], 'table_name': parts[1], 'set_name': parts[2], 'mask': _uint_value(parts[4])}
            converted(owner, kind, args, path)
            return
        if kind == 'download':
            parts = raw.strip().split(None, 1)
            if len(parts) < 2 or not parts[1].strip():
                issue(path, 'download 快捷参数必须包含 URL 和目录', 'quick-setup')
            else:
                converted(owner, kind, {'downloads': [{'url': parts[0], 'dir': parts[1].strip()}]}, path)
            return
        if kind == 'upgrade':
            args = {}
            for token in tokens:
                if token in ('force', 'no_restart'):
                    args[token] = True
                    continue
                key, sep, value = token.partition('=')
                if not sep or key not in ('force', 'no_restart', 'github_token', 'bundle'):
                    issue(path, 'upgrade 包含不支持的快捷参数', 'quick-setup')
                elif key in ('force', 'no_restart'):
                    if value not in ('true', 'false'):
                        issue(path, 'upgrade 开关必须为 true 或 false', 'quick-setup')
                    else:
                        args[key] = value == 'true'
                else:
                    args[key] = value.lower() if key == 'bundle' else value
            converted(owner, kind, args, path)

    def expression(owner, raw, path, expected, sequence=False, cron=False):
        if not isinstance(raw, str) or _ENV.search(raw):
            return
        raw = raw.lstrip()
        if expected == 'matcher' and raw.startswith('!'):
            raw = raw[1:].lstrip()
        if not raw.strip():
            issue(path, '表达式不能为空', 'expression')
            return
        if raw.startswith('$'):
            reference(owner, raw[1:], path, expected)
            return
        first, *rest = raw.split(None, 1)
        param = rest[0] if rest else None
        if sequence and first in ('accept', 'return'):
            return
        if sequence and first in ('jump', 'goto'):
            if not param or param.strip().startswith('$'):
                issue(path, 'jump/goto 必须引用不带 $ 的 sequence tag', 'expression')
            else:
                reference(owner, param, path, 'executor', exact='sequence')
            return
        if sequence and first == 'reject':
            tokens = (param or '').split()
            if tokens and (len(tokens) != 1 or not dns_code_valid('base-rcode', tokens[0])):
                issue(path, 'reject 只接受一个基础响应码 0–15 或对应名称', 'expression')
            return
        if sequence and first in ('mark', 'set_mark'):
            tokens = _tokens(param)
            if not tokens or any(not _uint(token) for token in tokens):
                issue(path, 'mark/set_mark 至少需要一个 0–4294967295 整数', 'expression')
            return
        if cron and plugin_schemas.get(first, {}).get('x-kind') != 'executor':
            reference(owner, raw, path, 'executor')
            return
        quick(owner, first, param, path, expected)

    for index, plugin in enumerate(plugins):
        if not isinstance(plugin, dict):
            continue
        owner, kind, args = plugin.get('tag'), plugin.get('type'), plugin.get('args')
        if not isinstance(owner, str) or not isinstance(kind, str):
            continue
        path, cfg = ['plugins', index, 'args'], _mapping(args)
        if kind in ('udp_server', 'tcp_server', 'quic_server'):
            reference(owner, cfg.get('entry'), path + ['entry'], 'executor')
        if kind == 'http_server':
            for j, entry in enumerate(cfg.get('entries') if isinstance(cfg.get('entries'), list) else []):
                reference(owner, _mapping(entry).get('exec'), path + ['entries', j, 'exec'], 'executor')
        if kind == 'fallback':
            for key in ('primary', 'secondary'):
                reference(owner, cfg.get(key), path + [key], 'executor')
        if kind in ('prefer_ipv4', 'prefer_ipv6'):
            reference(owner, cfg.get('probe_executor'), path + ['probe_executor'], 'executor')
        if kind == 'learn_domain':
            reference(owner, cfg.get('provider'), path + ['provider'], 'provider', exact='dynamic_domain_set')
        if kind in ('domain_set', 'ip_set'):
            for j, value in enumerate(cfg.get('sets') if isinstance(cfg.get('sets'), list) else []):
                reference(owner, value, path + ['sets', j], 'provider', capability='domain' if kind == 'domain_set' else 'ip')
        if kind in _RULES:
            provider_rules(owner, kind, args, path)
        if kind == 'sequence' and isinstance(args, list):
            for j, rule in enumerate(args):
                rule = _mapping(rule)
                expression(owner, rule.get('exec'), path + [j, 'exec'], 'executor', sequence=True)
                matches = rule.get('matches')
                if isinstance(matches, str):
                    expression(owner, matches, path + [j, 'matches'], 'matcher')
                elif isinstance(matches, list):
                    for k, match in enumerate(matches):
                        expression(owner, match, path + [j, 'matches', k], 'matcher')
        if kind == 'any_match':
            values = args if isinstance(args, list) else args.splitlines() if isinstance(args, str) else []
            for j, value in enumerate(values):
                if isinstance(value, str) and value.strip():
                    expression(owner, value, path + ([j] if isinstance(args, list) else []), 'matcher')
        if kind == 'cron':
            for j, job in enumerate(cfg.get('jobs') if isinstance(cfg.get('jobs'), list) else []):
                values = _mapping(job).get('executors')
                for k, value in enumerate(values if isinstance(values, list) else []):
                    expression(owner, value, path + ['jobs', j, 'executors', k], 'executor', cron=True)

    if dependency_graph is not None:
        dependency_graph.update({tag: [target for target, _ in references] for tag, references in edges.items()})

    # Iterative DFS avoids Python recursion limits on large valid configurations.
    done, active = set(), set()
    for start in edges:
        if start in done:
            continue
        active.add(start)
        stack = [(start, iter(edges[start]))]
        while stack:
            node, iterator = stack[-1]
            try:
                target, path = next(iterator)
            except StopIteration:
                active.discard(node)
                done.add(node)
                stack.pop()
                continue
            if target in active:
                issue(path, '插件依赖形成循环，无法初始化', 'dependency-cycle')
            elif target not in done:
                active.add(target)
                stack.append((target, iter(edges.get(target, []))))
    return issues
