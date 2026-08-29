"""Backward-compatible July-only wrapper for the generic R5-T4 runner."""

from __future__ import annotations

import argparse

from run_r5_t4_trailing_event import VARIANTS, resolved as _resolved, run as _run


def resolved(variant: str):
    return _resolved(variant, "2026-07")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", action="append", choices=VARIANTS)
    args = parser.parse_args()
    _run(("2026-07",), tuple(args.variant or VARIANTS))


if __name__ == "__main__":
    main()
