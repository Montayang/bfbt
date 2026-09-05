"""Built-in factor registry and version-pinned computation pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

import polars as pl

from bfbt.config.durations import duration_seconds, is_integer_multiple
from bfbt.config.factor import FactorDefinition
from bfbt.data.hashing import content_sha256
from bfbt.factors.base import FactorError, FactorResult, require_columns
from bfbt.factors.liquidity import quote_volume_raw, taker_buy_ratio_raw
from bfbt.factors.momentum import momentum_raw
from bfbt.factors.open_source_momentum import open_source_momentum_raw
from bfbt.factors.ema import intrabar_ema_ratio_raw
from bfbt.factors.transforms import apply_preprocess
from bfbt.factors.volatility import realized_volatility_raw
from bfbt.factors.illiquidity import amihud_illiquidity_raw
from bfbt.factors.gtja191 import gtja191_raw
from bfbt.factors.sampled_mean import (
    sampled_mean_ratio_inverse_raw,
    sampled_mean_ratio_raw,
)

FACTOR_ENGINE_VERSION = "a07-factor-v1"
RawComputer = Callable[..., pl.LazyFrame]


@dataclass(frozen=True)
class RegisteredFactor:
    name: str
    version: str
    required_columns: tuple[str, ...]
    compute_raw: RawComputer
    display_name_en: str = ""
    display_name_zh: str = ""
    formula: str = ""
    description_zh: str = ""
    stateful: bool = False


def _momentum(frame, definition, *, base_interval):
    return momentum_raw(frame, definition, base_interval=base_interval)


def _reversal(frame, definition, *, base_interval):
    return momentum_raw(
        frame, definition, base_interval=base_interval, reverse=True
    )


_BASE = (
    "open_time",
    "close_time",
    "symbol",
    "interval",
    "close",
    "is_complete",
    "dataset_version",
)


def _gtja(alpha: int):
    def compute(frame, definition, *, base_interval):
        return gtja191_raw(
            frame, definition, base_interval=base_interval, alpha=alpha
        )

    return compute


def _open_source(factor: str):
    def compute(frame, definition, *, base_interval):
        return open_source_momentum_raw(
            frame,
            definition,
            base_interval=base_interval,
            factor=factor,
        )

    return compute


_GTJA_FORMULAS = {
    18: "CLOSE / DELAY(CLOSE,5)",
    20: "(CLOSE-DELAY(CLOSE,6))/DELAY(CLOSE,6)*100",
    24: "SMA(CLOSE-DELAY(CLOSE,5),5,1)",
    31: "(CLOSE-MEAN(CLOSE,12))/MEAN(CLOSE,12)*100",
    40: "SUM(up VOLUME,26)/SUM(non-up VOLUME,26)*100",
    53: "COUNT(CLOSE>DELAY(CLOSE,1),12)/12*100",
    66: "(CLOSE-MEAN(CLOSE,6))/MEAN(CLOSE,6)*100",
    71: "(CLOSE-MEAN(CLOSE,24))/MEAN(CLOSE,24)*100",
    88: "(CLOSE-DELAY(CLOSE,20))/DELAY(CLOSE,20)*100",
    89: "2*(SMA(CLOSE,13,2)-SMA(CLOSE,27,2)-SMA(diff,10,2))",
    112: "(SUM(up change,12)-SUM(abs down change,12))/(sum both)*100",
    151: "SMA(CLOSE-DELAY(CLOSE,20),20,1)",
}


_GTJA_FACTORS = {
    f"gtja_alpha{alpha:03d}": RegisteredFactor(
        name=f"gtja_alpha{alpha:03d}",
        version="v1",
        required_columns=_BASE + (("quote_volume", "volume") if alpha == 40 else ()),
        compute_raw=_gtja(alpha),
        display_name_en=f"GTJA Alpha{alpha}",
        display_name_zh=f"国泰君安 Alpha{alpha}",
        formula=formula,
        description_zh="按原报告的 K 线根数计算；用于截面快速研究。",
    )
    for alpha, formula in _GTJA_FORMULAS.items()
}


_OSS_FACTOR_METADATA = {
    "oss_qlib_beta": (
        "Qlib Normalized Trend Slope",
        "Qlib 归一化趋势斜率",
        "Slope(CLOSE, window_bars) / CLOSE",
        "用含截距线性回归斜率衡量窗口内的归一化价格趋势速度。",
        (),
    ),
    "oss_qlib_signed_rsqr": (
        "Signed Trend R-squared",
        "有方向趋势拟合度",
        "sign(Slope(CLOSE, window_bars)) * R-squared(CLOSE, time)",
        "将线性趋势方向与拟合优度组合，区分平滑上涨和平滑下跌。",
        (),
    ),
    "oss_qlib_rsv": (
        "Qlib Range Position",
        "Qlib 区间位置",
        "(CLOSE-MIN(LOW,n))/(MAX(HIGH,n)-MIN(LOW,n))",
        "衡量当前收盘价在近期最高价与最低价通道中的位置。",
        ("high", "low"),
    ),
    "oss_qlib_imxd": (
        "Qlib Extreme-time Difference",
        "Qlib 高低极值时序差",
        "(IdxMax(HIGH,n)-IdxMin(LOW,n))/n",
        "比较窗口内最早最高价与最早最低价的出现次序。",
        ("high", "low"),
    ),
    "oss_qlib_cntd": (
        "Qlib Direction Persistence",
        "Qlib 方向持续率",
        "MEAN(CLOSE>DELAY(CLOSE,1),n)-MEAN(CLOSE<DELAY(CLOSE,1),n)",
        "衡量上涨 K 线占比与下跌 K 线占比之差。",
        (),
    ),
    "oss_ta_trix": (
        "TRIX",
        "三重指数平滑变化率",
        "ROC(EMA(EMA(EMA(CLOSE,n),n),n),1)*100",
        "衡量三重指数平滑价格的单根变化率。",
        (),
    ),
    "oss_ta_tsi": (
        "True Strength Index",
        "真实强度指数",
        "100*EMA(EMA(DELTA(CLOSE),slow),fast)/EMA(EMA(ABS(DELTA(CLOSE)),slow),fast)",
        "同时平滑价格变化及其绝对值，形成有符号的动量强度。",
        (),
    ),
    "oss_ta_kst": (
        "Know Sure Thing",
        "多周期加权动量",
        "100*SUM(weight_i*SMA(ROC(CLOSE,roc_i),smooth_i), i=1..4)",
        "把四个预先固定的收益周期及平滑周期组合成多尺度动量。",
        (),
    ),
    "oss_ta_kama_distance": (
        "KAMA Distance",
        "KAMA 自适应趋势偏离",
        "CLOSE / KAMA(CLOSE, efficiency_bars, fast_span, slow_span) - 1",
        "衡量价格相对噪声自适应移动平均线的归一化偏离。",
        (),
    ),
    "oss_ta_vortex_diff": (
        "Vortex Difference",
        "Vortex 方向差",
        "SUM(VM+,n)/SUM(TR,n)-SUM(VM-,n)/SUM(TR,n)",
        "使用高低价方向运动与真实波幅衡量正负趋势强度差。",
        ("high", "low"),
    ),
    "oss_ta_vpt_roll": (
        "Rolling Volume-price Trend",
        "有限窗口量价趋势",
        "SUM(VOLUME*RETURN,n)/SUM(VOLUME,n)",
        "将累计 VPT 改为适合跨合约比较的有限窗口成交量加权收益。",
        ("quote_volume", "volume"),
    ),
    "oss_qlib_roc_mom": (
        "Qlib ROC Momentum Adaptation",
        "Qlib ROC 正动量适配",
        "CLOSE / DELAY(CLOSE,window_bars) - 1",
        "Qlib ROC 的正向动量适配，用作与既有 GTJA 动量的重复基准。",
        (),
    ),
    "oss_qlib_sumd": (
        "Qlib Up-down Strength",
        "Qlib 上下行幅度强度",
        "(SUM(UP,n)-SUM(DOWN,n))/SUM(ABS(DELTA(CLOSE)),n)",
        "衡量窗口内上涨幅度与下跌幅度的净强度，主要用于重复性校验。",
        (),
    ),
    "oss_qlib_cord": (
        "Qlib Price-volume Change Correlation",
        "Qlib 价量变化相关",
        "CORR(CLOSE/DELAY(CLOSE,1),LOG(VOLUME/DELAY(VOLUME,1)+1),n)",
        "衡量价格相对变化与成交量相对变化的滚动相关性。",
        ("quote_volume", "volume"),
    ),
}


_OSS_FACTORS = {
    name: RegisteredFactor(
        name=name,
        version="v1",
        required_columns=_BASE + extra_columns,
        compute_raw=_open_source(name),
        display_name_en=display_name_en,
        display_name_zh=display_name_zh,
        formula=formula,
        description_zh=description_zh,
    )
    for name, (
        display_name_en,
        display_name_zh,
        formula,
        description_zh,
        extra_columns,
    ) in _OSS_FACTOR_METADATA.items()
}


FACTOR_REGISTRY = {
    "momentum": RegisteredFactor(
        "momentum",
        "v1",
        _BASE,
        _momentum,
        display_name_en="Momentum",
        display_name_zh="动量因子",
        formula="close(t-skip) / close(t-skip-lookback) - 1",
        description_zh=(
            "衡量合约过去一段时间的价格涨跌幅。值越高表示过去表现越强，"
            "当前截面排序下会优先进入多头候选。"
        ),
    ),
    "reversal": RegisteredFactor(
        "reversal",
        "v1",
        _BASE,
        _reversal,
        display_name_en="Reversal",
        display_name_zh="反转因子",
        formula="-(close(t) / close(t-lookback) - 1)",
        description_zh=(
            "动量收益率取反。值越高表示此前相对更弱，用于研究短期价格反转。"
        ),
    ),
    "intrabar_ema_ratio": RegisteredFactor(
        "intrabar_ema_ratio",
        "v1",
        _BASE,
        intrabar_ema_ratio_raw,
        display_name_en="Intrabar EMA Ratio",
        display_name_zh="盘中 EMA 比值因子",
        formula="EMA(fast, source candle) / EMA(slow, source candle) - 1",
        description_zh=(
            "在较慢 K 线周期上维护已收盘 EMA，并用当前未收盘 K 线的最新价格"
            "每个基础周期更新一次临时比值；同一根慢 K 线不会被重复递归计入。"
        ),
        stateful=True,
    ),
    "sampled_mean_ratio": RegisteredFactor(
        "sampled_mean_ratio",
        "v1",
        _BASE,
        sampled_mean_ratio_raw,
        display_name_en="Sampled Mean Ratio",
        display_name_zh="相位采样均值比正向因子",
        formula=(
            "close(t) / mean(close(t-k*sample_interval), "
            "k=0..sample_count-1) - 1"
        ),
        description_zh=(
            "在基础 K 线上按固定相位间隔抽取价格点。值越高表示当前价格相对"
            "同相位历史均值越强；不会重采样成自然慢周期 K 线。"
        ),
    ),
    "sampled_mean_ratio_inverse": RegisteredFactor(
        "sampled_mean_ratio_inverse",
        "v1",
        _BASE,
        sampled_mean_ratio_inverse_raw,
        display_name_en="Inverse Sampled Mean Ratio",
        display_name_zh="相位采样均值比反向因子",
        formula=(
            "-(close(t) / mean(close(t-k*sample_interval), "
            "k=0..sample_count-1) - 1)"
        ),
        description_zh=(
            "正向相位采样均值比取负。值越高表示当前价格相对同相位历史均值越弱，"
            "用于研究截面反转。"
        ),
    ),
    "realized_volatility": RegisteredFactor(
        "realized_volatility",
        "v1",
        _BASE,
        realized_volatility_raw,
        display_name_en="Realized Volatility",
        display_name_zh="已实现波动率",
        formula="std(log(close(t) / close(t-1)), window)",
        description_zh=(
            "计算滚动窗口内对数收益率的标准差。值越高表示近期价格波动越大。"
        ),
    ),
    "quote_volume": RegisteredFactor(
        "quote_volume",
        "v1",
        _BASE + ("quote_volume",),
        quote_volume_raw,
        display_name_en="Quote Volume",
        display_name_zh="成交额因子",
        formula="sum(quote_volume, window)",
        description_zh=(
            "汇总滚动窗口内以报价资产计量的成交额。值越高表示近期交易越活跃。"
        ),
    ),
    "taker_buy_ratio": RegisteredFactor(
        "taker_buy_ratio",
        "v1",
        _BASE + ("quote_volume", "taker_buy_quote_volume"),
        taker_buy_ratio_raw,
        display_name_en="Taker Buy Ratio",
        display_name_zh="主动买入占比",
        formula="sum(taker_buy_quote_volume, window) / sum(quote_volume, window)",
        description_zh=(
            "衡量滚动窗口内主动买入成交额占总成交额的比例。值越高表示主动买盘占比越高。"
        ),
    ),
    "amihud_illiquidity": RegisteredFactor(
        "amihud_illiquidity",
        "v1",
        _BASE + ("quote_volume",),
        amihud_illiquidity_raw,
        display_name_en="Amihud Illiquidity",
        display_name_zh="Amihud 非流动性因子",
        formula=(
            "mean(abs(log(close(t) / close(t-1))) / quote_volume(t), window) "
            "× 1,000,000"
        ),
        description_zh=(
            "衡量单位成交额引起的价格变化。值越高表示较小成交额也会带来较大价格波动，"
            "即合约相对更不流动。"
        ),
    ),
    **_GTJA_FACTORS,
    **_OSS_FACTORS,
}


def list_factors() -> tuple[RegisteredFactor, ...]:
    return tuple(FACTOR_REGISTRY[name] for name in sorted(FACTOR_REGISTRY))


def compute_factor(
    bars: pl.LazyFrame,
    universe: pl.LazyFrame,
    definition: FactorDefinition,
    *,
    base_interval: str,
    bars_dataset_version: str,
    universe_version: str,
    initial_state: pl.DataFrame | None = None,
    state_start: datetime | None = None,
) -> FactorResult:
    """Compute one factor on version-pinned bars and eligible PIT samples."""

    if not bars_dataset_version or bars_dataset_version.lower() == "latest":
        raise FactorError("bars_dataset_version must be explicit")
    if not universe_version or universe_version.lower() == "latest":
        raise FactorError("universe_version must be explicit")
    try:
        registered = FACTOR_REGISTRY[definition.name]
    except KeyError as exc:
        raise FactorError(f"unknown factor: {definition.name}") from exc
    if definition.version != registered.version:
        raise FactorError(
            f"{definition.name} supports version {registered.version}, "
            f"not {definition.version}"
        )
    if not is_integer_multiple(definition.compute_interval, base_interval):
        raise FactorError("compute_interval must be a multiple of base_interval")
    require_columns(bars, registered.required_columns)
    universe_required = {
        "timestamp",
        "symbol",
        "is_eligible",
        "universe_version",
    }
    missing = universe_required - set(universe.collect_schema().names())
    if missing:
        raise FactorError(f"universe input is missing columns: {sorted(missing)}")
    timestamp_type = pl.Datetime("ms", "UTC")
    prepared = (
        bars.filter(
            (pl.col("interval") == base_interval)
            & (pl.col("dataset_version") == bars_dataset_version)
        )
        .with_columns(
            pl.col("open_time").cast(timestamp_type),
            pl.col("close_time").cast(timestamp_type),
        )
        .sort(["symbol", "open_time"])
    )
    state = None
    if registered.stateful:
        raw, state = registered.compute_raw(
            prepared,
            definition,
            base_interval=base_interval,
            initial_state=initial_state,
            state_start=state_start,
        )
    else:
        if initial_state is not None or state_start is not None:
            raise FactorError("stateless factors do not accept carried state")
        raw = registered.compute_raw(
            prepared, definition, base_interval=base_interval
        )
    raw = raw.filter(
        pl.col("timestamp").cast(pl.Int64)
        % (duration_seconds(definition.compute_interval) * 1_000)
        == 0
    )
    eligible = (
        universe.filter(
            pl.col("is_eligible")
            & (pl.col("universe_version") == universe_version)
        )
        .filter(
            pl.col("timestamp").cast(pl.Int64)
            % (duration_seconds(definition.compute_interval) * 1_000)
            == 0
        )
        .select(pl.col("timestamp").cast(timestamp_type), "symbol")
    )
    values = (
        eligible.join(raw, on=["timestamp", "symbol"], how="left")
        .with_columns(
            (
                pl.col("raw_value").is_not_null()
                & pl.col("raw_value").is_finite()
            ).alias("is_valid"),
            pl.when(pl.col("raw_value").is_null())
            .then(pl.lit("INSUFFICIENT_OR_GAPPED_HISTORY"))
            .when(~pl.col("raw_value").is_finite())
            .then(pl.lit("NON_FINITE"))
            .otherwise(None)
            .alias("invalid_reason"),
            pl.col("raw_value").alias("value"),
        )
    )
    values = apply_preprocess(values, definition.preprocess)
    identity = {
        "engine": FACTOR_ENGINE_VERSION,
        "definition": definition.model_dump(mode="json"),
        "bars_dataset_version": bars_dataset_version,
        "universe_version": universe_version,
        "base_interval": base_interval,
    }
    version = f"{definition.version}-{content_sha256(identity)[:24]}"
    output = (
        values.with_columns(
            pl.lit(definition.name).alias("factor_name"),
            pl.lit(version).alias("factor_version"),
            pl.lit(universe_version).alias("universe_version"),
            pl.lit(bars_dataset_version).alias("dataset_version"),
        )
        .select(
            "timestamp",
            "symbol",
            "factor_name",
            "factor_version",
            "raw_value",
            "value",
            "is_valid",
            "invalid_reason",
            "universe_version",
            "dataset_version",
        )
        .sort(["timestamp", "symbol"])
    )
    return FactorResult(
        frame=output,
        factor_name=definition.name,
        factor_version=version,
        bars_dataset_version=bars_dataset_version,
        universe_version=universe_version,
        base_interval=base_interval,
        state=state,
    )
