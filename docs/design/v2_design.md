# 第二版架构设计

本文定义 `bfbt` 第二版的开发边界。第二版在第一版可复现、分块、目标权重回测
基础上增加状态化选币、增量仓位指令和事件触发风险调仓，同时保持第一版配置与结果
兼容。

## 1. 目标和边界

第二版必须完成三组用户需求：

1. 不再只支持连续的 Top/Bottom，允许为多头和空头指定任意当前 Rank；例如只做多
   第 2 名，同时做空第 1 名。
2. 允许当前调仓引用前 N 个快照的 Rank；例如在当前时刻做多上一个调仓快照中
   Rank 为 1 的合约。
3. 在目标权重之外支持固定保证金、固定名义价值、净值比例和现有仓位比例的增量
   加减仓，并支持止盈、止损和移动止损触发非定时调仓。

第二版仍然是分钟 K 线及以上的研究系统，不做逐笔撮合、盘口排队、真实维持保证金
阶梯、ADL 或完整复刻 Binance 强平引擎。`fixed_margin` 是可审计的回测资金分配
语义，不冒充交易所逐仓/全仓账户的完整实现。

## 2. 向后兼容

第一版配置继续有效。第二版配置显式增加：

```yaml
config_version: v2
```

缺少该字段的旧配置按 `v1` 适配，继续使用：

```text
当前快照 Top/Bottom → 目标权重 → 下一根基础 K 线开盘成交
```

兼容验收比较经济列，而不是正式 `run_id`。代码版本变化本来就会改变 source
fingerprint 和 `run_id`；但相同第一版配置下，剔除 `run_id` 等身份列后，targets、
trades、positions、costs、returns 和 metrics 必须与第一版 golden 结果一致。

第二版新字段不得通过含糊默认值改变旧策略。新配置必须显式选择 Rank、仓位指令和
风险事件语义，配置校验在任何数据扫描前完成。

## 3. 第二版数据流

```text
factor values
    ↓
rank snapshots ── bounded rank history state
    ↓
selection rules
    ↓
position instructions (target or incremental)
    ↓
constraints + margin checks
    ↓
scheduled order intents ──────────────┐
                                      ├→ event ledger → fills/PnL/costs
base-bar risk checks → risk intents ──┘
```

因子计算、Rank 快照、策略决策、风险检查和成交是五个不同时间概念，报告必须分别
展示，不能再笼统称为“信号频率”。

## 4. Rank 模型

### 4.1 统一排名语义

第二版统一规定：

- `ordinal_rank=1` 永远表示当期有效截面中分数最高的合约。
- 排序键为 `score DESC, symbol ASC`，从而在分数相同时保持确定性。
- `percentile_rank` 仍为 `[0, 1]`，值越大表示分数越高，供研究和加权使用。
- `sample_count` 保存该快照有效合约数；整数 Rank 不能脱离它解释。
- 原始分数、整数 Rank 和百分位 Rank 分列保存，不再用一个 `value` 同时表达三种
  语义。

因子预处理中的平均 Rank 仍可用于研究；组合选币使用确定性的 ordinal Rank。报告
必须显示排名方向和并列值处理方式。

### 4.2 精确 Rank 选择

建议配置：

```yaml
portfolio:
  selection:
    mode: rank_set
    rank_order: descending
    long:
      ranks: [2]
      ranges: []
    short:
      ranks: [1]
      ranges: []
```

`ranks` 与闭区间 `ranges` 可以组合；同一 Rank 不能同时进入多头和空头。允许只存在
一侧持仓。第一版 `long_short_count` 和 `long_short_quantile` 通过兼容适配器映射为
连续 Rank 区间。

如果配置的 Rank 超过当期 `sample_count`，该 Rank 产生可审计的
`RANK_OUT_OF_RANGE`，不能悄悄改选其他合约。

### 4.3 跨快照 Rank

建议配置：

```yaml
portfolio:
  selection:
    mode: rank_set
    clock: rebalance
    lag: 1
    long:
      ranks: [1]
    short:
      ranks: [8]
```

语义为：在当前决策时刻 `t`，读取所选时钟严格前一个快照的 Rank，再与当前时刻的
eligible universe 相交。输出同时保存：

```text
decision_time
rank_source_time
rank_lag
rank_clock
symbol
ordinal_rank
sample_count
selection_reason
```

`clock=rebalance` 表示 lag 按调仓快照计数；`clock=factor` 表示按因子快照计数。
不能用 wall-clock 减法猜测前一个快照。历史 Rank 合约若当前不再 eligible，则跳过并
记录 `HISTORICAL_RANK_NOT_CURRENTLY_ELIGIBLE`，不得用当前 Rank 补位。

