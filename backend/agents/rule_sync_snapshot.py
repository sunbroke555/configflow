"""Read a bounded, authenticated snapshot of the Agent's managed live files."""
import hashlib
import gzip
import io
import json
import tarfile

import requests

from .deployment_bundle import MAX_FILE_BYTES, MAX_FILES, MAX_TOTAL_BYTES, _pack, deployment_content_fingerprint
from .oxidns_monitor import _agent_origin
from .rule_sync_projection import read_bundle_files


class _ArchiveBudget:
    """Count *all* decompressed bytes, including tar PAX/GNU extension headers."""
    def __init__(self, stream):
        self.stream = stream
        self.remaining = MAX_TOTAL_BYTES + MAX_FILES * 2048 + 3 * 1024 * 1024

    def read(self, size=-1):
        size = self.remaining + 1 if size < 0 else min(size, self.remaining + 1)
        value = self.stream.read(size)
        self.remaining -= len(value)
        if self.remaining < 0:
            raise ValueError('设备规则快照展开后超过大小限制')
        return value


def fetch_snapshot(manager, agent, baseline):
    previous = baseline['bundle']
    expected = previous.manifest
    expected_files = read_bundle_files(previous)
    try:
        with requests.Session() as session:
            session.trust_env = False
            with session.get(_agent_origin(agent) + '/api/rule-sync/snapshot',
                    headers={'Authorization': 'Bearer ' + agent['token'], 'Accept': 'application/gzip'},
                    timeout=(3, 90), allow_redirects=False, stream=True) as response:
                if response.status_code != 200:
                    raise ValueError('无法读取设备当前规则快照，请确认 Agent 已升级且没有其他操作正在进行')
                chunks, size = [], 0
                for chunk in response.iter_content(64 * 1024):
                    size += len(chunk)
                    if size > MAX_TOTAL_BYTES + 8 * 1024 * 1024:
                        raise ValueError('设备规则快照超过大小限制')
                    chunks.append(chunk)
        files, manifest, seen = {}, None, set()
        total = 0
        # Stream the untrusted tar headers; never extract paths, follow links,
        # or let tarfile scan an unbounded decompressed member list.
        with gzip.GzipFile(fileobj=io.BytesIO(b''.join(chunks)), mode='rb') as expanded, \
                tarfile.open(fileobj=_ArchiveBudget(expanded), mode='r|') as archive:
            for member in archive:
                if member.name in seen or not member.isfile() or len(seen) >= MAX_FILES + 1:
                    raise ValueError('设备规则快照包含无效文件')
                seen.add(member.name)
                if member.name == 'manifest.json':
                    if member.size > 2 * 1024 * 1024:
                        raise ValueError('设备规则快照清单过大')
                    manifest = json.loads(archive.extractfile(member).read())
                    continue
                path = member.name.removeprefix('files/')
                if (member.name != 'files/' + path or path not in expected_files
                        or not 0 <= member.size <= MAX_FILE_BYTES):
                    raise ValueError('设备规则快照文件不属于上次发布')
                total += member.size
                if total > MAX_TOTAL_BYTES:
                    raise ValueError('设备规则快照超过大小限制')
                files[path] = (archive.extractfile(member).read(), expected_files[path][1])
        if not isinstance(manifest, dict) or set(files) != set(expected_files):
            raise ValueError('设备规则快照文件不完整')
        for key in ('protocol_version', 'deployment_id', 'agent_id', 'profile_id', 'service_type', 'config_path'):
            if manifest.get(key) != expected.get(key):
                raise ValueError('设备配置已改变，请等待发布完成或重新建立同步基线')
        entries = manifest.get('files')
        if not isinstance(entries, list) or len(entries) != len(files):
            raise ValueError('设备规则快照清单不完整')
        checked = set()
        for entry in entries:
            path = entry['path']
            if path in checked or path not in files:
                raise ValueError('设备规则快照清单无效')
            checked.add(path)
            content, role = files[path]
            if (entry['role'] != role or type(entry['size']) is not int or entry['size'] != len(content)
                    or entry['sha256'] != hashlib.sha256(content).hexdigest()):
                raise ValueError('设备规则快照摘要不一致')
        path = expected['config_path']
        digest = hashlib.sha256(files[path][0]).hexdigest()
        confirmed_version = (agent.get('applied_content') or {}).get('config_version', agent.get('config_version'))
        if confirmed_version not in (digest, digest[:8]):
            raise ValueError('设备主配置已改变，请先成功发布一次配置')
        deployment_content_fingerprint(manifest)
        return _pack(manifest, files, hashlib.sha256(files[path][0]).hexdigest())
    except (requests.RequestException, tarfile.TarError, OSError, KeyError, TypeError,
            json.JSONDecodeError, UnicodeError):
        raise ValueError('无法验证设备当前规则快照，未继续自动应用') from None
