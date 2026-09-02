# 以现有 Amihud 实现为模板新增截面因子

[English](custom_factor_tutorial.md)

这份教程以仓库已经内置的 `amihud_illiquidity` 为完整参考，演示新因子需要具备的计算、
注册、测试、配置和真实小样本回测链路。当前仓库不要重复添加或重复注册 Amihud；开发自己的
新公式时，应换用新的稳定因子名、实现文件和测试。过程不会连接交易账户，也不会真实下单。

如果 `data/backtest/datasets/tutorial` 还不存在，请先完成 [`beginner_tutorial.zh-CN.md`](beginner_tutorial.zh-CN.md)。如果只是修改内置因子的窗口或预处理，只需编辑因子配置；本教程针对需要新公式的情况。

## 1. 示例因子

参考实现是 Amihud 风格的滚动非流动性因子：

```text
单根非流动性 = abs(log(close_t / close_t-1)) / quote_volume_t
因子值 = 最近 N 根单根非流动性的平均值
```

数值越大，表示同样成交额伴随越大的价格变化，即相对更不流动。公式只使用教程数据已有的 `close`、`quote_volume`、时间和完整性字段，不需要重新下载数据。

本例使用 24 小时窗口，每小时产生一次截面因子，回测仍按原教程每 4 小时调仓。

## 2. 进入环境

```bash
cd /path/to/bfbt
source .venv/bin/activate
bfbt research list-factors
```

当前列表应包含 `amihud_illiquidity` 和版本 `v1`。如果缺失，先确认分支和安装环境，不要按
本文盲目重复注册。

## 3. 阅读计算函数

现有实现位于 `src/bfbt/factors/illiquidity.py`。以下代码展示新滚动因子必须处理的时点、
连续窗口、完整 K 线和有限值边界；新增自己的因子时复制结构而不是覆盖该文件：

```python
"""Amihud-style rolling illiquidity factor."""

from __future__ import annotations

import polars as pl

from bfbt.config.durations import duration_seconds
from bfbt.config.factor import FactorDefinition
from bfbt.factors.base import FactorError


def amihud_illiquidity_raw(
    bars: pl.LazyFrame,
    definition: FactorDefinition,
    *,
    base_interval: str,
) -> pl.LazyFrame:
    value = definition.parameters.get("window")
    if not isinstance(value, str):
        raise FactorError("window must be a duration string")

    seconds = duration_seconds(value)
    base_seconds = duration_seconds(base_interval)
    if seconds % base_seconds:
        raise FactorError("window must be a multiple of base_interval")
    window = seconds // base_seconds

    log_return = (
        pl.col("close").log()
        - pl.col("close").shift(1).over("symbol").log()
    )
    first_time = pl.col("open_time").shift(window).over("symbol")
    contiguous = (
        pl.col("open_time").cast(pl.Int64)
        - first_time.cast(pl.Int64)
        == window * base_seconds * 1_000
    )

    return (
        bars.with_columns(log_return.alias("_log_return"))
        .with_columns(
            pl.when((pl.col("close") > 0) & (pl.col("quote_volume") > 0))
            .then(
                pl.col("_log_return").abs()
                / pl.col("quote_volume")
                * 1_000_000
            )
            .otherwise(None)
            .alias("_amihud")
        )
        .with_columns(
            pl.when(
                contiguous
                & (
                    pl.col("is_complete")
                    .cast(pl.Int64)
                    .rolling_sum(window + 1)
                    .over("symbol")
                    == window + 1
                )
                & (
                    pl.col("_amihud")
                    .is_finite()
                    .fill_null(False)
                    .cast(pl.Int64)
                    .rolling_sum(window)
                    .over("symbol")
                    == window
                )
            )
            .then(pl.col("_amihud").rolling_mean(window).over("symbol"))
            .otherwise(None)
            .alias("raw_value")
        )
        .select(
            pl.col("close_time").alias("timestamp"),
            "symbol",
            "raw_value",
        )
    )
```

乘以 `1_000_000` 只为让原始数值容易阅读，不改变截面排序。计算函数必须返回 `timestamp`、`symbol`、`raw_value`。滚动计算必须按 `symbol` 分组，并拒绝缺口、不完整 K 线和非有限值。

## 4. 阅读注册信息和数据依赖

当前 `src/bfbt/factors/registry.py` 已包含导入：

```python
from bfbt.factors.illiquidity import amihud_illiquidity_raw
```

并在 `FACTOR_REGISTRY` 中注册：

```python
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
        "衡量单位成交额引起的价格变化。值越高表示较小成交额也会带来"
        "较大价格波动，即合约相对更不流动。"
    ),
),
```

`required_columns` 必须声明公式额外使用的行情列。`display_name_en`、
`display_name_zh`、`formula` 和 `description_zh` 会直接显示在 HTML 回测报告中；新增
因子时应一起填写，避免报告只有内部变量名。只创建计算文件而不注册，运行时会报
`unknown factor`。

