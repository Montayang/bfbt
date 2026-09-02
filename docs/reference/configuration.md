# 配置模型设计

本文描述当前配置模型契约。A01 建立四类不可变配置、YAML 加载、路径解析、run-ready 校验和配置指纹；A09/A10 已把同一规范配置用于正式 artifact 与分块执行。

## 1. 配置分层

```text
data.yaml       数据源、数据集、时间范围、本地存储和质量规则
universe.yaml   point-in-time 合约池和过滤条件
factor.yaml     因子、参数、预处理和标签
backtest.yaml   组合、执行、成本、风险和输出
```

每次运行先加载配置，再由 Pydantic 解析为不可变模型。可通过 `bfbt config show` 查看完全展开结果；正式 run artifact 将同一结果保存为 `resolved_config.json`。

优先级从低到高：

```text
代码默认值 < YAML < 白名单环境路径覆盖
```

第一版不使用实盘根目录 `.env`。A01 只允许 `BIANBT_DATA_ROOT` 和 `BIANBT_OUTPUT_ROOT`；其他 `BIANBT_` 变量会报错。未来新增非策略覆盖时必须显式加入白名单和验收。

配置中的相对路径统一相对项目仓库根解析，不相对当前 shell 工作目录，也不相对单个 YAML 文件。这样现有 `data/backtest/datasets/default` 和 `data/backtest/runs` 会稳定指向仓库内被 Git 忽略的数据与产物目录。

## 2. DataConfig

目标结构：

```yaml
market:
  venue: binance
  segment: usd_m_futures
  contract_type: perpetual
  quote_asset: USDT
  margin_asset: USDT

datasets:
  bars:
    enabled: true
    base_interval: 1m
  mark_bars:
    enabled: true
    base_interval: 1m
  funding:
    enabled: true
  contracts:
    enabled: true
  index_bars:
    enabled: false

source:
  primary: binance_public_archive
  incremental: binance_rest
  allow_authenticated_endpoints: false
  request_timeout_seconds: 20
  max_retries: 4
  max_concurrency: 8

time:
  timezone: UTC
  start: 2024-01-01T00:00:00Z
  end: 2025-01-01T00:00:00Z
  range_semantics: left_closed_right_open

storage:
  root: data/backtest/datasets/default
  format: parquet
  compression: zstd
  partition_granularity: month
  target_file_size_mib: 256
  row_group_rows: 262144

validation:
  reject_duplicate_keys: true
  reject_bad_ohlc: true
  reject_non_positive_prices: true
  report_missing_bars: true
  verify_archive_checksums: true
  max_partition_missing_ratio: 0.01
```

约束：

- `timezone` 第一版只能为 UTC。
- `end` 不包含在范围内。
- `derived_intervals` 必须是 base interval 的整数倍。
- 公开回测数据不允许打开 authenticated endpoints。
- storage 路径必须解析到回测数据根目录，不能指向仓库根、HOME 或 `/`。

## 3. UniverseConfig

```yaml
schedule:
  interval: 1h

market:
  contract_type: perpetual
  quote_asset: USDT
  margin_asset: USDT

point_in_time:
  enabled: true
  use_contract_snapshots: true
  use_first_last_valid_bar: true

filters:
  trading_status_only: true
  min_listing_age_days: 30
  min_history_bars: 1440
  rolling_quote_volume:
    window: 24h
    minimum: null
  max_missing_ratio:
    window: 24h
    maximum: 0.01
  exclude_symbols: []

output:
  save_reason_codes: true
```

`minimum: null` 表示关闭过滤，而不是阈值为 0。

A06 运行时进一步要求 rolling 成交额窗口和缺失率窗口都是基础 K 线周期的整数倍；schedule 起点必须对齐其 UTC 周期。CLI 要求显式提供 `history_start`，不会在窗口历史不足时自动联网补数据。

## 4. FactorConfig

