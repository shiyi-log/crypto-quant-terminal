"""新重构后端必须进入 CI 与 CD 的源码检查清单。"""

from pathlib import Path
import shlex

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("workflow", ["ci.yml", "cd.yml"])
@pytest.mark.parametrize("command", ["python -m compileall -q", "ruff check"])
def test_refactor_backend_modules_are_compiled_and_linted_in_ci_and_cd(workflow, command):
    required = {
        "demo_acceptance_service.py",
        "demo_collection_run.py",
        "demo_command_store.py",
        "demo_equity_observation.py",
        "demo_safety_runtime.py",
        "demo_archive_providers.py",
        "research_artifact_verifier.py",
        "research_registry_store.py",
        "research_statistics.py",
        "research_two_stage.py",
        "tools/database_backup.py",
        "tools/database_restore_verification.py",
    }
    source = (ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
    # 分别检查两个实际命令，避免模块仅在注释或另一条命令中出现而漏检。
    assert source.count(command) == 1
    body = source.split(command, 1)[1]
    arguments = set()
    for line in body.splitlines():
        if line.strip().startswith(("- name:", "uv run ")):
            break
        # CD 使用 shell 续行；逐行移除续行符后解析，注释不计作源码参数。
        arguments.update(shlex.split(line.rstrip().removesuffix(chr(92)), comments=True))
    missing = required - arguments
    assert not missing, f"{workflow}: {command} omits {sorted(missing)}"
