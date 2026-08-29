# bianbt repository instructions

These instructions apply to the entire repository. User instructions in the current task
take precedence. Keep this file concise because Codex loads it at the start of every task.

## Start of every task

- Treat this Git repository as the complete `bianbt` project; do not depend on a parent
  repository or any live-trading package.
- Read `docs/maintainer/START_HERE.md`, then the maintainer documents it routes to.
- If `.local/CODEX_HANDOFF.md` exists, read it as machine-private context. Never add
  `.local/` to Git.
- Before changing anything, inspect the current branch, HEAD, worktree, and recent commits.
  If reality differs from the documents, report the Git and artifact facts first.

## Safety and authorization

- Never read `.env`, credentials, account data, or live-trading clients; never place or
  simulate sending a real order.
- Tests, network access, data downloads, formal backtests, commit, push, and merge require
  authorization from the current user task. Historical permission does not carry forward.
- Read-only inspection and static checks are allowed when relevant. Do not silently turn a
  usage request into engine development or a diagnostic request into a fix.
- Data, catalogs, checkpoints, workspaces, immutable runs, and generated reports belong
  under `data/backtest/` and must remain untracked.

## Architecture contracts

- Preserve the workflow: quick factor research -> Fast Matrix research -> Event formal run.
- V1 is compatibility-only. New conventional portfolio research uses Fast Matrix; strategies
  with path-dependent state, risk exits, or event arbitration use Event/V2.
- Preserve point-in-time data semantics, next-bar execution timing, explicit costs, immutable
  artifacts, deterministic reports, and exact run/config/data identities.
- Event/V2 chunked execution must retain bounded memory, warmup, checkpoint, recovery, and
  continuous-versus-resumed economic equivalence. Do not replace it with an unbounded collect.
- Curve reports may downsample display points, but must retain every trade, position-change
  timestamp, and risk event in their audit navigation.

## Strategy and backtest workflow

- Freeze factor timing, Rank behavior, decision clock, fill timing, sizing, costs, risk exits,
  and end-of-run handling before starting a formal backtest.
- Estimate turnover and fee/slippage drag before a high-frequency strategy run. Warn and wait
  for confirmation when turnover may dominate expected gross return.
- Formal results are identified by stable strategy alias plus immutable run ID. Never overwrite
  an old run or rewrite an artifact to change a result.
- For a user-authorized long backtest, launch the recorded background job and return control;
  inspect or monitor it only when the user later asks for status.

## Development and Git

- Start development from a verified `main` and use a `codex/` feature branch. Do not work
  directly on `main`.
- Preserve unrelated user changes and never use destructive Git recovery without explicit
  approval.
- Update code, focused tests, acceptance documentation, and maintainer state together when
  behavior or a durable contract changes.
- Do not claim tests passed unless they were run in the current repository state. Keep the
  previous verified baseline distinct from unverified changes.

