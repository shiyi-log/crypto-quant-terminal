from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
CI = (ROOT / ".github/workflows/ci.yml").read_text()
CD = (ROOT / ".github/workflows/cd.yml").read_text()


def test_ci_runs_new_frontend_business_unit_tests_before_build():
    assert "pnpm --dir vben exec vitest run apps/web-antd/src/views/quant" in CI
    assert "apps/web-antd/src/views/quant" in CI
    assert "typecheck" in CI
    assert "build:antd" in CI


def test_ci_compiles_and_lints_demo_risk_modules():
    for module in ("demo_risk.py", "demo_acceptance.py"):
        assert module in CI


def test_cd_requires_successful_ci_for_exact_packaged_commit():
    assert "actions: read" in CD
    assert "gh run list" in CD
    assert "--workflow CI" in CD
    assert "--commit" in CD
    assert "conclusion == \"success\"" in CD or "conclusion == 'success'" in CD
    assert "COMMIT_SHA" in CD
    assert "github.event.workflow_run.head_branch == 'main'" in CD
    assert "startsWith(github.event.workflow_run.head_branch, 'v')" not in CD


def test_cd_enforces_tag_and_python_project_version_identity():
    assert "pyproject.toml" in CD
    assert re.search(r"release_tag.*version|version.*release_tag", CD, re.I | re.S)
    assert "vX.Y.Z" in CD or "v[0-9]+\\.[0-9]+\\.[0-9]+" in CD
    assert '"version": "%s"' in CD
