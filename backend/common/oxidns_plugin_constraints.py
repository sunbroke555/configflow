"""Cross-field checks for OxiDNS 1.6.1 plugin arguments.

Based on the official docs and factory parsers at bd183f69348aa9c78c3b92bddbce28651d2bf66e.
This module never normalizes or deletes user values. Files, network endpoints and
platform permissions are checked by the target OxiDNS core at publish time.
"""
import ipaddress
import math
import os
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

U32 = 4294967295
U64 = 18446744073709551615
_RUNTIME_KEYS = set('qname qtype qtype_name qclass qclass_name client_ip client_port server_name url_path marks has_resp rcode rcode_name resp_ip cron_plugin_tag cron_job_name cron_trigger_kind cron_scheduled_at_unix_ms'.split())


def _str(value):
    return value.strip() if isinstance(value, str) else ''


def _number(value):
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _deferred(value):
    return isinstance(value, str) and '${' in value


def _tokens(value):
    values = value if isinstance(value, list) else [value]
    return [token for item in values if isinstance(item, (str, int)) and not isinstance(item, bool)
            for token in re.split(r'[\s,]+', str(item).strip()) if token]


def _uint(raw, maximum=U64):
    """Parse bounded decimal text without Python's unbounded int conversion."""
    if not isinstance(raw, str) or not re.fullmatch(r'[0-9]+', raw):
        return None
    normalized = raw.lstrip('0') or '0'
    bound = str(maximum)
    if len(normalized) > len(bound) or (len(normalized) == len(bound) and normalized > bound):
        return None
    return int(normalized)


def _duration(value):
    if isinstance(value, int) and not isinstance(value, bool):
        return value if 0 <= value <= U64 else None
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r'(\d+)\s*(ms|s|m|h|d)?', value.strip(), re.I)
    number = _uint(match[1]) if match else None
    if number is None:
        return None
    return number * {'ms': .001, 's': 1, 'm': 60, 'h': 3600, 'd': 86400}.get((match[2] or 's').lower())


def _regex_error(raw):
    # Rust regex deliberately has no backreferences or look-around. Unicode
    # classes and its set operations have no equivalent in Python's re parser.
    if re.search(r'(?<!\\)\\[1-9]|\(\?[=!]|\(\?<[=!]', raw):
        return 'OxiDNS 的正则不支持反向引用或前后查找'
    if r'\p' in raw or r'\P' in raw or '&&' in raw or '--' in raw or '~~' in raw:
        return None
    try:
        re.compile(raw)
    except re.error:
        return '正则表达式语法无效'
    return None


def _domain_error(raw, dynamic=False):
    if not isinstance(raw, str) or _deferred(raw):
        return None
    raw = raw.strip()
    if not raw or raw.startswith('#'):
        return '域名规则不能为空' if dynamic else None
    if raw.startswith('regexp:'):
        pattern = raw[7:]
        if not pattern.strip():
            return '正则表达式不能为空'
        return _regex_error(pattern)
    if dynamic or raw.startswith(('full:', 'domain:', 'keyword:')):
        value = raw.split(':', 1)[1] if raw.startswith(('full:', 'domain:', 'keyword:')) else raw
        if not value.strip().rstrip('.'):
            return '域名规则不能为空'
    return None


def _cron_error(raw):
    """Validate the standard five fields, including Croner's L/W/# extensions."""
    if not isinstance(raw, str) or _deferred(raw):
        return None
    parts = raw.split()
    if len(parts) != 5:
        return '必须使用 5 段 Cron 表达式（分 时 日 月 星期）'
    names = [{}, {}, {}, {x: i+1 for i,x in enumerate('JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC'.split())}, {x:i for i,x in enumerate('SUN MON TUE WED THU FRI SAT'.split())}]
    for index, (field, lo, hi) in enumerate(zip(parts, (0,0,1,1,0), (59,23,31,12,7))):
        field = field.upper()
        for name, value in names[index].items():
            field = re.sub(r'\b'+name+r'\b', str(value), field)
        for segment in field.split(','):
            if not segment:
                return 'Cron 字段中有空条目'
            if index == 2 and re.fullmatch(r'L(?:-\d+)?|LW|\d+W', segment):
                nums = [_uint(n, 31) for n in re.findall(r'\d+', segment)]
                if any(n is None or n < (0 if segment.startswith('L-') else 1) for n in nums):
                    return 'Cron 月日字段超出 1–31'
                continue
            if index == 4 and re.fullmatch(r'[0-7](?:L|#[1-5])', segment):
                continue
            fragments = segment.split('/')
            if len(fragments) > 2 or (len(fragments)==2 and (_uint(fragments[1]) in (None, 0))):
                return 'Cron 步长必须是字段范围内的正整数'
            base = fragments[0]
            if base in ('*','?'):
                if base == '?' and index not in (2,4):
                    return 'Cron 的 ? 只能用于日或星期字段'
                continue
            bounds = base.split('-')
            if len(bounds) > 2 or any(_uint(x, hi) is None or _uint(x, hi) < lo for x in bounds):
                return 'Cron 字段超出允许范围'
            if len(bounds)==2 and _uint(bounds[0], hi) > _uint(bounds[1], hi):
                return 'Cron 范围起点不能大于终点'
    return None


