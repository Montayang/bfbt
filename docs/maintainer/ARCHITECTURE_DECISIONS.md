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

## Agent and showcase boundary

- Natural language is translated into a versioned ResearchIntent before deterministic application
  code is invoked. Unresolved economic ambiguity fails closed.
- The bounded showcase implements this contract for a curated result-query scenario; it is not a
  general no-code Agent service and never embeds arbitrary code execution.
- Showcase pages are deterministic derived views of verified immutable artifacts. They may compare
  runs and derive opening margin from audited notional/leverage, but cannot rewrite result truth.
- Dirty source provenance, data warnings, and authorization action classes are presentation facts,
  not details the renderer may suppress.

## Public identity and languages

- The public brand is BFBT — Binance Futures Backtesting Framework; distribution, import namespace,
  module entry point, and CLI use `bfbt`. No pre-release `bianbt` compatibility package is retained.
- User-facing surfaces call the chronological formal engine the **Event Engine**. Internal module,
  configuration, schema, and compatibility identities may retain `v2`; they are implementation
  contracts and must not leak into README, guides, Showcase copy, or generated report headings.
- The historical `bianbt.*` Arrow metadata namespace is frozen as part of v1 wire-schema and run
  fingerprints. Brand migration must not invalidate immutable evidence by renaming those keys.
- English is the default repository and generated-report language. Human-facing HTML also publishes
  an independent Simplified-Chinese sibling. Machine-readable identities and evidence are not
  translated or duplicated.
- BFBT is independent from Binance and has no affiliation, endorsement, sponsorship, or financial
  relationship with Binance. This disclaimer must remain visible in the public front door.
