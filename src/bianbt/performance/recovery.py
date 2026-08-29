"""Crash-recoverable workspace and checkpoint contracts for V2 chunks."""

from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal, Mapping, Sequence

import polars as pl
import pyarrow.parquet as pq
from pydantic import Field, StringConstraints, field_validator, model_validator

from bianbt.config.common import StrictModel, as_utc
from bianbt.data.hashing import canonical_json_bytes, content_sha256, sha256_file
from bianbt.performance.chunks import TimeChunk

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")


class V2WorkspaceError(RuntimeError):
    """A V2 workspace cannot be created, resumed, or committed safely."""


class V2WorkspaceCorruptionError(V2WorkspaceError):
    """Committed state no longer matches its checkpoint manifest."""


class FrozenIntMap(dict[str, int]):
    """Integer mapping that cannot change after checkpoint validation."""

    def _immutable(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("checkpoint mappings are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable


def _utc(value: datetime) -> datetime:
    checked = as_utc(value)
    assert checked is not None
    return checked


def _relative_path(value: str) -> str:
    candidate = PurePosixPath(value)
    if (
        not value
        or chr(92) in value
        or candidate.is_absolute()
        or ".." in candidate.parts
        or value in {".", "./"}
    ):
        raise ValueError("must be a safe relative POSIX file path")
    return candidate.as_posix()


def _safe_name(value: str) -> str:
    if not _SAFE_NAME.fullmatch(value):
        raise V2WorkspaceError(f"unsafe checkpoint name: {value!r}")
    return value


def _plan_fingerprint(chunks: Sequence[TimeChunk]) -> str:
    if not chunks:
        raise V2WorkspaceError("chunk plan cannot be empty")
    for expected, item in enumerate(chunks):
        if item.ordinal != expected or item.end <= item.start:
            raise V2WorkspaceError("chunk plan ordinals or ranges are invalid")
        if expected and chunks[expected - 1].end != item.start:
            raise V2WorkspaceError("chunk plan must have contiguous core windows")
    payload = [
        {
            "ordinal": item.ordinal,
            "start": _utc(item.start).isoformat(),
            "end": _utc(item.end).isoformat(),
            "input_start": _utc(item.input_start).isoformat(),
        }
        for item in chunks
    ]
    return content_sha256(payload)


def _atomic_json(path: Path, payload: StrictModel) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    data = canonical_json_bytes(payload) + b"\n"
    with temporary.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _sync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class V2ChunkRunIdentity(StrictModel):
    identity_version: Literal["v2-chunk-run/v1"] = "v2-chunk-run/v1"
    run_id: str = Field(min_length=1)
    engine_version: str = Field(min_length=1)
    config_sha256: Sha256
    dataset_sha256: Sha256
    chunk_plan_sha256: Sha256
    run_start: datetime
    run_end: datetime
    chunk_interval: str = Field(min_length=1)
    overlap_seconds: int = Field(ge=0)

    @field_validator("run_start", "run_end")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_identity(self) -> "V2ChunkRunIdentity":
        if self.run_id.lower() == "latest":
            raise ValueError("run_id must be explicit")
        if self.run_end <= self.run_start:
            raise ValueError("run_end must be greater than run_start")
        return self

    @property
    def fingerprint(self) -> str:
        return content_sha256(self)

    @classmethod
    def from_plan(
        cls,
        *,
        run_id: str,
        engine_version: str,
        config_sha256: str,
        dataset_sha256: str,
        chunk_interval: str,
        overlap_seconds: int,
        chunks: Sequence[TimeChunk],
    ) -> "V2ChunkRunIdentity":
        plan_sha256 = _plan_fingerprint(chunks)
        return cls(
            run_id=run_id,
            engine_version=engine_version,
            config_sha256=config_sha256,
            dataset_sha256=dataset_sha256,
            chunk_plan_sha256=plan_sha256,
            run_start=_utc(chunks[0].start),
            run_end=_utc(chunks[-1].end),
            chunk_interval=chunk_interval,
            overlap_seconds=overlap_seconds,
        )


class CheckpointFile(StrictModel):
    relative_path: str
    format: Literal["json", "parquet"]
    sha256: Sha256
    byte_size: int = Field(ge=0)
    row_count: int | None = Field(default=None, ge=0)

    @field_validator("relative_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _relative_path(value)

    @model_validator(mode="after")
    def validate_format(self) -> "CheckpointFile":
        if self.format == "parquet" and self.row_count is None:
            raise ValueError("Parquet checkpoint files require row_count")
        if self.format == "json" and self.row_count is not None:
            raise ValueError("JSON checkpoint files cannot declare row_count")
        return self


class V2ChunkCheckpoint(StrictModel):
    checkpoint_version: Literal["v2-chunk-checkpoint/v1"] = (
        "v2-chunk-checkpoint/v1"
    )
    identity_sha256: Sha256
    ordinal: int = Field(ge=0)
    start: datetime
    end: datetime
    input_start: datetime
    next_start: datetime
    state_files: tuple[CheckpointFile, ...]
    output_parts: tuple[CheckpointFile, ...]
    counters: dict[str, int] = Field(default_factory=dict)

    @field_validator("start", "end", "input_start", "next_start")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("counters")
    @classmethod
    def validate_counters(cls, value: dict[str, int]) -> dict[str, int]:
        if any(not key or count < 0 for key, count in value.items()):
            raise ValueError(
                "checkpoint counters require names and non-negative values"
            )
        return FrozenIntMap(sorted(value.items()))

    @model_validator(mode="after")
    def validate_checkpoint(self) -> "V2ChunkCheckpoint":
        if self.end <= self.start or self.input_start > self.start:
            raise ValueError("checkpoint time range is invalid")
        if self.next_start != self.end:
            raise ValueError("next_start must equal the committed core end")
        paths = [
            item.relative_path for item in (*self.state_files, *self.output_parts)
        ]
        if len(paths) != len(set(paths)):
            raise ValueError("checkpoint file paths must be unique")
        return self


class V2WorkspaceManifest(StrictModel):
    workspace_version: Literal["v2-chunk-workspace/v1"] = "v2-chunk-workspace/v1"
    identity: V2ChunkRunIdentity
    identity_sha256: Sha256

    @model_validator(mode="after")
    def validate_fingerprint(self) -> "V2WorkspaceManifest":
        if self.identity_sha256 != self.identity.fingerprint:
            raise ValueError("workspace identity fingerprint does not match")
        return self


class V2ChunkTransaction:
    """Write one chunk privately, then expose it with a single rename."""

    def __init__(
        self,
        workspace: "V2ChunkWorkspace",
        chunk: TimeChunk,
        path: Path,
    ) -> None:
        self.workspace = workspace
        self.chunk = chunk
        self.path = path
        self._state_files: list[CheckpointFile] = []
        self._output_parts: list[CheckpointFile] = []
        self._committed = False

    def _record(
        self, path: Path, *, format: Literal["json", "parquet"]
    ) -> CheckpointFile:
        _sync_file(path)
        rows = (
            int(pq.ParquetFile(path).metadata.num_rows)
            if format == "parquet"
            else None
        )
        return CheckpointFile(
            relative_path=path.relative_to(self.path).as_posix(),
            format=format,
            sha256=sha256_file(path),
            byte_size=path.stat().st_size,
            row_count=rows,
        )

    def write_state_json(self, name: str, payload: Mapping[str, Any]) -> None:
        name = _safe_name(name)
        path = self.path / "state" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical_json_bytes(dict(payload)) + b"\n")
        self._state_files.append(self._record(path, format="json"))

    def write_state_frame(self, name: str, frame: pl.DataFrame) -> None:
        name = _safe_name(name)
        path = self.path / "state" / f"{name}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.write_parquet(path, compression="zstd", statistics=True)
        self._state_files.append(self._record(path, format="parquet"))

    def write_output_frame(self, table: str, frame: pl.DataFrame) -> None:
        table = _safe_name(table)
        path = self.path / "outputs" / f"{table}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.write_parquet(path, compression="zstd", statistics=True)
        self._output_parts.append(self._record(path, format="parquet"))

    def commit(
        self, *, counters: Mapping[str, int] | None = None
    ) -> V2ChunkCheckpoint:
        if self._committed:
            raise V2WorkspaceError("chunk transaction is already committed")
        target = self.workspace.chunks_root / f"chunk-{self.chunk.ordinal:06d}"
        if target.exists():
            raise V2WorkspaceError(f"chunk is already committed: {self.chunk.ordinal}")
        manifest = V2ChunkCheckpoint(
            identity_sha256=self.workspace.identity.fingerprint,
            ordinal=self.chunk.ordinal,
            start=self.chunk.start,
            end=self.chunk.end,
            input_start=self.chunk.input_start,
            next_start=self.chunk.end,
            state_files=tuple(
                sorted(self._state_files, key=lambda item: item.relative_path)
            ),
            output_parts=tuple(
                sorted(self._output_parts, key=lambda item: item.relative_path)
            ),
            counters=dict(counters or {}),
        )
        _atomic_json(self.path / "checkpoint.json", manifest)
        _sync_directory(self.path)
        os.replace(self.path, target)
        _sync_directory(self.workspace.chunks_root)
        self._committed = True
        return manifest


class V2ChunkWorkspace:
    """Durable V2 workspace retained across failures and process restarts."""

    def __init__(self, *, output_root: Path, identity: V2ChunkRunIdentity) -> None:
        resolved = output_root.resolve()
        if resolved in {Path("/"), Path.home().resolve()}:
            raise V2WorkspaceError(f"unsafe output root: {resolved}")
        work_root = resolved / ".work"
        work_root.mkdir(parents=True, exist_ok=True)
        self.identity = identity
        self.path = work_root / f"v2-{identity.fingerprint[:24]}"
        self.path.mkdir(parents=True, exist_ok=True)
        self.chunks_root = self.path / "chunks"
        self.staging_root = self.path / "staging"
        self.chunks_root.mkdir(exist_ok=True)
        self.staging_root.mkdir(exist_ok=True)
        marker = self.path / "workspace.json"
        expected = V2WorkspaceManifest(
            identity=identity,
            identity_sha256=identity.fingerprint,
        )
        if marker.exists():
            try:
                existing = V2WorkspaceManifest.model_validate_json(marker.read_bytes())
            except (OSError, ValueError) as exc:
                raise V2WorkspaceCorruptionError(
                    f"invalid V2 workspace marker: {marker}"
                ) from exc
            if existing != expected:
                raise V2WorkspaceError("workspace identity does not match this run")
        else:
            _atomic_json(marker, expected)
            _sync_directory(self.path)

    def begin(self, chunk: TimeChunk) -> V2ChunkTransaction:
        if chunk.ordinal < 0:
            raise V2WorkspaceError("chunk ordinal cannot be negative")
        target = self.chunks_root / f"chunk-{chunk.ordinal:06d}"
        if target.exists():
            raise V2WorkspaceError(f"chunk is already committed: {chunk.ordinal}")
        path = Path(
            tempfile.mkdtemp(
                prefix=f"chunk-{chunk.ordinal:06d}-",
                dir=self.staging_root,
            )
        )
        return V2ChunkTransaction(self, chunk, path)

    def committed(
        self, plan: Sequence[TimeChunk]
    ) -> tuple[V2ChunkCheckpoint, ...]:
        if (
            _plan_fingerprint(plan) != self.identity.chunk_plan_sha256
            or _utc(plan[0].start) != self.identity.run_start
            or _utc(plan[-1].end) != self.identity.run_end
        ):
            raise V2WorkspaceError("resume plan does not match workspace identity")
        directories = sorted(
            path
            for path in self.chunks_root.iterdir()
            if path.is_dir() and path.name.startswith("chunk-")
        )
        if len(directories) > len(plan):
            raise V2WorkspaceCorruptionError("workspace has more chunks than its plan")
        checkpoints: list[V2ChunkCheckpoint] = []
        for expected_ordinal, directory in enumerate(directories):
            if directory.name != f"chunk-{expected_ordinal:06d}":
                raise V2WorkspaceCorruptionError("committed chunks are not contiguous")
            checkpoint_path = directory / "checkpoint.json"
            try:
                checkpoint = V2ChunkCheckpoint.model_validate_json(
                    checkpoint_path.read_bytes()
                )
            except (OSError, ValueError) as exc:
                raise V2WorkspaceCorruptionError(
                    f"invalid checkpoint: {checkpoint_path}"
                ) from exc
            planned = plan[expected_ordinal]
            if (
                checkpoint.identity_sha256 != self.identity.fingerprint
                or checkpoint.ordinal != planned.ordinal
                or checkpoint.start != _utc(planned.start)
                or checkpoint.end != _utc(planned.end)
                or checkpoint.input_start != _utc(planned.input_start)
            ):
                raise V2WorkspaceCorruptionError(
                    f"checkpoint does not match chunk plan: {expected_ordinal}"
                )
            for item in (*checkpoint.state_files, *checkpoint.output_parts):
                path = (directory / item.relative_path).resolve()
                try:
                    path.relative_to(directory.resolve())
                except ValueError as exc:
                    raise V2WorkspaceCorruptionError(
                        f"checkpoint path escapes chunk directory: {item.relative_path}"
                    ) from exc
                if (
                    not path.is_file()
                    or path.stat().st_size != item.byte_size
                    or sha256_file(path) != item.sha256
                ):
                    raise V2WorkspaceCorruptionError(
                        "checkpoint file failed integrity validation: "
                        f"{item.relative_path}"
                    )
                if item.format == "parquet":
                    rows = int(pq.ParquetFile(path).metadata.num_rows)
                    if rows != item.row_count:
                        raise V2WorkspaceCorruptionError(
                            f"checkpoint row count changed: {item.relative_path}"
                        )
            checkpoints.append(checkpoint)
        return tuple(checkpoints)

    def next_chunk(self, plan: Sequence[TimeChunk]) -> TimeChunk | None:
        completed = self.committed(plan)
        return plan[len(completed)] if len(completed) < len(plan) else None
