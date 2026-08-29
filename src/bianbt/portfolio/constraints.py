"""Static portfolio constraints and versioned target construction."""

from __future__ import annotations

import polars as pl

from bianbt.config.backtest import PortfolioConfig, PortfolioV2Config
from bianbt.data.hashing import content_sha256
from bianbt.portfolio.base import (
    PORTFOLIO_CODE_VERSION,
    PortfolioError,
    PortfolioResult,
)
from bianbt.portfolio.crossover import FactorCrossoverTracker
from bianbt.portfolio.history import RankDescentTracker, RankHistoryBuffer
from bianbt.portfolio.ranking import RANKING_SCHEMA, build_rank_snapshots
from bianbt.portfolio.selection import select_long_short
from bianbt.portfolio.weighting import weight_selected


def apply_static_constraints(
    weighted: pl.LazyFrame,
    config: PortfolioConfig,
) -> pl.LazyFrame:
    if config.max_symbol_weight is None:
        target = pl.col("unconstrained_weight")
        constrained = pl.lit(False)
    else:
        limit = config.max_symbol_weight
        target = pl.col("unconstrained_weight").clip(-limit, limit)
        constrained = pl.col("unconstrained_weight").abs() > limit
    return weighted.with_columns(
        target.alias("target_weight"),
        pl.when(constrained)
        .then(pl.lit("MAX_SYMBOL_WEIGHT"))
        .otherwise(pl.lit(""))
        .alias("constraint_flags"),
    )


def _v2_target_weight_adapter(config: PortfolioV2Config) -> PortfolioConfig:
    sizing = config.sizing
    if sizing.mode != "target_weight":
        raise PortfolioError(
            "incremental sizing is handled by IncrementalPositionEngine"
        )
    assert sizing.weighting is not None
    assert sizing.target_gross_exposure is not None
    assert sizing.target_net_exposure is not None
    return PortfolioConfig(
        construction="long_short_count",
        long_count=1,
        short_count=1,
        weighting=sizing.weighting,
        gross_exposure=sizing.target_gross_exposure,
        net_exposure=sizing.target_net_exposure,
        max_symbol_weight=config.constraints.max_symbol_weight,
        max_turnover=config.constraints.max_turnover,
    )


def finalize_v2_selections(
    selected: pl.LazyFrame,
    config: PortfolioV2Config,
    *,
    factor_version: str,
    universe_version: str,
) -> tuple[pl.LazyFrame, str]:
    """Apply only sizing-facing static fields to cached strategy selections."""

    if config.sizing.mode == "target_weight":
        weighting_config = _v2_target_weight_adapter(config)
        constrained = apply_static_constraints(
            weight_selected(selected, weighting_config), weighting_config
        )
    else:
        constrained = selected.with_columns(
            pl.lit(0.0).alias("unconstrained_weight"),
            pl.lit(0.0).alias("target_weight"),
            pl.lit("INCREMENTAL_SIZING").alias("constraint_flags"),
        )
    identity = {
        "code_version": PORTFOLIO_CODE_VERSION,
        "config": config.model_dump(mode="json"),
        "factor_version": factor_version,
        "universe_version": universe_version,
    }
    version = f"a14-{content_sha256(identity)[:24]}"
    output = (
        constrained.with_columns(pl.lit(version).alias("portfolio_version"))
        .select(
            "signal_time",
            "symbol",
            "score",
            "side",
            "unconstrained_weight",
            "target_weight",
            "constraint_flags",
            "factor_version",
            "universe_version",
            "portfolio_version",
        )
        .sort(["signal_time", "symbol"])
    )
    return output, version


