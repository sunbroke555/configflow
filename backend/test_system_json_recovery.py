"""config.json 损坏时的启动恢复与备份保护

线上事故：磁盘写满后 /data/config.json 变成空文件，ProfileRepository 直接抛
ProfileRepositoryError，Flask 反复启动失败，supervisor 最终放弃。
备份文件本身也可能在同一次磁盘满时被 shutil.copy2 截断，导致最后一份好数据一起丢失。
"""
import json
import os
from pathlib import Path

import pytest

from backend.common.config_repository import ProfileRepository, ProfileRepositoryError


def _bootstrap(tmp_path: Path) -> ProfileRepository:
    """初始化并写入两次，使备份中已包含 alpha

    备份保存的是「写入前」的那一份，因此始终落后一个版本。要让备份里出现
    alpha，必须在创建 alpha 之后再发生一次写入。
    """
    repo = ProfileRepository(tmp_path)
    repo.create_profile({"id": "alpha", "name": "Alpha"})
    repo.create_profile({"id": "beta", "name": "Beta"})
    return repo


def test_empty_system_json_recovers_from_backup(tmp_path):
    _bootstrap(tmp_path)
    system_file = tmp_path / "config.json"
    backup_file = tmp_path / "config.json.bak"
    assert backup_file.exists(), "每次写入都应留下备份"

    # 复现磁盘写满：文件被截断为 0 字节
    system_file.write_text("", encoding="utf-8")

    repo = ProfileRepository(tmp_path)

    profiles = {p["id"] for p in repo.list_profiles()}
    assert "alpha" in profiles, "应从备份恢复而不是抛异常"
    assert json.loads(system_file.read_text(encoding="utf-8")), "恢复后 config.json 必须是有效 JSON"


def test_truncated_system_json_recovers_from_backup(tmp_path):
    _bootstrap(tmp_path)
    system_file = tmp_path / "config.json"

    # 半截 JSON，同样是磁盘满的典型产物
    system_file.write_text('{"schema_version": 5, "profil', encoding="utf-8")

    repo = ProfileRepository(tmp_path)
    assert "alpha" in {p["id"] for p in repo.list_profiles()}


def test_corrupt_system_json_is_preserved_for_diagnosis(tmp_path):
    _bootstrap(tmp_path)
    system_file = tmp_path / "config.json"
    system_file.write_text("", encoding="utf-8")

    ProfileRepository(tmp_path)

    corrupt_copies = list(tmp_path.glob("config.json.corrupt-*"))
    assert corrupt_copies, "损坏的原文件应保留副本供排查，而不是被直接覆盖"


def test_backup_survives_a_failed_write(tmp_path, monkeypatch):
    repo = _bootstrap(tmp_path)
    backup_file = tmp_path / "config.json.bak"
    good_backup = backup_file.read_text(encoding="utf-8")

    real_replace = os.replace

    def failing_replace(src, dst, *args, **kwargs):
        # 模拟磁盘满：新内容始终无法提交
        if str(dst).endswith("config.json"):
            raise OSError(28, "No space left on device")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(Exception):
        repo.create_profile({"id": "gamma", "name": "Gamma"})

    monkeypatch.undo()
    assert backup_file.read_text(encoding="utf-8") == good_backup, (
        "写入失败时不得破坏已有备份"
    )


def test_both_system_and_backup_corrupt_raises_clear_error(tmp_path):
    _bootstrap(tmp_path)
    (tmp_path / "config.json").write_text("", encoding="utf-8")
    (tmp_path / "config.json.bak").write_text("", encoding="utf-8")

    # 两份都坏时不应静默造出空配置，必须明确报错让人工介入
    with pytest.raises(ProfileRepositoryError):
        ProfileRepository(tmp_path)