def collect_plugin_issues(plugin, index, document):
    """Return non-mutating {path, message, code, severity} validation issues."""
    if not isinstance(plugin, dict):
        return []
    kind = plugin.get('type')
    args = plugin.get('args')
    issues = []
    prefix = ['plugins', index, 'args']

    def issue(path, message, code='constraint', severity='error'):
        issues.append({'path': prefix + path, 'message': message, 'code': code, 'severity': severity})

    def domain(raw, path, dynamic=False):
        error = _domain_error(raw, dynamic)
        if error:
            issue(path, error, 'domain-rule')

    def ip(raw, path, network=False):
        if not isinstance(raw, str) or _deferred(raw):
            return
        try:
            (ipaddress.ip_network(raw, strict=False) if network else ipaddress.ip_address(raw))
        except ValueError:
            issue(path, '需要有效的 IP 或 CIDR' if network else '需要有效的 IP 地址', 'ip')

    def nullable_default(name, default, mapping=None):
        mapping = args if mapping is None else mapping
        value = mapping.get(name)
        return default if value is None else value

    def compare(lower, upper, defaults=(None,None)):
        low, high = nullable_default(lower, defaults[0]), nullable_default(upper, defaults[1])
        if _number(low) and _number(high) and low > high:
            issue([lower], '%s 不能大于 %s' % (lower, upper), 'range-order')

    def template(raw, path):
        if not isinstance(raw, str):
            return
        # Environment references are intentionally deferred to the deployment
        # host; runtime placeholders have a finite documented vocabulary.
        stripped = raw.replace('$$','')
        for match in re.finditer(r'\$\{([^}]*)\}', stripped):
            key = match[1].strip()
            if not key:
                issue(path, '占位符名称不能为空', 'template')
        if '${' in re.sub(r'\$\{[^}]*\}', '', stripped):
            issue(path, '占位符缺少右花括号', 'template')

    if kind in ('qname','cname','client_ip','ptr_ip','resp_ip','question','qtype','qclass','rcode','mark','random','reload_provider'):
        tokens = _tokens(args)
        if not tokens:
            issue([], '至少配置一个匹配值' if kind != 'reload_provider' else '至少引用一个 Provider', 'required')
        for token in tokens:
            if _deferred(token):
                continue
            if kind in ('qname','cname') and not token.startswith(('$','&')):
                domain(token, [])
            if kind in ('client_ip','ptr_ip','resp_ip') and not token.startswith(('$','&')):
                ip(token, [], True)
            if kind in ('question','reload_provider') and (not token.startswith('$') or not token[1:].strip()):
                issue([], '这里只接受 $tag 形式的 Provider 引用', 'provider-reference')
            if kind in ('qtype','qclass','rcode'):
                from backend.common.oxidns_validation import dns_code_valid
                if not dns_code_valid(kind, token):issue([], 'DNS 类型、类别或响应码无效', 'dns-code')
            if kind == 'mark' and (_uint(token, U32) is None):
                issue([], '标记必须是 0–4294967295 的整数', 'range')
        if kind == 'random' and not any(_deferred(t) for t in tokens):
            try:
                valid = len(tokens)==1 and math.isfinite(float(tokens[0])) and 0 <= float(tokens[0]) <= 1
            except (ValueError, IndexError):
                valid = False
            if not valid:
                issue([], '概率必须且只能填写一个 0–1 的数字字符串', 'range')
    if kind == 'env':
        entries = args if isinstance(args, list) else [args]
        if not entries or all(not _str(x) for x in entries):
            issue([], '至少配置一个环境变量条件', 'required')
        for j, entry in enumerate(entries):
            if isinstance(entry,str) and (not entry.strip() or not re.split(r'[=:]', entry, maxsplit=1)[0].strip()):
                issue([j] if isinstance(args,list) else [], '环境变量名称不能为空', 'required')
    if kind == 'any_match':
        entries = args if isinstance(args,list) else args.splitlines() if isinstance(args,str) else []
        if not any(_str(x) for x in entries):
            issue([], '至少配置一个匹配表达式', 'required')
    if kind == 'string_exp':
        raw = ' '.join(x for x in args if isinstance(x,str)) if isinstance(args,list) else args
        if isinstance(raw,str) and not _deferred(raw):
            tokens=raw.split(); source=op=''; values=[]
            if tokens:
                for candidate in ('contains','prefix','suffix','regexp','eq','zl'):
                    if tokens[0].endswith(candidate) and len(tokens[0])>len(candidate):
                        source,op,values=tokens[0][:-len(candidate)],candidate,tokens[1:];break
                else:
                    if len(tokens)>=2:source,op,values=tokens[0],tokens[1],tokens[2:]
            sources={'qname','qtype','qclass','rcode','resp_ip','mark','client_ip','server_name','url_path'}
            if source not in sources and not (source.startswith('$') and len(source)>1):
                issue([], '字符串表达式的来源字段无效', 'expression')
            if op not in ('eq','prefix','suffix','contains','regexp','zl'):
                issue([], '字符串表达式的操作无效', 'expression')
            elif (op=='zl' and values) or (op!='zl' and not values):
                issue([], 'zl 不接受参数；其他操作至少需要一个参数', 'expression')
            if op=='regexp':
                for value in values:
                    error=_regex_error(value)
                    if error:issue([], error, 'regexp')
    if kind == 'client_ip_from_ecs' and isinstance(args,list):
        for j,raw in enumerate(args):
            if _str(raw):ip(raw.strip(),[j],True)
    if kind == 'ttl':
        if isinstance(args,str) and not _deferred(args):
            tokens=args.split('-')
            if len(tokens)>2 or any(_uint(x.strip(), U32) is None for x in tokens):
                issue([], 'TTL 简写需为非负整数或 min-max，数值不得大于 4294967295', 'ttl')
        elif isinstance(args,dict) and not any(args.get(k) is not None for k in ('fix','min','max')):
            issue([], 'TTL 至少需要 fix、min 或 max 中的一个非空值', 'required')
    if kind == 'forward_edns0opt' and isinstance(args,str) and not _deferred(args):
        if any(_uint(token, 65535) is None for token in _tokens(args)):
            issue([], 'EDNS0 代码必须为 0–65535 的整数', 'range')
    if kind == 'black_hole':
        if isinstance(args, dict):
            raw_mode=args.get('mode')
            mode=raw_mode.strip().lower() if isinstance(raw_mode,str) else raw_mode
            ips=args.get('ips')
            if 'mode' in args and raw_mode is None:
                # A YAML null is not the "null" mode: the core silently answers nxdomain.
                issue(['mode'],'mode 为空时 OxiDNS 会按 nxdomain 处理；黑洞请写 "null"（带引号）','null-mode','warning')
            if isinstance(mode,str) and mode not in ('nxdomain','nodata','null','custom','refused') and not _deferred(mode):
                issue(['mode'],'拦截模式无效','enum')
            if isinstance(ips,list):
                for j,raw in enumerate(ips):
                    if _str(raw):ip(raw,['ips',j])
                ips=[raw for raw in ips if _str(raw)]
            if mode=='custom' and isinstance(ips,(list,type(None))) and not ips:
                issue(['ips'], 'custom 模式至少需要一个 IP', 'required')
            elif mode is not None and mode!='custom' and isinstance(ips,list) and ips:
                issue(['ips'], '只有 custom 模式可以配置 ips', 'mutually-exclusive')
        elif isinstance(args,(str,list)):
            tokens=_tokens(args);mode=None
            if isinstance(args,str) and tokens and tokens[-1].startswith('short_circuit='):
                value=tokens.pop().split('=',1)[1]
                if value not in ('true','false'):issue([], 'short_circuit 必须为 true 或 false', 'boolean')
            if isinstance(args,str) and tokens and tokens[0].lower() in ('nxdomain','nodata','null','custom','refused'):
                mode=tokens.pop(0).lower()
            if mode=='custom' and not tokens:issue([], 'custom 模式至少需要一个 IP', 'required')
            if mode and mode!='custom' and tokens:issue([], '只有 custom 模式可以附加 IP', 'mutually-exclusive')
            for token in tokens:ip(token, [])
    if not isinstance(args, dict):
        return issues
    if kind=='sequence':
        return issues
    if kind=='cache':
        if args.get('dump_file') is not None and args.get('dump_interval')==0:
            issue(['dump_interval'], '配置持久化文件时，保存间隔必须大于 0', 'range')
        compare('min_positive_ttl','max_positive_ttl')
    if kind in ('prefer_ipv4','prefer_ipv6') and nullable_default('cache',True) is True and args.get('cache_ttl')==0:
        issue(['cache_ttl'], '启用探测缓存时 TTL 必须大于 0', 'range')
    if kind=='ip_selector':
        methods=args.get('probe_methods')
        if methods is not None and isinstance(methods,(str,list)):
            # The official parser splits commas, not internal spaces.
            raw_methods=methods if isinstance(methods,list) else [methods]
            tokens=[piece.strip() for raw in raw_methods if isinstance(raw,str) for piece in raw.split(',') if piece.strip()]
            if not tokens:issue(['probe_methods'], '至少配置一种探测方法', 'required')
            for token in tokens:
                if _deferred(token):continue
                match=re.fullmatch(r'tcp:(\d+)',token)
                if token not in ('ping','none') and not (match and _uint(match[1], 65535) not in (None, 0)):
                    issue(['probe_methods'], '探测方法应为 ping、none 或 tcp:1–65535', 'probe-method')
            if 'none' in tokens and len(set(tokens))>1:
                issue(['probe_methods'], 'none 不能与其他探测方法同时使用', 'mutually-exclusive')
        cache=args.get('cache')
        if isinstance(cache,dict) and nullable_default('enabled',True,cache) is True:
            for key in ('size','ttl','failure_ttl'):
                if cache.get(key)==0:issue(['cache',key], '启用缓存时此值必须大于 0', 'range')
    if kind=='ecs_handler' and _str(args.get('preset')):
        ip(args['preset'],['preset'])
    if kind in ('hosts','redirect'):
        key='entries' if kind=='hosts' else 'rules'
        entries=args.get(key,[])
        if isinstance(entries,list):
            for j,entry in enumerate(entries):
                if not isinstance(entry,str) or _deferred(entry) or not entry.strip() or entry.lstrip().startswith('#'):continue
                tokens=entry.split()
                if (kind=='hosts' and len(tokens)<2) or (kind=='redirect' and len(tokens)!=2):
                    issue([key,j], '需填写域名规则及至少一个 IP' if kind=='hosts' else '重定向规则需恰好填写域名规则和目标域名', 'rule');continue
                # hosts/redirect matchers accept an empty normalized literal;
                # only their regexp syntax is validated by the core factory.
                if tokens[0].startswith('regexp:'):
                    error=_regex_error(tokens[0][7:])
                    if error:issue([key,j],error,'regexp')
                if kind=='hosts':
                    for token in tokens[1:]:ip(token,[key,j])
                elif len(tokens[1].rstrip('.').encode())>253 or any(not part or len(part.encode())>63 for part in tokens[1].rstrip('.').split('.')):
                    issue([key,j], '重定向的目标域名无效', 'domain')
    if kind in ('domain_set','dynamic_domain_set'):
        key='exps' if kind=='domain_set' else 'bootstrap_rules'
        entries=args.get(key,[])
        if isinstance(entries,list):
            for j,entry in enumerate(entries):
                if isinstance(entry,str):
                    if kind=='dynamic_domain_set':domain(entry,[key,j],True)
                    else:
                        for line in entry.splitlines():domain(line,[key,j])
    if kind=='arbitrary' and isinstance(args.get('rules'),list):
        for j,raw in enumerate(args['rules']):
            error=_zone_records_error(raw)
            if error:issue(['rules',j],error,'dns-record')
            if isinstance(raw,str) and re.search(r'(?m)^\s*\$(?:INCLUDE|GENERATE)\b',raw,re.I):
                issue(['rules',j], '外部 zone 文件和生成记录需要目标核心在初始化时检查', 'runtime-zone','warning')
    if kind=='response':
        for key in ('answers','authorities','additionals'):
            records=args.get(key)
            if isinstance(records,list):
                for j,raw in enumerate(records):
                    if isinstance(raw,str) and re.search(r'(?m)^\s*\$(?:INCLUDE|GENERATE)\b',raw,re.I):
                        issue([key,j], '外部 zone 文件和生成记录需要目标核心在初始化时检查', 'runtime-zone','warning')
    if kind=='adguard_rule' and isinstance(args.get('rules'),list):
        for j,raw in enumerate(args['rules']):
            if not isinstance(raw,str):continue
            for line in raw.splitlines():
                message,severity=_adguard_error(line)
                if message:issue(['rules',j],message,'adguard-rule',severity)
    if kind=='geosite' and isinstance(args.get('selectors'),list):
        for j,selector in enumerate(args['selectors']):
            if isinstance(selector,str) and '@' in selector and not _deferred(selector):
                code,attr=selector.strip().split('@',1)
                if not code.strip() or not attr.strip():issue(['selectors',j], '分类名称和 @ 后的属性名不能为空', 'selector')
    if kind=='ip_set' and isinstance(args.get('ips'),list):
        for j,entry in enumerate(args['ips']):
            if not isinstance(entry,str):continue
            for line in entry.splitlines():
                token=line.split('#',1)[0].strip()
                if token:ip(token,['ips',j],True)
    if kind=='nftset':
        for suffix,limit in [('4',32),('6',128)]:
            if args.get('ipv'+suffix) is None and all(args.get(key+suffix) is not None for key in ('table_family','table_name','set_name')):
                family=args.get('table_family'+suffix)
                if isinstance(family,str) and family not in ('ip','ip6','inet'):
                    issue(['table_family'+suffix], '地址族必须为 ip、ip6 或 inet', 'enum')
                for key in ('table_name','set_name'):
                    if isinstance(args.get(key+suffix),str) and not args[key+suffix].strip():issue([key+suffix], '名称不能为空', 'required')
                mask=args.get('mask'+suffix)
                if _number(mask) and mask>limit:issue(['mask'+suffix], '前缀长度不能超过 '+str(limit), 'range')
    if kind in ('ros_route','ros_address_list'):
        compare('min_ttl','max_ttl',(60,3600))
        families=('gateway4','gateway6') if kind=='ros_route' else ('address_list4','address_list6')
        if not any(_str(args.get(key)) for key in families):issue([families[0]], '至少配置 '+ ' 或 '.join(families), 'required')
        if isinstance(args.get('comment_prefix'),str) and any(c in args['comment_prefix'] for c in ';='):
            issue(['comment_prefix'], '注释前缀不能包含 ; 或 =', 'delimiter')
        tls=args.get('tls')
        if isinstance(tls,dict) and tls.get('insecure') is True and _str(tls.get('ca')):
            issue(['tls','ca'], '跳过 TLS 校验时不能同时指定 CA', 'mutually-exclusive')
        seconds=_duration(args.get('wait_timeout'))
        if seconds==0:issue(['wait_timeout'], '等待超时必须大于 0', 'range')
        persistent=args.get('persistent')
        if isinstance(persistent,dict) and isinstance(persistent.get('ips'),list):
            for j,raw in enumerate(persistent['ips']):
                if not isinstance(raw,str) or _deferred(raw):continue
                for line in raw.splitlines():
                    line=line.split('#',1)[0].strip()
                    if not line:continue
                    try:network=ipaddress.ip_network(line,strict=False)
                    except ValueError:
                        issue(['persistent','ips',j], '常驻地址需为有效 IP 或 CIDR', 'ip');continue
                    if not _str(args.get(families[0 if network.version==4 else 1])):
                        issue(['persistent','ips',j], '未设置该地址族的目标，OxiDNS 会忽略此常驻地址', 'ignored-family','warning')
                    if kind=='ros_route' and network.prefixlen==0:
                        issue(['persistent','ips',j], 'OxiDNS 会忽略 /0 默认路由', 'ignored-default-route','warning')
    if kind in ('http_request','learn_domain','upgrade'):
        timeout=args.get('timeout')
        if _str(timeout) and not _deferred(timeout) and _duration(timeout) is None:
            issue(['timeout'], '超时需为非负整数秒或带 ms/s/m/h/d 单位的时长', 'duration')
    if kind=='http_request':
        body_count=sum((args.get('body') is not None,args.get('json') is not None,bool(args.get('form'))))
        if body_count>1:issue(['body'], 'body、json 和非空 form 不能同时配置', 'mutually-exclusive')
        if args.get('content_type') is not None and (args.get('json') is not None or args.get('form')):
            issue(['content_type'], 'content_type 仅用于原始 body', 'dependency')
        for key in ('url','body','content_type'):
            value=args.get(key)
            if isinstance(value,str):
                template(value,[key])
                if key=='content_type' and ('\r' in value or '\n' in value):issue([key], 'HTTP 头值不能包含换行', 'http-header')
        url=args.get('url')
        if isinstance(url,str) and not _deferred(url):
            try:parsed=urlsplit(url);valid=parsed.scheme.lower() in ('http','https') and bool(parsed.hostname)
            except ValueError:valid=False
            if not valid:issue(['url'],'需要有效的 HTTP / HTTPS URL','url')
        for key in ('headers','query_params','form'):
            mapping=args.get(key)
            if isinstance(mapping,dict):
                for name,value in mapping.items():
                    path=[key,name]
                    if key!='query_params' and not _str(name):issue(path,'名称不能为空','required')
                    if key=='headers' and isinstance(name,str) and not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+",name.strip()):issue(path,'HTTP 请求头名称无效','http-header')
                    if isinstance(value,str) and not value.strip():issue(path,'请求模板值不能为空','required')
                    template(value,path)
    if kind=='script':
        if isinstance(args.get('args'),list):
            for j,value in enumerate(args['args']):template(value,['args',j])
        if isinstance(args.get('env'),dict):
            for key,value in args['env'].items():
                if not _str(key):issue(['env',key],'环境变量名称不能为空','required')
                template(value,['env',key])
    if kind=='download' and isinstance(args.get('downloads'),list):
        seen=set()
        for j,item in enumerate(args['downloads']):
            if not isinstance(item,dict) or _deferred(item.get('url')) or _deferred(item.get('dir')):continue
            try:filename=_str(item.get('filename')) or urlsplit(_str(item.get('url'))).path.rsplit('/',1)[-1].strip()
            except ValueError:continue
            if not filename:issue(['downloads',j,'filename'], 'URL 无法推导文件名，请填写 filename', 'required')
            path=os.path.join(_str(item.get('dir')),filename)
            if path in seen:issue(['downloads',j], '下载目标文件重复', 'duplicate')
            seen.add(path)
    if kind in ('cron','time'):
        timezone=args.get('timezone')
        if kind=='cron' and _str(timezone) and not _deferred(timezone):
            try:ZoneInfo(timezone.strip())
            except (ZoneInfoNotFoundError,ValueError):issue(['timezone'], '需要有效的 IANA 时区，例如 Asia/Shanghai', 'timezone')
    if kind=='time' and isinstance(args.get('periods'),list):
        for j,period in enumerate(args['periods']):
            if not isinstance(period,dict):continue
            start,end=period.get('start'),period.get('end')
            if (start is None)!=(end is None):issue(['periods',j], 'start 和 end 必须同时填写或同时省略', 'dependency')
            for key,value in [('start',start),('end',end)]:
                if isinstance(value,str) and not _deferred(value) and not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',value.strip()):
                    issue(['periods',j,key], '时间需为 00:00–23:59 的 HH:MM', 'time')
            if isinstance(start,str) and isinstance(end,str) and not _deferred(start) and start.strip()==end.strip():
                issue(['periods',j,'end'], '开始和结束时间不能相同', 'range')
            if start is None and not period.get('weekdays') and not period.get('monthdays'):
                issue(['periods',j], '省略时间段时至少设置星期或月日', 'required')
            if isinstance(period.get('weekdays'),list):
                for k,day in enumerate(period['weekdays']):
                    if isinstance(day,str) and not _deferred(day) and day.strip().lower() not in ('mon','tue','wed','thu','fri','sat','sun'):
                        issue(['periods',j,'weekdays',k], '星期为 mon–sun 或数字 1–7；数字字符串不支持', 'weekday')
    if kind=='cron' and isinstance(args.get('jobs'),list):
        names=set()
        for j,job in enumerate(args['jobs']):
            if not isinstance(job,dict):continue
            name=_str(job.get('name'))
            if name in names:issue(['jobs',j,'name'], '任务名称重复', 'duplicate')
            names.add(name)
            schedule,interval=job.get('schedule'),job.get('interval')
            if bool(_str(schedule))==bool(_str(interval)):
                issue(['jobs',j], 'schedule 和 interval 必须且只能填写一个', 'mutually-exclusive')
            if _str(schedule):
                error=_cron_error(schedule)
                if error:issue(['jobs',j,'schedule'],error,'cron')
            if _str(interval) and not _deferred(interval):
                seconds=_duration(interval)
                if seconds is None or seconds<60:issue(['jobs',j,'interval'], '定时间隔至少为 1 分钟', 'range')
    return issues


