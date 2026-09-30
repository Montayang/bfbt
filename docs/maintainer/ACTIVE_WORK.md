# Active work

Updated: 2026-09-30.

## Current state

- Final open-source hardening is implemented and verified by A44: runtime and development
  dependencies are universally resolved and hash locked with `uv 0.12.20`; CI, Pages, and release
  Actions are pinned to immutable commits; reviewed weekly updates, tag/version/changelog gates,
  wheel/sdist inspection, SHA-256 publication, and bilingual release/extension policies are in
  place. The focused A44 suite passes 3 tests and the final complete offline suite passes 381 tests
  in 23.77 seconds. Fresh locked runtime installation, editable CLI startup, locked release-tool
  installation, warning-free wheel/sdist build, Twine validation, archive inspection, checksums,
  and installed-wheel CLI startup all pass. No market data, research, or backtest was run.

- The general supervised Agent workflow is merged into `main` at `9800438`: strict
  original-text-bound intent, semantic/data/backend/cost planning, plan-bound expiring grants,
  causal expression factors, resumable evidence hand-offs, human Matrix selection, Event source
  validation, and claim-level citations. A43 focused verification passes 10 tests, the combined
  factor/DE/Agent set passes 34, and the complete suite passes 378 tests in 24.23 seconds. No model
  API, network, market download, real research, or formal backtest was invoked.

- DE-v1 is merged into `main` at `94204cb`: a versioned requirement expands core dates by
  warmup and future tails; `data plan/inspect` is deterministic and side-effect-free; `data prepare`
  records resumable acquire/normalize/snapshot/readiness steps and publishes exact lineage evidence.
  The focused A42 offline suite passes 7 tests and the complete suite passes 368 tests in 39.73
  seconds. No network request, data download, research run, or formal backtest occurred; the
  separate D4 controlled real-data record remains authorization-gated.

- Fast Matrix phase two is implemented and verified by A41: multi-candidate research now
  shares market preparation, updates state only at joint rebalance/funding boundaries, values each
  interval in one columnar batch, and stores only non-zero holdings. A32-A34/A41 focused verification
  passes 8 tests, including exact checkpoint/result-hash equivalence for mark valuation and funding;
  the complete offline suite passes 361 tests in 24.72 seconds.
  A fixed offline 6-candidate benchmark measured 4.488 s independent versus 0.296 s joint execution
  (15.14× for that synthetic shape). No formal backtest, research run, or data download occurred.
- The source-pinned open-source trend/momentum batch now has 14 registered `v1` implementations:
  exact Qlib/`ta` formulas remain distinct from BFBT adaptations, duplicate controls remain visibly
  labelled, and gaps/invalid inputs fail closed. Focused formula, recursive-reference, warmup,
  causality, gap, parameter, and registry coverage passes 40 tests; the complete offline suite
  passes 360 tests. Quick Research remains unstarted; its `1m/5m/15m` source
  bars and `1/5/20`-bar forecasts are frozen to match the prior study, while dates and dataset
  identity still require a new study contract.
- The local-first data subsystem scope remains frozen in `DATA_ENGINE_PLAN.md`; DE-v1 code and
  offline acceptance are complete, while real-data D4 evidence is still pending separate approval.
- Confirmed unfinished work is consolidated in `DEVELOPMENT_BACKLOG.md`. It distinguishes active
  research/development lines from optional evidence reruns, capacity checks and demand-gated ideas.
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
- `main` contains six self-contained, path-sanitized report examples published through GitHub Pages:
  English and Simplified-Chinese Quick Research, Fast Matrix, and Event pages. Root and Showcase
  READMEs link each language to its matching live report.
- The first CI run for `8737507` exposed one stale A34 assertion that expected superseded Fast
  Matrix English copy. `codex/fix-ci-matrix-report-copy` aligns the assertion with the implemented
  Event Engine wording; its focused A34 file passes 3 tests and the complete offline suite passes
  337 tests locally.
- No formal backtest or research run is active.
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
- AI Agent readiness and remaining gaps are recorded in `AI_AGENT_READINESS.md`. The general
  supervised workflow is implemented and accepted by A43; A44 closes AG13 and AG15, while AG05,
  AG10, AG12 and AG16 retain the explicitly documented partial or missing follow-up scope.
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
- The first GitHub Actions runs exposed Python 3.10-only compatibility gaps despite the declared
  `>=3.10` support. The compatibility surface, universal dependency locks, and a native compile
  gate now make both supported CI runtimes validate syntax before the full offline suite. Local
  3.10 and 3.12 verification is required before the remote matrix is treated as release evidence.
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
- GitHub Pages publication is complete; it is not a pending release gate.

## No active formal run

No new market-data download or formal Event backtest is part of this release task. Separately
authorized README screenshot preparation uses a deterministic documentation fixture and derived
report rebuilding; it is not formal research evidence.
