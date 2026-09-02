# Data contract

## K 线主键

标准化 K 线使用 `(open_time, symbol, interval)` 作为唯一键，时间统一为 UTC。

建议字段：

```text
open_time, close_time, symbol, interval,
open, high, low, close,
volume, quote_volume, trades,
taker_buy_volume, taker_buy_quote_volume,
source, ingested_at
```

价格和数量在原始层保留原始字符串；标准化层使用明确的数值类型，并在元数据中记录转换规则。

## 截面因子表

因子输出使用长表：

```text
timestamp, symbol, factor_name, value, is_valid
```

因子值只能使用 `timestamp` 当时已经可见的数据。未来收益标签必须单独保存，不能混入因子输入。

## 合约元数据

至少记录：

```text
symbol, contract_type, quote_asset, margin_asset,
onboard_time, observed_first_bar, observed_last_bar,
status, price_tick, quantity_step, snapshot_time
```

元数据需要保留历史快照，不能只覆盖成当前状态。

## 数据质量报告

每次导入至少输出重复主键、缺失 K 线、非正价格、OHLC 关系异常和文件校验结果。

A05 固化 `quality/v1`：报告包含确定性 `report_id`、数据集/版本、行数和 symbol 数、重复键、必填空值、非有限数、OHLC、非正值、负值/不可能数量、bar 周期与缺口指标、来源对象、错误码和评估时间。报告 ID 不包含评估时间，因此相同数据和策略重复评估保持同一身份。失败报告允许留存审计，但禁止生成正式 Parquet、Partition manifest 或 Catalog Partition 记录。

## 有界标准化 release

年度全市场数据不得作为单个 Python batch 物化。标准化前先用全部目标
Raw object ID 和 SHA-256、schema fingerprint、normalizer 代码版本与参数构造
`NormalizationRelease`；release 的 `dataset_version` 对完整来源集合负责。后续多个
按月、按 symbol 拆分的有界批次共享该版本，但每个批次的来源必须是 release 来源
集合的子集，禁止额外 Raw 注入。

Partition manifest 继续记录本批的精确来源，DatasetSnapshot 则引用同一 release
版本下所有通过质量门的 partitions。这样既保持版本可复现，也不要求把全年长表
一次放入内存。

## 完整数据集清单

第一版至少包含四个标准化事实数据集和五类派生/结果数据集。

| 数据集 | 用途 | 主键 |
| --- | --- | --- |
| `bars` | 成交价格、量价因子和成交模拟 | `(open_time, symbol, interval)` |
| `mark_bars` | 标记价格估值和风险分析 | `(open_time, symbol, interval)` |
| `funding` | 永续资金费率现金流 | `(funding_time, symbol)` |
| `contracts` | 历史合约状态和交易规则 | `(snapshot_time, symbol)` |
| `universe` | 每个研究时点的可交易资格 | `(timestamp, symbol, universe_version)` |
| `factor_values` | 截面因子值 | `(timestamp, symbol, factor_name, factor_version)` |
| `forward_returns` | 与因子分离的未来收益标签 | `(timestamp, symbol, label_name, label_version)` |
| `positions` | 目标/实际持仓 | `(timestamp, symbol, run_id)` |
| `trades` | 调仓成交流水 | `(fill_time, symbol, sequence, run_id)` |

## Bars schema

标准化 `bars` 使用以下 Arrow 逻辑类型：

| 字段 | 类型 | 可空 | 语义 |
| --- | --- | --- | --- |
| `open_time` | `timestamp[ms, UTC]` | 否 | bar 左边界 |
| `close_time` | `timestamp[ms, UTC]` | 否 | bar 结束和最早可见时间 |
| `symbol` | `string` | 否 | Binance 原始合约代码，允许 Unicode |
| `interval` | `string/dictionary` | 否 | `1m`、`5m` 等 |
| `open/high/low/close` | `float64` | 否 | 成交价格 |
| `volume` | `float64` | 否 | base asset 成交量 |
| `quote_volume` | `float64` | 否 | quote asset 成交额 |
| `trades` | `int64` | 否 | 成交笔数 |
| `taker_buy_volume` | `float64` | 否 | 主动买入 base volume |
| `taker_buy_quote_volume` | `float64` | 否 | 主动买入 quote volume |
| `is_complete` | `bool` | 否 | 源 bar 是否完整 |
| `source` | `string/dictionary` | 否 | archive 或 REST |
| `source_object_id` | `string` | 否 | Raw manifest 引用 |
| `dataset_version` | `string` | 否 | 内容版本 |

