#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

fail() {
  echo "BFBT installation stopped: $*" >&2
  exit 1
}

install_python_packages() {
  command -v apt-get >/dev/null 2>&1 || fail \
    "python3 and venv are required; install Python 3.10+ and retry"

  local -a privilege=()
  if [[ "$(id -u)" -ne 0 ]]; then
    command -v sudo >/dev/null 2>&1 || fail \
      "sudo is required to install missing Ubuntu packages"
    privilege=(sudo)
  fi

  echo "Installing the required Ubuntu Python packages..."
  "${privilege[@]}" apt-get update
  "${privilege[@]}" apt-get install -y python3 python3-venv
}

[[ "$(uname -s)" == "Linux" ]] || fail "this helper supports Linux only"
[[ -f "${PROJECT_ROOT}/pyproject.toml" ]] || fail "run this script from a BFBT checkout"

if ! command -v python3 >/dev/null 2>&1; then
  install_python_packages
fi

python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' || fail \
  "Python 3.10 or newer is required; use Ubuntu 22.04/24.04 or install a newer Python"

cd "${PROJECT_ROOT}"

if [[ -e .venv && ! -x .venv/bin/python ]]; then
  fail ".venv exists but is not usable; ask the server operator to inspect it instead of overwriting it"
fi

if [[ ! -x .venv/bin/python ]]; then
  if ! python3 -m venv .venv; then
    install_python_packages
    python3 -m venv .venv
  fi
fi

echo "Installing BFBT and its runtime dependencies..."
.venv/bin/python -m pip install --require-hashes -r requirements/runtime.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .

mkdir -p \
  data/backtest/catalogs \
  data/backtest/datasets \
  data/backtest/workspaces \
  data/backtest/runs \
  data/backtest/reports \
  data/backtest/research_runs \
  data/backtest/research_studies \
  data/backtest/event_studies \
  data/backtest/agent_jobs \
  data/backtest/jobs \
  data/backtest/showcases

echo "Checking the installation..."
.venv/bin/bfbt doctor

echo
echo "BFBT is installed in ${PROJECT_ROOT}."
echo "No market data was downloaded and no backtest was started."
echo "An operator or server-connected AI Agent can now use ${PROJECT_ROOT}/.venv/bin/bfbt."
