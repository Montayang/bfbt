"""A27 acceptance for immutable analysis/signal snapshots and dependencies."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from bfbt.application.reuse import signal_identity
from bfbt.artifacts.reuse import (
    ReuseArtifactError,
    ReusableSnapshotStore,
    reuse_manifest_sha256,
)
from bfbt.data.hashing import sha256_file

START = datetime(2026, 6, 1, tzinfo=timezone.utc)


class _Selection:
    def __init__(self, start_rank: int, audit_top_n: int) -> None:
        self.start_rank = start_rank
        self.audit_top_n = audit_top_n

    def model_dump(self, *, mode: str) -> dict[str, object]:
        assert mode == "json"
        return {
            "mode": "rank_descent",
            "descent": {"start_rank_at_least": self.start_rank, "entry_rank": 1},
            "audit_top_n": self.audit_top_n,
        }


def _config(start_rank: int = 7, audit_top_n: int = 7):
    return SimpleNamespace(
        backtest=SimpleNamespace(
            portfolio=SimpleNamespace(
                selection=_Selection(start_rank, audit_top_n)
            ),
            schedule=SimpleNamespace(rebalance_interval="1m"),
        )
    )


def _part(path: Path, rows: int = 2) -> Path:
    pl.DataFrame(
        {"timestamp": list(range(rows)), "value": [float(i) for i in range(rows)]}
    ).write_parquet(path)
    return path


def test_signal_identity_ignores_audit_crop_but_binds_selection() -> None:
    first, first_hash = signal_identity(_config(), analysis_id="analysis-" + "a" * 24)
    audit_only, audit_hash = signal_identity(
        _config(audit_top_n=20), analysis_id="analysis-" + "a" * 24
    )
    changed, changed_hash = signal_identity(
        _config(start_rank=10), analysis_id="analysis-" + "a" * 24
    )
    assert (first, first_hash) == (audit_only, audit_hash)
    assert (first, first_hash) != (changed, changed_hash)


def test_analysis_and_signal_publish_are_atomic_immutable_and_verified(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    factor = _part(source / "factor.parquet")
    universe = _part(source / "universe.parquet")
    selections = _part(source / "selections.parquet", rows=1)
    rankings = _part(source / "rankings.parquet", rows=1)
    store = ReusableSnapshotStore(tmp_path / "reuse")
    analysis_id = "analysis-" + "1" * 24
    analysis = store.publish_analysis(
        analysis_id=analysis_id,
        dataset_manifest_sha256="2" * 64,
        dependency_sha256="3" * 64,
        start=START,
        end=START + timedelta(days=1),
        factor_name="intrabar_ema_ratio",
        factor_version="factor-a27",
        universe_version="universe-a27",
        tables={"factor_values": (factor,), "universe": (universe,)},
    )
    signal_id = "signal-" + "4" * 24
    signal = store.publish_signal(
        signal_id=signal_id,
        analysis_id=analysis_id,
        analysis_manifest_sha256=reuse_manifest_sha256(analysis),
        dependency_sha256="5" * 64,
        start=START,
        end=START + timedelta(days=1),
        factor_version="factor-a27",
        universe_version="universe-a27",
        tables={"selections": (selections,), "rankings_full": (rankings,)},
    )
    assert store.load_analysis(analysis_id) == analysis
    assert store.load_signal(signal_id) == signal
    assert store.publish_analysis(
        analysis_id=analysis_id,
        dataset_manifest_sha256="2" * 64,
        dependency_sha256="3" * 64,
        start=START,
        end=START + timedelta(days=1),
        factor_name="intrabar_ema_ratio",
        factor_version="factor-a27",
        universe_version="universe-a27",
        tables={"factor_values": (factor,), "universe": (universe,)},
    ) == analysis

    cached_part = store.directory("signal", signal_id) / signal.tables["selections"][0].path
    original_hash = sha256_file(cached_part)
    cached_part.write_bytes(cached_part.read_bytes() + b"tamper")
    assert sha256_file(cached_part) != original_hash
    with pytest.raises(ReuseArtifactError, match="byte size mismatch|hash mismatch"):
        store.load_signal(signal_id)


def test_wrong_signal_parent_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    selections = _part(source / "selections.parquet", rows=1)
    rankings = _part(source / "rankings.parquet", rows=1)
    store = ReusableSnapshotStore(tmp_path / "reuse")
    signal_id = "signal-" + "6" * 24
    store.publish_signal(
        signal_id=signal_id,
        analysis_id="analysis-" + "7" * 24,
        analysis_manifest_sha256="8" * 64,
        dependency_sha256="9" * 64,
        start=START,
        end=START + timedelta(days=1),
        factor_version="factor-a27",
        universe_version="universe-a27",
        tables={"selections": (selections,), "rankings_full": (rankings,)},
    )
    with pytest.raises(ReuseArtifactError, match="wrong parent"):
        store.publish_signal(
            signal_id=signal_id,
            analysis_id="analysis-" + "7" * 24,
            analysis_manifest_sha256="a" * 64,
            dependency_sha256="9" * 64,
            start=START,
            end=START + timedelta(days=1),
            factor_version="factor-a27",
            universe_version="universe-a27",
            tables={"selections": (selections,), "rankings_full": (rankings,)},
        )
