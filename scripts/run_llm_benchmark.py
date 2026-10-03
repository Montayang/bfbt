#!/usr/bin/env python3
"""Run or resume the frozen 2025-2026 LLM Event benchmark."""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from bfbt.data.hashing import sha256_file
from bfbt.data.manifests import load_manifest_auto, manifest_sha256
from bfbt.experiments.llm_benchmark import STRATEGIES
from bfbt.experiments.llm_benchmark_event import run_benchmark


def _atomic_status(path: Path, payload: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment-root", type=Path, required=True)
    arguments = parser.parse_args()
    root = arguments.experiment_root.resolve()
    status = root / "jobs/run_benchmark.status.json"
    snapshot_path = next((root / "data/snapshot-manifests").glob("*.json"))
    snapshot = load_manifest_auto(snapshot_path)
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if dirty:
        raise RuntimeError("formal benchmark requires a clean tracked worktree")
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    submission_identities: dict[str, dict[str, str]] = {}
    for strategy in STRATEGIES:
        directory = root / "submissions" / strategy.strategy_id
        identity = json.loads((directory / "identity.json").read_text(encoding="utf-8"))
        audit_path = directory / "implementation-audit.json"
        frozen = {
            "schema_version": "llm-benchmark-frozen-strategy/v1",
            "benchmark_id": "llm-cs-202509-202608-v1",
            "submission_id": strategy.strategy_id,
            "strategy_name": strategy.display_name,
            "factor_version": strategy.factor_version,
            "original_markdown_sha256": identity["stored_markdown_sha256"],
            "implementation_audit_sha256": sha256_file(audit_path),
            "source_commit": commit,
            "dataset_snapshot_sha256": manifest_sha256(snapshot),
            "core_interval": ["2025-09-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00"],
            "economics": {
                "initial_equity": 100000.0,
                "long_gross": 0.5,
                "short_gross": 0.5,
                "fee_rate": 0.0005,
                "slippage_rate": 0.0002,
                "fill_timing": "decision_plus_one_minute_or_strategy_frozen_missing-open rule",
            },
        }
        frozen_path = directory / "frozen-strategy.json"
        if frozen_path.is_file():
            existing = json.loads(frozen_path.read_text(encoding="utf-8"))
            if existing != frozen:
                # A failed source attempt is immutable evidence, not a file to
                # rewrite.  A corrected commit receives a separate freeze.
                frozen_path = directory / f"frozen-strategy.{commit[:12]}.json"
        if frozen_path.is_file():
            existing = json.loads(frozen_path.read_text(encoding="utf-8"))
            if existing != frozen:
                raise RuntimeError(f"frozen strategy identity conflict: {strategy.strategy_id}")
        else:
            _atomic_status(frozen_path, frozen)
        submission_identities[strategy.strategy_id] = {
            "original_markdown_sha256": frozen["original_markdown_sha256"],
            "implementation_audit_sha256": frozen["implementation_audit_sha256"],
        }
    try:
        result = run_benchmark(
            experiment_root=root,
            source_commit=commit,
            snapshot_hash=manifest_sha256(snapshot),
            submission_identities=submission_identities,
        )
    except BaseException as exc:
        _atomic_status(status, {
            "status": "failed", "stage": "failed",
            "error_type": type(exc).__name__, "error_detail": str(exc),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        raise
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
