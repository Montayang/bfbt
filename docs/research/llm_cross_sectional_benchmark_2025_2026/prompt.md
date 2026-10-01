# Anonymous Cross-Sectional Crypto Strategy Benchmark

You are participating in an anonymous quantitative strategy-design benchmark. Design exactly ONE
complete, systematic, mechanically executable cross-sectional strategy for USDⓈ-margined crypto
perpetual futures. Multiple leading models receive this identical prompt. Your frozen answer will
be implemented as written and evaluated by the same event-driven engine on unseen data. You will
not receive results or revise the strategy afterward.

## 1. Market and hidden period

- Instrument: USDⓈ-margined perpetual futures; long and short are supported.
- Evaluation: `[2025-09-01T00:00:00Z, 2026-09-01T00:00:00Z)`.
- Causal warm-up may begin at `2025-08-01T00:00:00Z`.
- No feature may require more than 30 calendar days of pre-evaluation history.
- No spot, options, dated futures, other exchanges or hidden-period fitting.

## 2. Available data

Completed one-minute trade bars provide UTC open/availability times, OHLC, base volume, quote
volume, trade count, taker-buy base volume and taker-buy quote volume. You may deterministically
aggregate them into UTC-aligned left-closed/right-open higher intervals; an aggregate is available
only after its final source bar completes.

You may also use actual historical funding settlement time/rate and the benchmark's point-in-time
eligibility. At a valid time you may compare causally available data across eligible assets.

Unavailable: open interest, liquidations, order books, bid/ask, individual or aggregate trades,
account data, sentiment/news/social/search, on-chain data, market cap, curated sectors, other
exchanges, future/revised information and parameters learned from the hidden period.

## 3. Fixed universe

Universe membership is recalculated hourly. A symbol is eligible only if it is a USDⓈ-margined
perpetual, has at least 30 calendar days of observed bar history, has no more than 1% missing
one-minute bars over the completed trailing 24 hours, ranks in the top 100 otherwise-eligible
contracts by trailing 24-hour quote volume, and has finite causal inputs for your strategy. Define
an exact fallback if fewer assets than your strategy requires are eligible. Do not create an
external-metadata universe.

## 4. Fixed economics and execution

- Initial equity: 100,000 USDT.
- Long gross 0.5; short gross 0.5; target net 0; total gross 1.0.
- Maximum absolute symbol weight 0.10; maximum leverage 1x.
- Taker fee: 5 bps of absolute traded notional.
- Slippage: 2 bps of absolute traded notional.
- Actual funding cash flows apply at recorded settlement times; required missing funding never
  silently becomes zero.
- Signals use completed bars. Decisions occur after completion. Ordinary and risk orders fill at
  the next available one-minute open; same-close and maker fills are forbidden.
- No partial fills, order-book queue, nonlinear impact, liquidation tiers or ADL are simulated.
- Factor/target updates and scheduled rebalances may occur no faster than hourly. Risk checks may
  inspect completed one-minute bars. Unknown intrabar ordering uses the adverse/worst case.
- Remaining positions close under the first executable terminal price with costs included.

## 5. Strategy scope

Submit ONE cross-sectional factor strategy producing one scalar `score(asset, time)`. The score may
combine causal components, but must be one coherent exact formula. You may define deterministic
preprocessing, selection, weighting, buffers, turnover controls, exits and regime exposure rules.
Do not submit alternatives, searches, discretionary language, unspecified ML or any instruction to
choose the best backtest. The implementer must make no strategy-design decision.

## 6. Causality

For each rule distinguish observation, availability, signal, decision and execution timestamps.
Prevent leakage through rolling windows, normalization, rankings, missing values, aggregation,
funding, universe membership, volatility, exits and terminal handling. Cross-sectional statistics
use only assets eligible at that timestamp. Centered windows, negative lags, full-sample
normalization and future membership are forbidden.

## 7. Required answer structure

### A. Strategy Name

One short name.

### B. Core Hypothesis

At most 300 words explaining why the return source may survive costs and funding.

### C. Factor Formula

Define the scalar score completely: fields, formulas, lookbacks, minimum observations, update
frequency, invalid-value treatment, bullish direction, clipping/winsorization/normalization and all
component weights.

### D. Cross-Sectional Preprocessing

Give the exact ordered validation, outlier, normalization, ranking, tie and missing-score rules.
State explicitly when a step is absent.

### E. Asset Selection

Give long/short counts, rank boundaries, deterministic ties, insufficient-universe behavior and
any holding buffer.

### F. Position Sizing

Map selections exactly to weights while satisfying fixed 0.5 long, 0.5 short, zero net, 1.0 gross,
0.10 per-symbol and 1x leverage limits. Define caps, floors, rescaling and missing data.

### G. Entry Rules

Define exactly when positions open or increase.

### H. Exit and Reversal Rules

Define reduction, closing, reversal, retention and whether exits occur between rebalances.

### I. Risk Management

Define every enabled stop, take profit, trailing rule, volatility target, drawdown rule, regime
filter, cooldown and turnover limit, including state transition, threshold, clock, trigger price,
fill, reset and re-entry. Explicitly state absent controls.

### J. Rebalancing and Execution Timeline

Give universe, factor, rebalance and risk frequencies; signal/decision/execution timestamps;
missing-next-bar and terminal behavior; and one concrete timestamp example.

### K. Parameters

Provide a table of EVERY numerical and categorical strategy parameter. No hidden parameter.

### L. State Variables

List all state retained between events, or state explicitly that the strategy is stateless.

### M. End-to-End Pseudocode

Chronological pseudocode must cover universe, features, cross-sectional transforms, targets,
orders, next-bar fills, costs, funding, risks, missing bars and terminal liquidation.

### N. Invariants and Implementation Tests

Give at least five deterministic invariants/examples covering causality, gross/net exposure,
symbol cap, ranks/ties and missing data.

### O. Failure Modes

Give the three most likely out-of-sample failures.

### P. Robustness Expectations

Identify structurally important parameters, parameters that should tolerate moderate perturbation,
and evidence of fragility. Do not give alternative parameter sets.

### Q. Final Completeness Declaration

End exactly with: “I submit [STRATEGY NAME] as one complete frozen strategy. It contains no
discretionary decisions, hidden parameters, alternative variants, or dependence on the hidden
evaluation data.”

## 8. Benchmark rules and ranking

The answer is immutable. A model may clarify an economically material ambiguity only before any
result is revealed. Mechanical interpretations are recorded; implementation bugs may only be fixed
to match the frozen answer. Every strategy uses the same data, costs, Event Engine and reports.

Reported metrics include after-cost total/annualized return, volatility, Sharpe, maximum drawdown,
Calmar, turnover, fee/slippage drag, funding, trades, exposure compliance and monthly consistency.
Primary rank is after-cost Sharpe. Positive-total-return strategies rank above non-positive ones;
ties use higher Calmar, lower drawdown, then lower turnover.

Submit exactly one final strategy.