def test_current_backup_recovers_system_shared_data_and_profile_together(tmp_path):
    repository = _bootstrap(tmp_path)
    system = repository.get_system()
    system['system_config']['config_token'] = 'preserved'
    repository.save_system(system)
    shared = repository.get_shared()
    shared['subscriptions'] = [{'id': 's', 'name': 'Source'}]
    repository.save_shared(shared)
    profile = repository.get_profile('alpha')
    profile['proxy_groups'] = [{'id': 'feed', 'name': 'Feed', 'type': 'select', 'subscriptions': ['s']}]
    repository.save_profile('alpha', profile)
    expected = repository.export_all()
    # Persist another revision so the backup contains the complete fixture.
    repository.save_profile('alpha', repository.get_profile('alpha'))
    (tmp_path / 'config.json').write_text('')
    recovered = ProfileRepository(tmp_path)
    assert recovered.export_all() == expected
    assert recovered.get_system()['system_config']['config_token'] == 'preserved'
    assert recovered.get_shared()['subscriptions'] == shared['subscriptions']
    assert recovered.get_profile('alpha')['proxy_groups'] == profile['proxy_groups']
    assert list(tmp_path.glob('config.json.corrupt-*'))


def test_legacy_backup_survives_migration_and_can_recover_after_corruption(tmp_path):
    legacy = {'subscriptions': [{'id': 's', 'name': 'Legacy'}]}
    (tmp_path / 'config.json').write_text(json.dumps(legacy))
    ProfileRepository(tmp_path)
    assert json.loads((tmp_path / 'config.json.bak').read_text()) == legacy
    (tmp_path / 'config.json').write_text('')
    repository = ProfileRepository(tmp_path)
    assert repository.get_shared()['subscriptions'] == legacy['subscriptions']
    assert 'resource_refs' not in repository.get_profile('default')


def test_semantically_corrupt_backup_is_never_committed(tmp_path):
    _bootstrap(tmp_path)
    damaged = {'schema_version': 5, 'system': {}, 'shared': {}, 'profiles': {}}
    (tmp_path / 'config.json').write_text('')
    (tmp_path / 'config.json.bak').write_text(json.dumps(damaged))
    with pytest.raises(ProfileRepositoryError):
        ProfileRepository(tmp_path)
    assert (tmp_path / 'config.json').read_text() == ''


def test_backup_refresh_failure_keeps_old_backup_and_new_primary(tmp_path, monkeypatch):
    repository = _bootstrap(tmp_path)
    previous_backup = (tmp_path / 'config.json.bak').read_bytes()
    replace = os.replace
    def fail_only_backup(source, destination):
        if Path(destination).name == 'config.json.bak':
            raise OSError(28, 'No space left on device')
        return replace(source, destination)
    monkeypatch.setattr(os, 'replace', fail_only_backup)
    repository.create_profile({'id': 'gamma'})
    assert 'gamma' in {profile['id'] for profile in repository.list_profiles()}
    assert (tmp_path / 'config.json.bak').read_bytes() == previous_backup


def test_partial_temp_write_is_rejected_without_touching_live_or_backup(tmp_path, monkeypatch):
    repository = _bootstrap(tmp_path)
    primary = repository.path.read_bytes()
    backup = repository.path.with_name('config.json.bak').read_bytes()
    original_fdopen = os.fdopen
    class ShortWriter:
        def __init__(self, fd, *args, **kwargs):
            self.handle = original_fdopen(fd, *args, **kwargs)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.handle.close()
        def write(self, content):
            return self.handle.write(content[:1])
        def flush(self):
            self.handle.flush()
        def fileno(self):
            return self.handle.fileno()
    monkeypatch.setattr(os, 'fdopen', ShortWriter)
    with pytest.raises(ProfileRepositoryError, match='Incomplete write'):
        repository.create_profile({'id': 'gamma'})
    assert repository.path.read_bytes() == primary
    assert repository.path.with_name('config.json.bak').read_bytes() == backup
    assert not list(tmp_path.glob('.config.json.*.tmp'))
