# Operations and formal-run handling

## Fresh Ubuntu deployment

The public no-programming path assumes a normal-user checkout on a 64-bit Ubuntu 22.04/24.04 LTS
server. After cloning the repository, `scripts/install_ubuntu.sh` creates or reuses the local
`.venv`, installs runtime dependencies, creates ignored `data/backtest/` directories, and runs the
read-only doctor. It may install missing `python3`/`python3-venv` packages through `sudo` on an
`apt`-based host; it must not overwrite an invalid existing `.venv`.

The helper does not configure an Agent vendor, download market data, execute research, start a
formal run, open a port, or create a background service. Those remain separate setup or authorized
actions. A server-connected Agent should use the repository as its workspace and `.venv/bin/bfbt`
as the executable. Never expose `data/backtest/` directly to the public internet.

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
├── agent_jobs/
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

## Supervised Agent jobs

Agent workflow state is recorded under `data/backtest/agent_jobs/` and remains untracked. An Agent
first creates a side-effect-free plan from a complete `AgentResearchIntent`; the plan does not
authorize execution. Every mutating stage requires an unexpired grant bound to the exact plan hash
and action class.

The recorded route is Data preparation → Quick Research → Fast Matrix → human promotion → Event →
evidence summary. Completed stages may be resumed across sessions only after their evidence hashes
are revalidated. Fast Matrix selection remains a human decision, and Event evidence must name the
selected Matrix source run. Long authorized operations follow the same background-job rule as
formal backtests: launch the recorded job, return control, and inspect it only after a later status
request.

The Agent surface never authorizes model-generated shell/Python, account access, credentials, live
orders, or silent expansion from research into a formal Event run. See
`docs/design/agent_workflow.md` and `docs/acceptance/A43.md` for the exact contracts and gates.

## Development workflow

1. Inspect `main`, upstream state, and the worktree.
2. With authorization for network synchronization, update local `main` without rewriting history.
3. Create a `codex/<task>` feature branch.
4. Implement only the requested scope and preserve unrelated work.
5. Run only the tests or checks authorized for the current task.
6. Review diff, artifacts, and documentation before any authorized commit/push/merge.

Never force-push, rewrite immutable results, or modify another system as an implied part of a
backtest task.

## Dependency and release operations

- Validate the committed dependency state offline with
  `.venv/bin/python scripts/release_tools.py check-lock`.
- Regenerating locks is a deliberate networked maintenance operation using the exact `uv` version
  in `requirements/lock-manifest.json`; review the complete diff and never edit a generated lock by
  hand.
- A release requires matching versions in `pyproject.toml` and `bfbt.__version__`, dated English and
  Chinese changelog sections, a clean merged `main`, and an owner-created annotated or signed tag.
- The tag workflow is the package/release gate; it does not create tags, publish to PyPI, download
  market data, or modify immutable research artifacts.
- Never move or delete a published release tag. Publish a corrective patch release and retain the
  original evidence. Full procedure: `docs/reference/open_source_release.md`.