# Zone text is parsed without opening $INCLUDE files. dnspython supplies the
# DNS name/RDATA parser; the wrapper retains the official zoneparser's defaults,
# limited text-RDATA support, and response placeholder rules.
def _zone_records_error(raw, single=False):
    import dns.name
    import dns.rdata
    import dns.rdataclass
    import dns.rdatatype
    import dns.tokenizer
    import dns.ttl
    from backend.common.oxidns_validation import dns_code_valid

    if not isinstance(raw, str) or _deferred(raw):
        return None
    if single:
        if raw.count('{qname}') > 1 or raw.count('{qclass}') > 1:
            return '每条响应记录中的 {qname} 和 {qclass} 各只能出现一次'
    source = raw.replace('{qname}', 'configflow-placeholder.invalid.').replace('{qclass}', 'CH') if single else raw
    tokenizer = dns.tokenizer.Tokenizer(source)
    origin = None
    current_owner = None
    current_class = 'IN'
    record_count = 0
    unresolved_include = False
    current_ttl = 3600
    allowed_text = set('A AAAA CNAME NS PTR DNAME ANAME MD MF MB MG MR NSAP-PTR NSAPPTR MX RT AFSDB RP MINFO HINFO TXT SPF AVC RESINFO SOA SRV NAPTR CAA'.split())
    name_positions = {name: [0] for name in 'CNAME NS PTR DNAME ANAME MD MF MB MG MR NSAP-PTR NSAPPTR'.split()}
    name_positions.update({'MX':[1], 'RT':[1], 'AFSDB':[1], 'RP':[0,1], 'MINFO':[0,1], 'SOA':[0,1], 'SRV':[3], 'NAPTR':[5]})
    try:
        while True:
            first = tokenizer.get(want_leading=True)
            leading = first.is_whitespace()
            if leading:
                first = tokenizer.get()
            if first.is_eof():
                break
            if first.is_eol():
                continue
            tokens = [first]
            while True:
                token = tokenizer.get()
                if token.is_eol_or_eof():
                    break
                tokens.append(token)
            values = [token.value for token in tokens]
            if values[0].startswith('$'):
                directive = values[0].upper()
                if directive == '$ORIGIN' and len(values) == 2:
                    origin = dns.name.from_text(values[1], origin=origin)
                    if not origin.is_absolute():return '$ORIGIN 必须能解析为绝对域名'
                    current_owner = None
                    continue
                if directive == '$TTL' and len(values) == 2:
                    current_ttl = dns.ttl.from_text(values[1])
                    if current_ttl > U32:return 'DNS 记录 TTL 超出 0–4294967295'
                    continue
                if directive == '$INCLUDE':
                    # Caller reports the unresolved file as a warning. Never
                    # allow server-side configuration validation to read it.
                    if len(values) not in (2,3):return '$INCLUDE 需要文件路径和可选 origin'
                    if not os.path.isabs(values[1]):return '内联 zone 中的 $INCLUDE 必须使用绝对路径'
                    unresolved_include = True
                    continue
                if directive == '$GENERATE':
                    # OxiDNS and dnspython both support this zone extension;
                    # validate the declaration here without expanding unbounded
                    # record ranges in a request-handling process.
                    if len(values)<5:return '$GENERATE 需要范围、名称、类型和 RDATA'
                    match=re.fullmatch(r'(\d+)-(\d+)(?:/(\d+))?', values[1])
                    if not match or any(_uint(part, 9223372036854775807) is None for part in (match[1],match[2],match[3] or '1')) or _uint(match[1])>_uint(match[2]) or _uint(match[3] or '1')==0:return '$GENERATE 范围或步长无效'
                    record_count += (_uint(match[2]) - _uint(match[1])) // _uint(match[3] or '1') + 1
                    if single and record_count > 1:return '响应记录条目不能包含多条生成记录'
                    continue
                return '不支持的 zone 指令或指令参数不完整'
            offset = 0
            if not leading:
                if tokens[0].is_quoted_string():return 'DNS 记录的域名不能放在引号内'
                current_owner = dns.name.from_text(values[0], origin=origin)
                offset = 1
            if current_owner is None:return '省略记录名称前必须先定义名称'
            if not current_owner.is_absolute():return '相对域名需要 $ORIGIN；否则名称必须以 . 结尾'
            rtype = None
            klass = current_class
            ttl = current_ttl
            type_index = None
            while offset < len(values):
                candidate = values[offset].upper()
                if re.fullmatch(r'TYPE\d+',candidate) and _uint(candidate[4:],65535) is not None or (not candidate.isdigit() and dns_code_valid('qtype',candidate)):
                    rtype=candidate;type_index=offset;offset+=1;break
                if dns_code_valid('qclass',candidate) and not candidate.isdigit():
                    klass=candidate
                else:
                    ttl=dns.ttl.from_text(values[offset])
                    if ttl>U32:return 'DNS 记录 TTL 超出 0–4294967295'
                offset+=1
            if rtype is None:return 'DNS 记录缺少类型和 RDATA'
            if single:
                original_tokens = re.findall(r'"(?:\\.|[^"\\])*"|\S+',raw)
                if '{qname}' in raw and (not original_tokens or original_tokens[0]!='{qname}'):
                    return '{qname} 只能作为未加引号的记录名称'
                if '{qclass}' in raw and not any(token=='{qclass}' for token in original_tokens[1:type_index]):
                    return '{qclass} 只能作为类型前的未加引号的记录类别'
            data_tokens=tokens[offset:]
            data_values=values[offset:]
            if not data_values:return 'DNS 记录缺少 RDATA'
            if data_values[0] in (r'\#','#'):
                if len(data_values)<2 or not data_values[1].isdigit():return 'RFC3597 记录需要长度和十六进制数据'
                hexdata=''.join(data_values[2:])
                if _uint(data_values[1],65535) is None or len(hexdata)!=_uint(data_values[1],65535)*2 or re.search(r'[^0-9a-fA-F]',hexdata):return 'RFC3597 数据长度或十六进制格式无效'
            else:
                if rtype not in allowed_text:
                    return '该记录类型在 OxiDNS 中需要 RFC3597 格式：\\# 长度 十六进制数据'
                for position in name_positions.get(rtype,[]):
                    if position>=len(data_values):return 'DNS 记录的 RDATA 参数不足'
                    name=dns.name.from_text(data_values[position],origin=origin)
                    if not name.is_absolute():return 'RDATA 中的相对域名需要 $ORIGIN；否则必须以 . 结尾'
                parsed_type=rtype
                if rtype in ('ANAME','MD','MF','MB','MG','MR','NSAPPTR','NSAP-PTR'):parsed_type='CNAME'
                if rtype in ('AVC','RESINFO'):parsed_type='TXT'
                encoded=' '.join(('"'+t.value+'"') if t.is_quoted_string() else t.value for t in data_tokens)
                dns.rdata.from_text(dns.rdataclass.IN,dns.rdatatype.from_text(parsed_type),encoded,origin=origin,relativize=False)
            current_class,current_ttl=klass,ttl
            record_count+=1
    except (ValueError, TypeError, dns.exception.DNSException):
        return 'DNS 记录的名称、TTL、类型或 RDATA 格式无效'
    if single and (record_count > 1 or (record_count != 1 and not unresolved_include)):
        return '每个响应记录条目必须恰好包含一条 DNS 记录'
    return None


