"""离线核验研究文件字节与闭盘质量；不授予来源真实性或交易资格。"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import stat
import sys

from research_protocol import (
    FrozenResearchEvidence,
    SYMBOLS,
    TIMEFRAMES,
    assess_candle_coverage,
)

LOGGER = logging.getLogger(__name__)
MAX_BYTES = 256 * 1024 * 1024
MAX_LINE_BYTES = 64 * 1024
MAX_ROWS = 600_000
CHUNK_BYTES = 64 * 1024
HASH = re.compile(r'[a-f0-9]{64}\Z')
REFERENCE = re.compile(r'[A-Za-z0-9_.:/@-]{1,200}\Z')
ARTIFACT_REFERENCE = re.compile(r'(?:artifact|archive|dataset|quality):[A-Za-z0-9_./-]{1,180}\Z')
SPEC_KEYS = {'symbol', 'timeframe', 'start_ms', 'end_ms', 'as_of_ms', 'source_ref'}
CANDLE_KEYS = {'timestamp_ms', 'available_ms', 'closed', 'open', 'high', 'low',
               'close', 'volume'}
MEDIA_TYPES = frozenset({'application/json', 'text/csv', 'application/octet-stream',
                         'image/png', 'application/pdf'})


class ArtifactVerificationError(ValueError):
    """只携带稳定错误码，避免原始数据、路径或凭据进入日志。"""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code):
    raise ArtifactVerificationError(code)


def _canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False)
    except (ValueError, TypeError, UnicodeError):
        _fail('json_invalid')


def _freeze(value):
    text = _canonical(value)
    return FrozenResearchEvidence(text, hashlib.sha256(text.encode('utf-8')).hexdigest())


def _reject_number(_):
    _fail('json_number_inexact')


def _exact_int(text):
    if len(text.lstrip('-')) > 96:
        _fail('json_number_invalid')
    return int(text)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail('json_duplicate_key')
        result[key] = value
    return result


def _exact_json(raw):
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                           parse_int=_exact_int, parse_float=_reject_number,
                           parse_constant=_reject_number)
        pending = [(value, 0)]
        while pending:
            item, depth = pending.pop()
            if depth > 32:
                _fail('json_invalid')
            if isinstance(item, dict):
                pending.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                pending.extend((child, depth + 1) for child in item)
        # 孤立代理字符不是可规范编码的 UTF-8 事实。
        _canonical(value).encode('utf-8')
        return value
    except ArtifactVerificationError:
        raise
    except (ValueError, UnicodeError, TypeError, RecursionError):
        _fail('json_invalid')


def _stamp(value):
    return type(value) is int and 0 <= value < 2**63


def _reference(value):
    return isinstance(value, str) and REFERENCE.fullmatch(value) is not None


def _spec(value):
    if (not isinstance(value, dict) or set(value) != SPEC_KEYS
            or not isinstance(value['symbol'], str) or value['symbol'] not in SYMBOLS
            or not isinstance(value['timeframe'], str) or value['timeframe'] not in TIMEFRAMES
            or not all(_stamp(value[key]) for key in ('start_ms', 'end_ms', 'as_of_ms'))
            or value['start_ms'] >= value['end_ms'] or not _reference(value['source_ref'])):
        _fail('spec_invalid')
    return dict(value)


def _limit(value, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        _fail('limit_invalid')
    return value


def _absolute(value):
    try:
        path = Path(value)
        str(path).encode(sys.getfilesystemencoding(), errors='strict')
        if '\x00' in str(path) or '..' in path.parts:
            _fail('path_unsafe')
        return Path(os.path.abspath(path))
    except (TypeError, ValueError, OSError):
        _fail('path_unsafe')


def _allowed_path(path, allowed_roots):
    path = _absolute(path)
    if not isinstance(allowed_roots, (list, tuple)) or not 1 <= len(allowed_roots) <= 32:
        _fail('path_unsafe')
    roots = [_absolute(root) for root in allowed_roots]
    matches = [root for root in roots if path.is_relative_to(root)]
    if not matches:
        _fail('path_unsafe')
    root = max(matches, key=lambda value: len(value.parts))
    return path, root


def _open_directory(path):
    """逐级打开真实目录，不先 resolve 后再打开，避免检查与使用之间跟随链接。"""
    fd = None
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open('/', flags)
        for part in path.parts[1:]:
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except OSError:
        if fd is not None:
            os.close(fd)
        _fail('path_unsafe')


def _open_parent(path, allowed_roots):
    path, root = _allowed_path(path, allowed_roots)
    if path == root:
        _fail('path_unsafe')
    return path, _open_directory(path.parent)


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _assert_stable(fd, original, path, allowed_roots):
    try:
        if _identity(os.fstat(fd)) != _identity(original):
            _fail('source_changed')
        _, parent_fd = _open_parent(path, allowed_roots)
        try:
            current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        finally:
            os.close(parent_fd)
        if _identity(current) != _identity(original) or not stat.S_ISREG(current.st_mode):
            _fail('source_changed')
    except (OSError, ArtifactVerificationError):
        _fail('source_changed')


def _read_file(path, *, allowed_roots, max_bytes=MAX_BYTES, on_chunk=None):
    """真实字节流计算 SHA；输入变化时不发出可登记的成功证据。"""
    max_bytes = _limit(max_bytes, MAX_BYTES)
    path, parent_fd = _open_parent(path, allowed_roots)
    fd = None
    try:
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=parent_fd)
        except OSError as exc:
            _fail('path_unsafe' if exc.errno in {errno.ELOOP, errno.ENOTDIR} else 'file_unreadable')
        original = os.fstat(fd)
        if not stat.S_ISREG(original.st_mode):
            _fail('file_not_regular')
        if original.st_size > max_bytes:
            _fail('limit_exceeded')
        sha, size, parse_error = hashlib.sha256(), 0, None
        while True:
            chunk = os.read(fd, min(CHUNK_BYTES, max_bytes - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                _fail('limit_exceeded')
            sha.update(chunk)
            if on_chunk is not None and parse_error is None:
                try:
                    on_chunk(chunk)
                except ArtifactVerificationError as exc:
                    # 即使解析已失败也完成有界读与身份检查，优先报告文件变化。
                    parse_error = exc
        _assert_stable(fd, original, path, allowed_roots)
        if size != original.st_size:
            _fail('source_changed')
        if parse_error is not None:
            raise parse_error
        return sha.hexdigest(), size
    except ArtifactVerificationError:
        raise
    except OSError:
        _fail('file_unreadable')
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent_fd)


class _CandleParser:
    """只保留有界行缓冲和协议所需的真实行，不补根或推断可用时间。"""

    def __init__(self, spec, max_line_bytes, max_rows):
        self.spec = spec
        self.line_limit = _limit(max_line_bytes, MAX_LINE_BYTES)
        self.row_limit = _limit(max_rows, MAX_ROWS)
        self.pending = b''
        self.rows = []

    def feed(self, chunk):
        self.pending += chunk
        while b'\n' in self.pending:
            line, self.pending = self.pending.split(b'\n', 1)
            self.line(line)
        if len(self.pending) > self.line_limit:
            _fail('limit_exceeded')

    def finish(self):
        if self.pending:
            self.line(self.pending)
        return self.rows

    def line(self, line):
        if len(line) > self.line_limit or len(self.rows) >= self.row_limit:
            _fail('limit_exceeded')
        row = _exact_json(line)
        optional = {'symbol', 'timeframe'}
        if (not isinstance(row, dict) or not CANDLE_KEYS <= set(row)
                or set(row) - CANDLE_KEYS - optional):
            _fail('candle_schema_invalid')
        for key in optional & set(row):
            if row[key] != self.spec[key]:
                _fail('candle_spec_mismatch')
        self.rows.append({key: row[key] for key in CANDLE_KEYS})


def build_candle_snapshot(source_path, frozen_spec, *, allowed_roots,
                          max_bytes=MAX_BYTES, max_line_bytes=MAX_LINE_BYTES, max_rows=MAX_ROWS):
    """从冻结条件与稳定 JSONL 字节生成质量证据，来源始终保持未认证。"""
    spec = _spec(frozen_spec)
    parser = _CandleParser(spec, max_line_bytes, max_rows)
    source_sha, size = _read_file(source_path, allowed_roots=allowed_roots,
                                  max_bytes=max_bytes, on_chunk=parser.feed)
    bars = parser.finish()
    # 当前没有独立内部不可变来源收据核验器，绝不能用输入标志或 SHA 提升信任。
    quality = assess_candle_coverage(bars, **spec, trusted_source=False)
    result = _freeze(dict(
        schema_version='research-candle-snapshot-v1', spec=spec,
        source=dict(sha256=source_sha, size_bytes=size, format='jsonl', authenticated=False),
        provenance=dict(status='unverified', reason_codes=['source_untrusted']),
        quality=quality.to_dict(), quality_sha256=quality.sha256, acceptance_eligible=False,
    ))
    LOGGER.info('研究快照完成 source_sha256=%s rows=%s status=not_estimable；来源未认证',
                source_sha, len(bars))
    return result


def verify_local_artifact(path, *, allowed_roots, expected_sha256, media_type, reference,
                          max_bytes=MAX_BYTES):
    """核对本地真实字节与声明，返回登记所需引用；不验证来源或替调用方登记。"""
    if (not isinstance(expected_sha256, str) or not HASH.fullmatch(expected_sha256)
            or not isinstance(media_type, str) or media_type not in MEDIA_TYPES
            or not isinstance(reference, str) or not ARTIFACT_REFERENCE.fullmatch(reference)):
        _fail('artifact_declaration_invalid')
    sha, size = _read_file(path, allowed_roots=allowed_roots, max_bytes=max_bytes)
    if sha != expected_sha256:
        _fail('sha256_mismatch')
    return _freeze(dict(uri=reference, sha256=sha, media_type=media_type,
                        size_bytes=size, source_authenticated=False))


def write_snapshot(snapshot, output_dir, *, allowed_roots):
    """用内容寻址名字创建新文件；同名产物即使相同也不覆盖。"""
    if not isinstance(snapshot, FrozenResearchEvidence):
        _fail('snapshot_invalid')
    try:
        data = snapshot.canonical_json.encode('utf-8')
    except (AttributeError, UnicodeError):
        _fail('snapshot_invalid')
    if (not isinstance(snapshot.sha256, str) or not HASH.fullmatch(snapshot.sha256)
            or hashlib.sha256(data).hexdigest() != snapshot.sha256):
        _fail('snapshot_invalid')
    if len(data) > MAX_LINE_BYTES:
        _fail('snapshot_invalid')
    try:
        if _canonical(_exact_json(data)) != snapshot.canonical_json:
            _fail('snapshot_invalid')
    except ArtifactVerificationError:
        _fail('snapshot_invalid')
    output_dir, _ = _allowed_path(output_dir, allowed_roots)
    parent_fd = _open_directory(output_dir)
    name, fd, created_identity = snapshot.sha256 + '.json', None, None
    try:
        try:
            fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o444, dir_fd=parent_fd)
            info = os.fstat(fd)
            created_identity = (info.st_dev, info.st_ino)
        except FileExistsError:
            _fail('output_exists')
        remaining = memoryview(data)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                _fail('output_unwritable')
            remaining = remaining[written:]
        os.fsync(fd)
        stable = os.fstat(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        written_sha = hashlib.sha256()
        read_size = 0
        while True:
            chunk = os.read(fd, CHUNK_BYTES)
            if not chunk:
                break
            written_sha.update(chunk)
            read_size += len(chunk)
            if read_size > len(data):
                _fail('output_changed')
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (written_sha.hexdigest() != snapshot.sha256 or read_size != len(data)
                or _identity(stable) != _identity(os.fstat(fd))
                or _identity(stable) != _identity(current) or not stat.S_ISREG(current.st_mode)):
            _fail('output_changed')
        # 重开目录核对身份，避免把已移走目录中的产物当作返回路径的产物。
        current_fd = _open_directory(output_dir)
        try:
            old, current = os.fstat(parent_fd), os.fstat(current_fd)
            if (old.st_dev, old.st_ino) != (current.st_dev, current.st_ino):
                _fail('output_changed')
        finally:
            os.close(current_fd)
        os.fsync(parent_fd)
        return output_dir / name
    except (OSError, ArtifactVerificationError) as exc:
        if created_identity is not None:
            try:
                current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == created_identity:
                    os.unlink(name, dir_fd=parent_fd)
            except OSError:
                pass
        if isinstance(exc, ArtifactVerificationError):
            raise
        _fail('output_unwritable')
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent_fd)


def _read_spec(path, roots):
    parts = []
    _read_file(path, allowed_roots=roots, max_bytes=MAX_LINE_BYTES, on_chunk=parts.append)
    return _spec(_exact_json(b''.join(parts)))


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse 默认会回显未知参数；这里不回显可能包含凭据的参数。
        _fail('cli_arguments_invalid')


def main(argv=None):
    """离线命令成功返回 0，声明/文件错误返回 2，摘要不包含原始行或路径。"""
    try:
        parser = _SafeArgumentParser(description='离线研究产物内容核验（来源未认证）')
        commands = parser.add_subparsers(dest='command', required=True,
                                         parser_class=_SafeArgumentParser)
        snapshot = commands.add_parser('snapshot', help='从冻结 spec 和 JSONL 生成质量产物')
        snapshot.add_argument('--source', required=True)
        snapshot.add_argument('--spec', required=True)
        snapshot.add_argument('--output-dir', required=True)
        verify = commands.add_parser('verify', help='核对本地文件与声明哈希')
        verify.add_argument('--source', required=True)
        verify.add_argument('--expected-sha256', required=True)
        verify.add_argument('--media-type', required=True)
        verify.add_argument('--reference', required=True)
        for command in (snapshot, verify):
            command.add_argument('--allow-root', action='append', required=True)
            command.add_argument('--max-bytes', type=int, default=MAX_BYTES)
        args = parser.parse_args(argv)
        if args.command == 'snapshot':
            result = build_candle_snapshot(
                args.source, _read_spec(args.spec, args.allow_root), allowed_roots=args.allow_root,
                max_bytes=args.max_bytes)
            write_snapshot(result, args.output_dir, allowed_roots=args.allow_root)
            quality = result.to_dict()['quality']
            print(_canonical(dict(结果='研究快照已写入', sha256=result.sha256,
                                  status=quality['status'], reason_codes=quality['reason_codes'])))
        else:
            result = verify_local_artifact(
                args.source, allowed_roots=args.allow_root, expected_sha256=args.expected_sha256,
                media_type=args.media_type, reference=args.reference, max_bytes=args.max_bytes)
            print(_canonical(dict(结果='本地字节核验通过', **result.to_dict())))
        return 0
    except ArtifactVerificationError as exc:
        print('研究产物核验失败 code=' + exc.code, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
