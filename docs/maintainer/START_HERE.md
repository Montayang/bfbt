# Maintainer start here

This is the durable entry point for a new development or backtest session. It records project
facts rather than chat transcripts and contains no credentials or machine-specific paths.

## Reading order

1. Repository `AGENTS.md` for safety, authorization, architecture, and Git rules.
2. `CURRENT_STATE.md` for implemented capabilities and the latest verified baseline.
3. `ACTIVE_WORK.md` for unfinished or unmerged work.
4. `AI_AGENT_READINESS.md` for the natural-language research target, readiness gaps, and ordered
   development backlog.
5. `ARCHITECTURE_DECISIONS.md` for decisions that cannot be reconstructed safely from one file.
6. `OPERATIONS.md` for data, artifact, formal-run, report, and background-job handling.
7. The relevant file under `docs/design/`, `docs/reference/`, `docs/acceptance/`,
   `docs/research/`, or `strategies/` for the current task.
8. `.local/CODEX_HANDOFF.md`, when present, for machine-private paths and preferences.

## Read-only startup check

Before proposing changes, inspect without fetching, testing, or modifying state:

```bash
pwd
git status --short --branch
git log -5 --oneline --decorate
git rev-parse HEAD
git remote -v
```

Report any mismatch between Git, immutable artifacts, and these documents. Git identifies the
source state; immutable run manifests identify formal result facts. Documentation is navigation,
not permission to overwrite either.

## Authorization is session-scoped

The startup check does not authorize tests, network access, downloads, backtests, commits,
pushes, merges, or external changes. Obtain authorization from the current task before each
category is used. A new session must not inherit old approvals.

## New-session bootstrap prompt

Use this generic prompt when opening a clean session in a checkout of this repository:

```text
Use this bianbt repository as the only backtest workspace.

First read AGENTS.md and docs/maintainer/START_HERE.md, follow its reading order, and read
.local/CODEX_HANDOFF.md if it exists. Then only inspect the branch, HEAD, worktree, recent
commits, and recorded job state. Do not modify files, fetch, test, download data, run a
backtest, commit, push, or merge.

Report the inherited architecture, current strategy/research identities, unfinished work,
and authorization boundaries, then wait for my task.
```
