#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

PYTHON="${PYTHON:-.venv/bin/python}"
UV="${UV:-.venv/bin/uv}"

[[ -x "${PYTHON}" ]] || {
  echo "lock update stopped: ${PYTHON} is not executable" >&2
  exit 2
}
[[ -x "${UV}" ]] || {
  echo "lock update stopped: install the pinned uv version in the repository environment" >&2
  exit 2
}

EXPECTED="$(${PYTHON} scripts/release_tools.py generator-version)"
ACTUAL="$(${UV} --version | awk '{print $2}')"
[[ "${ACTUAL}" == "${EXPECTED}" ]] || {
  echo "lock update stopped: expected uv ${EXPECTED}, found ${ACTUAL}" >&2
  exit 2
}

${UV} pip compile pyproject.toml requirements/bootstrap.in \
  --python-version 3.10 \
  --universal \
  --generate-hashes \
  --custom-compile-command 'scripts/update_locks.sh' \
  --output-file requirements/runtime.lock

${UV} pip compile pyproject.toml requirements/bootstrap.in \
  --python-version 3.10 \
  --extra test \
  --extra release \
  --universal \
  --generate-hashes \
  --custom-compile-command 'scripts/update_locks.sh' \
  --output-file requirements/dev.lock

${PYTHON} scripts/release_tools.py write-lock-manifest \
  --generator-version "${EXPECTED}"
${PYTHON} scripts/release_tools.py check-lock