第二版核心执行支持固定非负 `lag`。任意历史查询和大规模 Rank 矩阵分析通过分区
Parquet 研究接口完成，不把全部历史常驻执行引擎内存。

### 4.4 Rank 内存边界

令 `N` 为单快照合约数，`L` 为配置的最大 lag：

- `lag=1` 使用双缓冲或等价状态表，额外内存目标为 `O(N)`。
- 固定 `lag=L` 使用有界环形缓冲，额外内存目标为 `O(N × L)`。
- 当前截面排序保持 `O(N log N)`；历史读取不得使时间复杂度增加到
  `O(T² × N)`。
- 分块边界只序列化最近 `L` 个必要快照，不能 collect 全部历史 Rank。
- `max_rank_lag`、状态行数和增量 RSS 必须有硬门，超限明确失败。

完整 Rank 历史可以写入 Parquet，磁盘复杂度为 `O(T × N)`；这是可选审计产物，
不等于常驻内存复杂度。

## 5. 仓位指令和资金模型

### 5.1 目标模式与增量模式

第二版将“选了什么”和“如何改变仓位”分开：

```yaml
portfolio:
  sizing:
    mode: target_weight
```

`target_weight` 保留第一版语义。新增模式：

| 模式 | 每次同向信号请求的名义价值变化 |
|---|---|
| `fixed_margin` | `margin_amount × leverage` |
| `fixed_notional` | 固定 `notional_amount` |
| `equity_fraction` | `fraction × pretrade_equity` |
| `equity_margin_fraction` | `fraction × pretrade_equity × leverage` |
| `position_fraction` | `fraction × abs(pretrade_symbol_notional)` |

增量模式中的重复同向信号会重复加仓，不会重新计算到一个固定目标权重。每条指令都
必须保存请求增量、约束后增量和拒绝原因。

### 5.2 初始资金和保证金

固定金额模式要求显式资金配置：

```yaml
capital:
  currency: USDT
  initial_equity: 100000
  margin_model: simple_cross

risk:
  leverage: 2.0
```

`simple_cross` 的第一阶段定义：

```text
initial_margin = abs(signed_notional) / leverage
used_margin = sum(initial_margin by open symbol)
available_margin = max(0, equity - used_margin - reserved_cost_buffer)
```

未实现盈亏通过 equity 影响可用保证金，手续费、滑点和资金费率按现有账本扣减。
这不是 Binance 维持保证金阶梯；报告必须明确标注模型名称。

`position_fraction` 在零仓位时必须配置 `zero_position_policy`：`skip`、`error` 或用
另一个显式 bootstrap 模式开仓，不能默认猜测基数。

### 5.3 反向信号和约束

增量模式必须显式配置反向信号策略：

- `flatten_only`：只减到零，不在同一事件反向开仓。
- `flatten_then_open`：先平旧方向，再以剩余指令开新方向。
- `net_delta`：把请求直接作为 signed notional delta。

约束至少包括：

```text
max_gross_exposure
max_net_exposure
max_symbol_weight
max_symbol_notional
max_consecutive_adds
max_turnover
available_margin
```

约束顺序固定并进入版本指纹。任何缩放或拒绝都必须输出 reason code。

## 6. 止盈止损与事件触发调仓

### 6.1 配置草案

```yaml
risk:
  evaluation_interval: 1m
  trigger_price: trade
  intrabar_conflict: worst_case
  symbol_exits:
    stop_loss:
      enabled: true
      distance: 0.05
      action: close
    take_profit:
      enabled: true
      distance: 0.10
      action: close
    trailing_stop:
      enabled: false
      distance: 0.03
  portfolio_exits:
    stop_loss: null
    take_profit: null
    max_drawdown: null
  cooldown_bars: 0
  reentry_policy: next_scheduled_rebalance
```

风险检查频率独立于因子频率和定时调仓频率。小时因子、4 小时调仓可以配合 1 分钟
风险检查。

### 6.2 触发语义

- 单合约止盈止损基于该方向当前 `average_entry_price`。
- 每条规则可用 `distance` 表示多空对称阈值，或同时使用 `long_distance` 和
  `short_distance` 表示不同阈值；两种写法不能混用。
- 多头止损检查 low，多头止盈检查 high；空头方向相反。
- 移动止损保存最高/最低有利价格，但当前 bar 的新极值只能在完成本 bar 触发判断后
  更新，避免用未知盘中路径改善同一 bar 的止损线。
