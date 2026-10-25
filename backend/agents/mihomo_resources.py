"""Prepare missing Geo databases alongside a Mihomo publication.

Only the core's default URLs are defined here; a profile's geox-url always
overrides them. No mirror repository, branch or filename is substituted.
"""
from copy import deepcopy
import hashlib
import io
from pathlib import PurePosixPath
import re
import tarfile

import yaml

from backend.agents.deployment_bundle import (
    DeploymentPreparationError, MAX_FILE_BYTES, MAX_FILES, MAX_TOTAL_BYTES,
    RuleDownloadError, _fetch_bytes, _pack,
)
from backend.agents.diagnostics import failure_text
from backend.utils.ruleset_content import yaml_safe_load

# Defaults in the installer-supported Mihomo v1.19.32 config/config.go.
_DEFAULT_BASE = 'https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/'
GEO_RESOURCES = {
    'geosite': ('GeoSite', 'GeoSite.dat', 'geosite.dat', ('geosite.dat',)),
    'geoip': ('GeoIP', 'GeoIP.dat', 'geoip.dat', ('geoip.dat',)),
    'mmdb': ('GeoIP', 'geoip.metadb', 'geoip.metadb', ('country.mmdb', 'geoip.db', 'geoip.metadb')),
    'asn': ('ASN', 'ASN.mmdb', 'GeoLite2-ASN.mmdb', ('asn.mmdb',)),
}


def required_geo_resources(document, files):
    """Inspect rule consumers, including classical providers, DNS filters and sniffer."""
    required = set()

    def geoip(code):
        # NewGEOIP skips the database only for the "lan" pseudo code.
        if str(code).strip().lower() != 'lan':
            required.add('geoip')

    def geoip_list(values):
        for value in values if isinstance(values, list) else []:
            if str(value).lower().startswith('geoip:'):
                for code in str(value).split(':')[1].split(','):
                    geoip(code)

    def geosite_list(values):
        if isinstance(values, list) and any(str(value).lower().startswith('geosite:') for value in values):
            required.add('geosite')

    def rules(value):
        if isinstance(value, dict):
            for child in value.values():
                rules(child)
        elif isinstance(value, list):
            for child in value:
                rules(child)
        elif isinstance(value, str):
            if re.search(r'(?:^|[,(])\s*GEOSITE\s*,', value, re.I):
                required.add('geosite')
            for match in re.finditer(r'(?:^|[,(])\s*(?:SRC-)?GEOIP\s*,\s*([^,()]+)', value, re.I):
                geoip(match[1])
            if re.search(r'(?:^|[,(])\s*(?:SRC-)?IP-ASN\s*,', value, re.I):
                required.add('asn')

    rules(document.get('rules'))
    rules(document.get('sub-rules'))
    for provider in (document.get('rule-providers') or {}).values():
        if not isinstance(provider, dict) or provider.get('behavior') != 'classical':
            continue
        rules(provider.get('payload'))
        entry = files.get(str(provider.get('path', '')).removeprefix('./'))
        if entry and provider.get('format', 'yaml') in ('text', 'yaml'):
            try:
                text = entry[0].decode('utf-8-sig')
                rules(yaml_safe_load(text) if provider.get('format', 'yaml') == 'yaml' else text.splitlines())
            except (UnicodeError, yaml.YAMLError):
                pass  # Let the core report invalid provider content precisely.
    # parseDNS and parseSniffer load these even when DNS or sniffer is disabled.
    dns = document.get('dns') or {}
    if isinstance(dns, dict):
        for key in ('nameserver-policy', 'proxy-server-nameserver-policy'):
            policy = dns.get(key) or {}
            if isinstance(policy, dict):
                geosite_list(list(policy))
        filters = dns.get('fake-ip-filter') or []
        if dns.get('enhanced-mode') == 'fake-ip' and isinstance(filters, list):
            if dns.get('fake-ip-filter-mode') == 'rule':
                rules(filters)
            else:
                geosite_list(filters)
        fallback = dns.get('fallback-filter') or {}
        if dns.get('fallback') and isinstance(fallback, dict):
            if fallback.get('geoip', True):
                geoip(fallback.get('geoip-code', 'CN'))
            if fallback.get('geosite'):
                required.add('geosite')
    sniffer = document.get('sniffer') or {}
    if isinstance(sniffer, dict):
        geosite_list(sniffer.get('force-domain'))
        geosite_list(sniffer.get('skip-domain'))
        geoip_list(sniffer.get('skip-src-address'))
        geoip_list(sniffer.get('skip-dst-address'))
    if 'geoip' in required and document.get('geodata-mode') is not True:
        required.remove('geoip')
        required.add('mmdb')
    return [key for key in GEO_RESOURCES if key in required]


