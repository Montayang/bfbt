"""Frozen strategy contracts for the 2025-2026 one-shot LLM benchmark.

This module contains no model calls and no parameter search.  It translates the
four admitted answers into deterministic, result-blind factor and selection
functions.  The chronological runner lives in ``llm_benchmark_event.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import floor, isfinite, log, sqrt
from statistics import median, stdev
from typing import Iterable, Mapping, Sequence


@dataclass(frozen=True)
class HourPoint:
    available_at: datetime
    close: float
    minute_count: int
    quote_volume: float


@dataclass(frozen=True)
class FundingPoint:
    settlement_time: datetime
    rate: float


@dataclass(frozen=True)
class MarketView:
    now: datetime
    eligible: tuple[str, ...]
    hourly: Mapping[str, Sequence[HourPoint]]
    funding: Mapping[str, Sequence[FundingPoint]]
    quote_volume_24h: Mapping[str, float]


@dataclass(frozen=True)
class ScoreRow:
    symbol: str
    score: float
    ordinal_rank: int
    sample_count: int
    extras: Mapping[str, float]


@dataclass(frozen=True)
class StrategyDecision:
    strategy_id: str
    signal_time: datetime
    scores: tuple[ScoreRow, ...]
    target_weights: Mapping[str, float]
    metadata: Mapping[str, object]


@dataclass(frozen=True)
class FrozenStrategy:
    strategy_id: str
    display_name: str
    factor_version: str
    decision_hours: tuple[int, ...]
    required_history_hours: int


CHATGPT = FrozenStrategy(
    strategy_id="chatgpt-6-astra-ultra",
    display_name="Buffered Relative Momentum",
    factor_version="llmcs-brm-v1",
    decision_hours=(0,),
    required_history_hours=696,
)
KIMI = FrozenStrategy(
    strategy_id="kimi-k3-max",
    display_name="Tri-Blend Carry-Momentum-Reversal",
    factor_version="llmcs-tcmr-v1",
    decision_hours=(0, 4, 8, 12, 16, 20),
    required_history_hours=96,
)
GROK = FrozenStrategy(
    strategy_id="grok-4.7-xhigh",
    display_name="Residual Momentum-Carry Buffer",
    factor_version="llmcs-rmcb-v1",
    decision_hours=(0,),
    required_history_hours=168,
)
CLAUDE = FrozenStrategy(
    strategy_id="claude-fable-5.1-max",
    display_name="Trailing-Funding Carry Reversal",
    factor_version="llmcs-tfcr-v1",
    decision_hours=tuple(range(24)),
    required_history_hours=168,
)
STRATEGIES = (CHATGPT, KIMI, GROK, CLAUDE)


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and isfinite(float(value))


def _hour_map(points: Sequence[HourPoint]) -> dict[datetime, float]:
    return {
        point.available_at: point.close
        for point in points
        if _finite(point.close) and point.close > 0
    }


def _rank(
    values: Mapping[str, float],
    *,
    tie_key: Mapping[str, tuple[object, ...]] | None = None,
) -> tuple[ScoreRow, ...]:
    keys = tie_key or {}
    ordered = sorted(
        values,
        key=lambda symbol: (-values[symbol], *keys.get(symbol, (symbol,))),
    )
    count = len(ordered)
    return tuple(
        ScoreRow(
            symbol=symbol,
            score=float(values[symbol]),
            ordinal_rank=index,
            sample_count=count,
            extras={},
        )
        for index, symbol in enumerate(ordered, 1)
    )


def _robust_z(values: Mapping[str, float]) -> dict[str, float]:
    if not values:
        return {}
    center = median(values.values())
    mad = median(abs(value - center) for value in values.values())
    if mad == 0:
        return {symbol: 0.0 for symbol in values}
    scale = 1.4826 * mad
    return {
        symbol: max(-3.0, min(3.0, (value - center) / scale))
        for symbol, value in values.items()
    }


def _select_buffered(
    rows: Sequence[ScoreRow],
    positions: Mapping[str, float],
    *,
    long_count: int,
    short_count: int,
    long_keep_rank: int,
    short_keep_width: int,
    long_entry_rank: int,
    short_entry_width: int,
) -> tuple[list[str], list[str]]:
    count = len(rows)
    rank = {row.symbol: row.ordinal_rank for row in rows}
    ordered = [row.symbol for row in rows]
    held_long = [
        symbol for symbol, quantity in positions.items()
        if quantity > 0 and rank.get(symbol, count + 1) <= long_keep_rank
    ]
    held_long.sort(key=rank.__getitem__)
    longs = held_long[:long_count]
    for symbol in ordered[:long_entry_rank]:
        if len(longs) >= long_count:
            break
        if symbol not in longs:
            longs.append(symbol)
    short_floor = max(1, count - short_keep_width + 1)
    short_entry_floor = max(1, count - short_entry_width + 1)
    held_short = [
        symbol for symbol, quantity in positions.items()
        if quantity < 0 and rank.get(symbol, 0) >= short_floor
    ]
    held_short.sort(key=rank.__getitem__, reverse=True)
    shorts = held_short[:short_count]
    for symbol in reversed(ordered[short_entry_floor - 1 :]):
        if len(shorts) >= short_count:
            break
        if symbol not in shorts and symbol not in longs:
            shorts.append(symbol)
    return longs, shorts


def chatgpt_decision(
    view: MarketView, positions: Mapping[str, float]
) -> StrategyDecision:
    scores: dict[str, float] = {}
    for symbol in view.eligible:
        closes = _hour_map(view.hourly.get(symbol, ()))
        p_old = closes.get(view.now - timedelta(hours=696))
        p_skip = closes.get(view.now - timedelta(hours=24))
        vol_closes = [
            closes.get(view.now - timedelta(hours=offset))
            for offset in range(168, -1, -1)
        ]
        if p_old is None or p_skip is None or any(value is None for value in vol_closes):
            continue
        returns = [
            log(float(vol_closes[index]) / float(vol_closes[index - 1]))
            for index in range(1, len(vol_closes))
        ]
        sigma = stdev(returns)
        value = log(p_skip / p_old) / (sqrt(672.0) * max(sigma, 0.002))
        if isfinite(value):
            scores[symbol] = value
    rows = _rank(scores)
    targets: dict[str, float] = {}
    if len(rows) >= 40:
        longs, shorts = _select_buffered(
            rows, positions, long_count=15, short_count=15,
            long_keep_rank=20, short_keep_width=20,
            long_entry_rank=20, short_entry_width=20,
        )
        targets.update({symbol: 1.0 / 30.0 for symbol in longs})
        targets.update({symbol: -1.0 / 30.0 for symbol in shorts})
    return StrategyDecision(
        strategy_id=CHATGPT.strategy_id,
        signal_time=view.now,
        scores=rows,
        target_weights=targets,
        metadata={"eligible_score_count": len(rows)},
    )


def kimi_decision(
    view: MarketView,
    positions: Mapping[str, float],
    *,
    risk_scale: float,
) -> StrategyDecision:
    carry: dict[str, float] = {}
    momentum: dict[str, float] = {}
    reversal: dict[str, float] = {}
    for symbol in view.eligible:
        funding = [
            item.rate for item in view.funding.get(symbol, ())
            if item.settlement_time <= view.now and _finite(item.rate)
        ]
        if len(funding) >= 3:
            carry[symbol] = -sum(funding[-3:]) / 3.0
        closes = _hour_map(view.hourly.get(symbol, ()))
        points = [
            (view.now - timedelta(hours=offset), closes.get(view.now - timedelta(hours=offset)))
            for offset in range(96, 23, -1)
        ]
        present = [(timestamp, value) for timestamp, value in points if value is not None]
        if (
            closes.get(view.now - timedelta(hours=96)) is not None
            and closes.get(view.now - timedelta(hours=24)) is not None
            and len(present) >= 66
        ):
            scaled = []
            for (left_time, left), (right_time, right) in zip(present, present[1:]):
                gap = (right_time - left_time).total_seconds() / 3600.0
                scaled.append(log(float(right) / float(left)) / sqrt(gap))
            if len(scaled) >= 60:
                sigma = stdev(scaled)
                if sigma > 0 and isfinite(sigma):
                    momentum[symbol] = log(
                        closes[view.now - timedelta(hours=24)]
                        / closes[view.now - timedelta(hours=96)]
                    ) / sigma
        current = closes.get(view.now)
        prior = closes.get(view.now - timedelta(hours=24))
        if current is not None and prior is not None:
            reversal[symbol] = -log(current / prior)
    z_carry, z_mom, z_rev = _robust_z(carry), _robust_z(momentum), _robust_z(reversal)
    valid = set(z_carry) & set(z_mom) & set(z_rev)
    combined = {
        symbol: 0.40 * z_carry[symbol] + 0.35 * z_mom[symbol] + 0.25 * z_rev[symbol]
        for symbol in valid
    }
    tie = {
        symbol: (-view.quote_volume_24h.get(symbol, 0.0), symbol)
        for symbol in combined
    }
    rows = _rank(combined, tie_key=tie)
    count = len(rows)
    targets: dict[str, float] = {}
    if count >= 10:
        side_count = 10 if count >= 20 else floor(count / 2)
        longs, shorts = _select_buffered(
            rows, positions, long_count=side_count, short_count=side_count,
            long_keep_rank=min(15, count), short_keep_width=min(15, count),
            long_entry_rank=side_count, short_entry_width=side_count,
        )
        if longs and shorts:
            targets.update({symbol: risk_scale * 0.5 / len(longs) for symbol in longs})
            targets.update({symbol: -risk_scale * 0.5 / len(shorts) for symbol in shorts})
    return StrategyDecision(
        strategy_id=KIMI.strategy_id,
        signal_time=view.now,
        scores=rows,
        target_weights=targets,
        metadata={"eligible_score_count": count, "risk_scale": risk_scale},
    )


def _midrank_scores(values: Mapping[str, float]) -> dict[str, float]:
    ordered = sorted(values, key=lambda symbol: (values[symbol], symbol))
    count = len(ordered)
    if count < 2:
        return {}
    result: dict[str, float] = {}
    index = 0
    while index < count:
        end = index + 1
        while end < count and values[ordered[end]] == values[ordered[index]]:
            end += 1
        midrank = ((index + 1) + end) / 2.0
        score = (midrank - 1.0) / (count - 1.0) - 0.5
        for item in ordered[index:end]:
            result[item] = score
        index = end
    return result


def grok_decision(
    view: MarketView, positions: Mapping[str, float]
) -> StrategyDecision:
    # The residual median is deliberately recomputed on the *current* E(t),
    # exactly as submitted, rather than using historical membership.
    returns_by_lag: dict[int, dict[str, float]] = {}
    maps = {symbol: _hour_map(view.hourly.get(symbol, ())) for symbol in view.eligible}
    for lag in range(168):
        at = view.now - timedelta(hours=lag)
        values: dict[str, float] = {}
        for symbol, closes in maps.items():
            current, previous = closes.get(at), closes.get(at - timedelta(hours=1))
            if current is not None and previous is not None:
                values[symbol] = log(current / previous)
        if len(values) >= 30:
            center = median(values.values())
            returns_by_lag[lag] = {symbol: value - center for symbol, value in values.items()}
    mom_raw: dict[str, float] = {}
    fund_raw: dict[str, float] = {}
    for symbol in view.eligible:
        momentum = [returns_by_lag[lag][symbol] for lag in range(24, 168) if symbol in returns_by_lag.get(lag, {})]
        vol = [returns_by_lag[lag][symbol] for lag in range(168) if symbol in returns_by_lag.get(lag, {})]
        if len(momentum) >= 115 and len(vol) >= 134:
            sigma = stdev(vol)
            if sigma >= 1e-8 and isfinite(sigma):
                mom_raw[symbol] = sum(momentum) / sigma
        rates = [
            item.rate for item in view.funding.get(symbol, ())
            if view.now - timedelta(hours=168) <= item.settlement_time < view.now
        ]
        if len(rates) >= 18 and all(_finite(value) for value in rates):
            fund_raw[symbol] = -sum(rates)
    valid = set(mom_raw) & set(fund_raw)
    mom_score = _midrank_scores({symbol: mom_raw[symbol] for symbol in valid})
    fund_score = _midrank_scores({symbol: fund_raw[symbol] for symbol in valid})
    combined = {symbol: 0.5 * mom_score[symbol] + 0.5 * fund_score[symbol] for symbol in valid}
    rows = _rank(combined)
    targets: dict[str, float] = {}
    if len(rows) >= 50:
        longs, shorts = _select_buffered(
            rows, positions, long_count=15, short_count=15,
            long_keep_rank=25, short_keep_width=25,
            long_entry_rank=15, short_entry_width=15,
        )
        # The common benchmark contract overrides the submitted 0.45 side
        # ceiling: fixed gross is 0.50 per side.
        if len(longs) >= 8 and len(shorts) >= 8:
            targets.update({symbol: 0.5 / len(longs) for symbol in longs})
            targets.update({symbol: -0.5 / len(shorts) for symbol in shorts})
    return StrategyDecision(
        strategy_id=GROK.strategy_id,
        signal_time=view.now,
        scores=rows,
        target_weights=targets,
        metadata={"eligible_score_count": len(rows), "benchmark_side_gross_override": 0.5},
    )


def _competition_ranks(values: Mapping[str, float], *, descending: bool) -> dict[str, int]:
    unique = sorted(set(values.values()), reverse=descending)
    return {
        symbol: 1 + sum(1 for candidate in values.values() if candidate > value)
        if descending
        else 1 + sum(1 for candidate in values.values() if candidate < value)
        for symbol, value in values.items()
    }


def _waterfill(symbols: Iterable[str], volatility: Mapping[str, float]) -> dict[str, float]:
    remaining = set(symbols)
    capped: set[str] = set()
    weights: dict[str, float] = {}
    while remaining:
        budget = 0.5 - 0.05 * len(capped)
        if budget <= 0:
            weights.update({symbol: 0.0 for symbol in remaining})
            break
        raw = {symbol: 1.0 / max(volatility[symbol], 0.001) for symbol in remaining}
        total = sum(raw.values())
        trial = {symbol: budget * value / total for symbol, value in raw.items()}
        over = {symbol for symbol, value in trial.items() if value > 0.05}
        if not over:
            weights.update(trial)
            break
        capped.update(over)
        remaining.difference_update(over)
        weights.update({symbol: 0.05 for symbol in over})
    return weights


def claude_decision(
    view: MarketView,
    positions: Mapping[str, float],
    cooldown_until: Mapping[str, datetime],
) -> StrategyDecision:
    score: dict[str, float] = {}
    sigma: dict[str, float] = {}
    latest_rate: dict[str, float] = {}
    ret24: dict[str, float] = {}
    for symbol in view.eligible:
        rates = [
            item for item in view.funding.get(symbol, ())
            if view.now - timedelta(hours=72) <= item.settlement_time < view.now
        ]
        closes = _hour_map(view.hourly.get(symbol, ()))
        returns = []
        for lag in range(168):
            at = view.now - timedelta(hours=lag)
            current, previous = closes.get(at), closes.get(at - timedelta(hours=1))
            if current is not None and previous is not None:
                returns.append(log(current / previous))
        if len(rates) >= 3 and len(returns) >= 120:
            rms = sqrt(sum(value * value for value in returns) / len(returns))
            if isfinite(rms):
                score[symbol] = -sum(item.rate for item in rates)
                sigma[symbol] = rms
                latest_rate[symbol] = rates[-1].rate
        current, previous = closes.get(view.now), closes.get(view.now - timedelta(hours=24))
        if current is not None and previous is not None:
            ret24[symbol] = log(current / previous)
    valid_ret = {symbol: ret24[symbol] for symbol in score if symbol in ret24}
    ex24: dict[str, float] = {}
    if len(valid_ret) >= 10:
        center = median(valid_ret.values())
        ex24 = {symbol: value - center for symbol, value in valid_ret.items()}
    bullish = _competition_ranks(score, descending=True)
    bearish = _competition_ranks(score, descending=False)
    count = len(score)
    k = min(20, floor(count / 3))
    band = min(35, floor(count / 2))
    long_order = sorted(score, key=lambda s: (-score[s], latest_rate[s], -view.quote_volume_24h.get(s, 0.0), s))
    short_order = sorted(score, key=lambda s: (score[s], -latest_rate[s], -view.quote_volume_24h.get(s, 0.0), s))
    targets: dict[str, float] = {}
    longs: list[str] = []
    shorts: list[str] = []
    if k >= 5:
        retained_longs = [s for s, q in positions.items() if q > 0 and bullish.get(s, count + 1) <= band and ex24.get(s, 0.0) > -0.22]
        retained_shorts = [s for s, q in positions.items() if q < 0 and bearish.get(s, count + 1) <= band and ex24.get(s, 0.0) < 0.22]
        longs = sorted(retained_longs, key=long_order.index)[:k]
        shorts = sorted(retained_shorts, key=short_order.index)[:k]
        for symbol in long_order:
            if len(longs) >= k or bullish[symbol] > k:
                break
            if symbol in longs or symbol in shorts or view.now < cooldown_until.get(symbol, datetime.min.replace(tzinfo=view.now.tzinfo)) or ex24.get(symbol, 0.0) <= -0.22:
                continue
            longs.append(symbol)
        for symbol in short_order:
            if len(shorts) >= k or bearish[symbol] > k:
                break
            if symbol in shorts or symbol in longs or view.now < cooldown_until.get(symbol, datetime.min.replace(tzinfo=view.now.tzinfo)) or ex24.get(symbol, 0.0) >= 0.22:
                continue
            shorts.append(symbol)
        targets.update(_waterfill(longs, sigma))
        targets.update({symbol: -value for symbol, value in _waterfill(shorts, sigma).items()})
    rows = tuple(
        ScoreRow(symbol=symbol, score=score[symbol], ordinal_rank=index, sample_count=count,
                 extras={"sigma": sigma[symbol], "ex24": ex24.get(symbol, float("nan"))})
        for index, symbol in enumerate(long_order, 1)
    )
    return StrategyDecision(
        strategy_id=CLAUDE.strategy_id,
        signal_time=view.now,
        scores=rows,
        target_weights=targets,
        metadata={"eligible_score_count": count, "k": k, "band": band, "longs": longs, "shorts": shorts},
    )


def decide(
    strategy: FrozenStrategy,
    view: MarketView,
    positions: Mapping[str, float],
    *,
    risk_scale: float = 1.0,
    cooldown_until: Mapping[str, datetime] | None = None,
) -> StrategyDecision:
    if strategy is CHATGPT:
        return chatgpt_decision(view, positions)
    if strategy is KIMI:
        return kimi_decision(view, positions, risk_scale=risk_scale)
    if strategy is GROK:
        return grok_decision(view, positions)
    if strategy is CLAUDE:
        return claude_decision(view, positions, cooldown_until or {})
    raise ValueError(f"unknown frozen strategy: {strategy.strategy_id}")
