"""新重构后端必须进入 CI 与 CD 的源码检查清单。"""

from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("workflow", ["ci.yml", "cd.yml"])
def test_refactor_backend_modules_are_compiled_and_linted_in_ci_and_cd(workflow):
    required = {
        "demo_acceptance_service.py",
        "demo_collection_run.py",
        "demo_safety_runtime.py",
        "demo_archive_providers.py",
        "research_artifact_verifier.py",
        "research_registry_store.py",
        "tools/database_backup.py",
        "tools/database_restore_verification.py",
    }
    source = (ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
    # 分别检查两个实际命令，避免模块仅在注释或另一条命令中出现而漏检。
    for command in ("python -m compileall -q", "ruff check"):
        assert source.count(command) == 1
        body = source.split(command, 1)[1]
        body = re.split(r"\n\s*(?:- name:|uv run )", body, maxsplit=1)[0]
        arguments = set(body.split())
        for module in required:
            assert module in arguments, f"{workflow}: {command} omits {module}"
