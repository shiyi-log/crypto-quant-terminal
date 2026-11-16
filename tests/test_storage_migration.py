"""Storage migration contracts use real files and an active SQLite WAL writer."""
from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

import runtime_config

migration = (importlib.import_module("tools.storage_migration")
             if importlib.util.find_spec("tools.storage_migration") else None)


def storage_layout(*args, **kwargs):
    assert hasattr(runtime_config, "storage_layout"), "data-root layout capability missing"
    return runtime_config.storage_layout(*args, **kwargs)


def test_explicit_data_root_maps_runtime_files_and_preserves_legacy_default(tmp_path):
    project = tmp_path / 'project'
    external = tmp_path / 'disk' / 'quant-data'
    project.mkdir()
    local = storage_layout(project, environ={})
    moved = storage_layout(project, environ={'QUANT_DATA_ROOT': str(external)})
    assert local.path('bot/user_data/models') == project / 'bot/user_data/models'
    assert moved.path('bot/user_data/models') == external / 'files/bot/user_data/models'
    with pytest.raises(ValueError):
        moved.path('../outside')


def test_unmounted_or_replaced_external_volume_never_creates_destination(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    destination = tmp_path / 'unmounted' / 'quant-data'
    layout = storage_layout(project, environ={
        'QUANT_DATA_ROOT': str(destination), 'QUANT_DATA_VOLUME_UUID': 'expected-disk',
        'QUANT_DATA_MOUNT': str(tmp_path / 'unmounted'),
    })
    monkeypatch.setattr(migration, 'probe_volume', lambda path: {'mounted': False, 'uuid': None})
    with pytest.raises(RuntimeError, match='unavailable'):
        migration.precopy(layout, ['logs'])
    assert not destination.exists()
    (tmp_path / 'unmounted').mkdir()
    monkeypatch.setattr(migration, 'probe_volume', lambda path: {'mounted': True, 'uuid': 'other-disk'})
    with pytest.raises(RuntimeError, match='identity'):
        migration.precopy(layout, ['logs'])
    assert not destination.exists()


def _layout(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    disk = tmp_path / 'disk'
    disk.mkdir()
    layout = storage_layout(project, environ={
        'QUANT_DATA_ROOT': str(disk / 'quant-data'),
        'QUANT_DATA_MOUNT': str(disk), 'QUANT_DATA_VOLUME_UUID': 'test-volume',
    })
    monkeypatch.setattr(migration, 'probe_volume', lambda path: {'mounted': True, 'uuid': 'test-volume'})
    return project, layout


def test_inventory_and_dry_run_do_not_copy_or_change_source(tmp_path, monkeypatch):
    project, layout = _layout(tmp_path, monkeypatch)
    (project / 'logs').mkdir()
    (project / 'logs/service.log').write_text('original\n')
    report = migration.precopy(layout, ['logs'], dry_run=True)
    assert report['files'] == 1
    assert report['bytes'] == 9
    assert not layout.data_root.exists()
    assert (project / 'logs/service.log').read_text() == 'original\n'


def test_precopy_verifies_hashes_and_resume_reuses_only_equal_files(tmp_path, monkeypatch):
    project, layout = _layout(tmp_path, monkeypatch)
    (project / 'logs').mkdir()
    source = project / 'logs/service.log'
    source.write_text('first\n')
    first = migration.precopy(layout, ['logs'])
    target = layout.path('logs/service.log')
    assert target.read_text() == 'first\n'
    assert first['copied'] == 1
    repeated = migration.precopy(layout, ['logs'])
    assert repeated['reused'] == 1
    source.write_text('second\n')
    final = migration.precopy(layout, ['logs'])
    assert final['copied'] == 1
    assert target.read_text() == 'second\n'
    manifest = json.loads(Path(final['manifest']).read_text())
    assert manifest['phase'] == 'precopy'
    assert manifest['ready_for_cutover'] is False
    assert manifest['entries'][0]['sha256'] == '480c2336b410f1ad5f8bf1b28944490255804b65350c527787e74ebdd511e3a4'
    assert source.read_text() == 'second\n'


def test_live_sqlite_backup_includes_wal_and_is_integrity_checked(tmp_path, monkeypatch):
    project, layout = _layout(tmp_path, monkeypatch)
    (project / 'bot').mkdir()
    source = project / 'bot/tradesv3.dryrun.sqlite'
    writer = sqlite3.connect(source)
    try:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('CREATE TABLE trades (id INTEGER PRIMARY KEY, amount TEXT)')
        writer.execute("INSERT INTO trades VALUES (1, '17.25')")
        writer.commit()
        assert Path(str(source) + '-wal').exists()
        report = migration.precopy(layout, ['bot/tradesv3.dryrun.sqlite'])
        backup = sqlite3.connect(layout.path('bot/tradesv3.dryrun.sqlite'))
        try:
            assert backup.execute('SELECT * FROM trades').fetchall() == [(1, '17.25')]
            assert backup.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        finally:
            backup.close()
        manifest = json.loads(Path(report['manifest']).read_text())
        assert manifest['entries'][0]['method'] == 'sqlite_online_backup'
        assert not layout.path('bot/tradesv3.dryrun.sqlite-wal').exists()
    finally:
        writer.close()


def test_copy_rejects_escaping_symlink_and_insufficient_space(tmp_path, monkeypatch):
    project, layout = _layout(tmp_path, monkeypatch)
    (project / 'logs').mkdir()
    outside = tmp_path / 'outside'
    outside.write_text('private')
    (project / 'logs/escaped').symlink_to(outside)
    with pytest.raises(RuntimeError, match='symlink'):
        migration.precopy(layout, ['logs'])
    (project / 'logs/escaped').unlink()
    (project / 'logs/data').write_bytes(b'x' * 10)
    monkeypatch.setattr(migration.shutil, 'disk_usage', lambda path: type('Usage', (), {'free': 0})())
    with pytest.raises(RuntimeError, match='space'):
        migration.precopy(layout, ['logs'])
    assert not layout.data_root.exists()


def test_destination_symlink_cannot_overwrite_unrelated_file(tmp_path, monkeypatch):
    project, layout = _layout(tmp_path, monkeypatch)
    (project / 'logs').mkdir()
    (project / 'logs/data').write_text('new')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'data').write_text('keep')
    layout.data_root.mkdir()
    (layout.data_root / 'files').mkdir()
    (layout.data_root / 'files/logs').symlink_to(outside)
    with pytest.raises(RuntimeError, match='symlink'):
        migration.precopy(layout, ['logs'])
    assert (outside / 'data').read_text() == 'keep'


def test_default_inventory_includes_live_iteration_records_but_not_source_or_locks(tmp_path, monkeypatch):
    project, layout = _layout(tmp_path, monkeypatch)
    (project / 'bot/user_data/strategies').mkdir(parents=True)
    (project / 'bot/user_data/auto_spaces').mkdir()
    (project / 'bot/user_data/iterations/run-001/evidence').mkdir(parents=True)
    (project / 'bot/user_data/auto_iteration_status.json').write_text('{}')
    (project / 'bot/user_data/model_versions_smoke.json').write_text('{}')
    (project / 'bot/user_data/auto_spaces/latest.json').write_text('{}')
    (project / 'bot/user_data/iterations/run-001/manifest.json').write_text('{}')
    (project / 'bot/user_data/iterations/run-001/evidence/report.json').write_text('{}')
    (project / 'bot/user_data/auto_iteration.lock').write_text('123')
    (project / 'bot/user_data/strategies/frozen.py').write_text('class Strategy: pass')
    report = migration.inventory(layout)
    assert sorted(item['path'] for item in report['entries']) == [
        'bot/user_data/auto_iteration_status.json',
        'bot/user_data/auto_spaces/latest.json',
        'bot/user_data/iterations/run-001/evidence/report.json',
        'bot/user_data/iterations/run-001/manifest.json',
        'bot/user_data/model_versions_smoke.json',
    ]


def test_external_volume_is_revalidated_before_creating_migration_lock_directory(
        tmp_path, monkeypatch):
    project, layout = _layout(tmp_path, monkeypatch)
    (project / 'logs').mkdir()
    (project / 'logs/service.log').write_text('data')
    original_validate = migration.validate_storage
    calls = 0

    def detach_after_inventory(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_validate(*args, **kwargs)
        raise RuntimeError('external storage unavailable: volume is not mounted')

    monkeypatch.setattr(migration, 'validate_storage', detach_after_inventory)
    with pytest.raises(RuntimeError, match='unavailable'):
        migration.precopy(layout, ['logs'])
    assert not layout.data_root.exists()


def test_modified_target_is_preserved_before_copy_and_manifest_excludes_process_lock(tmp_path, monkeypatch):
    project, layout = _layout(tmp_path, monkeypatch)
    (project / 'logs').mkdir()
    (project / 'logs/data').write_text('new')
    (project / 'logs/service.lock').write_text('pid')
    target = layout.path('logs/data')
    target.parent.mkdir(parents=True)
    target.write_text('old')
    report = migration.precopy(layout, ['logs'])
    assert report['files'] == 1
    assert target.read_text() == 'new'
    previous = Path(report['entries'][0]['previous'])
    assert previous.read_text() == 'old'
    assert (project / 'logs/data').read_text() == 'new'


def test_copy_corruption_is_detected_before_target_is_published(tmp_path, monkeypatch):
    project, layout = _layout(tmp_path, monkeypatch)
    (project / 'logs').mkdir()
    (project / 'logs/data').write_text('source')
    real_copy = migration.shutil.copy2

    def corrupt_copy(source, destination):
        real_copy(source, destination)
        Path(destination).write_text('corruption')

    monkeypatch.setattr(migration.shutil, 'copy2', corrupt_copy)
    with pytest.raises(RuntimeError, match='checksum'):
        migration.precopy(layout, ['logs'])
    assert not layout.path('logs/data').exists()
    assert not list((layout.data_root / 'migration').glob('*.json'))


def test_data_root_cannot_escape_its_mount_using_parent_segments(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    mount = tmp_path / 'disk'
    with pytest.raises(ValueError, match='inside'):
        storage_layout(project, environ={
            'QUANT_DATA_ROOT': str(mount / '..' / 'elsewhere'),
            'QUANT_DATA_MOUNT': str(mount),
            'QUANT_DATA_VOLUME_UUID': 'expected',
        })
