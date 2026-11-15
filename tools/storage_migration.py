"""可恢复预复制：保留源文件和旧目标，外盘异常时拒绝创建本地替代目录。"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import sqlite3
import subprocess
import sys
from typing import Iterable, Iterator
import uuid

from runtime_config import StorageLayout, load_environment, storage_layout


DEFAULT_PATHS = (
    'bot/user_data/data', 'bot/user_data/models', 'bot/user_data/freqaimodels',
    'bot/user_data/backtest_results', 'bot/user_data/hyperopt_results',
    'bot/user_data/t039_research_data', 'bot/user_data/logs', 'bot/logs',
    'bot/user_data/auto_spaces', 'bot/user_data/*.json', 'bot/user_data/*.jsonl',
    'bot/tradesv3.dryrun.sqlite', 'database/runtime', 'logs',
)


def probe_volume(mount: Path) -> dict:
    """读取卷身份，不把同名目录当作真实挂载；不创建任何文件。"""
    if not os.path.ismount(mount):
        return {'mounted': False, 'uuid': None}
    try:
        process = subprocess.run(['diskutil', 'info', '-plist', str(mount)],
                                 capture_output=True, check=True, timeout=10)
        info = plistlib.loads(process.stdout)
        return {'mounted': info.get('MountPoint') == str(mount),
                'uuid': info.get('VolumeUUID'), 'writable': info.get('Writable', False)}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {'mounted': True, 'uuid': None, 'writable': False}


def _no_symlink(path: Path) -> None:
    for part in (path, *path.parents):
        if part.is_symlink():
            raise RuntimeError('symlink runtime paths require an explicit reviewed cutover')


def validate_storage(layout: StorageLayout, *, required_bytes: int = 0,
                     reserve_bytes: int = 64 * 1024 * 1024) -> dict:
    """每轮写入前验证目标身份、权限与空间；失败时不执行 mkdir。"""
    if not layout.external or layout.mount is None:
        raise RuntimeError('external storage unavailable: explicit data root and mount required')
    _no_symlink(layout.data_root)
    observed = probe_volume(layout.mount)
    if not observed.get('mounted') or not layout.mount.is_dir():
        raise RuntimeError('external storage unavailable: volume is not mounted')
    if not layout.volume_uuid or observed.get('uuid') != layout.volume_uuid:
        raise RuntimeError('external storage identity mismatch')
    if observed.get('writable') is False or not os.access(layout.mount, os.W_OK | os.X_OK):
        raise RuntimeError('external storage unavailable: volume is not writable')
    available = shutil.disk_usage(layout.mount).free
    if available < required_bytes + reserve_bytes:
        raise RuntimeError('insufficient external storage space')
    return {'mount': str(layout.mount), 'volume_uuid': observed['uuid'],
            'available_bytes': available, 'required_bytes': required_bytes}


def _source_files(layout: StorageLayout, paths: list[str] | tuple[str, ...]) -> Iterator[tuple[str, Path]]:
    seen = set()
    for relative in paths:
        # 路径验证与目标映射共用同一约束；目录不追随符号链接。
        layout.path(relative)
        matches = (layout.project_root.glob(relative) if any(mark in relative for mark in '*?[')
                   else [layout.project_root / relative])
        for source in matches:
            _no_symlink(source)
            if not source.exists():
                continue
            candidates = [source] if source.is_file() else source.rglob('*')
            yield from _file_candidates(layout, candidates, seen)


def _file_candidates(layout: StorageLayout, candidates: Iterable[Path],
                     seen: set[str]) -> Iterator[tuple[str, Path]]:
    for item in candidates:
        if item.is_symlink():
            raise RuntimeError('symlink source cannot be copied implicitly')
        if not item.is_file():
            continue
        name = item.relative_to(layout.project_root).as_posix()
        if name in seen:
            continue
        seen.add(name)
        # 锁文件代表进程生命周期；SQLite WAL 由在线备份吸收，不能裸拷。
        if item.name.endswith(('.lock', '.pid', '-wal', '-shm')):
            continue
        yield name, item


def inventory(layout: StorageLayout, paths: list[str] | tuple[str, ...] = DEFAULT_PATHS) -> dict:
    """只读列出大小，不把扫描结果当作一致性快照。"""
    entries = []
    for relative, path in _source_files(layout, paths):
        status = path.stat()
        entries.append({'path': relative, 'size_bytes': status.st_size,
                        'mtime_ns': status.st_mtime_ns})
    return {'schema': 'quant-storage-inventory-v1', 'files': len(entries),
            'bytes': sum(item['size_bytes'] for item in entries), 'entries': entries}


def _sha256(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _sqlite_backup(source: Path, target: Path) -> None:
    # mode=ro 不会创建缺失源文件，backup API 可包含已经提交的 WAL。
    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=10) as origin:
        with sqlite3.connect(target) as destination:
            origin.backup(destination, pages=2048, sleep=0.05)
            result = destination.execute('PRAGMA integrity_check').fetchall()
            if result != [('ok',)]:
                raise RuntimeError('SQLite backup integrity check failed')
            destination.execute('PRAGMA journal_mode=DELETE')


@contextmanager
def _migration_lock(layout: StorageLayout):
    directory = layout.data_root / 'migration'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / 'precopy.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('storage precopy already running') from None
        try:
            yield directory
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _copy_one(layout: StorageLayout, relative: str, run_id: str) -> dict:
    source = layout.project_root / relative
    target = layout.path(relative)
    _no_symlink(source)
    _no_symlink(target)
    validate_storage(layout)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.parent / ('.' + target.name + '.' + uuid.uuid4().hex + '.partial')
    method = 'sqlite_online_backup' if source.suffix in {'.sqlite', '.sqlite3', '.db'} else 'atomic_copy'
    before = source.stat()
    try:
        if method == 'sqlite_online_backup':
            _sqlite_backup(source, temporary)
        else:
            original_hash = _sha256(source)
            shutil.copy2(source, temporary)
            after = source.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                return {'path': relative, 'status': 'source_changed', 'method': method}
        checksum = _sha256(temporary)
        if method == 'atomic_copy' and checksum != original_hash:
            raise RuntimeError('copy checksum mismatch; destination was not published')
        reused = target.is_file() and _sha256(target) == checksum
        preserved = None
        if not reused:
            if target.exists():
                preserved = layout.data_root / 'migration' / run_id / 'previous' / relative
                _no_symlink(preserved)
                preserved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                # 旧目标保留原 inode；当前目标发布使用同目录原子替换。
                os.link(target, preserved)
            validate_storage(layout)
            with temporary.open('rb') as output:
                os.fsync(output.fileno())
            os.replace(temporary, target)
        return {'path': relative, 'status': 'reused' if reused else 'copied',
                'size_bytes': target.stat().st_size, 'sha256': checksum, 'method': method,
                'previous': str(preserved) if preserved else None}
    finally:
        temporary.unlink(missing_ok=True)


def precopy(layout: StorageLayout, paths: list[str] | tuple[str, ...] = DEFAULT_PATHS, *,
            dry_run: bool = False, reserve_bytes: int = 64 * 1024 * 1024) -> dict:
    """在线预复制从不宣称可切换；改变中的普通文件留到下一轮。"""
    plan = inventory(layout, paths)
    # 最坏情形保留旧目标和完整新副本，空间预算不依赖可变目标的缓存。
    volume = validate_storage(layout, required_bytes=plan['bytes'], reserve_bytes=reserve_bytes)
    report = {**plan, 'volume': volume, 'dry_run': dry_run, 'phase': 'precopy',
              'ready_for_cutover': False, 'copied': 0, 'reused': 0}
    if dry_run:
        return report
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    with _migration_lock(layout) as directory:
        entries = []
        for item in plan['entries']:
            entry = _copy_one(layout, item['path'], run_id)
            entries.append(entry)
            if entry['status'] in {'copied', 'reused'}:
                report[entry['status']] += 1
        report.update({'entries': entries, 'run_id': run_id,
                       'source_changed': sum(item['status'] == 'source_changed' for item in entries)})
        path = directory / f'{run_id}.json'
        with path.open('x', encoding='utf-8') as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        report['manifest'] = str(path)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='运行数据清单与外盘在线预复制（默认只读）')
    parser.add_argument('operation', choices=('inventory', 'precopy'))
    parser.add_argument('paths', nargs='*', help='项目相对路径；缺省使用运行数据清单')
    parser.add_argument('--apply', action='store_true', help='写入预复制；不会切换服务路径')
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    load_environment(root)
    layout = storage_layout(root)
    try:
        if args.operation == 'inventory':
            result = inventory(layout, args.paths or DEFAULT_PATHS)
        else:
            result = precopy(layout, args.paths or DEFAULT_PATHS, dry_run=not args.apply)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        print(json.dumps({'status': 'failed', 'error': 'storage_operation_failed'}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