def validate_plugin_format(name, value):
    """Plugin-specific formats invoked by the shared validation engine."""
    if name == 'dns-record':
        return _zone_records_error(value, single=True)
    if name == 'domain-expression':
        return _domain_error(value)
    raise ValueError('Unsupported plugin validation format: '+str(name))


def _adguard_error(raw):
    """Validate the DNS-filter subset; unsupported rules remain warnings."""
    from backend.common.oxidns_validation import dns_code_valid
    raw=raw.strip()
    if not raw or raw.startswith(('!','#')) or _deferred(raw):return None,'warning'
    if any(marker in raw for marker in ('##','#@#','#$#','#@$#','#%#','#@%#','#?#','#@?#')):
        return '此为非 DNS 过滤规则，OxiDNS 会跳过','warning'
    words=raw.split()
    if len(words)>1:
        try:ipaddress.ip_address(words[0]);return 'Hosts 格式规则会被 AdGuard Provider 跳过','warning'
        except ValueError:pass
    if raw.startswith('@@'):raw=raw[2:].strip()
    modifiers=None
    if raw.startswith('/'):
        if raw.endswith('/') and len(raw)>1:pattern=raw
        elif '/$' in raw:
            pattern,modifiers=raw.rsplit('/$',1);pattern+='/'
        else:return '路径或 URL 过滤规则不用于 DNS，OxiDNS 会跳过','warning'
    else:
        pattern,sep,modifiers=raw.partition('$')
        if not sep:modifiers=None
    pattern=pattern.strip()
    parsed=[]
    if modifiers is not None:
        parsed=[part.strip().partition('=') for part in modifiers.split(',') if part.strip()]
    if not pattern:
        if modifiers is None or any(name.lower() in ('important','badfilter','denyallow','dnstype') for name,_,_ in parsed):
            return 'AdGuard 规则的匹配模式不能为空','error'
        return '规则仅含不支持的修饰符，OxiDNS 会跳过','warning'
    if not pattern.startswith('/') and re.search(r'[/?:#\s]',pattern):
        return '路径或 URL 过滤规则不用于 DNS，OxiDNS 会跳过','warning'
    if any(name.lower() not in ('important','badfilter','denyallow','dnstype') for name,_,_ in parsed):
        return '规则含 OxiDNS 不支持的修饰符，整条规则会跳过','warning'
    for name,separator,value in parsed:
        name=name.lower()
        if name in ('denyallow','dnstype') and not separator:return 'denyallow/dnstype 修饰符需要等号和值','error'
        if name=='denyallow':
            if any(not re.fullmatch(r'[A-Za-z0-9._-]+',domain.strip().rstrip('.')) for domain in value.split('|') if domain.strip()):
                return 'denyallow 中需要合法域名','error'
        if name=='dnstype':
            for token in value.split('|'):
                token=token.strip().lstrip('~')
                if token and (token.isdigit() or not dns_code_valid('qtype',token)):
                    return 'dnstype 中需要有效的 DNS 类型名称','error'
    if pattern.startswith('/'):
        regex=pattern[1:-1]
        if not regex.strip():return 'AdGuard 正则不能为空','error'
        error=_regex_error(regex)
        return error,'error' if error else 'warning'
    normalized=pattern.rstrip('.').lower()
    if not normalized:return 'AdGuard 匹配域名不能为空','error'
    rest=normalized[2:] if normalized.startswith('||') else normalized[1:] if normalized.startswith('|') else normalized
    rest=rest[:-1] if rest.endswith('|') else rest
    if '|' in rest:return 'AdGuard 模式内部不能包含 |','error'
    return None,'warning'
