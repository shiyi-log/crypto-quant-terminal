"""新重构后端必须进入 CI 与 CD 的源码检查清单。"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_refactor_backend_modules_are_compiled_and_linted_in_ci_and_cd():
    required = {
        "demo_acceptance_service.py",
        "research_registry_store.py",
        "tools/database_backup.py",
        "tools/database_restore_verification.py",
    }
    for workflow in ("ci.yml", "cd.yml"):
        source = (ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
        assert "python -m compileall -q" in source
        assert "ruff check" in source
        for module in required:
            assert module in source, f"{workflow} omits {module}"