检查现有注册：

```bash
bfbt research list-factors
```

输出中应出现 `amihud_illiquidity` 和版本 `v1`。

## 5. 参考离线公式测试

现有 `tests/acceptance/test_acceptance_07_factors_research.py` 已覆盖注册集合和公式。新因子应
仿照其中的 Amihud 用例，增加自己需要的 import、注册名称和期望值，例如现有用例使用：

```python
import math
```

注册集合包含：

```python
"amihud_illiquidity",
```

公式参数包含：

```python
(
    "amihud_illiquidity",
    {"window": "2m"},
    math.log(1.1) / 10 * 1_000_000,
),
```

执行：

```bash
pytest tests/acceptance/test_acceptance_07_factors_research.py -q
```

这项测试只使用仓库内固定数据，不联网、不修改教程数据。正式扩展时还应为历史不足、时间缺口、不完整 K 线和未来数据不影响历史值分别增加用例。

## 6. 创建独立因子配置

保留原来的 `factor.json`，新建 `data/backtest/workspaces/tutorial/configs/factor-amihud.json`：

```json
{
  "factors": [
    {
      "name": "amihud_illiquidity",
      "version": "v1",
      "parameters": {
        "window": "24h"
      },
      "compute_interval": "1h",
      "preprocess": [
        {
          "name": "rank"
        }
      ]
    }
  ],
  "labels": [
    {
      "name": "forward_return_4h",
      "signal_delay_bars": 1,
      "horizon": "4h",
      "entry_field": "open",
      "exit_field": "open"
    }
  ],
  "cache": {
    "enabled": true
  }
}
```

参数名使用 `window` 很重要：正式运行规划器会据此加载回测开始前所需的 24 小时历史。若自定义其他持续时间参数名，需要同步扩展运行规划器。

## 7. 在刚才的数据集上运行

先设置第四步准备器实际输出的版本。以下是本次 2026-06 教程数据；如果重新准备过，必须使用自己终端里的值：

```bash
DATA_ROOT=data/backtest
DATASET_ROOT="$DATA_ROOT/datasets/tutorial"
DB="$DATA_ROOT/catalogs/tutorial.duckdb"
CONFIG_ROOT="$DATA_ROOT/workspaces/tutorial/configs"
LOG_ROOT="$DATA_ROOT/workspaces/tutorial/logs"
RUN_ROOT="$DATA_ROOT/runs"
DATASET_ID="binance-usdm-real-e2e-smoke-2026-06"
DATASET_VERSION="live-smoke-a345bf75422a6bad1f333017"
```

运行：

```bash
bfbt run \
  "$DATASET_ID" \
  "$DATASET_VERSION" \
  amihud_illiquidity \
  --database "$DB" \
  --data-config "$CONFIG_ROOT/data.json" \
  --universe-config "$CONFIG_ROOT/universe.json" \
  --factor-config "$CONFIG_ROOT/factor-amihud.json" \
  --backtest-config "$CONFIG_ROOT/backtest.json" \
  --verify-hashes \
  | tee "$LOG_ROOT/run-amihud.log"
```

成功时会显示 `status=succeeded` 和新的 `run_id=a09-...`。填写并检查结果：

```bash
RUN_ID="a09-替换成实际值"
python tests/live/validate_real_backtest_smoke.py \
  "$RUN_ROOT/$RUN_ID"
echo "$RUN_ROOT/$RUN_ID/report.html"
```

新增自己的因子源码会改变 source fingerprint，所以新 run ID 与既有回测不同是正常现象。
代码提交前后环境指纹也可能不同，不要依赖旧 run ID。

## 8. 理解多空方向

`rank` 会让原始值最高的合约得到最高分。当前教程组合做多最高分的两个合约，做空最低分的两个合约，因此本例测试的是：

```text
做多相对不流动的合约，做空相对流动的合约
```

如果假设相反，希望高分代表流动性更好，不应静默修改已经发布的
`amihud_illiquidity:v1`。应以新名称和版本注册反向定义，例如新计算函数中取负：

```python
.then(-pl.col("_amihud").rolling_mean(window).over("symbol"))
```

注册新定义后应先重跑离线公式测试，再执行正式回测。

## 9. 换成自己的公式

接入任意新因子都遵循同一流程：

1. 明确公式、时间窗口、数值方向和行情字段。
2. 在 `src/bfbt/factors/` 实现只使用当前及历史数据的 `raw_value`。
3. 在 registry 声明名称、版本、依赖列和计算函数。
4. 添加公式、缺口、完整性和防未来数据测试。
5. 创建独立因子配置，保留原始基准配置。
6. 先跑小型离线验收，再复用真实教程数据正式运行。
7. 检查因子值、成交、成本、指标和 HTML 报告，不要只看总收益。

字段含义参见 [`../reference/data_contract.md`](../reference/data_contract.md)，全部因子配置参见 [`../reference/configuration.md`](../reference/configuration.md)。
