# Contributing to BFBT

[简体中文](CONTRIBUTING.zh-CN.md)

Thanks for contributing. BFBT prioritizes causal timing, explicit economic semantics,
bounded-memory execution, and immutable auditability over adding commands or factor count.

## Development workflow

1. Create a short-lived feature branch from a synchronized and verified `main`.
2. Freeze behavior, data/configuration identities, and compatibility boundaries in the issue or
   change description.
3. Put reusable capabilities in `src/bfbt/`; put real strategy specifications and run mappings in
   `strategies/`.
4. Update focused tests, acceptance documentation, and maintainer state together.
5. Run focused tests, then the complete offline suite:

```bash
python -m pip install -e ".[test]"
python -B -m pytest -q
```

6. Before committing, run `git diff --check` and confirm that no data, credentials, absolute local
   paths, or generated runs are tracked.

## Non-negotiable contracts

- Preserve the `Quick Research -> Fast Matrix -> Event/V2` responsibility boundary.
- Preserve point-in-time data, next-bar execution, explicit costs, and UTC `[start, end)` semantics.
- Formal full-market Event/V2 runs must be chunked, bounded in memory, checkpointable/recoverable,
  and economically equivalent to continuous execution.
- Never overwrite a successful or failed terminal run; revisions receive new identities.
- Reports may compress display curves but must retain every fill, position change, and risk event.
- Do not add account clients, API credentials, order entry points, or `.env` dependencies.

## Data and network tests

Do not commit `data/backtest/`, datasets, catalogs, checkpoints, workspaces, runs, or derived
reports. Default tests must be offline and use small deterministic fixtures. Acceptance work that
downloads public market data or executes a long backtest must be called out separately and retain
exact data and artifact identities.

## New factors

Every factor must declare input columns, window/warmup, availability timing, gap policy,
finite-value policy, and version. Add no-lookahead, cross-chunk, and boundary fixtures. Arbitrary
`eval`/`exec` or direct Agent-generated code execution is not an acceptable public factor API.
