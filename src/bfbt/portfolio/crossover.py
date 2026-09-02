"""Bounded point-in-time factor crossover selection state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import polars as pl

from bfbt.config.backtest import FactorCrossoverConfig, RankSelectionConfig
from bfbt.portfolio.base import PortfolioError

CROSSOVER_CODE_VERSION = "a30-factor-crossover-v1"
UTC_MS = pl.Datetime("ms", "UTC")

_STATE_SCHEMA = {
    "symbol": pl.String,
    "previous_score": pl.Float64,
    "last_seen_time": UTC_MS,
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


@dataclass(frozen=True)
class CrossoverStateStats:
    code_version: str
    state_rows: int
    max_state_rows: int


def _frame(
    rows: list[dict[str, object]], schema: dict[str, pl.DataType]
) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(schema=schema)
    return pl.DataFrame(rows).select(list(schema)).cast(schema)


class FactorCrossoverTracker:
    """Emit LONG/FLAT only after a genuine adjacent-score threshold crossing."""

    def __init__(
        self,
        *,
        config: FactorCrossoverConfig,
        max_state_rows: int,
        restored_state: pl.DataFrame | None = None,
    ) -> None:
        if max_state_rows < 1:
            raise PortfolioError("max_rank_state_rows must be positive")
        self.config = config
        self.max_state_rows = max_state_rows
        state = restored_state if restored_state is not None else pl.DataFrame(
            schema=_STATE_SCHEMA
        )
        missing = set(_STATE_SCHEMA) - set(state.columns)
        if missing:
            raise PortfolioError(
                f"factor crossover checkpoint is missing columns: {sorted(missing)}"
            )
        if state.height > max_state_rows:
            raise PortfolioError(
                f"factor crossover state rows {state.height} exceed "
                f"max_rank_state_rows={max_state_rows}"
            )
        self._state = {
            str(row["symbol"]): row
            for row in state.select(list(_STATE_SCHEMA)).to_dicts()
        }
        if len(self._state) != state.height:
            raise PortfolioError("factor crossover checkpoint has duplicate symbols")
        self._last_time = (
            max(row["last_seen_time"] for row in self._state.values())
            if self._state else None
        )

    @property
    def stats(self) -> CrossoverStateStats:
        return CrossoverStateStats(
            code_version=CROSSOVER_CODE_VERSION,
            state_rows=len(self._state),
            max_state_rows=self.max_state_rows,
        )

    def export_state(self) -> pl.DataFrame:
        return _frame(
            [self._state[symbol] for symbol in sorted(self._state)],
            _STATE_SCHEMA,
        )

    def select(
        self,
        scores: pl.LazyFrame,
        *,
        decision_times: pl.LazyFrame,
        selection: RankSelectionConfig,
    ) -> pl.LazyFrame:
        if (
            selection.mode != "factor_crossover"
            or selection.crossover != self.config
        ):
            raise PortfolioError("factor crossover state does not match selection config")
        decisions = set(
            decision_times.select(pl.col("timestamp").cast(UTC_MS))
            .unique()
            .collect(engine="streaming")["timestamp"]
            .to_list()
        )
        valid = (
            scores.filter(
                pl.col("is_valid")
                & pl.col("value").is_not_null()
                & pl.col("value").is_finite()
            )
            .select(
                pl.col("timestamp").cast(UTC_MS),
                pl.col("symbol").cast(pl.String),
                pl.col("value").cast(pl.Float64).alias("score"),
                pl.col("factor_version").cast(pl.String),
                pl.col("universe_version").cast(pl.String),
            )
            .sort(["timestamp", "symbol"])
            .collect(engine="streaming")
        )
        selected: list[dict[str, object]] = []
        for snapshot in valid.partition_by("timestamp", maintain_order=True):
            timestamp = snapshot.item(0, "timestamp")
            if self._last_time is not None and timestamp <= self._last_time:
                raise PortfolioError(
                    "factor crossover timestamps must advance across chunks"
                )
            seen = set(str(item) for item in snapshot["symbol"].to_list())
            self._state = {
                symbol: row for symbol, row in self._state.items() if symbol in seen
            }
            sample_count = snapshot.height
            for row in snapshot.iter_rows(named=True):
                symbol = str(row["symbol"])
                score = float(row["score"])
                previous = self._state.get(symbol)
                side: str | None = None
                if previous is not None:
                    prior = float(previous["previous_score"])
                    if prior <= self.config.entry_threshold < score:
                        side = "LONG"
                    elif prior >= self.config.exit_threshold > score:
                        side = "FLAT"
                if side is not None and timestamp in decisions:
                    selected.append(
                        {
                            "timestamp": timestamp,
                            "signal_time": timestamp,
                            "rank_source_time": None,
                            "symbol": symbol,
                            "score": score,
                            "side": side,
                            "factor_version": row["factor_version"],
                            "universe_version": row["universe_version"],
                            "ordinal_rank": None,
                            "sample_count": sample_count,
                        }
                    )
                self._state[symbol] = {
                    "symbol": symbol,
                    "previous_score": score,
                    "last_seen_time": timestamp,
                }
            self._last_time = timestamp
            if len(self._state) > self.max_state_rows:
                raise PortfolioError(
                    f"factor crossover state rows {len(self._state)} exceed "
                    f"max_rank_state_rows={self.max_state_rows}"
                )
        return _frame(selected, _SELECTED_SCHEMA).sort(
            ["signal_time", "symbol"]
        ).lazy()
