from __future__ import annotations

import io
import re
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts.release_tools import (
    ReleaseContractError,
    check_dist,
    check_lock,
    check_tag,
    write_lock_manifest,
)


ROOT = Path(__file__).resolve().parents[2]


def _release_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "requirements").mkdir(parents=True)
    (root / "src/bfbt").mkdir(parents=True)
    (root / "scripts").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "bfbt"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (root / "src/bfbt/__init__.py").write_text(
        '__version__ = "0.1.0"\n', encoding="utf-8"
    )
    (root / "requirements/bootstrap.in").write_text(
        "setuptools>=69,<85\n", encoding="utf-8"
    )
    (root / "scripts/update_locks.sh").write_text(
        "#!/usr/bin/env bash\n", encoding="utf-8"
    )
    locked = "example==1.0 --hash=sha256:" + "a" * 64 + "\n"
    (root / "requirements/runtime.lock").write_text(locked, encoding="utf-8")
    (root / "requirements/dev.lock").write_text(locked, encoding="utf-8")
    (root / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 0.1.0 - 2026-09-29\n", encoding="utf-8"
    )
    (root / "CHANGELOG.zh-CN.md").write_text(
        "# 变更记录\n\n## 0.1.0 - 2026-09-29\n", encoding="utf-8"
    )
    write_lock_manifest("1.2.3", root)
    return root


def test_lock_and_tag_contracts_fail_closed(tmp_path: Path) -> None:
    root = _release_root(tmp_path)
    payload = check_lock(root)
    assert payload["generator_version"] == "1.2.3"
    assert payload["python_requires"] == ">=3.10,<3.13"
    assert check_tag("v0.1.0", root) == "0.1.0"

    with pytest.raises(ReleaseContractError, match="vMAJOR.MINOR.PATCH"):
        check_tag("0.1.0", root)
    with pytest.raises(ReleaseContractError, match="does not match"):
        check_tag("v0.1.1", root)

    (root / "requirements/runtime.lock").write_text("changed\n", encoding="utf-8")
    with pytest.raises(ReleaseContractError, match="changed without refreshing"):
        check_lock(root)


def test_distribution_gate_rejects_extra_packages_and_writes_checksums(
    tmp_path: Path,
) -> None:
    root = _release_root(tmp_path)
    dist = root / "dist"
    dist.mkdir()
    wheel = dist / "bfbt-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("bfbt/__init__.py", '__version__ = "0.1.0"\n')
        archive.writestr(
            "bfbt-0.1.0.dist-info/METADATA",
            "Metadata-Version: 2.4\nName: bfbt\nVersion: 0.1.0\n"
            "License-Expression: MIT\nRequires-Python: <3.13,>=3.10\n",
        )
    source = dist / "bfbt-0.1.0.tar.gz"
    with tarfile.open(source, "w:gz") as archive:
        contents = {
            "LICENSE": b"MIT\n",
            "README.md": b"# BFBT\n",
            "pyproject.toml": b"[project]\nname = 'bfbt'\n",
            "requirements/runtime.lock": b"locked\n",
            "requirements/lock-manifest.json": b"{}\n",
            "scripts/release_tools.py": b"# release gate\n",
        }
        for name, content in contents.items():
            info = tarfile.TarInfo(f"bfbt-0.1.0/{name}")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))

    hashes = check_dist("0.1.0", dist, root)
    assert set(hashes) == {wheel.name, source.name}
    checksums = (dist / "SHA256SUMS").read_text(encoding="utf-8")
    assert wheel.name in checksums and source.name in checksums

    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("other_package/__init__.py", "")
    with pytest.raises(ReleaseContractError, match="outside bfbt"):
        check_dist("0.1.0", dist, root)


def test_repository_release_automation_and_public_policies_are_wired() -> None:
    tests = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    dependabot = (ROOT / ".github/dependabot.yml").read_text(encoding="utf-8")
    installer = (ROOT / "scripts/install_ubuntu.sh").read_text(encoding="utf-8")
    assert "requirements/dev.lock" in tests
    assert "--require-hashes" in tests
    assert "actions/checkout@v" not in tests
    assert "actions/setup-python@v" not in tests
    assert 'tags:\n      - "v*.*.*"' in release
    assert "check-tag" in release and "check-dist" in release
    assert "gh release create" in release and "--verify-tag" in release
    assert "--draft" in release and "--draft=false" in release
    assert "workflow_dispatch" not in release
    assert not any(
        f"actions/{name}@v" in release
        for name in ("checkout", "setup-python", "upload-artifact", "download-artifact")
    )
    assert "package-ecosystem: pip" in dependabot
    assert "package-ecosystem: github-actions" in dependabot
    assert "interval: weekly" in dependabot
    assert "--require-hashes -r requirements/runtime.lock" in installer
    assert "--no-deps --no-build-isolation -e ." in installer

    for workflow in (ROOT / ".github/workflows").glob("*.yml"):
        contents = workflow.read_text(encoding="utf-8")
        action_refs = re.findall(r"uses:\s+[^\s@]+@([^\s#]+)", contents)
        assert action_refs
        assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in action_refs)

    pairs = (
        ("docs/reference/open_source_release.md", "open_source_release.zh-CN.md"),
        ("docs/reference/extension_policy.md", "extension_policy.zh-CN.md"),
    )
    for english_name, chinese_name in pairs:
        english = (ROOT / english_name).read_text(encoding="utf-8")
        chinese_path = ROOT / "docs/reference" / chinese_name
        chinese = chinese_path.read_text(encoding="utf-8")
        assert chinese_name in english
        assert Path(english_name).name in chinese

    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    documentation = (ROOT / "docs/README.md").read_text(encoding="utf-8")
    assert "requirements/dev.lock" in contributing
    assert "reference/open_source_release.md" in documentation
    assert "reference/extension_policy.md" in documentation
