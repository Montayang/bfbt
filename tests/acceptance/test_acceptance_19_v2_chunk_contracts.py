"""User-run A19 acceptance for recoverable V2 chunk contracts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest

from bfbt.config.backtest import BacktestPerformanceV2Config
from bfbt.engine.v2 import V2ExecutionCheckpoint
from bfbt.engine.v2_checkpoint import (
    read_v2_execution_checkpoint,
    write_v2_execution_checkpoint,
)
from bfbt.performance.chunks import TimeChunk, plan_time_chunks
from bfbt.performance.diagnostics import MemoryBudgetExceeded
from bfbt.performance.memory import AbsoluteMemoryMonitor
from bfbt.performance.recovery import (
    V2ChunkRunIdentity,
    V2ChunkWorkspace,
    V2WorkspaceCorruptionError,
    V2WorkspaceError,
)
from bfbt.portfolio.instructions import PositionCheckpoint
from bfbt.risk.state_machine import RiskCheckpoint

START = datetime(2026, 6, 1, tzinfo=timezone.utc)
END = START + timedelta(hours=12)
SHA_A = "a" * 64
SHA_B = "b" * 64


def _plan() -> tuple[TimeChunk, ...]:
    return plan_time_chunks(
        start=START,
        end=END,
        chunk_interval="6h",
        overlap_seconds=24 * 60 * 60,
        earliest_input=START - timedelta(hours=24),
    )


def _identity(
    *,
    run_id: str = "a19-contract-run",
    config_sha256: str = SHA_A,
) -> V2ChunkRunIdentity:
    return V2ChunkRunIdentity.from_plan(
        run_id=run_id,
        engine_version="v2-chunk-a19",
        config_sha256=config_sha256,
        dataset_sha256=SHA_B,
        chunk_interval="6h",
        overlap_seconds=24 * 60 * 60,
        chunks=_plan(),
    )


def _commit_first(workspace: V2ChunkWorkspace) -> None:
    transaction = workspace.begin(_plan()[0])
    transaction.write_state_json(
        "engine",
        {
            "position_sequence": 7,
            "risk_sequence": 3,
            "last_decision_time": START.isoformat(),
        },
    )
    transaction.write_state_frame(
        "positions",
        pl.DataFrame(
            {
                "symbol": ["BTCUSDT"],
                "quantity": [1.0],
                "average_entry_price": [60_000.0],
            }
        ),
    )
    transaction.write_output_frame(
        "rankings",
        pl.DataFrame(
            {
                "timestamp": [START],
                "symbol": ["BTCUSDT"],
                "ordinal_rank": [1],
            }
        ),
    )
    checkpoint = transaction.commit(
        counters={"position_sequence": 7, "risk_sequence": 3}
    )
    assert checkpoint.ordinal == 0
    assert checkpoint.next_start == _plan()[0].end


def test_a19_v2_performance_config_declares_absolute_rss_and_resume_policy() -> None:
    config = BacktestPerformanceV2Config.model_validate(
        {
            "mode": "chunked",
            "chunk_interval": "6h",
            "max_process_rss_mib": 5632,
            "resume_policy": "resume",
        }
    )
    assert config.max_process_rss_mib == 5632
    assert config.resume_policy == "resume"
    with pytest.raises(ValueError, match="greater than or equal to 256"):
        BacktestPerformanceV2Config(max_process_rss_mib=128)


def test_a19_committed_chunk_resumes_and_partial_staging_is_ignored(
    tmp_path: Path,
) -> None:
    workspace = V2ChunkWorkspace(output_root=tmp_path / "runs", identity=_identity())
    _commit_first(workspace)

    partial = workspace.begin(_plan()[1])
    partial.write_state_json("engine", {"position_sequence": 999})
    assert partial.path.exists()

    restored = V2ChunkWorkspace(
        output_root=tmp_path / "runs",
        identity=_identity(),
    )
    checkpoints = restored.committed(_plan())
    assert len(checkpoints) == 1
    assert checkpoints[0].counters == {
        "position_sequence": 7,
        "risk_sequence": 3,
    }
    with pytest.raises(TypeError, match="immutable"):
        checkpoints[0].counters["position_sequence"] = 8
    assert restored.next_chunk(_plan()) == _plan()[1]
    assert partial.path.exists()


def test_a19_codec_round_trips_rolling_position_state(tmp_path: Path) -> None:
    workspace = V2ChunkWorkspace(output_root=tmp_path / "runs", identity=_identity())
    transaction = workspace.begin(_plan()[0])
    empty = pl.DataFrame()
    checkpoint = V2ExecutionCheckpoint(
        run_id="a19-contract-run",
        position=PositionCheckpoint(
            cash_balance=2_125.0,
            sequence=4,
            last_decision_time=START,
            positions=empty,
            rolling_margin=325.0,
            rolling_active_margin=None,
            rolling_round_net_pnl=0.0,
            rolling_reset_count=2,
            rolling_last_reset_reason="above_max",
        ),
        risk=RiskCheckpoint(
            evaluation_count=1,
            sequence=0,
            last_open_time=START,
            last_close_time=START + timedelta(minutes=1),
            portfolio_peak_equity=2_125.0,
            risk_positions=empty,
            cooldowns=empty,
            pending_intents=empty,
        ),
        sequence=4,
        previous_equity=2_125.0,
        peak_equity=2_125.0,
        warnings=(),
        max_position_state_rows_observed=1,
        max_risk_state_rows_observed=1,
        max_pending_risk_intents_observed=0,
        input_trade_bar_rows=10,
        input_risk_bar_rows=10,
        last_close_marks={"AUSDT": 100.0},
    )
    write_v2_execution_checkpoint(transaction, checkpoint)
    transaction.commit()
    restored = read_v2_execution_checkpoint(
        workspace.chunks_root / "chunk-000000"
    )
    assert restored.position.rolling_margin == 325.0
    assert restored.position.rolling_reset_count == 2
    assert restored.position.rolling_last_reset_reason == "above_max"

def test_a19_commit_is_atomic_and_duplicate_commit_is_rejected(
    tmp_path: Path,
) -> None:
    workspace = V2ChunkWorkspace(output_root=tmp_path / "runs", identity=_identity())
    transaction = workspace.begin(_plan()[0])
    transaction.write_state_json("engine", {"sequence": 1})
    transaction.commit()
    assert not transaction.path.exists()
    assert (workspace.chunks_root / "chunk-000000" / "checkpoint.json").is_file()
    with pytest.raises(V2WorkspaceError, match="already committed"):
        transaction.commit()
    with pytest.raises(V2WorkspaceError, match="already committed"):
        workspace.begin(_plan()[0])


def test_a19_resume_rejects_tampered_state(tmp_path: Path) -> None:
    workspace = V2ChunkWorkspace(output_root=tmp_path / "runs", identity=_identity())
    _commit_first(workspace)
    state = workspace.chunks_root / "chunk-000000" / "state" / "engine.json"
    state.write_text('{"position_sequence":8}\n', encoding="utf-8")
    with pytest.raises(V2WorkspaceCorruptionError, match="integrity"):
        workspace.committed(_plan())


def test_a19_resume_rejects_marker_identity_tampering(tmp_path: Path) -> None:
    workspace = V2ChunkWorkspace(output_root=tmp_path / "runs", identity=_identity())
    marker = workspace.path / "workspace.json"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["identity"]["config_sha256"] = "c" * 64
    marker.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(V2WorkspaceCorruptionError, match="marker"):
        V2ChunkWorkspace(output_root=tmp_path / "runs", identity=_identity())


def test_a19_resume_rejects_non_contiguous_commit(tmp_path: Path) -> None:
    workspace = V2ChunkWorkspace(output_root=tmp_path / "runs", identity=_identity())
    transaction = workspace.begin(_plan()[1])
    transaction.write_state_json("engine", {"sequence": 2})
    transaction.commit()
    with pytest.raises(V2WorkspaceCorruptionError, match="not contiguous"):
        workspace.committed(_plan())


def test_a19_run_identity_changes_with_config_and_plan(tmp_path: Path) -> None:
    first = _identity()
    second = _identity(config_sha256="d" * 64)
    shorter = plan_time_chunks(
        start=START,
        end=START + timedelta(hours=6),
        chunk_interval="6h",
        overlap_seconds=24 * 60 * 60,
        earliest_input=START - timedelta(hours=24),
    )
    third = V2ChunkRunIdentity.from_plan(
        run_id="a19-contract-run",
        engine_version="v2-chunk-a19",
        config_sha256=SHA_A,
        dataset_sha256=SHA_B,
        chunk_interval="6h",
        overlap_seconds=24 * 60 * 60,
        chunks=shorter,
    )
    assert len({first.fingerprint, second.fingerprint, third.fingerprint}) == 3
    assert first.chunk_plan_sha256 != third.chunk_plan_sha256
    workspace = V2ChunkWorkspace(output_root=tmp_path / "runs", identity=first)
    with pytest.raises(V2WorkspaceError, match="plan does not match"):
        workspace.committed(shorter)


def test_a19_absolute_memory_gate_is_deterministic_without_allocation() -> None:
    readings = iter((512, 5600, 5700))
    monitor = AbsoluteMemoryMonitor(
        max_process_rss_mib=5632,
        reader=lambda: next(readings) * 1024 * 1024,
    )
    assert monitor.checkpoint(phase="startup", ordinal=0).rss_mib == 512
    assert monitor.checkpoint(phase="chunk", ordinal=0).rss_mib == 5600
    with pytest.raises(MemoryBudgetExceeded, match="max_process_rss_mib=5632"):
        monitor.checkpoint(phase="chunk", ordinal=1)
    assert monitor.observed_peak_rss_mib == 5700
    assert len(monitor.samples) == 3