Raw 层保留原始数字字符串；Normalized 层统一为 float64 以便批量计算。若将来需要逐笔精确结算，再单独引入 decimal 表，不在主面板中混用。

A04/A05 不假设 symbol 只含 ASCII。真实 Binance USD-M 市场存在
`币安人生USDT`；下载边界允许 Unicode 字母/数字/下划线，同时拒绝路径和 URI
标点，Raw 路径仍经过根目录逃逸检查。

A05 将 Binance K 线源中“包含式最后毫秒”的 close time 加 `1ms`，转换成系统统一的右开可用边界。例如 `00:00:59.999` 标准化为 `00:01:00.000 UTC`。标准化不修复坏行，错误由质量门显式拦截。

## Mark bars schema

`mark_bars` 的时间、symbol、interval、OHLC、source 和版本字段与 `bars` 相同。标记价格 K 线没有成交量语义，因此不得用 0 冒充 volume；这些列不出现在 schema 中。

## Funding schema

| 字段 | 类型 | 可空 | 语义 |
| --- | --- | --- | --- |
| `funding_time` | `timestamp[ms, UTC]` | 否 | 实际结算时点 |
| `symbol` | `string` | 否 | 合约代码 |
| `funding_rate` | `float64` | 否 | 正值表示多头支付空头 |
| `mark_price` | `float64` | 是 | 官方返回的结算关联标记价格 |
| `funding_interval_hours` | `float64` | 是 | 当时已知的结算间隔 |
| `source_object_id` | `string` | 否 | 来源引用 |
| `dataset_version` | `string` | 否 | 内容版本 |

不能假设所有 symbol 永远八小时结算一次。缺失 interval 时以真实 funding records 为准，不人工补造现金流。

## Contracts schema

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `snapshot_time` | `timestamp[ms, UTC]` | 获取该状态的时间 |
| `symbol` | `string` | 合约代码 |
| `contract_type` | `string` | 第一版要求 `PERPETUAL` |
| `status` | `string` | 当时交易状态 |
| `base_asset/quote_asset/margin_asset` | `string` | 资产定义 |
| `onboard_time/delivery_time` | timestamp | 官方时间字段 |
| `price_tick` | `float64` | PRICE_FILTER tick size |
| `quantity_step` | `float64` | LOT_SIZE step size |
| `min_quantity/min_notional` | `float64` | 已知时保存 |
| `observed_first_bar/observed_last_bar` | timestamp | 数据侧观察区间 |
| `source_object_id` | `string` | 快照来源 |

交易精度必须来自 filters 的 tick/step size，不能用 `pricePrecision` 或 `quantityPrecision` 替代交易规则。

## Universe schema

```text
timestamp, symbol, universe_version,
is_eligible, reason_code,
listing_age_days, history_bars,
rolling_quote_volume, missing_ratio,
contract_status, dataset_version
```

一条记录只表达该时点是否有资格参与截面，不表达目标方向或权重。

A06 的实际 LazyFrame 字段为：

```text
timestamp, symbol, is_eligible, reason_code,
listing_age_days, history_bars, rolling_quote_volume, missing_ratio,
contract_status, universe_version,
bars_dataset_version, contracts_dataset_version
```

`reason_code` 只保存按稳定优先级命中的第一个原因；`ELIGIBLE` 是唯一合格值。完整原因集合为 `EXPLICITLY_EXCLUDED`、`NO_CONTRACT_SNAPSHOT`、`NOT_PERPETUAL`、`WRONG_QUOTE_ASSET`、`WRONG_MARGIN_ASSET`、`NOT_LISTED`、`DELISTED`、`NOT_TRADING`、`WARMUP`、`INSUFFICIENT_HISTORY`、`MISSING_DATA` 和 `ILLIQUID`。

## Factor 与标签 schema

Factor：

```text
timestamp, symbol, factor_name, factor_version,
raw_value, value, is_valid, invalid_reason,
universe_version, dataset_version
```

其中 `raw_value` 是预处理前结果，`value` 是 winsorize/rank/zscore 等 pipeline 后用于研究和组合的值。

Forward return：

```text
timestamp, symbol, label_name, label_version,
entry_time, exit_time, entry_price, exit_price,
forward_return, is_valid, invalid_reason,
dataset_version
```

A07 中 factor `invalid_reason` 使用 `INSUFFICIENT_OR_GAPPED_HISTORY`、`NON_FINITE` 或变换失败原因；label 使用 `INSUFFICIENT_FUTURE`、`GAPPED_FUTURE`、`INCOMPLETE_PRICE_BAR`、`INVALID_PRICE`。`timestamp` 是信号时点，默认 `signal_delay_bars=1` 时 entry 是紧接信号的下一根 bar 开盘，exit 相对 entry 前进完整 horizon。

