"""统一加载本地环境配置，供后端入口、数据同步与运维命令复用。"""

from pathlib import Path

from dotenv import load_dotenv


def load_environment(root: str | Path | None = None) -> Path:
    """读取项目根目录的 .env；保留终端或 CI 已经设置的环境变量。"""
    project_root = Path(root).resolve() if root is not None else Path(__file__).resolve().parent
    load_dotenv(project_root / ".env", override=False)
    return project_root
