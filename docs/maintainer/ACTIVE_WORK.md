# Active work

Updated: 2026-09-02.

## Current state

- No engine development or formal backtest is active.
- Public-release preparation is active on `codex/bfbt-public-release`. It performs the complete
  `bianbt` → BFBT/`bfbt` brand, distribution, import, CLI, and repository-link migration; makes the
  English README the front door; adds a standalone Chinese README and Binance-independence
  disclosure; and publishes separate English/Simplified-Chinese human-facing HTML.
- The bounded Showcase S0–S5 implementation was committed as `9f991f2`, fast-forwarded into `main`,
  and pushed. Its prior 6 focused A39 tests and 331-test full offline suite remain the verified
  pre-rename baseline.
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
- The BFBT public-release candidate passes 48 focused report, Showcase, and language-contract tests
  and the complete offline suite (`334 passed in 21.58s`). Its real derived Showcase rebuild
  verified all three immutable H2 runs. Editable installation, CLI metadata, dependencies, and a
  `bfbt`-only wheel are verified. Public-surface/history, secret/path, format, link, generated-output,
  and diff checks passed. Commit, push, GitHub repository rename, and visibility change remain
  pending external publication actions.
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
- A hosted GitHub Pages Showcase is intentionally deferred as a future presentation enhancement;
  no date or implemented status is claimed.

## No active formal run

No new data download, Fast Matrix study, or Event formal backtest is part of this release task.
Only derived report rebuilding is allowed for language and Showcase verification.
