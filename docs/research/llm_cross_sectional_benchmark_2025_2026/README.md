# LLM cross-sectional strategy benchmark, 2025-09 to 2026-08

This directory freezes the protocol for an owner-directed comparison of one-shot strategy designs
from leading language models. It is a use of BFBT, not evidence that a model or strategy is
profitable.

## Frozen protocol

- Benchmark ID: `llm-cs-202509-202608-v1`.
- Core evaluation interval: `[2025-09-01T00:00:00Z, 2026-09-01T00:00:00Z)`.
- Warm-up data starts at `2025-08-01T00:00:00Z`; submitted features may use at most 30 calendar
  days of causal history.
- Every model receives byte-identical [`prompt.md`](prompt.md) and returns exactly one strategy.
- A model may clarify an economically material ambiguity only before seeing any result. It may not
  revise parameters or logic after a backtest.
- Original response text, model/vendor/version, generation timestamp and SHA-256 are retained under
  the ignored local experiment workspace before implementation.
- Implementation first uses deterministic synthetic fixtures. All accepted strategies then run on
  one exact DatasetSnapshot and the Event Engine with identical costs and execution semantics.
- The primary ranking metric is after-cost Sharpe ratio. Positive-return strategies rank above
  non-positive-return strategies; ties use Calmar ratio, lower maximum drawdown, then lower
  turnover.
- Reports also compare total and annualized return, volatility, drawdown, turnover, fees,
  slippage, funding, trades, exposure compliance and monthly consistency.

## Evidence layout

Generated and potentially large evidence is untracked under:

```text
data/backtest/experiments/llm-cs-202509-202608-v1/
├── submissions/<model-id>/
│   ├── original.md
│   ├── identity.json
│   ├── implementation-audit.json
│   └── frozen-strategy.json
├── data/
├── jobs/
├── runs/
└── comparison/
```

The original response is immutable after admission. Mechanical implementation decisions must be
recorded; they cannot silently improve a submission. Dataset preparation, formal Event runs and
reports retain their own immutable identities.

## Known qualification

BFBT can infer point-in-time eligibility from observed bars, listing/history boundaries and
trailing liquidity. Complete historical exchange-status snapshots are not available for every
timestamp; any accepted current-snapshot limitation must remain visible in the final comparison.

