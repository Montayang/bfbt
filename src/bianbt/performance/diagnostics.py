"""Row and process-memory gates with stable per-chunk diagnostics."""

from __future__ import annotations

import json
import resource
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from time import perf_counter
from typing import Mapping


class RowBudgetExceeded(RuntimeError):
    """A planned chunk exceeds its configured input row budget."""


class MemoryBudgetExceeded(RuntimeError):
    """Observed process RSS exceeds an incremental or absolute budget."""


def _peak_rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if sys.platform == "darwin" else value * 1_024


@dataclass(frozen=True)
class ChunkDiagnostic:
    phase: str
    ordinal: int
    start: str
    end: str
    input_rows: Mapping[str, int]
    output_rows: Mapping[str, int]
    elapsed_seconds: float
    peak_rss_mib: float
    incremental_peak_rss_mib: float


@dataclass(frozen=True)
class PerformanceDiagnostics:
    diagnostics_version: str
    mode: str
    chunk_interval: str
    max_input_rows_per_chunk: int
    max_incremental_rss_mib: int
    baseline_peak_rss_mib: float
    observed_peak_rss_mib: float
    observed_incremental_peak_rss_mib: float
    chunks: tuple[ChunkDiagnostic, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def to_artifact_dict(self) -> dict[str, object]:
        """Return deterministic proof of the enforced plan and budget gates."""

        return {
            "diagnostics_version": self.diagnostics_version,
            "mode": self.mode,
            "chunk_interval": self.chunk_interval,
            "max_input_rows_per_chunk": self.max_input_rows_per_chunk,
            "max_incremental_rss_mib": self.max_incremental_rss_mib,
            "memory_budget_passed": (
                self.observed_incremental_peak_rss_mib
                <= self.max_incremental_rss_mib
            ),
            "chunks": [
                {
                    "phase": item.phase,
                    "ordinal": item.ordinal,
                    "start": item.start,
                    "end": item.end,
                    "input_rows": dict(item.input_rows),
                    "output_rows": dict(item.output_rows),
                }
                for item in self.chunks
            ],
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"


class PerformanceMonitor:
    """Record successful chunk boundaries and enforce declared budgets."""

    VERSION = "a10-performance-v1"

    def __init__(
        self,
        *,
        mode: str,
        chunk_interval: str,
        max_input_rows_per_chunk: int,
        max_incremental_rss_mib: int,
    ) -> None:
        self.mode = mode
        self.chunk_interval = chunk_interval
        self.max_input_rows_per_chunk = max_input_rows_per_chunk
        self.max_incremental_rss_mib = max_incremental_rss_mib
        self._baseline = _peak_rss_bytes()
        self._peak = self._baseline
        self._chunks: list[ChunkDiagnostic] = []

    def start(self) -> float:
        return perf_counter()

    def check_rows(self, rows: Mapping[str, int]) -> None:
        total = sum(rows.values())
        if total > self.max_input_rows_per_chunk:
            detail = ", ".join(f"{key}={rows[key]}" for key in sorted(rows))
            raise RowBudgetExceeded(
                f"chunk input rows {total} exceed "
                f"max_input_rows_per_chunk={self.max_input_rows_per_chunk}; {detail}"
            )

    def checkpoint(
        self,
        *,
        phase: str,
        ordinal: int,
        start: datetime,
        end: datetime,
        input_rows: Mapping[str, int],
        output_rows: Mapping[str, int],
        started_at: float,
    ) -> None:
        self.check_rows(input_rows)
        peak = _peak_rss_bytes()
        self._peak = max(self._peak, peak)
        incremental_mib = (self._peak - self._baseline) / (1_024 * 1_024)
        if incremental_mib > self.max_incremental_rss_mib:
            raise MemoryBudgetExceeded(
                f"incremental peak RSS {incremental_mib:.2f} MiB exceeds "
                f"max_incremental_rss_mib={self.max_incremental_rss_mib}"
            )
        self._chunks.append(
            ChunkDiagnostic(
                phase=phase,
                ordinal=ordinal,
                start=start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                end=end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                input_rows=dict(sorted(input_rows.items())),
                output_rows=dict(sorted(output_rows.items())),
                elapsed_seconds=round(perf_counter() - started_at, 9),
                peak_rss_mib=round(self._peak / (1_024 * 1_024), 6),
                incremental_peak_rss_mib=round(incremental_mib, 6),
            )
        )

    def result(self) -> PerformanceDiagnostics:
        return PerformanceDiagnostics(
            diagnostics_version=self.VERSION,
            mode=self.mode,
            chunk_interval=self.chunk_interval,
            max_input_rows_per_chunk=self.max_input_rows_per_chunk,
            max_incremental_rss_mib=self.max_incremental_rss_mib,
            baseline_peak_rss_mib=round(self._baseline / (1_024 * 1_024), 6),
            observed_peak_rss_mib=round(self._peak / (1_024 * 1_024), 6),
            observed_incremental_peak_rss_mib=round(
                (self._peak - self._baseline) / (1_024 * 1_024), 6
            ),
            chunks=tuple(self._chunks),
        )
