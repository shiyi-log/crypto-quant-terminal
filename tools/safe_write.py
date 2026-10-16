"""原子写入工具 —— 防止 open(path,'w') 先截断后失败导致文件全失。

用法（Python）:
    from tools.safe_write import atomic_write
    atomic_write(path, text)            # 写前自动备份到 path.bak
用法（CLI）:
    python3 tools/safe_write.py <path> < new_content   # 从 stdin 读并原子写入
"""
import os, shutil, sys, tempfile


def atomic_write(path, text, *, backup=True):
    """先写同目录临时文件，再 os.replace 原子替换；可选先备份原文件。"""
    path = os.path.abspath(path)
    d = os.path.dirname(path) or '.'
    if backup and os.path.exists(path):
        try:
            shutil.copy2(path, path + '.bak')
        except OSError:
            pass
    fd, tmp = tempfile.mkstemp(dir=d, prefix='.sw_', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    text = sys.stdin.read()
    if not text:
        print('  拒绝: 输入为空（防止误清空文件）', file=sys.stderr)
        return 3
    p = atomic_write(argv[1], text)
    print(f'  ✅ 原子写入 {p} · {len(text)} 字符 · 备份 {p}.bak')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
