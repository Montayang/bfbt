"""Restricted columnar portfolio backend for rapid research."""

from bianbt.engine.fast_matrix.capabilities import (
    BackendDecision,
    MatrixCapabilityError,
    ReasonCode,
    plan_backend,
)
from bianbt.engine.fast_matrix.kernel import run_fast_matrix
from bianbt.engine.fast_matrix.chunked import run_fast_matrix_chunked
from bianbt.engine.fast_matrix.batch import run_fast_matrix_batch
from bianbt.engine.fast_matrix.target_schedule import TargetSchedule, build_target_schedule

__all__ = [
    "BackendDecision",
    "MatrixCapabilityError",
    "ReasonCode",
    "TargetSchedule",
    "build_target_schedule",
    "plan_backend",
    "run_fast_matrix",
    "run_fast_matrix_batch",
    "run_fast_matrix_chunked",
]
