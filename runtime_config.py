"""统一加载本地环境配置，供后端入口、数据同步与运维命令复用。"""

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
from typing import Mapping

from dotenv import load_dotenv


def load_environment(root: str | Path | None = None) -> Path:
    """读取项目根目录的 .env；保留终端或 CI 已经设置的环境变量。"""
    project_root = Path(root).resolve() if root is not None else Path(__file__).resolve().parent
    load_dotenv(project_root / ".env", override=False)
    return project_root


@dataclass(frozen=True)
class StorageLayout:
    """路径配置本身不创建目录；挂载校验由每个写入入口执行。"""

    project_root: Path
    data_root: Path
    external: bool = False
    mount: Path | None = None
    volume_uuid: str | None = None

    def path(self, relative: str | Path) -> Path:
        value = PurePosixPath(str(relative))
        if value.is_absolute() or ".." in value.parts or not value.parts:
            raise ValueError("runtime path must be project-relative")
        base = self.data_root / "files" if self.external else self.project_root
        return base.joinpath(*value.parts)


def storage_layout(root: str | Path | None = None, *,
                   environ: Mapping[str, str] | None = None) -> StorageLayout:
    """显式配置外盘时映射运行文件；未配置继续使用原路径。"""
    settings = os.environ if environ is None else environ
    project = Path(root).resolve() if root is not None else Path(__file__).resolve().parent
    configured = settings.get("QUANT_DATA_ROOT", "").strip()
    if not configured:
        return StorageLayout(project, project)
    # 规范化父目录片段而不 resolve，保留后续对符号链接本身的拒绝。
    destination = Path(os.path.abspath(Path(configured).expanduser()))
    mount_value = settings.get("QUANT_DATA_MOUNT", "").strip()
    mount = Path(os.path.abspath(Path(mount_value).expanduser())) if mount_value else None
    if mount is None and destination.parts[:2] == ("/", "Volumes") and len(destination.parts) > 2:
        mount = Path(*destination.parts[:3])
    if mount is not None and destination != mount and mount not in destination.parents:
        raise ValueError("data root must be inside the configured mount")
    return StorageLayout(project, destination, True, mount,
                         settings.get("QUANT_DATA_VOLUME_UUID", "").strip() or None)
