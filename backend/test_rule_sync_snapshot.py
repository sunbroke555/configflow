"""Live snapshots are authenticated, bounded, and constrained to a deployment."""
from copy import deepcopy
import gzip
import hashlib
import io
import json
import tarfile

import pytest
import requests

from backend.agents.deployment_bundle import _pack
from backend.agents import rule_sync_snapshot
from backend.agents.rule_sync_projection import read_bundle_files


def _manifest(files):
    return {
        'protocol_version': 1, 'deployment_id': 'published-1', 'agent_id': 'agent-1',
        'profile_id': 'home', 'service_type': 'oxidns', 'config_path': 'config.yaml',
        'files': [
            {'path': path, 'role': role, 'size': len(content),
             'sha256': hashlib.sha256(content).hexdigest()}
            for path, (content, role) in sorted(files.items())
        ],
    }


@pytest.fixture
def snapshot():
    files = {
        'config.yaml': (b'plugins: []\n', 'config'),
        'rules/direct.txt': (b'old.example\n', 'rule'),
        'providers/upstream.yaml': (b'old-provider: []\n', 'provider'),
    }
    manifest = _manifest(files)
    baseline = {'bundle': _pack(manifest, files, 'baseline-config-version')}
    live = deepcopy(files)
    live['rules/direct.txt'] = (b'new.example\nsecond.example\n', 'rule')
    live['providers/upstream.yaml'] = (b'live-provider: [192.0.2.1]\n', 'provider')
    confirmed_version = hashlib.sha256(files['config.yaml'][0]).hexdigest()
    return {
        'agent': {'id': 'agent-1', 'host': '192.0.2.12', 'port': 8848,
                  'token': 'private-agent-token', 'config_version': confirmed_version,
                  'applied_content': {'config_version': confirmed_version}},
        'baseline': baseline, 'files': live, 'manifest': _manifest(live),
    }


def _archive(manifest, files, *, omit=(), extra=(), raw_manifest=None, pax_headers=None):
    members = [('manifest.json', json.dumps(manifest).encode() if raw_manifest is None else raw_manifest)]
    members.extend(('files/' + path, content) for path, (content, _) in files.items())
    members.extend(extra)
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz', format=tarfile.PAX_FORMAT,
                      pax_headers=pax_headers) as archive:
        for name, content in members:
            member = name if isinstance(name, tarfile.TarInfo) else tarfile.TarInfo(name)
            if member.name in omit:
                continue
            if member.isfile():
                member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return output.getvalue()


def _serve(monkeypatch, content=b'', status=200, error=None):
    calls = []

    def request(session, method, url, **kwargs):
        calls.append((method, url, session.trust_env, kwargs))
        if error is not None:
            raise error
        response = requests.Response()
        response.status_code = status
        response._content = content
        response._content_consumed = True
        return response

    monkeypatch.setattr(requests.Session, 'request', request)
    return calls


def _fetch(snapshot):
    # The manager is intentionally inert: reading a snapshot must not publish,
    # persist a new baseline, or mutate repository state.
    return rule_sync_snapshot.fetch_snapshot(object(), snapshot['agent'], snapshot['baseline'])


def test_authenticated_fixed_agent_url_returns_actual_live_bytes_without_mutation(monkeypatch, snapshot):
    original = deepcopy(snapshot)
    monkeypatch.setenv('HTTP_PROXY', 'http://untrusted-proxy.example:8080')
    calls = _serve(monkeypatch, _archive(snapshot['manifest'], snapshot['files']))

    result = _fetch(snapshot)

    assert len(calls) == 1
    method, url, trust_env, options = calls[0]
    assert method == 'GET'
    assert url == 'http://192.0.2.12:8848/api/rule-sync/snapshot'
    assert trust_env is False
    assert options['allow_redirects'] is False
    assert options['stream'] is True
    assert options['timeout'] == (3, 90)
    assert options['headers'] == {
        'Authorization': 'Bearer private-agent-token', 'Accept': 'application/gzip',
    }
    assert read_bundle_files(result) == snapshot['files']
    assert result.manifest == snapshot['manifest']
    assert result.archive != snapshot['baseline']['bundle'].archive
    assert result.sha256 == hashlib.sha256(result.archive).hexdigest()
    assert snapshot == original


