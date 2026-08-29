# Active work

Updated: 2026-08-29.

## Current state

- No engine development or formal backtest is active.
- An AI Agent readiness audit is recorded in `AI_AGENT_READINESS.md`. It defines the intended
  natural-language research workflow and AG01–AG16 backlog; implementation has not started.
- Durable cross-session guidance is maintained by root `AGENTS.md`, `docs/maintainer/`, and the
  ignored local `.local/CODEX_HANDOFF.md` when it is present.
- The standalone offline pytest suite passed all 322 tests on 2026-08-29 against HEAD `69e8588`;
  only the current maintainer documentation changes were uncommitted.
- Inspect Git for the exact current branch, commit, worktree, and upstream state rather than relying
  on a branch name recorded in this document.

## Pending verification

- Future test runs still require explicit authorization from the current task; the completed
  `322 passed` baseline does not grant permission to rerun them in a later session.
- Confirm a clean new Codex session automatically reads root `AGENTS.md`, follows
  `docs/maintainer/START_HERE.md`, and reads the ignored local handoff when present.

## No active formal run

No new data download, Fast Matrix study, Event formal backtest, or report rebuild is part of this
handoff feature.
