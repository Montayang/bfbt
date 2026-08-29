# Architecture decisions

This file summarizes durable choices that must survive across sessions. Detailed contracts remain
in `docs/design/` and `docs/reference/`.

## Engine roles

1. Quick Research computes factor diagnostics such as Rank IC, quantile spread, coverage, and Rank
   turnover without simulating an account.
2. Fast Matrix performs constrained, columnar portfolio research for conventional cross-sectional
   target schedules. It produces research artifacts, not formal strategy truth.
3. Event/V2 maintains chronological account, position, risk, and rolling state. It is the formal
   engine for path-dependent strategies and terminal run artifacts.
4. V1 remains readable and regression-compatible but is not the default new-strategy interface.

Unsupported Fast Matrix behavior fails closed or is explicitly promoted to Event; it must not be
silently approximated.

## Time and causality

- All intervals use UTC left-closed/right-open semantics.
- Factor availability, Rank, decisions, fills, risk triggers, funding, and valuation have explicit
  clocks. Future bars must never enter an earlier decision.
- Next-bar-open execution and intrabar conflict policy are part of the strategy identity.
- Point-in-time universe membership and missing-bar handling must be explicit and auditable.

## Low-memory execution

- Event/V2 formal full-market runs use chronological chunks with bounded warmup and serialized
  state checkpoints.
- Resume must be economically equivalent to continuous execution.
- Worker memory gates and staged publication are correctness constraints, not optional tuning.
- Reusable analysis and signal snapshots are immutable and content-addressed; cache reuse must not
  change economic output.

## Identity and artifacts

- Dataset snapshot, resolved configuration, factor version, source state, dependency state, and
  economic result jointly determine run identity.
- Successful and failed terminal artifacts are immutable. Revisions receive new IDs.
- Reports are deterministic views built from artifacts and may be rebuilt outside an immutable run.
- Display downsampling cannot delete trades, position changes, or risk events from audit navigation.

## Strategy research governance

- Quick Research rules may be versioned and automated.
- Fast Matrix has no universal automatic promotion rule; the user reviews its reports and chooses
  candidates and Event overlays.
- Event strategy acceptance is manual economically, while technical correctness remains covered by
  shared contracts and acceptance tests.

