# Active work

Updated: 2026-09-04.

## Current state

- The clean-source 1x-leverage May Event run `a17-ca0c6d168e37c07b06239452` completed and all 19
  immutable artifacts verified. Its English and Simplified-Chinese reports replace the prior 5x
  hosted Event example. The Fast Matrix English Pages copy also restores lowercase language-switch
  machine identifiers so its selector is right-aligned and functional like the other reports.
- Public Showcase discovery was reframed and merged into `main` at `82e57eb`: English-first and
  independent Chinese guides now lead a fresh-checkout user through the three report layers, while
  the prior H2 three-month comparison is retained only as an optional prepared-machine evidence
  case. The root README preview now represents report surfaces rather than selected run returns.
- The four public documentation entry points linked from the root README now follow one convention:
  unsuffixed files are the complete English editions and `.zh-CN.md` siblings are independent
  Simplified-Chinese editions. Navigation links and the public-release contract cover both editions.
- Event report language generation corrupted interactive JavaScript by treating comparison operators
  inside `<script>` as HTML text boundaries. The fix merged into `main` at `8737507`; it isolates
  executable and literal blocks, localizes JavaScript string literals without altering operators,
  and adds an A40 regression. Rebuilt English and Chinese May reports retain all 1,655 curve points
  and 668 snapshots, and all generated JavaScript blocks pass static syntax validation.
- `main` now contains six self-contained, path-sanitized report examples for GitHub Pages:
  English and Simplified-Chinese Quick Research, Fast Matrix, and Event pages. Root and Showcase
  READMEs link each language to its matching report. Publication still requires public repository
  visibility and GitHub Pages to use GitHub Actions as its source.
- The first CI run for `8737507` exposed one stale A34 assertion that expected superseded Fast
  Matrix English copy. `codex/fix-ci-matrix-report-copy` aligns the assertion with the implemented
  Event Engine wording; its focused A34 file passes 3 tests and the complete offline suite passes
  337 tests locally.
- No engine development or formal backtest is active.
- The public-release implementation was completed on `codex/bfbt-public-release` and approved for
  fast-forward publication to `main`. It performs the complete
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
  and the complete offline suite (`335 passed in 37.81s`). Its real derived Showcase rebuild
  verified all three immutable H2 runs. Editable installation, CLI metadata, dependencies, and a
  `bfbt`-only wheel are verified. Public-surface/history, secret/path, format, link, generated-output,
  and diff checks passed. The implementation is committed as `3507c52`; the GitHub repository was
  renamed to `Montayang/bfbt`. Public visibility and any optional tag/release remain separate
  owner-controlled publication actions.
- Final public-front-door polish is complete on `codex/public-polish`: English is now the primary
  language for README navigation, the preview image, contribution guidance, security policy, and
  changelog; independent Simplified-Chinese counterparts remain linked. The renamed remote now
  exposes only `main`; all merged remote feature branches were removed before visibility changes.
- The first GitHub Actions runs exposed Python 3.10-only standard-library imports despite the
  declared `>=3.10` support. `codex/ci-python310` replaces them with a tested compatibility surface;
  local 3.12 full-suite and 3.10 syntax/contract gates pass. The remote 3.10/3.12 matrix remains the
  final confirmation after push.
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
- GitHub Pages report publication is prepared on `main` but not yet live: public repository
  visibility and the repository owner's one-time Pages source selection remain pending.

## No active formal run

No new market-data download or formal Event backtest is part of this release task. Separately
authorized README screenshot preparation uses a deterministic documentation fixture and derived
report rebuilding; it is not formal research evidence.
