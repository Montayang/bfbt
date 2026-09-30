#!/usr/bin/env python3
"""Offline lock, version, and distribution release gates."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tarfile
import zipfile
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
LOCK_SCHEMA = "bfbt-lock-manifest/v1"
LOCK_SOURCES = (
    Path("pyproject.toml"),
    Path("requirements/bootstrap.in"),
    Path("scripts/update_locks.sh"),
)
LOCK_FILES = (Path("requirements/runtime.lock"), Path("requirements/dev.lock"))
TAG_PATTERN = re.compile(
    r"^v(?P<version>(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))$"
)


class ReleaseContractError(RuntimeError):
    """Raised when a release gate fails closed."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _relative_hashes(paths: tuple[Path, ...], root: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for relative in paths:
        path = root / relative
        if not path.is_file():
            raise ReleaseContractError(f"required release file is missing: {relative}")
        values[relative.as_posix()] = _sha256(path)
    return values


def write_lock_manifest(generator_version: str, root: Path = ROOT) -> Path:
    if not re.fullmatch(r"\d+\.\d+\.\d+", generator_version):
        raise ReleaseContractError("generator version must be an exact X.Y.Z version")
    payload = {
        "schema_version": LOCK_SCHEMA,
        "generator": "uv",
        "generator_version": generator_version,
        "python_requires": ">=3.10,<3.13",
        "sources": _relative_hashes(LOCK_SOURCES, root),
        "locks": _relative_hashes(LOCK_FILES, root),
    }
    path = root / "requirements" / "lock-manifest.json"
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def check_lock(root: Path = ROOT) -> dict[str, object]:
    path = root / "requirements" / "lock-manifest.json"
    if not path.is_file():
        raise ReleaseContractError("requirements/lock-manifest.json is missing")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != LOCK_SCHEMA:
        raise ReleaseContractError("unsupported lock manifest schema")
    if payload.get("generator") != "uv" or not re.fullmatch(
        r"\d+\.\d+\.\d+", str(payload.get("generator_version", ""))
    ):
        raise ReleaseContractError("lock manifest must pin an exact uv generator version")
    if payload.get("python_requires") != ">=3.10,<3.13":
        raise ReleaseContractError("lock manifest Python range does not match the project")
    expected_sources = _relative_hashes(LOCK_SOURCES, root)
    expected_locks = _relative_hashes(LOCK_FILES, root)
    if payload.get("sources") != expected_sources:
        raise ReleaseContractError("lock inputs changed; regenerate both lock files")
    if payload.get("locks") != expected_locks:
        raise ReleaseContractError("a lock file changed without refreshing its manifest")
    for relative in LOCK_FILES:
        text = (root / relative).read_text(encoding="utf-8")
        if "--hash=sha256:" not in text:
            raise ReleaseContractError(f"{relative} is not hash locked")
    return payload


def _declared_version(root: Path) -> str:
    project = (root / "pyproject.toml").read_text(encoding="utf-8")
    package = (root / "src/bfbt/__init__.py").read_text(encoding="utf-8")
    project_match = re.search(r'^version = "([^"]+)"$', project, re.MULTILINE)
    package_match = re.search(r'^__version__ = "([^"]+)"$', package, re.MULTILINE)
    if project_match is None or package_match is None:
        raise ReleaseContractError("cannot read both declared versions")
    if project_match.group(1) != package_match.group(1):
        raise ReleaseContractError("pyproject.toml and bfbt.__version__ disagree")
    return project_match.group(1)


def check_tag(tag: str, root: Path = ROOT) -> str:
    match = TAG_PATTERN.fullmatch(tag)
    if match is None:
        raise ReleaseContractError("release tag must use vMAJOR.MINOR.PATCH")
    version = match.group("version")
    declared = _declared_version(root)
    if version != declared:
        raise ReleaseContractError(
            f"tag {tag} does not match declared project version {declared}"
        )
    heading = rf"^## {re.escape(version)} - \d{{4}}-\d{{2}}-\d{{2}}$"
    for name in ("CHANGELOG.md", "CHANGELOG.zh-CN.md"):
        changelog = (root / name).read_text(encoding="utf-8")
        if not re.search(heading, changelog, re.MULTILINE):
            raise ReleaseContractError(
                f"{name} has no dated {version} release section"
            )
    check_lock(root)
    return version