实现阶段新增 `configs/factor.yaml`：

```yaml
factors:
  - name: momentum
    version: v1
    parameters:
      lookback: 24h
      skip_recent: 1h
    compute_interval: 1h
    preprocess:
      - name: winsorize
        method: quantile
        lower: 0.01
        upper: 0.99
      - name: zscore
        cross_sectional: true

labels:
  - name: forward_return_4h
    signal_delay_bars: 1
    horizon: 4h
    entry_field: open
    exit_field: open

cache:
  enabled: true
```

约束：

- factor version 必须显式填写。
- lookback 必须能转换为 base interval 的 bar 数。
- preprocess 顺序影响结果，属于版本指纹的一部分。
- 标签不允许进入 factor 的 required columns。

A07 registry 只接受配置中与内建实现完全匹配的 `name + version`。`compute_interval`、lookback/window 和 label horizon 必须是基础 K 线周期的整数倍。`history_start` 与 `future_end` 是运行边界而非自动推导的下载请求：前者覆盖最大回看，后者覆盖 signal delay + horizon。

## 5. BacktestConfig

```yaml
config_version: v1

run:
  name: momentum_ls_4h
  start: 2024-01-01T00:00:00Z
  end: 2025-01-01T00:00:00Z
  dataset_version: explicit-version
  random_seed: 42

schedule:
  factor_interval: 1h
  rebalance_interval: 4h
  signal_delay_bars: 1

portfolio:
  construction: long_short_quantile
  long_quantile: 0.2
  short_quantile: 0.2
  long_count: null
  short_count: null
  weighting: equal
  gross_exposure: 1.0
  net_exposure: 0.0
  max_symbol_weight: 0.05
  max_turnover: null

execution:
  fill_price: next_bar_open
  partial_fill: false
  fee:
    model: fixed_bps
    taker_bps: null
    maker_bps: null
  slippage:
    model: fixed_bps
    bps: null
  funding:
    enabled: true
    missing_policy: error

valuation:
  price: mark_close

risk:
  leverage: 1.0
  enforce_liquidation: false

performance:
  mode: chunked
  chunk_interval: 2d
  max_input_rows_per_chunk: 5000000
  max_incremental_rss_mib: 2048
  collect_diagnostics: true

output:
  root: data/backtest/runs
  save_factor_values: true
  save_universe: true
  save_positions: true
  save_trades: true
  save_costs: true
  render_html: true
```

约束：

- `dataset_version` 在正式 run 中必须显式解析，不能用浮动的 `latest` 写入 manifest。
- `long_quantile + short_quantile <= 1`。
- `construction=long_short_count` 时必须同时填写 `long_count` 和 `short_count`；quantile 模式禁止填写 count。
- 市场中性组合要求净敞口可实现。
- `next_bar_open` 要求至少一根 signal delay。
- fee/slippage 为 null 时不得静默按 0 运行；必须由用户显式给值或选择 zero model。
- funding 缺失策略必须明确为 `error`、`exclude_symbol` 或 `assume_zero`，正式研究默认 `error`。

`performance.mode=in_memory` 使用 A08 单次有状态账本，适合小样本基准；
`chunked` 使用左闭右开的时间块、历史 overlap、临时 Parquet spool 和跨块账本。
`chunk_interval` 必须是基础 K 线、universe schedule、factor interval 和 rebalance
interval 的整数倍。每块所有行情输入的总行数不得超过
`max_input_rows_per_chunk`；相对进程启动时 high-water RSS 的增量不得超过
`max_incremental_rss_mib`。`collect_diagnostics=true` 时成功产物增加确定性的
`performance.json`。配置改变会改变 resolved config hash 和正式 run ID。

## 6. 配置输出与 hash

运行前生成规范化配置：

