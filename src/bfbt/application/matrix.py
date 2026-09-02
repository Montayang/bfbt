"""Research publication and explicit promotion hand-off."""

from __future__ import annotations

from dataclasses import dataclass

from bfbt.config.backtest import BacktestConfig, ExecutionEngineConfig


@dataclass(frozen=True)
class MatrixPromotionRequest:
    source_matrix_run_id: str
    event_config: BacktestConfig
    required_equivalence_audit: bool = True


def prepare_event_promotion(
    source_matrix_run_id: str, config: BacktestConfig
) -> MatrixPromotionRequest:
    """Create an Event/formal config without mutating or renaming the research run."""

    if not source_matrix_run_id.startswith("fm-"):
        raise ValueError("promotion source must be an fm-* research run")
    event = config.model_copy(update={
        "engine": ExecutionEngineConfig(
            backend="event", purpose="formal", equivalence_audit=True,
            source_matrix_run_id=source_matrix_run_id,
        )
    })
    return MatrixPromotionRequest(source_matrix_run_id, event)