def test_unconfirmed_main_config_change_is_rejected_even_with_matching_manifest_digest(monkeypatch, snapshot):
    snapshot['files']['config.yaml'] = (b'plugins: [changed]\n', 'config')
    snapshot['manifest'] = _manifest(snapshot['files'])
    _serve(monkeypatch, _archive(snapshot['manifest'], snapshot['files']))
    with pytest.raises(ValueError, match='主配置已改变'):
        _fetch(snapshot)


@pytest.mark.parametrize('confirmation_source', ['applied_content', 'config_version'])
@pytest.mark.parametrize('digest_length', [64, 8])
def test_confirmed_device_adapted_main_is_accepted(monkeypatch, snapshot, confirmation_source, digest_length):
    # Agent AIO/gateway setup can adapt the published config before reporting its
    # applied digest. Rule sync must retain those confirmed, device-local bytes.
    content = b'plugins: []\napi:\n  http: 127.0.0.1:9091\n'
    digest = hashlib.sha256(content).hexdigest()
    snapshot['files']['config.yaml'] = (content, 'config')
    snapshot['manifest'] = _manifest(snapshot['files'])
    if confirmation_source == 'applied_content':
        snapshot['agent']['applied_content']['config_version'] = digest[:digest_length]
        # The confirmed applied version takes precedence over a stale legacy one.
        assert snapshot['agent']['config_version'] != digest
    else:
        snapshot['agent'].pop('applied_content')
        snapshot['agent']['config_version'] = digest[:digest_length]
    original = deepcopy(snapshot)
    _serve(monkeypatch, _archive(snapshot['manifest'], snapshot['files']))

    result = _fetch(snapshot)

    assert read_bundle_files(result) == snapshot['files']
    assert result.config_version == digest
    assert read_bundle_files(snapshot['baseline']['bundle'])['config.yaml'][0] != content
    assert snapshot == original


@pytest.mark.parametrize('version', [None, '', '0' * 64, '0' * 8])
def test_missing_or_wrong_confirmed_main_digest_is_rejected(monkeypatch, snapshot, version):
    # Matching the stored upload alone is insufficient without a current device
    # confirmation, including for unchanged main-config bytes.
    snapshot['agent'].pop('applied_content')
    snapshot['agent']['config_version'] = version
    _serve(monkeypatch, _archive(snapshot['manifest'], snapshot['files']))
    with pytest.raises(ValueError, match='主配置已改变'):
        _fetch(snapshot)


def test_mismatched_applied_digest_cannot_fall_back_to_matching_legacy_digest(monkeypatch, snapshot):
    snapshot['agent']['applied_content']['config_version'] = '0' * 64
    _serve(monkeypatch, _archive(snapshot['manifest'], snapshot['files']))
    with pytest.raises(ValueError, match='主配置已改变'):
        _fetch(snapshot)


@pytest.mark.parametrize(('key', 'value'), [
    ('protocol_version', 2), ('deployment_id', 'different-publication'),
    ('agent_id', 'different-device'), ('profile_id', 'different-profile'),
    ('service_type', 'mihomo'), ('config_path', 'different.yaml'),
])
def test_snapshot_identity_must_match_published_baseline(monkeypatch, snapshot, key, value):
    snapshot['manifest'][key] = value
    _serve(monkeypatch, _archive(snapshot['manifest'], snapshot['files']))
    with pytest.raises(ValueError, match='配置已改变'):
        _fetch(snapshot)


