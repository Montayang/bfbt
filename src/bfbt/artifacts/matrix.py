"""Immutable, content-verified MatrixResearchRun publication."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from pydantic import Field, field_validator

from bfbt.config.common import StrictModel, as_utc
from bfbt.data.hashing import content_sha256, sha256_file
from bfbt.engine.fast_matrix.result import MatrixResult
from bfbt.engine.fast_matrix.target_schedule import TargetSchedule
from bfbt.reports.locales import write_html_variants
from bfbt.reports.matrix import render_matrix_research_report


class MatrixArtifactError(RuntimeError):
    pass


class MatrixPart(StrictModel):
    path: str
    row_count: int = Field(ge=0)
    byte_size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
            raise ValueError("matrix part path must be safe and relative")
        return path.as_posix()


class MatrixResearchManifest(StrictModel):
    manifest_version: str = "matrix-research-run/v1"
    run_id: str = Field(pattern=r"^fm-[0-9a-f]{24}$")
    created_at: datetime
    result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_schedule_id: str = Field(pattern=r"^target-[0-9a-f]{24}$")
    target_parent_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    market_identity: str = Field(min_length=1)
    resolved_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    backend_decision: dict[str, object]
    research_context: dict[str, object] = Field(default_factory=dict)
    tables: dict[str, MatrixPart]
    files: dict[str, MatrixPart]

    @field_validator("created_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        checked = as_utc(value)
        assert checked is not None
        return checked


class MatrixResearchStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        if self.root in {Path("/"), Path.home().resolve()}:
            raise MatrixArtifactError("unsafe matrix research root")
        self.root.mkdir(parents=True, exist_ok=True)

    def directory(self, run_id: str) -> Path:
        if "/" in run_id or ".." in run_id or not run_id.startswith("fm-"):
            raise MatrixArtifactError("unsafe matrix run identity")
        return self.root / run_id

    def load(self, run_id: str) -> MatrixResearchManifest | None:
        directory = self.directory(run_id)
        if not directory.exists():
            return None
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            raise MatrixArtifactError("matrix run is only partially published")
        manifest = MatrixResearchManifest.model_validate_json(manifest_path.read_text())
        if manifest.run_id != run_id:
            raise MatrixArtifactError("matrix directory identity mismatch")
        self.verify(directory, manifest)
        return manifest

    def verify(self, directory: Path, manifest: MatrixResearchManifest) -> None:
        root = directory.resolve()
        for part in (*manifest.tables.values(), *manifest.files.values()):
            path = (root / part.path).resolve()
            try:
                path.relative_to(root)
            except ValueError as exc:
                raise MatrixArtifactError("matrix table escapes run root") from exc
            if not path.is_file() or path.stat().st_size != part.byte_size or sha256_file(path) != part.sha256:
                raise MatrixArtifactError(f"matrix table is missing or tampered: {part.path}")

    def publish(
        self, result: MatrixResult, schedule: TargetSchedule,
        *, resolved_config: dict[str, object], market_identity: str,
        research_context: dict[str, object] | None = None,
    ) -> MatrixResearchManifest:
        existing = self.load(result.run_id)
        config_hash = content_sha256(resolved_config)
        if existing is not None:
            if existing.result_hash != result.result_hash or existing.resolved_config_sha256 != config_hash:
                raise MatrixArtifactError("matrix run identity collision")
            return existing
        staging = Path(tempfile.mkdtemp(prefix=f".{result.run_id}-", dir=self.root))
        final = self.directory(result.run_id)
        try:
            tables = {
                "returns": result.returns,
                "rebalance_summary": result.rebalance_summary,
                "target_schedule": schedule.frame,
            }
            parts: dict[str, MatrixPart] = {}
            for name, frame in tables.items():
                path = staging / "tables" / f"{name}.parquet"
                path.parent.mkdir(parents=True, exist_ok=True)
                frame.write_parquet(path)
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
                parts[name] = MatrixPart(
                    path=path.relative_to(staging).as_posix(), row_count=frame.height,
                    byte_size=path.stat().st_size, sha256=sha256_file(path),
                )
            config_path = staging / "resolved_config.json"
            config_path.write_text(
                json.dumps(resolved_config, indent=2, sort_keys=True) + "\n"
            )
            metrics, report = render_matrix_research_report(
                result,
                schedule,
                resolved_config=resolved_config,
                market_identity=market_identity,
                research_context=research_context,
            )
            metrics_path = staging / "metrics.json"
            metrics_path.write_text(
                json.dumps(metrics, default=str, indent=2, sort_keys=True) + "\n"
            )
            report_path = staging / "report.html"
            report_paths = write_html_variants(report_path, report)
            files: dict[str, MatrixPart] = {}
            for name, path in {
                "resolved_config": config_path,
                "metrics": metrics_path,
                "report": report_path,
                "report_en": report_paths["en"],
                "report_zh_cn": report_paths["zh-CN"],
            }.items():
                files[name] = MatrixPart(
                    path=path.relative_to(staging).as_posix(), row_count=0,
                    byte_size=path.stat().st_size, sha256=sha256_file(path),
                )
            manifest = MatrixResearchManifest(
                run_id=result.run_id,
                created_at=datetime.now(timezone.utc),
                result_hash=result.result_hash,
                target_schedule_id=schedule.schedule_id,
                target_parent_sha256=schedule.parent_manifest_sha256,
                market_identity=market_identity, resolved_config_sha256=config_hash,
                backend_decision=dict(result.diagnostics["backend_decision"]),
                research_context=dict(research_context or {}),
                tables=parts, files=files,
            )
            (staging / "manifest.json").write_text(json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n")
            staging.replace(final)
            self.verify(final, manifest)
            return manifest
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