def prepare_mihomo_resources(bundle, capability, config, *, progress=None):
    """Reuse Agent-local assets, then fetch only missing required databases.

    Old Agents keep their existing native download behavior until upgraded.
    Supplemental databases do not change the profile content fingerprint:
    like native-downloaded resources they are persisted by the Agent.
    """
    if bundle.manifest['service_type'] != 'mihomo' or not isinstance(capability.get('mihomo_resources'), list):
        return bundle
    files = {}
    with tarfile.open(fileobj=io.BytesIO(bundle.archive), mode='r:gz') as archive:
        for item in bundle.manifest['files']:
            files[item['path']] = (archive.extractfile('files/' + item['path']).read(), item['role'])
    document = yaml.safe_load(files[bundle.manifest['config_path']][0])
    existing = {name.lower() for name in capability['mihomo_resources'] if isinstance(name, str)}
    existing.update(path.lower() for path in files)
    manifest = deepcopy(bundle.manifest)
    sources = document.get('geox-url') or {}
    if not isinstance(sources, dict):
        raise DeploymentPreparationError('geox-url 必须是资源名称与下载地址的映射')
    for key in required_geo_resources(document, files):
        label, path, default_file, aliases = GEO_RESOURCES[key]
        if any(alias in existing for alias in aliases):
            if progress is not None:
                progress(f'复用已有 {label} 资源（{path}），不重复下载')
            continue
        url = sources.get(key, _DEFAULT_BASE + default_file)
        target = str(PurePosixPath(capability.get('config_directory') or 'Agent 配置目录') / path)
        try:
            if not isinstance(url, str) or not url.strip():
                raise RuleDownloadError(f'geox-url.{key} 未填写有效下载地址')
            if progress is not None:
                progress(f'正在下载 {label} 资源：{path}')
            value = _fetch_bytes(url, config)
            if progress is not None:
                progress(f'正在检查 {path} 下载内容与文件大小')
            if not value or value.lstrip().lower().startswith((b'<!doctype html', b'<html')):
                raise RuleDownloadError('下载结果为空或是网页，不是 Geo 数据文件')
        except RuleDownloadError as exc:
            reason, _, details = str(exc).partition('\n')
            raise RuleDownloadError(failure_text(
                f'{label} 下载失败。本次发布已停止，设备仍使用原配置。\n'
                f'原因：{reason}\n下载地址：{url}\n目标文件：{target}\n'
                f'修复：更换 geox-url.{key}，或检查「GitHub 代理域名」和「系统设置 → 规则下载代理」。\n'
                f'也可手动下载对应数据文件，放到 Agent 机器的 {target}，确保 Mihomo 可读取，再重新发布。'
                + (f'\n{details}' if details else '')
            ), remedy=exc.remedy) from None
        if (len(value) > MAX_FILE_BYTES or len(files) >= MAX_FILES
                or sum(len(item[0]) for item in files.values()) + len(value) > MAX_TOTAL_BYTES):
            raise DeploymentPreparationError(f'{label} 超出发布文件大小限制')
        files[path] = (value, 'resource')
        manifest['files'].append({'path': path, 'name': label, 'role': 'resource',
                                  'size': len(value), 'sha256': hashlib.sha256(value).hexdigest()})
        if progress is not None:
            progress(f'{path} 已准备（{len(value):,} 字节），已计算发布用 SHA256')
    return _pack(manifest, files, bundle.config_version) if len(files) != len(bundle.manifest['files']) else bundle
