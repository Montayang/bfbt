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
- Every model has exactly one response. Follow-up clarification is forbidden: ambiguity, omission
  or inconsistency is part of the submitted model's quality. The implementer applies the common
  benchmark contract first, then the narrowest deterministic interpretation, and records every
  such decision before seeing any result.
- Original response text, provider, model identity, independently selected reasoning effort and
  SHA-256 are retained under the ignored local experiment workspace before implementation. Effort
  labels such as `Max` or `Ultra` are not treated as part of the model name. Web access is allowed;
  generation time is not required metadata.
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
│   ├── frozen-strategy.json
│   └── frozen-strategy.<corrected-source-commit>.json  # only after a retained failed attempt
├── data/
├── jobs/
├── runs/
└── comparison/
```

The original response is immutable after admission. Mechanical implementation decisions must be
recorded; they cannot silently improve a submission. Dataset preparation, formal Event runs and
reports retain their own immutable identities.

## Frozen implementation

The four admitted answers are implemented as deterministic factor contracts and isolated account
state machines under `bfbt.experiments`. One chronological Event adapter shares only the market
scan; cash, positions, pending orders, risk state, costs and artifacts remain independent for each
submission. It retains each answer's distinct selection buffers, sizing, breaker, cooldown and
missing-fill rules, including defects recorded before any result access.

The prepared local snapshot contains 1-minute bars from 2025-08-01 through the terminal execution
tail and the matching observed funding stream. Scheduled decisions use completed information and
fill no earlier than the next minute open. Weekly parts and checkpoints make the run resumable;
the formal runner refuses a dirty tracked checkout and binds each immutable run ID to the source
commit, snapshot, factor version and original-response/audit hashes.

See [`A45`](../../acceptance/A45.md) for offline acceptance and the formal-run boundary. At the
common 5 bp fee plus 2 bp slippage, full-replacement turnover ceilings are economically severe:
the owner accepted that risk before authorizing the benchmark. No result should be inferred from
implementation or fixture-test completion.

## Completed formal evidence

The corrected clean-source benchmark completed all four isolated Event runs. The immutable run IDs,
leaderboard, attribution and qualifications are recorded in [`RESULTS.md`](RESULTS.md). Generated
Parquet and bilingual HTML remain ignored local evidence rather than source-repository payload.

## Known qualification

BFBT can infer point-in-time eligibility from observed bars, listing/history boundaries and
trailing liquidity. Complete historical exchange-status snapshots are not available for every
timestamp; any accepted current-snapshot limitation must remain visible in the final comparison.