- `trigger_price=trade` 使用成交 K 线；`mark` 使用标记价格 K 线。成交仍使用明确的
  fill 模型。
- `next_bar_open` 在触发后下一根基础 K 线开盘成交；未来可增加明确的 stop-market
  模型，但不能把触发价无条件当成可成交价。
- 跳空时使用实际可见开盘价和不利滑点，不能按已经穿越的止损线理想成交。

若同一根 OHLC K 线同时触及止盈和止损，且没有更细数据证明顺序：

- `worst_case` 默认选择对策略更不利的结果；
- `error` 终止运行并要求更细数据；
- 其他规则必须显式命名并写入报告。

### 6.3 触发后的仓位行为

每个风险规则的 action 可以是 `close` 或显式 `reduce_fraction`。反向和立即补位属于
更高风险行为，只有配置明确开启时才允许。止损后可配置：

- 等到下一次定时调仓才重新入场；
- 冷却 N 根风险检查 K 线；
- 当前 Rank 仍满足条件时也禁止立即回补。

风险退出不能仅把目标权重临时设零而不保留原因；必须生成独立 risk event 和
position instruction。

## 7. 统一事件顺序

第二版仍复用第一版账本，不引入外部回测框架。每根基础 K 线按确定顺序处理：

1. 执行此前已排队、应在本 bar 开盘成交的风险和策略指令。
2. 更新成交后的数量、均价、保证金和成本。
3. 使用本 bar 可见的 high/low/close 检查盘中风险触发。
4. 按明确 fill 模型立即成交或排队到下一根开盘。
5. 在真实 funding time 结算资金费率。
6. 使用配置的 mark price 完成本 bar 估值和收益恒等式。
7. 更新只允许在本 bar 完成后可见的 trailing extrema 和历史状态。
8. 在因子/调仓时钟到达时生成之后才能成交的策略指令。

同一决策时刻的意图优先级固定为：

```text
未来强平保留位
  > 组合级紧急风险退出
  > 单合约止损/止盈
  > universe 强制退出
  > 定时策略调仓
```

高优先级退出触发冷却时，低优先级策略不能在同一时间点重新开回。冲突解决结果写入
`constraint_flags` 和 `suppressed_intents`。

## 8. 有界状态

跨块状态只允许保存：

- 每个持仓的数量、方向、均价、目标/请求状态和连续加仓次数：`O(P)`。
- 每个持仓的止盈止损线、有利极值、冷却和触发计数：`O(P)`。
- 最近 `L` 个 Rank 快照：`O(N × L)`。
- 下一根开盘待成交指令：`O(P + N)`。
- 组合 equity、peak、保证金和资金费率状态：`O(1)`。

`P` 为持仓合约数。不得把完整历史 positions、risk events 或 Rank 矩阵保存在 Python
列表中等待回测结束；明细按块写临时 Parquet，最终原子发布。

## 9. 新增产物

第二版在第一版七张核心表之外增加：

### `rankings.parquet`

```text
timestamp, rank_clock, symbol, factor_name, raw_score, ordinal_rank, percentile_rank,
sample_count, factor_version, universe_version, run_id
```

### `position_instructions.parquet`

```text
instruction_id, decision_time, rank_source_time, symbol, side, instruction_mode,
requested_delta_notional, constrained_delta_notional, requested_target_weight,
source_event_id, reason_code, priority, run_id
```

### `risk_events.parquet`

```text
event_id, evaluation_time, trigger_time, symbol, event_type, direction, entry_price,
trigger_level, observed_price, conflict_policy, action, fill_time, reason_code, run_id
```

现有 `trades.parquet` 增加 instruction/risk event 引用，但继续作为所有实际成交的唯一
事实表。`positions` 增加 used margin、available margin、止损线和加仓计数等审计列。
Schema 变更必须提升版本，旧 run 仍由旧 schema 正常重建报告。

这些产物仍写入 `data/backtest/runs/<run_id>/`，报告仍写入
`data/backtest/reports/<run_id>/`。原始和标准化数据只属于
`data/backtest/datasets/<dataset_id>/`，不得把 run 或报告重新放回数据集目录；
研究过程文件使用 `workspaces`，成功发布后按现有清理策略处理。

## 10. 报告要求

第二版报告必须直接显示：

- Rank 方向、精确 Rank 规则、lag、引用快照时间和当前样本数。
- 仓位模式、每次增量、初始资金、杠杆、保证金模型和所有仓位上限。
- 因子、Rank、调仓、风险检查四种频率。
- 止盈止损线、触发原因、冲突规则、冷却和重新入场政策。
- 定时成交与风险成交分别统计的笔数、成本和收益影响。

