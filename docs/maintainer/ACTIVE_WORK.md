# Active work

Updated: 2026-09-02.

## Current state

- No engine development or formal backtest is active.
- The bounded pre-open-source Showcase implementation is active on `codex/showcase-plan`. S0–S5 are
  implemented and verified: contracts, doctor/preflight, verified static hub, one-command
  preparation, root README, CI definition, minimum public-facing policy documents, 6 focused A39
  tests, and the 331-test full offline suite. Final diff review, commit, push, and merge remain.
- The three authorized `R5-T4-H2-ROLLING` May–July formal runs are complete and registered; their
  generated artifacts and derived margin-trajectory report remain ignored local data.
- An AI Agent readiness audit is recorded in `AI_AGENT_READINESS.md`. It defines the intended
  natural-language research workflow and AG01–AG16 backlog; implementation has not started.
- Durable cross-session guidance is maintained by root `AGENTS.md`, `docs/maintainer/`, and the
  ignored local `.local/CODEX_HANDOFF.md` when it is present.
- The standalone offline pytest suite passed all 322 tests on 2026-08-29 against HEAD `69e8588`;
  only the current maintainer documentation changes were uncommitted.
- The H2 result registration changes passed the full offline suite on 2026-08-30:
  `325 passed in 37.65s`.
- The Showcase candidate passed `331 passed in 21.58s` on 2026-09-02; its real local preparation
  verified three H2 runs with one expected dirty-provenance warning and zero failures.
- Inspect Git for the exact current branch, commit, worktree, and upstream state rather than relying
  on a branch name recorded in this document.

## Pending verification

- Future test runs still require explicit authorization from the current task; the completed
  `322 passed` baseline does not grant permission to rerun them in a later session.
- Confirm a clean new Codex session automatically reads root `AGENTS.md`, follows
  `docs/maintainer/START_HERE.md`, and reads the ignored local handoff when present.
- The current Showcase uses existing H2 `r01` evidence with a visible dirty-provenance qualification.
  Clean `r02` formal evidence remains a separate future decision and requires explicit backtest
  authorization; it is not part of this implementation task.

## No active formal run

No new data download, Fast Matrix study, Event formal backtest, or report rebuild is part of this
handoff feature.
