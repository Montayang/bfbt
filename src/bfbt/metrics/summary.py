"""Combined metrics result and deterministic JSON representation."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import polars as pl

from bfbt.metrics.attribution import ReturnAttribution, compute_return_attribution
from bfbt.metrics.performance import PerformanceMetrics, compute_performance_metrics
from bfbt.metrics.risk import RiskMetrics, compute_risk_metrics

METRICS_VERSION = "a09-metrics-v1"


@dataclass(frozen=True)
class RunMetrics:
    metrics_version: str
    performance: PerformanceMetrics
    risk: RiskMetrics
    attribution: ReturnAttribution

    def to_dict(self) -> dict[str, Any]:
        return _json_value(asdict(self))

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"


def _json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    return value


def compute_run_metrics(
    returns: pl.LazyFrame,
    *,
    base_interval: str,
) -> RunMetrics:
    attribution = compute_return_attribution(returns)
    performance = compute_performance_metrics(
        returns, base_interval=base_interval
    )
    risk = compute_risk_metrics(returns)
    return RunMetrics(
        metrics_version=METRICS_VERSION,
        performance=performance,
        risk=risk,
        attribution=attribution,
    )
