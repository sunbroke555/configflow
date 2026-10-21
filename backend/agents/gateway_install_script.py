"""Pinned, opt-in Linux gateway core installer, isolated from ordinary installs.

Release archive digests are the official GitHub release asset ``digest`` values
verified on 2026-10-19. No remote installer script is executed. OxiDNS's official
musl bundles work on both Alpine and glibc distributions.
"""
from __future__ import annotations

import hashlib
import ipaddress
import os
from pathlib import Path
import re
import shlex
from urllib.parse import urlsplit

from .go_install_script import generate_go_aio_install_script
from .install_output import with_install_output

_TEMPLATE = (Path(__file__).parent / 'scripts' / 'install-gateway.sh').read_text(encoding='utf-8')
CORE_RELEASES = {
    'mihomo': {
        'version': 'v1.19.32',
        'linux-amd64': {'url': 'https://github.com/MetaCubeX/mihomo/releases/download/v1.19.32/mihomo-linux-amd64-compatible-v1.19.32.gz',
                        'sha256': 'ba3ce607747a07f948fc35780e108a4a7c7f552a38b9bd4d115f313ebcb89c20'},
        'linux-arm64': {'url': 'https://github.com/MetaCubeX/mihomo/releases/download/v1.19.32/mihomo-linux-arm64-v1.19.32.gz',
                        'sha256': '9dd862e28b46ff7d775f169cceebc28deccaa0a9e804237d421cd2571e0caba0'},
    },
    'oxidns': {
        'version': 'v1.6.1',
        'linux-amd64': {'url': 'https://github.com/svenshi/oxidns/releases/download/v1.6.1/oxidns-x86_64-unknown-linux-musl.tar.gz',
                        'sha256': '2ac8e103e03407a73c29da848f6ce2d524e751ce1da8a744aa9168fc1384e3b0'},
        'linux-arm64': {'url': 'https://github.com/svenshi/oxidns/releases/download/v1.6.1/oxidns-aarch64-unknown-linux-musl.tar.gz',
                        'sha256': 'd879d4b9199c5c911d296af4df8d9e974523fb98a6ee2418ae7274d152365e42'},
    },
}


def local_agent_checksums() -> dict[str, str]:
    """Hash precisely the binaries served by agents.download_agent_binary."""
    directory = Path(os.getenv('AGENTS_STATIC_DIR', '/opt/configflow/static/agents'))
    if not directory.exists():
        directory = Path(__file__).parent.parent / 'static' / 'agents'
    checksums = {}
    for arch in ('linux-amd64', 'linux-arm64'):
        binary = directory / f'configflow-agent-{arch}'
        if binary.is_file():
            digest = hashlib.sha256()
            with binary.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
            checksums[arch] = digest.hexdigest()
    return checksums


def _http_url(value: str, name: str) -> str:
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f'{name} must be an HTTP(S) URL')
    parsed = urlsplit(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError(f'{name} must be an HTTP(S) URL without credentials or fragment')
    return value.rstrip('/')


def generate_gateway_install_script(
    server_url: str,
    agent_name: str,
    agent_ip: str = '',
    binary_download_url: str = '',
    mihomo_agent_port: int = 8080,
    oxidns_agent_port: int = 8082,
    enrollment_url: str = '',
    enrollment_token: str = '',
    agent_checksums: dict[str, str] | None = None,
    installation_id: str = '',
) -> str:
    """Generate one install command's script; never enable TUN or forwarding.

    ``enrollment_token`` is a short-lived installation-session credential, not
    an administrator or global registration secret. Successful enrollment is
    deferred until both authenticated Agent identities have been registered.
    Existing standard-layout cores are validated and attached without edits;
    mismatched or partial foreign installations fail before any changes.
    """
    server_url = _http_url(server_url, 'server_url')
    binary_download_url = _http_url(binary_download_url or server_url + '/api/agents/download', 'binary_download_url')
    if agent_ip:
        try:
            address = ipaddress.IPv4Address(agent_ip)
        except (ValueError, TypeError) as exc:
            raise ValueError('Gateway Agent address must be an IPv4 address') from exc
        if address.is_loopback or address.is_multicast or address.is_unspecified or address.is_link_local:
            raise ValueError('Gateway Agent address must be a usable LAN IPv4 address')
    if bool(enrollment_url) != bool(enrollment_token):
        raise ValueError('Both enrollment_url and enrollment_token are required together')
    if enrollment_url:
        enrollment_url = _http_url(enrollment_url, 'enrollment_url')
        if urlsplit(enrollment_url)[:2] != urlsplit(server_url)[:2]:
            raise ValueError('Enrollment endpoint must use the ConfigFlow server origin')
    for token in (enrollment_token,):
        if token and (not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{16,512}', token)):
            raise ValueError('Invalid installation-session token')
    checksums = local_agent_checksums() if agent_checksums is None else dict(agent_checksums)
    if not checksums:
        raise ValueError('ConfigFlow 尚未提供 Linux Agent 二进制，请先构建/部署最新版 Agent 后重试。')
    for arch, digest in checksums.items():
        if arch not in ('linux-amd64', 'linux-arm64') or not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
            raise ValueError('Invalid Agent architecture or SHA256 digest')
    aio = generate_go_aio_install_script(
        server_url, agent_name, agent_ip=agent_ip, binary_download_url=binary_download_url,
        mihomo_agent_port=mihomo_agent_port, oxidns_agent_port=oxidns_agent_port,
        mihomo_service_binary='/usr/local/bin/mihomo', oxidns_service_binary='/usr/local/bin/oxidns',
        non_interactive=True, verified_agent_path='$GATEWAY_STAGE/configflow-agent',
        installation_id=installation_id, installation_token=enrollment_token if installation_id else "",
        manage_ipv4_forwarding=False,
        manage_tun=False,
        # The gateway script has just prepared the cores in the previous step.
        installation_description='为上一步准备好的 Mihomo 和 OxiDNS 安装 Agent，不改核心配置、不重启核心。',
    )
    values = {
        'aio_script': aio,
        'binary_download_url_shell': shlex.quote(binary_download_url),
        'enrollment_url_shell': shlex.quote(enrollment_url),
        'enrollment_token_shell': shlex.quote(enrollment_token),
        'agent_amd64_sha_shell': shlex.quote(checksums.get('linux-amd64', '')),
        'agent_arm64_sha_shell': shlex.quote(checksums.get('linux-arm64', '')),
    }
    for service, release in CORE_RELEASES.items():
        values[f'{service}_version_shell'] = shlex.quote(release['version'])
        for arch in ('amd64', 'arm64'):
            asset = release['linux-' + arch]
            values[f'{service}_{arch}_url_shell'] = shlex.quote(asset['url'])
            values[f'{service}_{arch}_sha_shell'] = shlex.quote(asset['sha256'])
    return with_install_output(re.sub(r'\{(' + '|'.join(values) + r')\}', lambda m: values[m.group(1)], _TEMPLATE))
