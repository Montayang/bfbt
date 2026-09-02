#!/usr/bin/env bash
set -euo pipefail

BACKTEST_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
DATA_ROOT=${1:-"$BACKTEST_ROOT/data/backtest/datasets/binance_usdm_perpetual_1m"}
WORKERS=${WORKERS:-6}
CLI="$BACKTEST_ROOT/.venv/bin/bfbt"
RAW_ROOT="$DATA_ROOT/raw"
MANIFEST_ROOT="$DATA_ROOT/manifests/raw"
LOG_ROOT="$DATA_ROOT/logs/download"
SYMBOLS_FILE="$DATA_ROOT/symbols-2026-06.txt"
CUTOFF_MS=1782864000000

mkdir -p "$RAW_ROOT" "$MANIFEST_ROOT" "$LOG_ROOT"
EXCHANGE_JSON=$(find "$RAW_ROOT" -name 'rest-contracts-exchangeInfo-*.json' -type f | sort | tail -n 1)
if [[ -z "$EXCHANGE_JSON" ]]; then
  echo "missing exchangeInfo raw snapshot under $RAW_ROOT" >&2
  exit 2
fi

jq -r --argjson cutoff "$CUTOFF_MS" '
  .symbols[]
  | select(
      .contractType == "PERPETUAL"
      and .quoteAsset == "USDT"
      and .marginAsset == "USDT"
      and .onboardDate < $cutoff
    )
  | .symbol
' "$EXCHANGE_JSON" | sort -u > "$SYMBOLS_FILE"

download_symbol() {
  local symbol=$1
  local log="$LOG_ROOT/$symbol.log"
  {
    echo "symbol=$symbol"
    "$CLI" data archive-sync bars "$symbol"       2026-05-31T00:00:00Z 2026-06-01T00:00:00Z       --interval 1m --frequency daily --workers 1       --raw-root "$RAW_ROOT" --manifest-root "$MANIFEST_ROOT"
    "$CLI" data archive-sync bars "$symbol"       2026-06-01T00:00:00Z 2026-07-01T00:00:00Z       --interval 1m --frequency monthly --workers 1       --raw-root "$RAW_ROOT" --manifest-root "$MANIFEST_ROOT"
    "$CLI" data archive-sync bars "$symbol"       2026-07-01T00:00:00Z 2026-07-02T00:00:00Z       --interval 1m --frequency daily --workers 1       --raw-root "$RAW_ROOT" --manifest-root "$MANIFEST_ROOT"
    "$CLI" data archive-sync funding "$symbol"       2026-06-01T00:00:00Z 2026-08-01T00:00:00Z       --frequency monthly --workers 1       --raw-root "$RAW_ROOT" --manifest-root "$MANIFEST_ROOT"
    echo "status=complete"
  } > "$log" 2>&1
}

export CLI RAW_ROOT MANIFEST_ROOT LOG_ROOT
export -f download_symbol

symbol_count=$(wc -l < "$SYMBOLS_FILE")
echo "symbols=$symbol_count workers=$WORKERS"
xargs -r -n 1 -P "$WORKERS" bash -c 'download_symbol "$1"' _ < "$SYMBOLS_FILE"

complete=$(rg -l '^status=complete$' "$LOG_ROOT"/*.log | wc -l)
echo "complete=$complete expected=$symbol_count"
if [[ "$complete" -ne "$symbol_count" ]]; then
  echo "some symbol downloads failed; inspect $LOG_ROOT" >&2
  exit 1
fi