第一版已有的因子公式、中文解释、数据范围、下单时点、持仓规则、结束处理和中英
双语结果名称必须保留。重要摘要直接展示；低频详细表继续默认折叠。交互快照应能
查看当时 Rank 来源、仓位状态、待处理指令、风险事件及关联成交。持仓和关联成交
在同一纵向区域展开，不能让关联成交表在右侧溢出；浏览器 payload 必须有界。

## 11. 确定性和失败策略

- 所有状态转移使用稳定排序和显式优先级。
- 同样输入下，分块和非分块经济列必须一致。
- chunk 边界不能重复触发或漏掉 Rank lag、止损、资金费率或冷却计数。
- 数据精度无法支持所选触发语义时，按配置告警或失败，不能静默乐观成交。
- Rank/margin/risk 状态超过配置预算时，在 OOM 前明确失败并发布 terminal failed
  run。
- 所有配置、状态版本、冲突政策和 reason code 都进入 resolved config 与 run
  manifest。

## 12. 第二版明确不包含

- 盘口、限价排队、部分成交和市场冲击曲线。
- Binance 完整维持保证金阶梯、强平费用和 ADL。
- 任意用户 Python 回调在事件循环中修改状态。
- 把全部历史 Rank 展开成常驻宽矩阵。
- 自动连接实盘账户或把回测仓位发送给实盘系统。

## 13. 完整配置草案

下面的配置只定义第二版预期契约，不代表当前代码已经支持。开发时先以 Pydantic 模型
和 resolved config 固化字段，再逐阶段接入执行引擎。

```yaml
config_version: v2

schedule:
  factor_interval: 1h
  rebalance_interval: 4h

capital:
  currency: USDT
  initial_equity: 100000
  margin_model: simple_cross
  reserved_cost_buffer: 500

portfolio:
  selection:
    mode: rank_set
    rank_order: descending
    clock: rebalance
    lag: 1
    long:
      ranks: [1, 3]
      ranges: []
    short:
      ranks: [8]
      ranges: [[10, 12]]
  sizing:
    mode: fixed_margin
    margin_amount: 1000
    reverse_policy: flatten_then_open
    zero_position_policy: skip
  constraints:
    max_gross_exposure: 2.0
    max_net_exposure: 0.5
    max_symbol_weight: 0.25
    max_symbol_notional: 25000
    max_consecutive_adds: 3
    max_turnover: 1.0

risk:
  leverage: 2.0
  evaluation_interval: 1m
  trigger_price: trade
  fill_model: next_bar_open
  intrabar_conflict: worst_case
  symbol_exits:
    stop_loss:
      enabled: true
      distance: 0.05
      action: close
    take_profit:
      enabled: true
      distance: 0.10
      action: close
    trailing_stop:
      enabled: false
      distance: 0.03
      action: close
  portfolio_exits:
    stop_loss: null
    take_profit: null
    max_drawdown: null
  cooldown_bars: 1
  reentry_policy: next_scheduled_rebalance

performance:
  max_rank_lag: 24
  max_rank_state_rows: 20000
  max_incremental_rss_mib: 1024
```

关键跨字段校验如下：

- `selection.lag` 不得超过 `performance.max_rank_lag`。
- `fixed_margin` 必须同时给出 `margin_amount`、初始净值和正数杠杆；
  `fixed_notional`、`equity_fraction`、`position_fraction` 同理只接受各自所需字段。
- `position_fraction` 必须显式设置零仓位政策。
- Rank 列表和区间只能包含正整数，且多空集合不能静态重叠。
- 风险检查频率不得小于输入基础 K 线精度；若不能被数据时钟准确表达，配置失败。
- `trigger_price=mark` 要求所需区间存在 mark bars，否则在运行前失败。
- 所有比例、止盈止损距离、杠杆和金额必须落在明确的有限正数区间。
- `next_bar_open` 必须保证结束边界有可成交 bar；否则最后未成交意图进入审计表而不
  虚构成交。

## 14. 开发决策记录

开发期间若需要改变本文语义，必须先更新本文并说明兼容影响，不能让实现事实反过来
成为未记录的默认规则。尤其需要记录：

- 配置字段改名、默认值和 schema 版本变化；
- Rank 并列、缺失和 universe 交集政策；
- 保证金、反向信号、约束缩放和拒绝顺序；
- 风险事件的触发、成交、冲突和重新入场政策；
- 任何会改变第一版经济结果或第二版内存数量级的决定。