def construct_portfolio(
    scores: pl.LazyFrame,
    config: PortfolioConfig | PortfolioV2Config,
    *,
    factor_name: str | None = None,
    rank_scores: pl.LazyFrame | None = None,
    rank_state: RankHistoryBuffer | RankDescentTracker | FactorCrossoverTracker | None = None,
    max_rank_lag: int = 24,
    max_rank_state_rows: int = 20_000,
    factor_version: str,
    universe_version: str,
) -> PortfolioResult:
    """Construct lazy static targets; execution applies stateful turnover."""

    for name, value in (
        ("factor_version", factor_version),
        ("universe_version", universe_version),
    ):
        if not value or value.lower() == "latest":
            raise PortfolioError(f"{name} must be explicit")
    filtered = scores.filter(
        (pl.col("factor_version") == factor_version)
        & (pl.col("universe_version") == universe_version)
    )
    is_v2 = isinstance(config, PortfolioV2Config)
    rank_clock = config.selection.clock if is_v2 else "rebalance"
    rank_input = (
        rank_scores.filter(
            (pl.col("factor_version") == factor_version)
            & (pl.col("universe_version") == universe_version)
        )
        if is_v2 and rank_scores is not None
        else filtered
    )
    full_rankings = (
        pl.DataFrame(schema=RANKING_SCHEMA).lazy()
        if is_v2 and config.selection.mode == "factor_crossover"
        else build_rank_snapshots(
            rank_input,
            factor_name=factor_name or factor_version,
            factor_version=factor_version,
            universe_version=universe_version,
            rank_clock=rank_clock,
        )
    )
    diagnostics: pl.LazyFrame | None = None
    active_state = rank_state
    if isinstance(config, PortfolioConfig):
        selected = select_long_short(filtered, config)
        weighting_config = config
    else:
        if factor_name is None:
            raise PortfolioError("factor_name is required for V2 portfolio construction")
        if config.selection.mode == "factor_crossover":
            assert config.selection.crossover is not None
            restored = active_state.export_state() if active_state else None
            active_state = FactorCrossoverTracker(
                config=config.selection.crossover,
                max_state_rows=max_rank_state_rows,
                restored_state=restored,
            )
            selected = active_state.select(
                filtered,
                decision_times=filtered.select("timestamp"),
                selection=config.selection,
            )
            diagnostics = None
        elif config.selection.mode == "rank_descent":
            assert config.selection.descent is not None
            restored = active_state.export_state() if active_state else None
            active_state = RankDescentTracker(
                config=config.selection.descent,
                max_state_rows=max_rank_state_rows,
                restored_state=restored,
            )
        else:
            restored = active_state.export_state() if active_state else None
            active_state = RankHistoryBuffer(
                lag=config.selection.lag,
                max_rank_lag=max_rank_lag,
                max_state_rows=max_rank_state_rows,
                restored_state=restored,
            )
        if config.selection.mode != "factor_crossover":
            selected, diagnostics = active_state.select(
                full_rankings,
                decision_times=filtered.select("timestamp"),
                selection=config.selection,
            )
        if config.sizing.mode == "target_weight":
            weighting_config = _v2_target_weight_adapter(config)
            if (
                weighting_config.weighting == "inverse_volatility"
                and "volatility" in filtered.collect_schema().names()
            ):
                selected = selected.join(
                    filtered.select("timestamp", "symbol", "volatility"),
                    on=["timestamp", "symbol"],
                    how="left",
                )
        else:
            weighting_config = None
    if isinstance(config, PortfolioV2Config):
        output, version = finalize_v2_selections(
            selected,
            config,
            factor_version=factor_version,
            universe_version=universe_version,
        )
    else:
        assert weighting_config is not None
        constrained = apply_static_constraints(
            weight_selected(selected, weighting_config), weighting_config
        )
        identity = {
            "code_version": PORTFOLIO_CODE_VERSION,
            "config": config.model_dump(mode="json"),
            "factor_version": factor_version,
            "universe_version": universe_version,
        }
        version = f"a14-{content_sha256(identity)[:24]}"
        output = (
            constrained.with_columns(pl.lit(version).alias("portfolio_version"))
            .select(
                "signal_time", "symbol", "score", "side",
                "unconstrained_weight", "target_weight", "constraint_flags",
                "factor_version", "universe_version", "portfolio_version",
            )
            .sort(["signal_time", "symbol"])
        )
    return PortfolioResult(
        frame=output,
        portfolio_version=version,
        factor_version=factor_version,
        universe_version=universe_version,
        rankings=(
            full_rankings.filter(
                pl.col("ordinal_rank") <= config.selection.audit_top_n
            )
            if is_v2 and config.selection.audit_top_n is not None
            else full_rankings
        ),
        full_rankings=full_rankings,
        selections=selected.sort(["signal_time", "symbol"]),
        selection_diagnostics=diagnostics,
        rank_state=active_state,
    )
