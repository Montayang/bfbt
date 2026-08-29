"""Create paired in-memory/chunked configs from the isolated A09 fixture."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("workdir", type=Path)
    args = parser.parse_args()
    workdir = args.workdir.resolve()
    source = workdir / "backtest.json"
    payload = json.loads(source.read_text(encoding="utf-8"))

    in_memory = copy.deepcopy(payload)
    in_memory["run"]["name"] = "a10_equivalence_in_memory"
    in_memory["output"]["root"] = str(workdir / "runs-in-memory")
    in_memory["performance"] = {
        "mode": "in_memory",
        "chunk_interval": "2m",
        "max_input_rows_per_chunk": 1000,
        "max_incremental_rss_mib": 512,
        "collect_diagnostics": False,
    }

    chunked = copy.deepcopy(payload)
    chunked["run"]["name"] = "a10_equivalence_chunked"
    chunked["output"]["root"] = str(workdir / "runs-chunked")
    chunked["performance"] = {
        "mode": "chunked",
        "chunk_interval": "2m",
        "max_input_rows_per_chunk": 1000,
        "max_incremental_rss_mib": 512,
        "collect_diagnostics": True,
    }

    in_memory_path = workdir / "backtest-a10-in-memory.json"
    chunked_path = workdir / "backtest-a10-chunked.json"
    in_memory_path.write_text(json.dumps(in_memory, indent=2), encoding="utf-8")
    chunked_path.write_text(json.dumps(chunked, indent=2), encoding="utf-8")
    print(f"a10_in_memory_config={in_memory_path}")
    print(f"a10_chunked_config={chunked_path}")
    print(f"a10_in_memory_root={workdir / 'runs-in-memory'}")
    print(f"a10_chunked_root={workdir / 'runs-chunked'}")


if __name__ == "__main__":
    main()
