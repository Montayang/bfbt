# Reproducible dependency environments

`runtime.lock` is the end-user environment. `dev.lock` adds the `test` and `release` extras. Both
files are universal Python 3.10–3.12, hash-checked requirements resolved from the Python 3.10
compatibility floor in `pyproject.toml` and `bootstrap.in`; do not edit them by hand.

The exact generator version and SHA-256 identities are recorded in `lock-manifest.json`. Validate
the committed state without network access:

```bash
.venv/bin/python scripts/release_tools.py check-lock
```

Regeneration is a deliberate networked maintainer action:

```bash
UV_VERSION="$(python scripts/release_tools.py generator-version)"
python -m pip install "uv==${UV_VERSION}"
bash scripts/update_locks.sh
```

Review every direct and transitive change. Run the complete Python 3.10/3.12 CI matrix before
merging. Dependency update policy is documented in
[`docs/reference/open_source_release.md`](../docs/reference/open_source_release.md).
