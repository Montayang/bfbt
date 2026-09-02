# Operations and formal-run handling

## Local storage

All generated state is rooted at `data/backtest/` and ignored by Git:

```text
data/backtest/
├── datasets/
├── catalogs/
├── workspaces/
├── reuse/
├── runs/
├── reports/
├── research_runs/
├── research_studies/
├── event_studies/
├── showcases/
└── jobs/
```

Dataset snapshots and immutable runs are facts. Workspace files, caches, and rebuilt reports may be
derived from them, but must not be used to rewrite their identities or economic results.

## Formal backtest checklist

Before an authorized formal run:

1. Freeze strategy alias and revision.
2. Freeze dataset ID/version and exact UTC interval.
3. Confirm factor timing, Rank path, decision/rebalance clocks, and fill timing.
4. Confirm sizing, leverage, costs, funding, valuation, risk exits, and terminal handling.
5. Estimate turnover and expected fee/slippage drag; stop for confirmation when cost may dominate.
6. Validate local inputs and output identity without substituting `latest`.
7. Record the background job identity, log, and status file.

For long user-facing runs, launch the job and return control. Do not spend a session continuously
polling it. A later status request should read the recorded status/log and verify completed artifact
manifests before reporting success.

## Reports

- A run-directory report is part of the immutable run when included in its manifest.
- A display report under `reports/` may be deterministically rebuilt from verified artifacts.
- Every curve report with trade artifacts must expose every fill and every position-change timestamp.
- Strategy-family parent reports may index multiple immutable child reports but must not merge their
  identities.
- New human-facing HTML publishes `*.en.html` and `*.zh-CN.html`; the compatibility `*.html` path is
  the English document. JSON, Parquet, manifests, hashes, and metric keys remain language-neutral.
- Existing immutable runs are not rewritten merely to add a language file. Rebuild language variants
  under `reports/` or another derived-output root after verifying the original manifest.

Showcase pages under `showcases/` are derived presentation views. They must verify every selected
immutable run before reading metrics, display source/warning qualifications, avoid absolute machine
paths and remote assets, and never write inside `runs/`. Rebuilding a showcase does not authorize a
research run or formal backtest.

## Development workflow

1. Inspect `main`, upstream state, and the worktree.
2. With authorization for network synchronization, update local `main` without rewriting history.
3. Create a `codex/<task>` feature branch.
4. Implement only the requested scope and preserve unrelated work.
5. Run only the tests or checks authorized for the current task.
6. Review diff, artifacts, and documentation before any authorized commit/push/merge.

Never force-push, rewrite immutable results, or modify another system as an implied part of a
backtest task.
