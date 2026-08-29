"""Temporary ordered Parquet parts used to keep A10 intermediates off heap."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

import polars as pl
import pyarrow.parquet as pq


class SpoolError(RuntimeError):
    """A temporary chunk table cannot be written or scanned safely."""


class ChunkWorkspace:
    def __init__(self, output_root: Path) -> None:
        work_root = output_root.resolve() / ".work"
        if work_root in {Path("/"), Path.home().resolve()}:
            raise SpoolError(f"unsafe work root: {work_root}")
        work_root.mkdir(parents=True, exist_ok=True)
        self.path = Path(tempfile.mkdtemp(prefix="a10-", dir=work_root))
        (self.path / "workspace.json").write_text(
            json.dumps(
                {"created_unix": time.time(), "pid": os.getpid()},
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self.spool = ParquetSpool(self.path)

    def __enter__(self) -> "ChunkWorkspace":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self.path.exists():
            shutil.rmtree(self.path)


class ParquetSpool:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._parts: dict[str, list[Path]] = {}

    def _path(self, table: str) -> Path:
        if not table or "/" in table or ".." in table:
            raise SpoolError(f"unsafe spool table name: {table!r}")
        ordinal = len(self._parts.get(table, ()))
        path = self.root / table / f"part-{ordinal:06d}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def append_lazy(self, table: str, frame: pl.LazyFrame) -> int:
        path = self._path(table)
        frame.sink_parquet(
            path,
            compression="zstd",
            statistics=True,
            engine="streaming",
        )
        rows = int(pq.ParquetFile(path).metadata.num_rows)
        self._parts.setdefault(table, []).append(path)
        return rows

    def append_frame(self, table: str, frame: pl.DataFrame) -> int:
        path = self._path(table)
        frame.write_parquet(path, compression="zstd", statistics=True)
        rows = frame.height
        self._parts.setdefault(table, []).append(path)
        return rows

    def attach(self, table: str, paths: tuple[Path, ...]) -> None:
        """Register verified immutable Parquet parts without copying them."""

        if table in self._parts:
            raise SpoolError(f"spool table is already attached: {table}")
        checked: list[Path] = []
        for path in paths:
            resolved = path.resolve()
            if not resolved.is_file() or resolved.suffix != ".parquet":
                raise SpoolError(f"attached spool part is not Parquet: {resolved}")
            pq.ParquetFile(resolved)
            checked.append(resolved)
        if not checked:
            raise SpoolError(f"attached spool table has no parts: {table}")
        self._parts[table] = checked

    def scan(
        self,
        table: str,
        *,
        schema: dict[str, pl.DataType] | None = None,
    ) -> pl.LazyFrame:
        paths = self._parts.get(table, [])
        if not paths:
            if schema is None:
                raise SpoolError(f"spool table has no parts: {table}")
            return pl.DataFrame(schema=schema).lazy()
        return pl.concat(
            [
                pl.scan_parquet(path, hive_partitioning=False)
                for path in paths
            ],
            how="vertical",
        )

    def files(self, table: str) -> tuple[Path, ...]:
        return tuple(self._parts.get(table, ()))

    def row_count(self, table: str) -> int:
        return sum(
            int(pq.ParquetFile(path).metadata.num_rows)
            for path in self._parts.get(table, ())
        )


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def cleanup_stale_workspaces(
    output_root: Path,
    *,
    older_than_seconds: int,
    apply: bool = False,
    now_unix: float | None = None,
) -> tuple[Path, ...]:
    """List or remove only dead, marked A10 workspaces; never touch run dirs."""

    if older_than_seconds < 0:
        raise SpoolError("older_than_seconds must be non-negative")
    resolved_output = output_root.resolve()
    if resolved_output in {Path("/"), Path.home().resolve()}:
        raise SpoolError(f"unsafe output root: {resolved_output}")
    work_root = resolved_output / ".work"
    if not work_root.exists():
        return ()
    if not work_root.is_dir():
        raise SpoolError(f"work root is not a directory: {work_root}")
    now = time.time() if now_unix is None else now_unix
    stale = []
    for path in sorted(work_root.iterdir()):
        if not path.is_dir() or not path.name.startswith("a10-"):
            continue
        marker = path / "workspace.json"
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            created = float(payload["created_unix"])
            pid = int(payload["pid"])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if now - created < older_than_seconds or _process_is_alive(pid):
            continue
        stale.append(path)
    if apply:
        for path in stale:
            resolved = path.resolve()
            if resolved.parent != work_root or not resolved.name.startswith("a10-"):
                raise SpoolError(f"unsafe stale workspace target: {resolved}")
            shutil.rmtree(resolved)
    return tuple(stale)
