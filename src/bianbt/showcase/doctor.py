"""Side-effect-free environment and showcase readiness checks."""

from __future__ import annotations

import importlib.metadata
import os
import shutil
import socket
import sys
from pathlib import Path
from typing import Any

from bianbt import __version__
from bianbt.showcase.models import ShowcaseSpec
from bianbt.showcase.service import ShowcaseError, inspect_showcase


REQUIRED_DISTRIBUTIONS = (
    "duckdb",
    "httpx",
    "polars",
    "pydantic",
    "pyarrow",
    "pytz",
    "PyYAML",
    "typer",
)


def _check(
    check_id: str,
    status: str,
    summary: str,
    remedy: str | None = None,
) -> dict[str, str | None]:
    return {
        "check_id": check_id,
        "status": status,
        "summary": summary,
        "remedy": remedy,
    }


def _existing_parent(path: Path) -> Path:
    candidate = path.resolve()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def doctor(
    *,
    project_root: Path,
    output_root: Path,
    spec: ShowcaseSpec | None = None,
    runs_root: Path | None = None,
    port: int | None = None,
) -> dict[str, Any]:
    """Return stable readiness checks without creating files or directories."""

    checks: list[dict[str, str | None]] = []
    version = sys.version_info
    checks.append(
        _check(
            "runtime.python",
            "pass" if version >= (3, 10) else "fail",
            f"Python {version.major}.{version.minor}.{version.micro}",
            None if version >= (3, 10) else "Install Python 3.10 or newer.",
        )
    )
    package_path = Path(__file__).resolve().parents[2]
    inside_project = False
    try:
        package_path.relative_to(project_root.resolve())
        inside_project = True
    except ValueError:
        pass
    checks.append(
        _check(
            "runtime.package",
            "pass" if inside_project else "fail",
            f"bianbt {__version__} is loaded from the selected project"
            if inside_project
            else "bianbt is not loaded from the selected project",
            None if inside_project else "Install this checkout with pip install -e .",
        )
    )
    missing = []
    resolved = []
    for name in REQUIRED_DISTRIBUTIONS:
        try:
            resolved.append(f"{name}=={importlib.metadata.version(name)}")
        except importlib.metadata.PackageNotFoundError:
            missing.append(name)
    checks.append(
        _check(
            "runtime.dependencies",
            "fail" if missing else "pass",
            "missing: " + ", ".join(missing)
            if missing
            else f"{len(resolved)} required distributions resolved",
            "Install the project dependencies." if missing else None,
        )
    )
    parent = _existing_parent(output_root)
    writable = parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)
    checks.append(
        _check(
            "storage.output",
            "pass" if writable else "fail",
            "showcase output parent is writable"
            if writable
            else "showcase output parent is not writable",
            None if writable else "Choose a writable data/backtest output root.",
        )
    )
    try:
        free = shutil.disk_usage(parent).free
        disk_status = "pass" if free >= 1024**3 else "warn"
        checks.append(
            _check(
                "storage.disk",
                disk_status,
                f"{free / 1024**3:.2f} GiB free",
                None if disk_status == "pass" else "Free at least 1 GiB before a presentation.",
            )
        )
    except OSError as exc:
        checks.append(_check("storage.disk", "fail", f"cannot inspect disk: {exc}"))

    if spec is not None:
        checks.append(
            _check(
                "intent.ambiguities",
                "pass" if spec.intent.executable else "fail",
                "all economic ambiguities are resolved"
                if spec.intent.executable
                else ", ".join(spec.intent.unresolved_ambiguities),
                None if spec.intent.executable else "Resolve every ambiguity before building.",
            )
        )
        if spec.catalog_path is not None:
            catalog = (project_root / spec.catalog_path).resolve()
            try:
                catalog.relative_to(project_root.resolve())
                catalog_safe = True
            except ValueError:
                catalog_safe = False
            catalog_ok = catalog_safe and catalog.is_file()
            checks.append(
                _check(
                    "data.catalog",
                    "pass" if catalog_ok else "warn",
                    "selected local catalog is available"
                    if catalog_ok
                    else "selected local catalog is unavailable",
                    None if catalog_ok else "Restore the versioned local data workspace if needed.",
                )
            )
        if runs_root is None:
            checks.append(
                _check(
                    "artifacts.verified",
                    "fail",
                    "runs_root is required for showcase verification",
                )
            )
        else:
            try:
                evidence = inspect_showcase(
                    spec,
                    runs_root=runs_root,
                    output_root=output_root,
                )
            except ShowcaseError as exc:
                checks.append(
                    _check(
                        "artifacts.verified",
                        "fail",
                        str(exc),
                        "Restore exact immutable runs; do not bypass hash verification.",
                    )
                )
            else:
                dirty = int(evidence["summary"]["dirty_run_count"])
                checks.append(
                    _check(
                        "artifacts.verified",
                        "pass",
                        f"{evidence['summary']['verified_run_count']} immutable runs verified",
                    )
                )
                checks.append(
                    _check(
                        "artifacts.provenance",
                        "warn" if dirty else "pass",
                        f"{dirty} runs record dirty source provenance"
                        if dirty
                        else "all selected runs record clean source provenance",
                        "Use visible qualification or separately authorize clean revisions."
                        if dirty
                        else None,
                    )
                )

    if port is not None:
        if not 1 <= port <= 65535:
            checks.append(_check("presentation.port", "fail", "port is outside 1..65535"))
        else:
            available = False
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
                    stream.bind(("127.0.0.1", port))
                    available = True
            except OSError:
                pass
            checks.append(
                _check(
                    "presentation.port",
                    "pass" if available else "warn",
                    f"loopback port {port} is available"
                    if available
                    else f"loopback port {port} is already in use",
                    None if available else "Choose another loopback port.",
                )
            )
    failed = sum(item["status"] == "fail" for item in checks)
    warned = sum(item["status"] == "warn" for item in checks)
    return {
        "doctor_version": "bianbt-doctor/v1",
        "ready": failed == 0,
        "summary": {"passed": len(checks) - failed - warned, "warned": warned, "failed": failed},
        "checks": checks,
    }
