# Open-source dependency and release policy

[简体中文](open_source_release.zh-CN.md)

This policy defines how BFBT dependencies, versions, tags, and downloadable packages are produced.
It does not authorize a release by itself.

## Supported source and installation states

- `main` is the current development and security-fix line.
- A Git tag `vMAJOR.MINOR.PATCH` identifies a released source state.
- GitHub Release assets are the supported immutable binary/source distributions.
- `requirements/runtime.lock` is the hash-locked end-user environment.
- `requirements/dev.lock` adds the test and release toolchain used by CI.
- Editable installation without the matching lock is convenient development state, not a
  reproducible release environment.

The lock files cover Python 3.10–3.12 and Linux-compatible artifacts. They pin direct and transitive
versions and hashes. The source hashes, lock hashes, and exact `uv` generator version are recorded
in `requirements/lock-manifest.json`. `scripts/release_tools.py check-lock` verifies this contract
offline.

## Reproducible installation

Runtime installation uses the lock before installing BFBT without dependency resolution:

```bash
python -m pip install --require-hashes -r requirements/runtime.lock
python -m pip install --no-deps --no-build-isolation -e .
```

CI and release jobs use `requirements/dev.lock` the same way. A missing hash, changed input, edited
lock, incompatible Python version, or unavailable locked artifact fails closed. The Ubuntu helper
implements the runtime path; it does not silently fall back to an unlocked install.

## Dependency updates

Dependabot checks Python and GitHub Actions dependencies weekly. It may open pull requests, but BFBT
does not enable automatic merging. Every update must:

1. explain direct and relevant transitive changes;
2. regenerate both locks with the exact generator in the manifest;
3. review package source, license, supported Python versions, and unexpected new dependencies;
4. pass the lock gate and complete Python 3.10/3.12 offline suite;
5. preserve economic equivalence and artifact identities unless an explicit versioned contract
   change says otherwise.

Security fixes may be handled immediately. Major runtime upgrades and changes to Polars, PyArrow,
DuckDB, Pydantic, packaging, or GitHub release actions receive isolated pull requests. No dependency
update is accepted only because a bot says it is current.

Lock regeneration is an explicitly networked maintainer operation documented in
`requirements/README.md`. Generated market data and backtest artifacts never enter a dependency
update.

## Versioning and compatibility

BFBT uses Semantic Versioning for the Python distribution and Git tags. While the project is in
`0.x`, minor releases may change undocumented Python internals. Patch releases do not intentionally
change documented research economics.

Schema, factor, dataset, research, and run identities have their own explicit versions. A package
release never rewrites an immutable artifact or silently changes its recorded economic meaning.
Breaking changes to a documented CLI or contract require release notes and, when feasible, one
minor release of deprecation guidance.

The version must match in `pyproject.toml`, `bfbt.__version__`, the tag, and the changelog release
heading. `scripts/release_tools.py check-tag` enforces those facts.

## Release procedure

1. Start from a clean feature branch based on synchronized `main`.
2. Set the same version in `pyproject.toml` and `src/bfbt/__init__.py`.
3. Move user-visible entries from `Unreleased` into `## X.Y.Z - YYYY-MM-DD` in both changelogs.
4. Regenerate locks if dependency inputs changed.
5. Pass focused checks, the complete offline Python 3.10/3.12 matrix, package build, `twine check`,
   wheel/sdist content inspection, secret/path scanning, and documentation checks.
6. Merge to `main`, then create and push one annotated or signed `vX.Y.Z` tag at that exact commit.
7. The `release` workflow rechecks the tag and lock, reruns both test environments, builds with the
   locked toolchain, creates `SHA256SUMS`, and publishes the wheel, sdist, and checksums in one
   GitHub Release.

The workflow never creates a tag itself. Creating the tag is the maintainer's explicit publication
decision. Failed jobs do not publish a partial Release; fix the source with a new commit and version
decision rather than moving an already published tag.

PyPI publication is not currently enabled. It may be added only with PyPI Trusted Publishing and a
protected GitHub environment; long-lived upload tokens are not accepted. GitHub Release artifacts
remain the authoritative packages until that separate infrastructure is configured and verified.

## Repository settings required from the owner

GitHub repository settings cannot be committed in source. The owner should protect `main`, require
the offline test workflow before merge, prevent force pushes and deletion, and restrict release tag
creation to maintainers. The release workflow needs permission to create GitHub Releases; no
repository secret is required for the current GitHub-only publication path.

## Rollback and security

Published tags and assets are immutable. If a release is defective, mark it clearly in GitHub,
publish a fixed patch version, and retain the original evidence. For a security issue, follow
`SECURITY.md`; do not disclose exploit details in the changelog before coordinated remediation.