- 时间统一 ISO 8601 UTC。
- 路径转为规范绝对路径，但 hash 时使用相对数据根的稳定表示。
- 字典键排序。
- duration 保留无歧义原始表达并验证为 base interval 的整数倍；等价 bar 数将在数据 manifest 阶段一并固化。
- 默认值完全展开。

规范化结果计算 `resolved_config_hash`，写入 run manifest。CLI 输出的临时覆盖也必须出现在 resolved config 中。

## 7. 校验错误

配置错误在任何数据扫描或网络请求前失败，并给出字段路径，例如：

```text
portfolio.long_quantile: must be in (0, 0.5]
execution.fee.taker_bps: required when fee.model=fixed_bps
run.end: must be greater than run.start
datasets.mark_bars: required when valuation.price=mark_close
```

## 8. A12 第二版配置契约

`backtest.yaml` 现在具有显式版本：

- 缺少 `config_version` 或填写 `v1`：使用第一版原有字段和默认值。
- 填写 `v2`：严格使用第二版 selection、sizing、capital、risk 和 performance
  模型，不能混用第一版扁平 portfolio 字段。

可审阅示例为 `configs/backtest_v2.example.yaml`。A18 已开放 V2 的正式事件循环，A20
已支持 `performance.mode=chunked`；`bfbt run` 会发布 run/v2 manifest、Rank、仓位
指令、风险事件、带引用成交和交互报告。

V2 关键校验包括：

- 多空 Rank 集合不得重叠，Rank 和闭区间只接受正整数，至少选择一侧。
- `selection.lag <= performance.max_rank_lag`。
- `max_position_state_rows` 和 `max_pending_instructions` 分别限制 A15 当前持仓
  状态与单批待处理策略指令；超限必须在状态突变前失败。
- `max_risk_state_rows` 和 `max_pending_risk_intents` 分别限制 A16 活跃持仓/冷却
  状态与下一开盘待成交风险意图。
- `max_process_rss_mib` 是 V2 worker 的进程绝对 RSS 上限。A20 起
  `performance.mode=chunked` 必须显式填写；整机 6 GiB 建议 5120，当前
  7.7 GiB 服务器可使用 5632。
- `resume_policy=resume` 允许身份完全一致时从最后一个原子检查点继续；
  `error_if_exists` 要求已有 committed 或 staging workspace 时失败。A20 已接入
  正式 V2 chunked 执行。
- 五种 sizing 只能填写各自字段；增量模式必须填写反向政策。
- position fraction 必须声明零仓位政策，bootstrap 必须声明名义金额。
- 初始净值为正，成本缓冲小于初始净值。
- risk evaluation interval 是基础 K 线的整数倍，chunk interval 是其整数倍。
- mark 触发必须启用 mark bars。
- 止盈止损 action 为 reduce fraction 时必须给出合法比例。
- 单合约风险线可使用统一 distance，或同时填写 long distance 和 short distance。

第二版使用 `simple_cross` 资金模型；A18 已将风险状态、next-open 意图和事件仲裁接入
正式 V2 in-memory 全链路，A20 增加了相同经济内核的可恢复 chunked 执行。完整
大表的流式 V2 发布仍以 A21 验收状态为准。
组合级配置同时预留 stop loss、take profit 和 max drawdown。完整字段及语义见
`docs/design/v2_design.md`；正式执行能力以各阶段验收状态为准。

## 9. A31 执行后端配置

V2 可选增加：

```yaml
engine:
  backend: auto          # auto | fast_matrix | event
  purpose: research      # research | formal
  equivalence_audit: false
```

缺少 `engine` 时保持历史行为：V2 仍走 Event，且该缺省不进入序列化或旧 fingerprint。
`fast_matrix` 只允许研究用途和能力规划器确认支持的目标权重语义，发布 `fm-*`；正式运行必须
使用 `event/formal`。`auto` 遇到风险事件、动态 sizing 或状态型约束时记录稳定 reason code
并选择 Event；显式 `fast_matrix` 对同样配置直接失败。
