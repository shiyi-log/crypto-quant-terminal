"""在打包前核对单一版本标签，拒绝含糊或不匹配的发布身份。"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import tomllib


def release_tag_for_version(version, tags):
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("项目版本必须为 X.Y.Z")
    if not isinstance(tags, list) or len(tags) > 1:
        raise ValueError("同一提交只能存在一个发布标签")
    if not tags:
        return ""
    if tags != ["v" + version]:
        raise ValueError("发布标签必须精确匹配项目版本 vX.Y.Z")
    return tags[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    version = tomllib.loads((args.root / "pyproject.toml").read_text())["project"]["version"]
    tags = subprocess.check_output(
        ["git", "tag", "--points-at", "HEAD", "v*"], cwd=args.root, text=True).splitlines()
    print(release_tag_for_version(version, tags))


if __name__ == "__main__":
    main()
