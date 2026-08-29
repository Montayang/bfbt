"""Runtime, dependency, and source fingerprints for reproducible runs."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from importlib import metadata
from pathlib import Path

from bianbt.data.hashing import content_sha256, sha256_file


class EnvironmentError(RuntimeError):
    """The local source or dependency environment cannot be identified."""


@dataclass(frozen=True)
class EnvironmentInfo:
    git_commit: str
    source_fingerprint: str
    git_dirty: bool
    python_version: str
    dependency_fingerprint: str
    dependencies: tuple[str, ...]

    def to_json(self) -> str:
        return json.dumps(
            asdict(self),
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"


def _git(root: Path, *args: str) -> bytes:
    try:
        return subprocess.run(
            ("git", *args),
            cwd=root,
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise EnvironmentError(f"cannot inspect Git environment: {exc}") from exc


def _dependencies() -> tuple[str, ...]:
    requirements = metadata.requires("bianbt") or []
    names = []
    for requirement in requirements:
        if "extra ==" in requirement:
            continue
        matched = re.match(r"[A-Za-z0-9_.-]+", requirement)
        if matched is not None:
            names.append(matched.group(0))
    resolved = []
    for name in sorted(set(names), key=str.lower):
        try:
            version = metadata.version(name)
        except metadata.PackageNotFoundError as exc:
            raise EnvironmentError(f"required distribution is missing: {name}") from exc
        resolved.append(f"{name.lower()}=={version}")
    return tuple(resolved)


def capture_environment(project_root: Path) -> EnvironmentInfo:
    root = project_root.resolve()
    commit = _git(root, "rev-parse", "HEAD").decode().strip()
    status = _git(root, "status", "--porcelain", "--untracked-files=all", "--", ".")
    diff = _git(root, "diff", "--binary", "HEAD", "--", ".")
    untracked_rows = []
    for line in status.decode("utf-8", errors="strict").splitlines():
        if not line.startswith("?? "):
            continue
        relative = line[3:]
        path = root / relative
        if path.is_file():
            untracked_rows.append((relative, sha256_file(path)))
    source_fingerprint = content_sha256(
        {
            "commit": commit,
            "diff_sha256": content_sha256(diff.hex()),
            "untracked": sorted(untracked_rows),
        }
    )
    dependencies = _dependencies()
    dependency_fingerprint = content_sha256(dependencies)
    return EnvironmentInfo(
        git_commit=commit,
        source_fingerprint=source_fingerprint,
        git_dirty=bool(status),
        python_version=".".join(str(item) for item in sys.version_info[:3]),
        dependency_fingerprint=dependency_fingerprint,
        dependencies=dependencies,
    )