A07 research 表不进入交易账本：IC 表为 `timestamp, sample_count, ic, rank_ic`；coverage 明确保存 eligible、factor、label 和 aligned valid 数；quantile/turnover 均按时点排序。

标签表是研究输出，禁止作为因子输入。

## 组合和成交 schema

目标权重：

```text
signal_time, symbol, score, side,
unconstrained_weight, target_weight,
constraint_flags, portfolio_version, run_id
```

成交：

```text
signal_time, fill_time, symbol, sequence,
old_weight, target_weight, filled_weight,
turnover, reference_price, fill_price,
notional, status, run_id
```

仓位：

```text
timestamp, symbol, quantity, signed_notional,
target_weight, actual_weight, mark_price,
unrealized_pnl, run_id
```

成本：

```text
timestamp, symbol, fee_cost, slippage_cost,
funding_cashflow, total_cost, run_id
```

收益：

```text
timestamp, gross_price_return,
fee_cost, slippage_cost, funding_return,
net_return, equity, drawdown,
gross_exposure, net_exposure, turnover, run_id
```

所有收益和成本字段都采用“对组合净值的贡献”口径。funding 使用现金流符号：收入为正、支出为负；fee/slippage 字段保存正的成本值，在净收益公式中扣除。

## A09 正式运行目录

```text
<output_root>/<run_id>/
├── manifest.json
├── metrics.json
├── performance.json            # chunked 且启用 diagnostics 时存在
├── warnings.json
├── resolved_config.json
├── environment.json
├── run_metadata.json
├── report.html                 # default English compatibility entry
├── report.en.html              # explicit English
├── report.zh-CN.html           # Simplified Chinese
└── tables/{targets,returns,trades,positions,costs,factor_values,universe}.parquet
```

`manifest.json` 不把自身列入 artifact hash，其他文件必须与 manifest 的路径、字节数和 SHA-256 集合完全一致。failed run 只保存 error/config/environment 和 terminal manifest，不生成报告或成功账本。

## A10 performance artifact

`performance.json` 的 `diagnostics_version=a10-performance-v1`，包含
`mode`、`chunk_interval`、两个配置预算、`memory_budget_passed`，以及按执行顺序排列
的 chunks。每个 chunk 固化 `phase`、`ordinal`、UTC `start/end` 和按名称排序的
`input_rows/output_rows`。analysis 与 execution 分别使用自己的 ordinal；边界均为
左闭右开 core，analysis 的历史 overlap 不改变其输出边界。

该文件只证明计划、实际处理行数和本次预算门已通过。进程 baseline/observed peak
RSS 与 elapsed time 会被运行时监测，但不写入正式 artifact，因为它们受机器和调度
影响。预算超限会使 run 进入 terminal failed，而不会产生伪成功的 performance
artifact。`performance.json` 与其他成功文件一样受 manifest 字节数和 SHA-256 保护。

## Run manifest

`manifest.json` 至少包含：

```text
run_id, created_at, status,
git_commit, python_version, dependency_fingerprint,
dataset_refs, schema_versions, quality_report_ids,
resolved_config_hash, factor_versions,
random_seed, artifact_hashes, warnings_count
```

## Null、NaN 与排序规则

- 主键、价格主字段和版本字段不可空。
- 不使用无穷值；因子计算产生的无穷值转为 invalid。
- Null 表示“不存在/不可得”，0 表示真实数值 0，二者不能互换。
- 写入正式 Parquet 前按主键排序。
- 浮点比较和质量规则使用显式容差。
- 所有 enum 值和 reason code 由集中定义管理，不在模块内自由拼写。

## Schema registry 与 fingerprint

A02 在 `src/bfbt/data/schemas.py` 登记四个精确版本：

```text
bars/v1
mark_bars/v1
funding/v1
contracts/v1
```

每个 Arrow schema 的 metadata 包含 dataset、schema version、primary key、sort key、UTC 和语义说明。Schema fingerprint 对规范化逻辑描述计算 SHA-256，包括字段顺序、逻辑类型、nullability、字段 metadata 和 schema metadata；它不直接依赖 IPC 二进制编码。

Registry 不接受 `latest`。字段、类型、nullability 或语义变化都必须登记新 schema version，不能覆盖 v1。Parquet dictionary encoding 属于物理优化，不改变这里以 `string` 表达的逻辑 schema。

## Manifest 契约

A02 定义以下 JSON manifest：

