from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
CI = (ROOT / ".github/workflows/ci.yml").read_text()
CD = (ROOT / ".github/workflows/cd.yml").read_text()


def test_ci_runs_full_frontend_units_and_browser_flows_around_build():
    assert "pnpm --dir vben run test:unit" in CI
    assert CI.index("test:unit") < CI.index("typecheck") < CI.index("build:antd")
    assert CI.index("build:antd") < CI.index("playwright install chromium --with-deps")
    assert CI.index("playwright install chromium --with-deps") < CI.index("run test:e2e")


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


def test_cd_verifies_archive_checksum_and_build_identity_before_upload():
    verification = CD.index("验证发布归档哈希与 build.json 身份")
    upload = CD.index("上传发布归档")
    assert verification < upload
    for contract in ("sha256sum --check \"$archive.sha256\"", "build.json", "tarfile.open",
                     "COMMIT_SHA", "PROJECT_VERSION", "package_name"):
        assert contract in CD[verification:upload]
