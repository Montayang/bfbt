"""Versioned contracts for the bounded Agent/showcase workflow."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from bianbt.config.common import StrictModel, as_utc
from bianbt.data.hashing import sha256_bytes


Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
SafeId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")]
ActionClass = Literal[
    "read_only_inspection",
    "derived_report_write",
    "test_execution",
    "network_access",
    "data_download",
    "research_execution",
    "formal_event_execution",
    "source_control_change",
]


class ShowcaseContractError(ValueError):
    """A showcase or intent contract cannot be loaded safely."""


class MarketIntent(StrictModel):
    exchange: Literal["binance"] = "binance"
    segment: Literal["usd_m_futures"] = "usd_m_futures"
    contract_type: Literal["perpetual"] = "perpetual"
    quote_asset: Literal["USDT"] = "USDT"


class PeriodIntent(StrictModel):
    label: str = Field(min_length=1, max_length=80)
    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        normalized = as_utc(value)
        assert normalized is not None
        return normalized

    @model_validator(mode="after")
    def validate_interval(self) -> "PeriodIntent":
        if self.end <= self.start:
            raise ValueError("period end must be after start")
        return self


class FactorIntent(StrictModel):
    name: SafeId
    version: str = Field(min_length=1, max_length=120)
    direction: Literal["positive", "negative"]
    parameters: dict[str, str | int | float | bool] = Field(default_factory=dict)


class ExecutionSemantics(StrictModel):
    universe: str = Field(min_length=1)
    rank_rule: str = Field(min_length=1)
    decision_clock: str = Field(min_length=1)
    rebalance_clock: str = Field(min_length=1)
    fill_timing: str = Field(min_length=1)
    sizing: str = Field(min_length=1)
    costs: str = Field(min_length=1)
    risk_exits: str = Field(min_length=1)
    terminal_handling: str = Field(min_length=1)


class ResearchIntent(StrictModel):
    intent_version: Literal["research-intent/showcase-v1"] = (
        "research-intent/showcase-v1"
    )
    operation: Literal[
        "factor_diagnostic",
        "portfolio_research",
        "formal_backtest",
        "result_query",
    ]
    user_text: str = Field(min_length=1, max_length=4_000)
    user_text_sha256: Sha256
    market: MarketIntent
    periods: tuple[PeriodIntent, ...] = Field(min_length=1)
    factor: FactorIntent
    semantics: ExecutionSemantics
    unresolved_ambiguities: tuple[str, ...] = ()
    user_decisions: tuple[str, ...] = ()
    requested_outputs: tuple[str, ...] = Field(min_length=1)
    required_actions: tuple[ActionClass, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_user_text_identity(self) -> "ResearchIntent":
        actual = sha256_bytes(self.user_text.encode("utf-8"))
        if actual != self.user_text_sha256:
            raise ValueError("user_text_sha256 does not match user_text")
        if len(set(self.required_actions)) != len(self.required_actions):
            raise ValueError("required_actions must be unique")
        return self

    @property
    def executable(self) -> bool:
        return not self.unresolved_ambiguities


class ShowcaseRunReference(StrictModel):
    run_id: SafeId
    label: str = Field(min_length=1, max_length=80)
    period_label: str = Field(min_length=1, max_length=80)


class ShowcaseSpec(StrictModel):
    showcase_version: Literal["bianbt-showcase/v1"] = "bianbt-showcase/v1"
    showcase_id: SafeId
    title: str = Field(min_length=1, max_length=160)
    subtitle: str = Field(min_length=1, max_length=240)
    strategy_identity: str = Field(min_length=1, max_length=160)
    intent: ResearchIntent
    runs: tuple[ShowcaseRunReference, ...] = Field(min_length=1)
    narrative: tuple[str, ...] = Field(min_length=1)
    disclosures: tuple[str, ...] = Field(min_length=1)
    catalog_path: str | None = None

    @field_validator("catalog_path")
    @classmethod
    def validate_catalog_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("catalog_path must be a safe project-relative path")
        return path.as_posix()

    @model_validator(mode="after")
    def validate_references(self) -> "ShowcaseSpec":
        run_ids = [item.run_id for item in self.runs]
        if len(run_ids) != len(set(run_ids)):
            raise ValueError("showcase run IDs must be unique")
        period_labels = {item.label for item in self.intent.periods}
        missing = sorted(
            {item.period_label for item in self.runs} - period_labels
        )
        if missing:
            raise ValueError(f"run references unknown period labels: {missing}")
        return self


def load_showcase_spec(path: Path) -> ShowcaseSpec:
    """Load one strict JSON showcase contract."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ShowcaseContractError(f"cannot load showcase spec {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ShowcaseContractError("showcase spec must contain a JSON object")
    try:
        return ShowcaseSpec.model_validate(payload)
    except ValueError as exc:
        raise ShowcaseContractError(f"invalid showcase spec: {exc}") from exc