| Manifest | 作用 | 关键约束 |
| --- | --- | --- |
| `raw-object/v1` | 一个已校验的原始 HTTP 对象 | HTTPS 无凭证、正字节数、SHA-256、UTC |
| `partition/v1` | 一个已发布标准化分区 | 安全相对路径、schema fingerprint、coverage、质量报告 |
| `dataset-snapshot/v1` | 一组不可变数据集引用 | 显式版本、时间覆盖、分区和质量引用 |
| `run/v1` | 一次回测的输入与产物追踪 | Git/依赖/配置/数据/schema/因子/产物指纹和状态机 |

Manifest 内容 hash 对完全展开、键排序、无 NaN 的规范 JSON 计算。文件 `content_sha256` 则始终表示文件原始字节 hash，两种含义不能混用。

分区路径和 artifact 路径只保存相对于各自根目录的 POSIX 路径，禁止绝对路径、反斜杠和 `..`。正式数据版本、schema version 以及 run 引用都必须是显式值，不能使用浮动 `latest`。

## Catalog 契约

A03 的 DuckDB catalog schema 使用独立整数版本，不与 Arrow schema version 或 dataset version 混用。版本 `1` 的核心关系如下：

```text
schema_registry
raw_objects ← partition_sources → partitions
partitions ← dataset_partitions → dataset_members → dataset_snapshots
quality_report_refs ← dataset_quality_refs / run_quality_refs
runs → run_dataset_refs / run_schema_refs / run_factor_refs / run_artifacts
```

数据库中的主体表保留规范 manifest JSON 和其 SHA-256；关系列用于精确解析、引用完整性和 coverage 查询，JSON 用于无损还原 A02 模型。业务主键分别是 `object_id`、`partition_id`、`(dataset_id, dataset_version)` 和 `run_id`。已存在主键只有在 manifest hash 完全相同时才视为幂等，不能用 upsert 静默改变历史含义。

DatasetSnapshot 的 `available_to` 是开区间上界，必须严格晚于其所有非空 Partition 的 `max_time`。Catalog coverage 返回 Partition 实际 `MIN(min_time)` 和 `MAX(max_time)`，因此 coverage 输出的 `available_to` 字段表示已登记数据的最后时刻，而不是 Snapshot 的开区间边界；两者语义不可互换。

## A12 第二版 artifact schema

A12 在不改变四张 V1 市场数据 schema 列表的前提下，增加独立 artifact registry：

| Artifact | Schema | 主键 | 用途 |
|---|---|---|---|
| `rankings` | `v1` | timestamp、factor_name、symbol | 原始分数、整数/百分位 Rank 和样本数 |
| `position_instructions` | `v1` | instruction_id | 请求、约束、抑制和目标/增量仓位指令 |
| `risk_events` | `v1` | event_id | 单合约及组合风险触发和成交关联 |

三张表尚未在 A12 生成。其字段顺序、Arrow 类型、nullability、主键、排序键和 metadata
已在 `src/bfbt/data/schemas.py` 固定，并由
`tests/fixtures/config/acceptance_12/v2_contract_golden.json` 审计。

新增 identity 字段的原因：

- `factor_name` 使多个同版本号因子的 Rank 快照不发生主键冲突。
- `instruction_id` 允许成交和风险事件稳定引用同一仓位指令。
- `event_id` 允许组合事件使用 null symbol 而仍具有非空主键。
- `source_event_id` 将风险指令关联回触发事件。

## A12 事件契约

`src/bfbt/data/v2_contracts.py` 集中定义：

- 优先级：未来强平保留位、组合风险、单合约风险、universe 强退、定时策略。
- 选择、约束、资金、风险、冷却、抑制和结束边界 reason code。
- Rank 方向为 score descending，并列按 score descending、symbol ascending。
- `events/v3` 版本和规范内容 SHA-256；v2 新增可审计的
  `RANK_DESCENT_TRIGGERED`，v3 新增 `ALREADY_HELD` 与
  `REPLACED_BY_SIGNAL`。

数字越小优先级越高。后续阶段只能引用集中枚举，不能在执行模块自由拼写新 reason
code；修改枚举、优先级或 Rank 语义必须提升契约版本或更新设计并重新验收。

## A12 run/v2 manifest

`run/v1` 保持原模型和序列化不变。新增 `run/v2`，在其基础上强制增加：

```text
config_version=v2
event_contract_version
event_contract_fingerprint
artifact_schema_versions
```

`artifact_schema_versions` 必须恰好包含 rankings、position instructions 和 risk
events，且每个 fingerprint 必须匹配注册表。A12 只验证该 manifest，不发布 V2 run。
