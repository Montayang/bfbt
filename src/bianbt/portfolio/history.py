"""Bounded cross-snapshot Rank state and no-lookahead selection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterator

import polars as pl

from bianbt.config.backtest import (
    RankDescentConfig,
    RankSelectionConfig,
    RankSideConfig,
)
from bianbt.data.v2_contracts import V2ReasonCode
from bianbt.portfolio.base import PortfolioError
from bianbt.portfolio.ranking import UTC_MS

RANK_STATE_CODE_VERSION = "a14-rank-state-v1"
RANK_DESCENT_CODE_VERSION = "a22-rank-descent-v1"

_STATE_SCHEMA = {
    "timestamp": UTC_MS,
    "rank_clock": pl.String,
    "symbol": pl.String,
    "factor_name": pl.String,
    "raw_score": pl.Float64,
    "ordinal_rank": pl.Int32,
    "percentile_rank": pl.Float64,
    "sample_count": pl.Int32,
    "factor_version": pl.String,
    "universe_version": pl.String,
    "run_id": pl.String,
}
_SELECTED_SCHEMA = {
    "timestamp": UTC_MS,
    "signal_time": UTC_MS,
    "rank_source_time": UTC_MS,
    "symbol": pl.String,
    "score": pl.Float64,
    "side": pl.String,
    "factor_version": pl.String,
    "universe_version": pl.String,
    "ordinal_rank": pl.Int32,
    "sample_count": pl.Int32,
}
_DIAGNOSTIC_SCHEMA = {
    "timestamp": UTC_MS,
    "decision_time": UTC_MS,
    "rank_source_time": UTC_MS,
    "rank_lag": pl.Int32,
    "rank_clock": pl.String,
    "symbol": pl.String,
    "side": pl.String,
    "requested_rank": pl.Int32,
    "sample_count": pl.Int32,
    "reason_code": pl.String,
}


class RankStateBudgetExceeded(PortfolioError):
    """The configured bounded Rank state cannot fit its hard row budget."""


_DESCENT_STATE_SCHEMA = {
    "symbol": pl.String,
    "previous_rank": pl.Int32,
    "sequence_start_rank": pl.Int32,
    "sequence_start_time": UTC_MS,
    "last_seen_time": UTC_MS,
}


def _typed_frame(
    rows: list[dict[str, object]], schema: dict[str, pl.DataType]
) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(schema=schema)
    return pl.DataFrame(rows).select(list(schema)).cast(schema)


class RankDescentTracker:
    """Track one monotone non-increasing Rank path per active symbol."""

    def __init__(
        self,
        *,
        config: RankDescentConfig,
        max_state_rows: int,
        restored_state: pl.DataFrame | None = None,
    ) -> None:
        if max_state_rows < 1:
            raise PortfolioError("max_rank_state_rows must be positive")
        self.config = config
        self.max_state_rows = max_state_rows
        state = (
            restored_state
            if restored_state is not None
            else _empty(_DESCENT_STATE_SCHEMA)
        )
        missing = set(_DESCENT_STATE_SCHEMA) - set(state.columns)
        if missing:
            raise PortfolioError(
                f"Rank descent checkpoint is missing columns: {sorted(missing)}"
            )
        if state.height > max_state_rows:
            raise RankStateBudgetExceeded(
                f"Rank descent state rows {state.height} exceed "
                f"max_rank_state_rows={max_state_rows}"
            )
        self._state = {
            str(row["symbol"]): row
            for row in state.select(list(_DESCENT_STATE_SCHEMA)).to_dicts()
        }
        if len(self._state) != state.height:
            raise PortfolioError("Rank descent checkpoint contains duplicate symbols")
        self._last_time = (
            max(row["last_seen_time"] for row in self._state.values())
            if self._state
            else None
        )

    @property
    def stats(self) -> RankStateStats:
        return RankStateStats(
            code_version=RANK_DESCENT_CODE_VERSION,
            lag=0,
            snapshot_count=1 if self._last_time is not None else 0,
            state_rows=len(self._state),
            max_rank_lag=0,
            max_state_rows=self.max_state_rows,
        )

    def export_state(self) -> pl.DataFrame:
        rows = [self._state[symbol] for symbol in sorted(self._state)]
        return _typed_frame(rows, _DESCENT_STATE_SCHEMA)

    def select(
        self,
        rankings: pl.LazyFrame,
        *,
        decision_times: pl.LazyFrame,
        selection: RankSelectionConfig,
    ) -> tuple[pl.LazyFrame, pl.LazyFrame]:
        if selection.mode != "rank_descent" or selection.descent != self.config:
            raise PortfolioError("Rank descent state does not match selection config")
        decisions = set(
            decision_times.select(pl.col("timestamp").cast(UTC_MS))
            .unique()
            .collect(engine="streaming")["timestamp"]
            .to_list()
        )
        selected_rows: list[dict[str, object]] = []
        diagnostic_rows: list[dict[str, object]] = []
        snapshots = iter_rank_snapshots(
            rankings,
            chunk_size=max(1_024, min(self.max_state_rows, 65_536)),
            state_row_limit=self.max_state_rows,
            retain_history=False,
        )
        for snapshot in snapshots:
            timestamp = snapshot["timestamp"][0]
            if self._last_time is not None and timestamp <= self._last_time:
                raise PortfolioError(
                    "Rank snapshots must advance strictly across chunk boundaries"
                )
            seen = set(snapshot["symbol"].to_list())
            self._state = {
                symbol: row
                for symbol, row in self._state.items()
                if symbol in seen
            }
            for row in snapshot.iter_rows(named=True):
                self._advance(
                    row,
                    timestamp=timestamp,
                    emit=timestamp in decisions,
                    selected_rows=selected_rows,
                    diagnostic_rows=diagnostic_rows,
                    rank_clock=selection.clock,
                )
            self._last_time = timestamp
            if len(self._state) > self.max_state_rows:
                raise RankStateBudgetExceeded(
                    f"Rank descent state rows {len(self._state)} exceed "
                    f"max_rank_state_rows={self.max_state_rows}"
                )
        selected = _typed_frame(selected_rows, _SELECTED_SCHEMA)
        diagnostics = _typed_frame(diagnostic_rows, _DIAGNOSTIC_SCHEMA)
        return (
            selected.sort(["signal_time", "symbol"]).lazy(),
            diagnostics.sort(["decision_time", "symbol"]).lazy(),
        )

    def _advance(
        self,
        row: dict[str, object],
        *,
        timestamp: datetime,
        emit: bool,
        selected_rows: list[dict[str, object]],
        diagnostic_rows: list[dict[str, object]],
        rank_clock: str,
    ) -> None:
        symbol = str(row["symbol"])
        rank = int(row["ordinal_rank"])
        state = self._state.get(symbol)
        if state is None:
            if rank >= self.config.start_rank_at_least:
                self._state[symbol] = {
                    "symbol": symbol,
                    "previous_rank": rank,
                    "sequence_start_rank": rank,
                    "sequence_start_time": timestamp,
                    "last_seen_time": timestamp,
                }
            return
        previous = int(state["previous_rank"])
        resets = rank > previous or (
            rank == previous and self.config.equal_policy == "reset"
        )
        if resets:
            self._state.pop(symbol, None)
            if rank >= self.config.start_rank_at_least:
                self._state[symbol] = {
                    "symbol": symbol,
                    "previous_rank": rank,
                    "sequence_start_rank": rank,
                    "sequence_start_time": timestamp,
                    "last_seen_time": timestamp,
                }
            return
        state["previous_rank"] = rank
        state["last_seen_time"] = timestamp
        if rank != self.config.entry_rank or not emit:
            return
        selected_rows.append(
            {
                "timestamp": timestamp,
                "signal_time": timestamp,
                "rank_source_time": timestamp,
                "symbol": symbol,
                "score": float(row["raw_score"]),
                "side": "LONG",
                "factor_version": row["factor_version"],
                "universe_version": row["universe_version"],
                "ordinal_rank": rank,
                "sample_count": int(row["sample_count"]),
            }
        )
        diagnostic_rows.append(
            {
                "timestamp": timestamp,
                "decision_time": timestamp,
                "rank_source_time": timestamp,
                "rank_lag": 0,
                "rank_clock": rank_clock,
                "symbol": symbol,
                "side": "LONG",
                "requested_rank": rank,
                "sample_count": int(row["sample_count"]),
                "reason_code": V2ReasonCode.RANK_DESCENT_TRIGGERED.value,
            }
        )
        self._state.pop(symbol, None)


@dataclass(frozen=True)
class RankStateStats:
    code_version: str
    lag: int
    snapshot_count: int
    state_rows: int
    max_rank_lag: int
    max_state_rows: int


def _requested(side: RankSideConfig) -> tuple[int, ...]:
    values = set(side.ranks)
    for start, end in side.ranges:
        values.update(range(start, end + 1))
    return tuple(sorted(values))


def _requests(
    selection: RankSelectionConfig,
) -> tuple[tuple[str, int], ...]:
    return tuple(
        (side_name, rank)
        for side_name, side in (
            ("LONG", selection.long),
            ("SHORT", selection.short),
        )
        for rank in _requested(side)
    )


def _empty(schema: dict[str, pl.DataType]) -> pl.DataFrame:
    return pl.DataFrame(schema=schema)


def _split_snapshots(frame: pl.DataFrame) -> list[pl.DataFrame]:
    if frame.is_empty():
        return []
    return [
        item.sort(["ordinal_rank", "symbol"])
        for item in frame.sort(["timestamp", "ordinal_rank", "symbol"]).partition_by(
            "timestamp", maintain_order=True
        )
    ]


def iter_rank_snapshots(
    rankings: pl.LazyFrame,
    *,
    chunk_size: int,
    state_row_limit: int,
    retain_history: bool,
) -> Iterator[pl.DataFrame]:
    """Yield complete timestamp snapshots from ordered streaming batches."""

    pending: pl.DataFrame | None = None
    for batch in rankings.collect_batches(
        chunk_size=chunk_size,
        maintain_order=True,
        engine="streaming",
    ):
        if batch.is_empty():
            continue
        merged = batch if pending is None else pl.concat([pending, batch])
        times = merged["timestamp"].unique(maintain_order=True).to_list()
        if len(times) == 1:
            pending = merged
            if retain_history and pending.height > state_row_limit:
                raise RankStateBudgetExceeded(
                    f"Rank snapshot rows {pending.height} exceed "
                    f"max_rank_state_rows={state_row_limit}"
                )
            continue
        for timestamp in times[:-1]:
            yield merged.filter(pl.col("timestamp") == timestamp).sort(
                ["ordinal_rank", "symbol"]
            )
        pending = merged.filter(pl.col("timestamp") == times[-1])
        if retain_history and pending.height > state_row_limit:
            raise RankStateBudgetExceeded(
                f"Rank snapshot rows {pending.height} exceed "
                f"max_rank_state_rows={state_row_limit}"
            )
    if pending is not None and not pending.is_empty():
        yield pending.sort(["ordinal_rank", "symbol"])


class RankHistoryBuffer:
    """Keep exactly the previous lag Rank snapshots between decisions/chunks."""

    def __init__(
        self,
        *,
        lag: int,
        max_rank_lag: int,
        max_state_rows: int,
        restored_state: pl.DataFrame | None = None,
    ) -> None:
        if lag < 0:
            raise PortfolioError("rank lag must be non-negative")
        if lag > max_rank_lag:
            raise RankStateBudgetExceeded(
                f"rank lag {lag} exceeds max_rank_lag={max_rank_lag}"
            )
        if max_state_rows < 1:
            raise PortfolioError("max_rank_state_rows must be positive")
        self.lag = lag
        self.max_rank_lag = max_rank_lag
        self.max_state_rows = max_state_rows
        self._snapshots = _split_snapshots(
            restored_state if restored_state is not None else _empty(_STATE_SCHEMA)
        )
        if len(self._snapshots) > lag:
            raise PortfolioError(
                "restored Rank state contains more snapshots than configured lag"
            )
        self._check_budget(self._snapshots)

    def _check_budget(self, snapshots: list[pl.DataFrame]) -> None:
        rows = sum(item.height for item in snapshots)
        if rows > self.max_state_rows:
            raise RankStateBudgetExceeded(
                f"Rank state rows {rows} exceed "
                f"max_rank_state_rows={self.max_state_rows}"
            )

    def _remember(self, snapshot: pl.DataFrame) -> None:
        if self.lag == 0:
            self._snapshots = []
            return
        candidate = [*self._snapshots, snapshot][-self.lag :]
        self._check_budget(candidate)
        if self._snapshots:
            previous = self._snapshots[-1]["timestamp"][0]
            current = snapshot["timestamp"][0]
            if current <= previous:
                raise PortfolioError(
                    "Rank snapshots must advance strictly across chunk boundaries"
                )
        self._snapshots = candidate

    @property
    def stats(self) -> RankStateStats:
        return RankStateStats(
            code_version=RANK_STATE_CODE_VERSION,
            lag=self.lag,
            snapshot_count=len(self._snapshots),
            state_rows=sum(item.height for item in self._snapshots),
            max_rank_lag=self.max_rank_lag,
            max_state_rows=self.max_state_rows,
        )

    def export_state(self) -> pl.DataFrame:
        if not self._snapshots:
            return _empty(_STATE_SCHEMA)
        return (
            pl.concat(self._snapshots, how="vertical")
            .select(list(_STATE_SCHEMA)).cast(_STATE_SCHEMA)
        )

    def select(
        self,
        rankings: pl.LazyFrame,
        *,
        decision_times: pl.LazyFrame,
        selection: RankSelectionConfig,
    ) -> tuple[pl.LazyFrame, pl.LazyFrame]:
        """Consume ordered clock snapshots and select source Rank at each decision."""

        if selection.lag != self.lag:
            raise PortfolioError("selection lag does not match Rank state")
        for snapshot in self._snapshots:
            clocks = snapshot["rank_clock"].unique().to_list()
            if clocks != [selection.clock]:
                raise PortfolioError(
                    "restored Rank state clock does not match selection clock"
                )
        decisions = set(
            decision_times.select(pl.col("timestamp").cast(UTC_MS))
            .unique()
            .collect(engine="streaming")["timestamp"]
            .to_list()
        )
        seen_decisions: set[datetime] = set()
        selected_rows: list[dict[str, object]] = []
        diagnostic_rows: list[dict[str, object]] = []
        requests = _requests(selection)
        chunk_size = max(1_024, min(self.max_state_rows, 65_536))
        snapshots = iter_rank_snapshots(
            rankings,
            chunk_size=chunk_size,
            state_row_limit=self.max_state_rows,
            retain_history=self.lag > 0,
        )
        for current in snapshots:
            decision_time = current["timestamp"][0]
            if decision_time in decisions:
                seen_decisions.add(decision_time)
                source = (
                    current
                    if self.lag == 0
                    else (
                        self._snapshots[-self.lag]
                        if len(self._snapshots) >= self.lag
                        else None
                    )
                )
                self._decide(
                    current=current,
                    source=source,
                    decision_time=decision_time,
                    selection=selection,
                    requests=requests,
                    selected_rows=selected_rows,
                    diagnostic_rows=diagnostic_rows,
                )
            self._remember(current)

        for decision_time in sorted(decisions - seen_decisions):
            for side, rank in requests:
                diagnostic_rows.append(
                    self._diagnostic(
                        decision_time=decision_time,
                        source_time=None,
                        selection=selection,
                        symbol=None,
                        side=side,
                        requested_rank=rank,
                        sample_count=None,
                        reason=V2ReasonCode.INSUFFICIENT_RANK_HISTORY,
                    )
                )
        selected = (
            pl.DataFrame(selected_rows, schema=_SELECTED_SCHEMA)
            if selected_rows
            else _empty(_SELECTED_SCHEMA)
        )
        diagnostics = (
            pl.DataFrame(diagnostic_rows, schema=_DIAGNOSTIC_SCHEMA)
            if diagnostic_rows
            else _empty(_DIAGNOSTIC_SCHEMA)
        )
        return (
            selected.sort(["signal_time", "symbol"]).lazy(),
            diagnostics.sort(
                ["decision_time", "side", "requested_rank", "symbol"]
            ).lazy(),
        )

    def _decide(
        self,
        *,
        current: pl.DataFrame,
        source: pl.DataFrame | None,
        decision_time: datetime,
        selection: RankSelectionConfig,
        requests: tuple[tuple[str, int], ...],
        selected_rows: list[dict[str, object]],
        diagnostic_rows: list[dict[str, object]],
    ) -> None:
        if source is None:
            for side, rank in requests:
                diagnostic_rows.append(
                    self._diagnostic(
                        decision_time=decision_time,
                        source_time=None,
                        selection=selection,
                        symbol=None,
                        side=side,
                        requested_rank=rank,
                        sample_count=None,
                        reason=V2ReasonCode.INSUFFICIENT_RANK_HISTORY,
                    )
                )
            return

        source_time = source["timestamp"][0]
        sample_count = int(source["sample_count"][0])
        eligible = set(current["symbol"].to_list())
        for side, rank in requests:
            matched = source.filter(pl.col("ordinal_rank") == rank)
            if matched.is_empty():
                diagnostic_rows.append(
                    self._diagnostic(
                        decision_time=decision_time,
                        source_time=source_time,
                        selection=selection,
                        symbol=None,
                        side=side,
                        requested_rank=rank,
                        sample_count=sample_count,
                        reason=V2ReasonCode.RANK_OUT_OF_RANGE,
                    )
                )
                continue
            row = matched.row(0, named=True)
            symbol = str(row["symbol"])
            if symbol not in eligible:
                diagnostic_rows.append(
                    self._diagnostic(
                        decision_time=decision_time,
                        source_time=source_time,
                        selection=selection,
                        symbol=symbol,
                        side=side,
                        requested_rank=rank,
                        sample_count=sample_count,
                        reason=(
                            V2ReasonCode.HISTORICAL_RANK_NOT_CURRENTLY_ELIGIBLE
                        ),
                    )
                )
                continue
            selected_rows.append(
                {
                    "timestamp": decision_time,
                    "signal_time": decision_time,
                    "rank_source_time": source_time,
                    "symbol": symbol,
                    "score": float(row["raw_score"]),
                    "side": side,
                    "factor_version": str(row["factor_version"]),
                    "universe_version": str(row["universe_version"]),
                    "ordinal_rank": rank,
                    "sample_count": sample_count,
                }
            )
            diagnostic_rows.append(
                self._diagnostic(
                    decision_time=decision_time,
                    source_time=source_time,
                    selection=selection,
                    symbol=symbol,
                    side=side,
                    requested_rank=rank,
                    sample_count=sample_count,
                    reason=(
                        V2ReasonCode.SELECTED_CURRENT_RANK
                        if self.lag == 0
                        else V2ReasonCode.SELECTED_HISTORICAL_RANK
                    ),
                )
            )

    def _diagnostic(
        self,
        *,
        decision_time: datetime,
        source_time: datetime | None,
        selection: RankSelectionConfig,
        symbol: str | None,
        side: str,
        requested_rank: int,
        sample_count: int | None,
        reason: V2ReasonCode,
    ) -> dict[str, object]:
        return {
            "timestamp": decision_time,
            "decision_time": decision_time,
            "rank_source_time": source_time,
            "rank_lag": selection.lag,
            "rank_clock": selection.clock,
            "symbol": symbol,
            "side": side,
            "requested_rank": requested_rank,
            "sample_count": sample_count,
            "reason_code": reason.value,
        }
