"""Build self-contained, byte-preserving Agent deployment archives.

All callbacks are resolved from the captured profile, never from a second HTTP
request to ConfigFlow. A missing resource aborts publication instead of leaving
an Agent to download a partial configuration.
"""
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import gzip
import io
import ipaddress
import json
from pathlib import PurePosixPath
import re
import tarfile
from threading import Lock
from urllib.parse import parse_qs, unquote, urlsplit
import uuid

import requests
import yaml

from backend.converters.mosdns import parse_clash_rule_value
from backend.utils.rule_fetch import is_internal_rule_url, request_rule

MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_FILES = 4096
MAX_RAW_CACHE_BYTES = 16 * 1024 * 1024
MAX_RAW_CACHE_ENTRIES = 128


class DeploymentPreparationError(ValueError):
    """An artifact cannot be published as a complete deployment."""


class RuleDownloadError(DeploymentPreparationError):
    """A rule source could not be fetched before contacting the Agent."""


def _download_error_message(exc):
    # Requests exceptions can contain rule tokens or proxy credentials.
    if isinstance(exc, requests.exceptions.Timeout):
        return '下载超时，请检查网络连接或稍后重试'
    if isinstance(exc, requests.exceptions.ProxyError):
        return '代理连接失败，请检查规则下载代理是否可用'
    if isinstance(exc, requests.exceptions.SSLError):
        return '无法建立 HTTPS 安全连接，请检查规则源及其证书'
    if isinstance(exc, requests.exceptions.ConnectionError):
        return '连接失败，请检查规则下载地址及网络连接'
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        return f'下载服务器返回错误（HTTP {exc.response.status_code}），请检查规则下载地址或稍后重试'
    return '下载失败，请检查规则下载地址及网络连接'


@dataclass(frozen=True)
class DeploymentBundle:
    manifest: dict
    archive: bytes
    sha256: str
    config_version: str


def relative_path(value):
    if not isinstance(value, str) or not value or '\\' in value or any(ord(c) < 32 for c in value):
        raise DeploymentPreparationError('Invalid deployment file path')
    while value.startswith('./'):
        value = value[2:]
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(p in ('..', '.', '') for p in value.split('/')):
        raise DeploymentPreparationError('Deployment files must use safe relative paths')
    if value.startswith('.configflow') or value == 'manifest.json':
        raise DeploymentPreparationError('Reserved deployment file path')
    return path.as_posix()


