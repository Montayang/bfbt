"""Compare complete A09 and A10 formal outputs while ignoring internal run IDs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import polars as pl
from polars.testing import assert_frame_equal

SORT_KEYS = {
    "targets": ["signal_time", "symbol"],
    "trades": ["fill_time", "symbol", "sequence"],
    "positions": ["timestamp", "symbol"],
    "costs": ["timestamp", "symbol"],
    "returns": ["timestamp"],
    "factor_values": ["timestamp", "symbol"],
    "universe": ["timestamp", "symbol"],
}


def _frame(path: Path, name: str) -> pl.DataFrame:
    frame = pl.read_parquet(path / "tables" / f"{name}.parquet")
    if "run_id" in frame.columns:
        frame = frame.drop("run_id")
    return frame.sort(SORT_KEYS[name])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("in_memory_run", type=Path)
    parser.add_argument("chunked_run", type=Path)
    args = parser.parse_args()
    in_memory = args.in_memory_run.resolve()
    chunked = args.chunked_run.resolve()
    for name in SORT_KEYS:
        assert_frame_equal(
            _frame(chunked, name),
            _frame(in_memory, name),
            check_exact=True,
        )
    in_memory_metrics = json.loads(
        (in_memory / "metrics.json").read_text(encoding="utf-8")
    )
    chunked_metrics = json.loads(
        (chunked / "metrics.json").read_text(encoding="utf-8")
    )
    if in_memory_metrics != chunked_metrics:
        raise AssertionError("metrics differ between execution modes")
    diagnostics = json.loads(
        (chunked / "performance.json").read_text(encoding="utf-8")
    )
    if diagnostics.get("mode") != "chunked":
        raise AssertionError("performance artifact does not declare chunked mode")
    if diagnostics.get("memory_budget_passed") is not True:
        raise AssertionError("memory budget gate did not pass")
    chunks = diagnostics.get("chunks")
    if not isinstance(chunks, list):
        raise AssertionError("performance chunks must be a list")
    analysis = [item for item in chunks if item.get("phase") == "analysis"]
    execution = [item for item in chunks if item.get("phase") == "execution"]
    if len(analysis) < 2 or len(execution) < 2:
        raise AssertionError("fixture did not cross multiple chunk boundaries")
    row_limit = int(diagnostics["max_input_rows_per_chunk"])
    for item in chunks:
        if sum(int(value) for value in item["input_rows"].values()) > row_limit:
            raise AssertionError("recorded chunk exceeds row budget")
    if (in_memory / "performance.json").exists():
        raise AssertionError("in_memory fixture unexpectedly published diagnostics")
    print(f"tables_equal={len(SORT_KEYS)}")
    print("metrics_equal=true")
    print(f"analysis_chunks={len(analysis)}")
    print(f"execution_chunks={len(execution)}")
    print("memory_budget_passed=true")


if __name__ == "__main__":
    main()
