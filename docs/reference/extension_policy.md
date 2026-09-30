# Extension and compatibility policy

[简体中文](extension_policy.zh-CN.md)

BFBT favors explicit, auditable research contracts over implicit runtime plugins. This policy says
what external users may rely on today and what requires a normal source contribution.

## Supported extension paths

1. **Safe factor expressions.** `bfbt-factor-expression/v1` is the supported no-code formula path.
   It permits only documented causal fields and functions, never imports, arbitrary Python, shell,
   credentials, or network access.
2. **Versioned in-repository factors.** A Python factor may be contributed to `src/bfbt/factors/`
   with an explicit identity, inputs, warmup, availability time, gap/finite-value policy, source,
   fixtures, and causality tests. It becomes supported only after review and release.
3. **External orchestration.** An Agent or local application may invoke documented CLI commands and
   exchange versioned JSON/artifact contracts. It must preserve planning, authorization, immutable
   evidence, and human promotion gates.

BFBT does **not** currently discover third-party packages through Python entry points, load code
from a plugin directory, or promise stability for arbitrary imports from internal modules. A package
that monkey-patches engines or modifies artifact files is unsupported even if it appears to work.

## Compatibility surfaces

- Versioned schemas and their validators are stable within that schema version.
- Immutable artifacts remain readable according to their recorded schema/manifest contracts.
- Documented CLI commands and machine-readable fields follow the package release policy.
- The safe expression language is compatible within `bfbt-factor-expression/v1`; new syntax that
  changes meaning requires another expression version.
- Report HTML appearance and DOM structure are presentation details, not a plugin API. Machine-
  readable artifacts are the integration surface.
- Python objects not explicitly documented as public may change in a `0.x` minor release.

External packages should declare an upper BFBT bound such as `bfbt>=0.1,<0.2`, test every supported
Python/BFBT combination, and fail closed on unknown schema or capability versions. Compatibility
claims require deterministic fixtures, not only import success.

## Security and economic boundaries

An extension may not add exchange credentials, live-order clients, implicit network activity,
automatic Fast Matrix promotion, unreviewed generated code, future-data access, or artifact
rewrites. Data acquisition, research execution, formal Event execution, tests, and source control
remain separate authorization classes.

Factors must preserve point-in-time availability, next-bar execution boundaries, deterministic
identity, missing-data behavior, and explicit costs. Path-dependent behavior belongs in the Event
Engine and cannot be approximated silently in Fast Matrix.

## Future plugin API

A general plugin loader will be considered only after concrete external use cases demonstrate that
source contributions and safe expressions are insufficient. It requires a versioned entry-point
contract, isolated configuration namespace, capability declaration, path/resource limits,
deterministic fixtures, compatibility matrix, deprecation policy, and security review. Merely
placing a module on `PYTHONPATH` will never constitute a supported plugin contract.