def deployment_content_fingerprint(manifest):
    """Identify all materialized delivery bytes, independently of a transaction.

    The manifest must describe a complete validated bundle. This function does
    not fetch resources or inspect their contents: its digests come from the
    materialization step. An Agent's installed main filename is presentation
    metadata; every other managed file path remains part of the identity.
    """
    if (not isinstance(manifest, dict)
            or type(manifest.get('protocol_version')) is not int
            or manifest['protocol_version'] != 1
            or manifest.get('service_type') not in ('mihomo', 'mosdns', 'oxidns')):
        raise DeploymentPreparationError('Invalid deployment content manifest')

    def manifest_path(value):
        path = relative_path(value)
        if path != value or '\x7f' in path:
            raise DeploymentPreparationError('Manifest paths must be canonical relative paths')
        return path

    config_path = manifest_path(manifest.get('config_path'))
    if '/' in config_path:
        raise DeploymentPreparationError('Agent configuration filename must be a basename')
    entries = manifest.get('files')
    if not isinstance(entries, list) or not entries or len(entries) > MAX_FILES:
        raise DeploymentPreparationError('Manifest must contain a complete deployment file list')

    files, paths, canonical_paths = [], set(), set()
    config_count, total_size = 0, 0
    for item in entries:
        if not isinstance(item, dict):
            raise DeploymentPreparationError('Invalid deployment manifest file')
        path = manifest_path(item.get('path'))
        role, size, digest = item.get('role'), item.get('size'), item.get('sha256')
        if (not isinstance(role, str) or role not in ('config', 'provider', 'rule', 'custom')
                or type(size) is not int or not 0 <= size <= MAX_FILE_BYTES
                or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', digest)):
            raise DeploymentPreparationError('Invalid deployment manifest file metadata')
        if path in paths:
            raise DeploymentPreparationError('Duplicate deployment manifest path')
        paths.add(path)
        if role == 'config':
            config_count += 1
            if path != config_path or size == 0:
                raise DeploymentPreparationError('Invalid main configuration manifest entry')
            path = 'config.yaml'
        elif path == config_path:
            raise DeploymentPreparationError('Main configuration entry must have config role')
        if path in canonical_paths:
            raise DeploymentPreparationError('Conflicting canonical deployment paths')
        canonical_paths.add(path)
        total_size += size
        if total_size > MAX_TOTAL_BYTES:
            raise DeploymentPreparationError('Deployment exceeds size limit')
        files.append({'path': path, 'size': size, 'sha256': digest.lower(), 'role': role})
    if config_count != 1:
        raise DeploymentPreparationError('Manifest must contain exactly one main configuration')

    content = {'version': 1, 'service_type': manifest['service_type'],
               'files': sorted(files, key=lambda item: item['path'])}
    encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return 'v1:' + hashlib.sha256(encoded).hexdigest()


def _local_rule(url, config, base_url):
    """Return the snapshot's library entry only for a recognized local URL."""
    from backend.common.profile_context import profile_api_path
    parsed = urlsplit(url)
    settings = deepcopy(config.get('system_config', {}))
    settings['server_domain'] = settings.get('server_domain') or base_url
    if (parsed.scheme or parsed.netloc) and not is_internal_rule_url(url, {'system_config': settings}):
        return None
    prefix = urlsplit(settings['server_domain']).path.rstrip('/')
    path = unquote(parsed.path)
    for rule in config.get('rule_library', []):
        paths = [profile_api_path(config, '/rules/local/' + rule.get('name', '')),
                 profile_api_path(config, '/rule-library/content/' + rule.get('id', ''))]
        if path in paths or path in [prefix + p for p in paths]:
            return rule
    if '/api/' in path or path.startswith('/rules/local/') or path.startswith('/rule-library/content/'):
        raise DeploymentPreparationError('Rule callback is not part of this profile snapshot')
    return None


def _fetch_bytes(url, config):
    if not url:
        raise DeploymentPreparationError('Required rule has no source URL')
    response = None
    try:
        response = request_rule(url, timeout=(10, 60), config_data=config, stream=True)
        response.raise_for_status()
        value = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            value.extend(chunk)
            if len(value) > MAX_FILE_BYTES:
                raise DeploymentPreparationError('Rule exceeds deployment file size limit')
        return bytes(value)
    except DeploymentPreparationError:
        raise
    except Exception as exc:
        raise RuleDownloadError(_download_error_message(exc)) from None
    finally:
        if response is not None:
            response.close()


def materialize_rule(url, config, base_url, *, depth=0, raw_mosdns=False, fetch_raw=None):
    if depth > 3:
        raise DeploymentPreparationError('Recursive rule callback')
    parsed = urlsplit(url)
    settings = {**config.get('system_config', {}),
                'server_domain': config.get('system_config', {}).get('server_domain') or base_url}
    internal = not (parsed.scheme or parsed.netloc) or is_internal_rule_url(url, {'system_config': settings})
    if internal and parsed.path.endswith(('/mosdns/rule-proxy', '/oxidns/rule-proxy')):
        original = parse_qs(parsed.query).get('url', [''])[0]
        if not original:
            raise DeploymentPreparationError('Rule conversion callback has no source')
        value = materialize_rule(original, config, base_url, depth=depth + 1,
                                 raw_mosdns=raw_mosdns, fetch_raw=fetch_raw)
        # The final consuming plugin chooses domain/IP conversion for bundles.
        return value if raw_mosdns else convert_mosdns_rules(value)
    rule = _local_rule(url, config, base_url)
    if rule is not None:
        if rule.get('source_type') == 'content':
            return rule.get('content', '').encode('utf-8')
        source = rule.get('url', '')
        if source == url:
            raise DeploymentPreparationError('Rule callback refers to itself')
        return materialize_rule(source, config, base_url, depth=depth + 1,
                                raw_mosdns=raw_mosdns, fetch_raw=fetch_raw)
    return fetch_raw(url) if fetch_raw is not None else _fetch_bytes(url, config)


def convert_mosdns_rules(content, rule_type='domain_set'):
    """Convert text for the final MosDNS domain_set or ip_set consumer."""
    if rule_type not in ('domain_set', 'ip_set'):
        raise DeploymentPreparationError('Unsupported MosDNS rule plugin type')
    try:
        text = content.decode('utf-8-sig')
    except UnicodeError:
        raise DeploymentPreparationError('MosDNS requires text rules; binary MRS is only supported by Mihomo') from None
    if '\x00' in text:
        raise DeploymentPreparationError('MosDNS rule source is binary')
    lines = text.splitlines()
    if 'payload:' in text:
        try:
            document = yaml.safe_load(text)
            lines = document['payload']
            if not isinstance(lines, list) or any(not isinstance(line, str) for line in lines):
                raise ValueError
        except Exception:
            raise DeploymentPreparationError('Invalid rule YAML payload') from None
    converted = []
    mapping = {'DOMAIN': 'full', 'DOMAIN-SUFFIX': 'domain', 'DOMAIN-KEYWORD': 'keyword', 'DOMAIN-REGEX': 'regexp'}
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if rule_type == 'ip_set':
            if re.match(r'^(domain|full|keyword|regexp):', line):
                continue
            value = line[3:].strip() if line.startswith('ip:') else line
            if ',' in line:
                kind, value = parse_clash_rule_value(line)
                if kind not in ('IP-CIDR', 'IP-CIDR6'):
                    continue
            try:
                ipaddress.ip_network(value, strict=False)
            except ValueError:
                raise DeploymentPreparationError('Invalid IP network in MosDNS ip_set rule source') from None
            converted.append(value)
            continue
        if line.startswith('ip:'):
            continue
        if re.match(r'^(domain|full|keyword|regexp):', line):
            converted.append(line)
        elif ',' in line:
            # Classical provider regex commas are payload; ordinary rules
            # take only their first value field, ignoring subsequent params.
            kind, value = parse_clash_rule_value(line)
            if kind in mapping:
                converted.append(f'{mapping[kind]}:{value}')
            # Domain consumers omit IP/process rules, as rule-proxy did for
            # Clash input. In particular, a bare IPv6 CIDR cannot load in domain_set.
        elif line.startswith('+.'):
            converted.append('domain:' + line[2:])
        elif line.startswith('*.'):
            converted.append(r'regexp:^[^.]+\.' + re.escape(line[2:]) + '$')
        elif line.startswith('.'):
            converted.append(r'regexp:.+\.' + re.escape(line[1:]) + '$')
        else:
            try:
                ipaddress.ip_network(line, strict=False)
                # Raw address/CIDR lists must not reach a domain_set either.
            except ValueError:
                if '.' not in line or any(c.isspace() for c in line) or '<' in line or '>' in line:
                    raise DeploymentPreparationError('Unrecognized MosDNS rule source content') from None
                converted.append('full:' + line)
    return '\n'.join(converted).encode('utf-8')


def build_deployment_bundle(agent, config, config_content, *, provider_downloads=(),
                            ruleset_downloads=(), custom_files=(), base_url='', deployment_id=None,
                            progress=None):
    """Pack the exact generated graph and all its managed file references."""
    config = deepcopy(config)
    service = agent.get('service_type', 'mihomo')
    if service not in ('mihomo', 'mosdns', 'oxidns'):
        raise DeploymentPreparationError('Unsupported transactional deployment service')
    main = yaml.safe_load(config_content)
    if not isinstance(main, dict):
        raise DeploymentPreparationError('Generated configuration must be a YAML mapping')
    if service == 'oxidns' and main.get('include'):
        raise DeploymentPreparationError('OxiDNS deployment must inline included plugin definitions')
    rule_types = {}
    if service in ('mosdns', 'oxidns'):
        for plugin in main.get('plugins', []):
            rule_type = plugin.get('type')
            if rule_type not in ('domain_set', 'ip_set'):
                continue
            args = plugin.get('args') or {}
            if not isinstance(args, dict) or not isinstance(args.get('files', []), list):
                raise DeploymentPreparationError('MosDNS file references must be a list')
            for reference in args.get('files', []):
                if service == 'oxidns':
                    from backend.common.oxidns_native import relative_resource
                    if relative_resource(reference) is None:
                        continue
                path = relative_path(reference)
                if path in rule_types and rule_types[path] != rule_type:
                    raise DeploymentPreparationError('MosDNS file is used by both domain_set and ip_set: ' + path)
                rule_types[path] = rule_type
    files = {}

    def add(path, value, role, *, native=False):
        path = relative_path(path)
        if service == 'oxidns' and (path == 'state' or path.startswith('state/')):
            raise DeploymentPreparationError('OxiDNS runtime state cannot be managed by a deployment')
        value = value.encode('utf-8') if isinstance(value, str) else value
        if not isinstance(value, bytes):
            raise DeploymentPreparationError('Missing deployment file content')
        # OxiDNS attachments already contain native provider syntax. In
        # particular, a bare domain is a suffix rule, not MosDNS's full: rule.
        # Only downloaded/shared rule sources go through the rule converter.
        if path in rule_types and not (service == 'oxidns' and native):
            value = convert_mosdns_rules(value, rule_types[path])
        if len(value) > MAX_FILE_BYTES:
            raise DeploymentPreparationError('Deployment file exceeds size limit')
        if path in files:
            raise DeploymentPreparationError('Duplicate deployment path: ' + path)
        if len(files) >= MAX_FILES or sum(len(item[0]) for item in files.values()) + len(value) > MAX_TOTAL_BYTES:
            raise DeploymentPreparationError('Deployment exceeds size limit')
        files[path] = (value, role)
        return path

    for item in provider_downloads:
        add(item['local_path'], item.get('content'), 'provider')
    for item in custom_files:
        add(item['path'], item.get('content'), 'custom', native=item.get('native', False))

    # Use the final Mihomo configuration rather than reconstructing paths from
    # source suffixes; custom configuration can change provider format/path.
    downloads = list(ruleset_downloads)
    if service == 'mihomo' and 'rule-providers' in main:
        downloads = []
        for name, definition in main['rule-providers'].items():
            if definition.get('type') == 'inline':
                definition.pop('path', None)
                continue
            if not definition.get('path'):
                raise DeploymentPreparationError(f'Rule provider {name} has no bundled path')
            path = relative_path(definition['path'])
            if path in files:
                continue
            if not definition.get('url'):
                raise DeploymentPreparationError(f'Rule provider {name} references an unbundled local file; use a managed rule source')
            downloads.append({'name': name, 'url': definition['url'], 'local_path': path})
    # Share only original response bytes within this captured deployment. A
    # classical source may feed both domain and IP files through different
    # local callbacks; conversion and per-file/total limits still run for each
    # destination. Bound retained originals too: a large comment-only source
    # can become a very small converted file. Never reuse these responses
    # across separate publications.
    raw_downloads = OrderedDict()
    pending_downloads = {}
    cached_bytes = 0
    download_lock = Lock()

    def fetch_raw(url):
        nonlocal cached_bytes
        with download_lock:
            cached = raw_downloads.get(url)
            if cached is not None:
                raw_downloads.move_to_end(url)
                return cached
            future = pending_downloads.get(url)
            owner = future is None
            if owner:
                future = pending_downloads[url] = Future()
        if not owner:
            return future.result()
        # The owning worker fetches directly, without scheduling a second
        # pool task or holding the lock while other consumers wait.
        try:
            value = _fetch_bytes(url, config)
            with download_lock:
                if len(value) <= MAX_RAW_CACHE_BYTES and MAX_RAW_CACHE_ENTRIES > 0:
                    while raw_downloads and (cached_bytes + len(value) > MAX_RAW_CACHE_BYTES
                                             or len(raw_downloads) >= MAX_RAW_CACHE_ENTRIES):
                        _, evicted = raw_downloads.popitem(last=False)
                        cached_bytes -= len(evicted)
                    raw_downloads[url] = value
                    cached_bytes += len(value)
            future.set_result(value)
            return value
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with download_lock:
                pending_downloads.pop(url, None)

    def download(item):
        try:
            value = materialize_rule(item.get('url', ''), config, base_url,
                                     raw_mosdns=relative_path(item['local_path']) in rule_types,
                                     fetch_raw=fetch_raw)
        except RuleDownloadError as exc:
            name = item.get('name') or '未命名'
            raise DeploymentPreparationError(
                f'无法下载「{name}」规则集：{exc}。本次发布已停止，设备仍使用原配置。') from None
        except DeploymentPreparationError as exc:
            raise DeploymentPreparationError(f"Rule {item.get('name', 'unnamed')}: {exc}") from None
        return item, value
    if progress is not None:
        progress(0, len(downloads))
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = (pool.submit(download, item) for item in downloads)
            for done, future in enumerate(as_completed(futures), 1):
                item, value = future.result()
                add(item['local_path'], value, 'rule')
                if progress is not None:
                    progress(done, len(downloads))
    finally:
        raw_downloads.clear()
        pending_downloads.clear()

    if service == 'mihomo':
        for section in ('proxy-providers', 'rule-providers'):
            for name, provider in (main.get(section) or {}).items():
                if 'path' not in provider:
                    if provider.get('type') == 'inline':
                        continue
                    raise DeploymentPreparationError(f'Provider {name} has no bundled path')
                path = relative_path(provider['path'])
                if path not in files:
                    raise DeploymentPreparationError(f'Provider {name} has no bundled file')
                provider['path'] = './' + path
    elif service == 'oxidns':
        from backend.common.oxidns_native import validate_resource_files
        from backend.common.oxidns_settings import OxidnsSettingsError
        try:
            validate_resource_files(main, files)
        except OxidnsSettingsError as exc:
            raise DeploymentPreparationError(str(exc).replace(
                'OxiDNS 配置引用了未打包的资源文件：', 'OxiDNS references an unbundled resource file: ')) from None
    else:
        for plugin in main.get('plugins', []):
            args = plugin.get('args') or {}
            if not isinstance(args, dict):
                continue
            paths = args.get('files', [])
            if not isinstance(paths, list):
                raise DeploymentPreparationError('MosDNS file references must be a list')
            for index, value in enumerate(paths):
                path = relative_path(value)
                if path not in files:
                    raise DeploymentPreparationError('MosDNS references an unbundled file: ' + path)
                paths[index] = './' + path
    config_bytes = yaml.safe_dump(main, allow_unicode=True, sort_keys=False).encode('utf-8')
    add('config.yaml', config_bytes, 'config')
    from backend.common.config import get_default_profile_id
    manifest = {
        'protocol_version': 1, 'deployment_id': deployment_id or uuid.uuid4().hex,
        'agent_id': agent['id'], 'profile_id': config.get('profile_id') or agent.get('profile_id') or get_default_profile_id(),
        'service_type': service, 'config_path': 'config.yaml',
        'files': [{'path': path, 'size': len(value), 'sha256': hashlib.sha256(value).hexdigest(), 'role': role}
                  for path, (value, role) in sorted(files.items())],
    }
    return _pack(manifest, files, hashlib.sha256(config_bytes).hexdigest()[:8])


def _pack(manifest, files, config_version):
    archive = io.BytesIO()
    with gzip.GzipFile(fileobj=archive, mode='wb', mtime=0) as compressed, \
            tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as tar:
        members = [('manifest.json', json.dumps(manifest, ensure_ascii=False).encode('utf-8'))]
        members.extend(('files/' + path, value) for path, (value, _) in sorted(files.items()))
        for path, value in members:
            member = tarfile.TarInfo(path)
            member.size = len(value)
            member.mode = 0o600
            tar.addfile(member, io.BytesIO(value))
    content = archive.getvalue()
    return DeploymentBundle(manifest, content, hashlib.sha256(content).hexdigest(), config_version)


def retarget_config_path(bundle, config_path):
    """Honor the target's installed config filename without changing config bytes."""
    path = relative_path(config_path)
    if '/' in path:
        raise DeploymentPreparationError('Agent configuration filename must be a basename')
    current = bundle.manifest['config_path']
    if path == current:
        return bundle
    manifest = deepcopy(bundle.manifest)
    if any(item['path'] == path for item in manifest['files']):
        raise DeploymentPreparationError('Agent configuration filename collides with a managed file')
    manifest['config_path'] = path
    files = {}
    with tarfile.open(fileobj=io.BytesIO(bundle.archive), mode='r:gz') as archive:
        for item in manifest['files']:
            old_path = item['path']
            if old_path == current:
                item['path'] = path
            files[item['path']] = (archive.extractfile('files/' + old_path).read(), item['role'])
    return _pack(manifest, files, bundle.config_version)