@pytest.mark.parametrize(('key', 'value'), [
    ('path', '../outside.txt'), ('path', 'rules/unpublished.txt'),
    ('role', 'config'), ('role', 'custom'), ('size', 0), ('size', True),
    ('sha256', '0' * 64), ('sha256', 'private-malicious-digest'),
])
def test_manifest_path_role_size_and_digest_tampering_is_rejected(monkeypatch, snapshot, key, value):
    entry = next(entry for entry in snapshot['manifest']['files'] if entry['role'] == 'rule')
    entry[key] = value
    _serve(monkeypatch, _archive(snapshot['manifest'], snapshot['files']))
    with pytest.raises(ValueError):
        _fetch(snapshot)


def test_changed_file_bytes_require_updated_digest(monkeypatch, snapshot):
    snapshot['files']['rules/direct.txt'] = (b'tampered.example\n', 'rule')
    _serve(monkeypatch, _archive(snapshot['manifest'], snapshot['files']))
    with pytest.raises(ValueError, match='摘要不一致'):
        _fetch(snapshot)


@pytest.mark.parametrize('member', [
    'manifest.json', 'files/config.yaml', 'files/rules/direct.txt',
    'files/providers/upstream.yaml',
])
def test_missing_archive_member_is_rejected(monkeypatch, snapshot, member):
    _serve(monkeypatch, _archive(snapshot['manifest'], snapshot['files'], omit={member}))
    with pytest.raises(ValueError, match='文件不完整'):
        _fetch(snapshot)


@pytest.mark.parametrize('member', [
    'manifest.json', 'files/rules/direct.txt', 'files/unpublished.txt',
    '/etc/passwd', 'files/../outside.txt', 'files/rules/../../outside.txt',
    'files/./rules/direct.txt', 'files//rules/direct.txt', 'files/rules\\direct.txt',
])
def test_duplicate_unknown_and_unsafe_members_are_rejected(monkeypatch, snapshot, member):
    _serve(monkeypatch, _archive(snapshot['manifest'], snapshot['files'], extra=[(member, b'unsafe')]))
    with pytest.raises(ValueError):
        _fetch(snapshot)


@pytest.mark.parametrize('kind', [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE, tarfile.CHRTYPE])
def test_non_regular_archive_members_are_rejected(monkeypatch, snapshot, kind):
    member = tarfile.TarInfo('files/linked-rule')
    member.type = kind
    member.linkname = '../../private-secret'
    _serve(monkeypatch, _archive(snapshot['manifest'], snapshot['files'], extra=[(member, b'')]))
    with pytest.raises(ValueError, match='无效文件'):
        _fetch(snapshot)


@pytest.mark.parametrize('entries', [None, {}, [], [None, None, None]])
def test_malformed_manifest_file_list_is_rejected(monkeypatch, snapshot, entries):
    snapshot['manifest']['files'] = entries
    _serve(monkeypatch, _archive(snapshot['manifest'], snapshot['files']))
    with pytest.raises(ValueError):
        _fetch(snapshot)


def test_duplicate_manifest_entries_are_rejected(monkeypatch, snapshot):
    snapshot['manifest']['files'][1] = deepcopy(snapshot['manifest']['files'][2])
    _serve(monkeypatch, _archive(snapshot['manifest'], snapshot['files']))
    with pytest.raises(ValueError, match='清单无效'):
        _fetch(snapshot)


def test_compressed_download_limit_is_enforced_before_parsing(monkeypatch, snapshot):
    monkeypatch.setattr(rule_sync_snapshot, 'MAX_TOTAL_BYTES', 32)
    _serve(monkeypatch, b'x' * (8 * 1024 * 1024 + 33))
    with pytest.raises(ValueError, match='超过大小限制'):
        _fetch(snapshot)