def _safe_member(name: str, expected_root: str | None = None) -> bool:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        return False
    if expected_root is not None and (not path.parts or path.parts[0] != expected_root):
        return False
    lowered = name.lower()
    forbidden = (".env", "data/backtest", "/bianbt", "\\bianbt")
    return not any(item in lowered for item in forbidden)


def check_dist(version: str, dist: Path, root: Path = ROOT) -> dict[str, str]:
    wheel = dist / f"bfbt-{version}-py3-none-any.whl"
    source = dist / f"bfbt-{version}.tar.gz"
    if not wheel.is_file() or not source.is_file():
        raise ReleaseContractError("expected one canonical wheel and source distribution")
    if sorted(dist.glob("*.whl")) != [wheel] or sorted(dist.glob("*.tar.gz")) != [source]:
        raise ReleaseContractError("distribution directory contains unexpected package archives")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if not names or not all(_safe_member(name) for name in names):
            raise ReleaseContractError("wheel contains an unsafe path")
        allowed = ("bfbt/", f"bfbt-{version}.dist-info/")
        if any(not name.startswith(allowed) for name in names):
            raise ReleaseContractError("wheel contains a package outside bfbt")
        metadata_name = f"bfbt-{version}.dist-info/METADATA"
        if metadata_name not in names:
            raise ReleaseContractError("wheel metadata is missing")
        metadata = archive.read(metadata_name).decode("utf-8")
        required_metadata = (
            "Name: bfbt\n",
            f"Version: {version}\n",
            "License-Expression: MIT\n",
            "Requires-Python: <3.13,>=3.10\n",
        )
        if any(value not in metadata for value in required_metadata):
            raise ReleaseContractError("wheel release metadata is incorrect")
    expected_root = f"bfbt-{version}"
    with tarfile.open(source, "r:gz") as archive:
        members = archive.getmembers()
        if any(member.issym() or member.islnk() for member in members):
            raise ReleaseContractError("source distribution contains a link")
        names = [member.name for member in members]
        if not names or not all(_safe_member(name, expected_root) for name in names):
            raise ReleaseContractError("source distribution contains an unsafe path")
        required_source_files = {
            f"{expected_root}/LICENSE",
            f"{expected_root}/README.md",
            f"{expected_root}/pyproject.toml",
            f"{expected_root}/requirements/runtime.lock",
            f"{expected_root}/requirements/lock-manifest.json",
            f"{expected_root}/scripts/release_tools.py",
        }
        missing = sorted(required_source_files.difference(names))
        if missing:
            raise ReleaseContractError(
                "source distribution is missing release inputs: " + ", ".join(missing)
            )
    hashes = {wheel.name: _sha256(wheel), source.name: _sha256(source)}
    checksum = dist / "SHA256SUMS"
    checksum.write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(hashes.items())),
        encoding="utf-8",
    )
    return hashes


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check-lock")
    write = subparsers.add_parser("write-lock-manifest")
    write.add_argument("--generator-version", required=True)
    tag = subparsers.add_parser("check-tag")
    tag.add_argument("tag")
    distribution = subparsers.add_parser("check-dist")
    distribution.add_argument("--version", required=True)
    distribution.add_argument("--dist", type=Path, default=Path("dist"))
    subparsers.add_parser("generator-version")
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        if arguments.command == "check-lock":
            result = check_lock()
        elif arguments.command == "write-lock-manifest":
            result = {"path": str(write_lock_manifest(arguments.generator_version))}
        elif arguments.command == "check-tag":
            result = {"version": check_tag(arguments.tag)}
        elif arguments.command == "generator-version":
            manifest = json.loads(
                (ROOT / "requirements/lock-manifest.json").read_text(encoding="utf-8")
            )
            version = str(manifest.get("generator_version", ""))
            if not re.fullmatch(r"\d+\.\d+\.\d+", version):
                raise ReleaseContractError("lock manifest generator version is invalid")
            print(version)
            return 0
        else:
            result = check_dist(arguments.version, arguments.dist)
    except (OSError, ValueError, json.JSONDecodeError, ReleaseContractError) as exc:
        print(f"release contract failed: {exc}")
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