def test_decompressed_per_file_limit_is_enforced(monkeypatch, snapshot):
    monkeypatch.setattr(rule_sync_snapshot, 'MAX_FILE_BYTES', 32)
    snapshot['files']['rules/direct.txt'] = (b'x' * 33, 'rule')
    _serve(monkeypatch, _archive(_manifest(snapshot['files']), snapshot['files']))
    with pytest.raises(ValueError, match='文件不属于上次发布'):
        _fetch(snapshot)


def test_decompressed_total_file_limit_is_enforced(monkeypatch, snapshot):
    total = sum(len(content) for content, _ in snapshot['files'].values())
    monkeypatch.setattr(rule_sync_snapshot, 'MAX_TOTAL_BYTES', total - 1)
    _serve(monkeypatch, _archive(snapshot['manifest'], snapshot['files']))
    with pytest.raises(ValueError, match='超过大小限制'):
        _fetch(snapshot)


def test_decompressed_archive_metadata_is_also_bounded(monkeypatch, snapshot):
    # PAX headers are consumed internally by tarfile before it yields a member.
    # Counting only yielded file sizes therefore misses this compressible data.
    monkeypatch.setattr(rule_sync_snapshot, 'MAX_TOTAL_BYTES', 128)
    body = _archive(snapshot['manifest'], snapshot['files'],
                    pax_headers={'comment': 'x' * (16 * 1024 * 1024)})
    assert len(body) < 64 * 1024
    _serve(monkeypatch, body)
    with pytest.raises(ValueError):
        _fetch(snapshot)


def test_archive_file_count_limit_is_enforced(monkeypatch, snapshot):
    monkeypatch.setattr(rule_sync_snapshot, 'MAX_FILES', 2)
    _serve(monkeypatch, _archive(snapshot['manifest'], snapshot['files']))
    with pytest.raises(ValueError, match='无效文件'):
        _fetch(snapshot)


def test_manifest_size_limit_is_enforced(monkeypatch, snapshot):
    _serve(monkeypatch, _archive(snapshot['manifest'], snapshot['files'], raw_manifest=b' ' * (2 * 1024 * 1024 + 1)))
    with pytest.raises(ValueError, match='清单过大'):
        _fetch(snapshot)


@pytest.mark.parametrize('status', [301, 302, 401, 403, 404, 409, 500])
def test_non_success_response_is_rejected_without_following_redirect_or_exposing_body(monkeypatch, snapshot, status):
    calls = _serve(monkeypatch, b'private-token-and-upstream-body', status=status)
    with pytest.raises(ValueError) as failure:
        _fetch(snapshot)
    assert 'private' not in str(failure.value)
    assert len(calls) == 1
    assert calls[0][3]['allow_redirects'] is False


@pytest.mark.parametrize('error', [
    requests.ConnectionError('http://private:secret@host/private-token'),
    requests.Timeout('private-agent-token'),
    requests.exceptions.ChunkedEncodingError('private-stream-body'),
])
def test_network_errors_are_sanitized(monkeypatch, snapshot, error):
    _serve(monkeypatch, error=error)
    with pytest.raises(ValueError, match='无法验证设备当前规则快照') as failure:
        _fetch(snapshot)
    assert 'private' not in str(failure.value)


@pytest.mark.parametrize('body', [b'', b'private-invalid-gzip', gzip.compress(b'private-invalid-tar')])
def test_malformed_archives_are_sanitized(monkeypatch, snapshot, body):
    _serve(monkeypatch, body)
    with pytest.raises(ValueError, match='无法验证设备当前规则快照') as failure:
        _fetch(snapshot)
    assert 'private' not in str(failure.value)


@pytest.mark.parametrize('manifest', [b'{"private-secret":', b'\xffprivate-secret'])
def test_invalid_json_and_encoding_are_sanitized(monkeypatch, snapshot, manifest):
    _serve(monkeypatch, _archive(snapshot['manifest'], snapshot['files'], raw_manifest=manifest))
    with pytest.raises(ValueError, match='无法验证设备当前规则快照') as failure:
        _fetch(snapshot)
    assert 'private' not in str(failure.value)
