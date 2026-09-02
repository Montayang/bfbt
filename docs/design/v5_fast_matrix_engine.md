# 第五阶段设计：Fast Matrix 常规截面回测引擎

## 1. 状态与优先级

本文是 Fast Matrix 已实现架构的设计来源。A31–A35 已于 2026-08-23 完成，验收证据见
`docs/acceptance/A31.md`–`A35.md`。目标工作流为：

```text
快速因子研究
  → Fast Matrix 批量组合回测
  → 少量候选进入 Event（当前 V2）正式回测
```

第五阶段未修改既有正式 run；Fast Matrix 只发布 `fm-*` 研究结果，不以近似结果冒充 V2
正式经济结果。

## 2. 当前问题

当前系统已经具备两端：

- 因子、Label、IC、分层收益、覆盖率和 Rank 稳定性使用 Polars 列式研究路径；
- V2 使用按时间推进的账户、指令、成交、成本、风险与审计状态机。

缺少中间层：对于固定时钟、目标权重和线性成本的常规截面策略，当前仍需进入 V2 的逐时点
事件执行并生成大量明细。旧 `run_vectorized_backtest` 虽承载 V1 简化语义，但会收集行记录
并顺序维护账本，不是面向全市场、多因子批量研究的矩阵引擎。

第五阶段不继续扩充旧 V1，而是新增受限、可证明等价的 `fast_matrix` 后端；V1 退出新策略
日常使用，只保留旧配置、旧 artifact、历史复现和回归兼容。

## 3. 核心目标

1. 对适合矩阵化的常规截面策略，避免 Python 层逐成交、逐持仓事件循环。
2. 一次读取共享市场块，推进多个因子、窗口或组合候选，不把 N 份行情复制到内存。
3. 保持 DatasetSnapshot、AnalysisSnapshot、SignalSnapshot 的不可变身份和分层失效。
4. 对共同支持的语义，与 V2 在目标、换手、费用、funding、收益、权益和回撤上逐时点等价。
5. Fast Matrix 先发布研究结果；用户选中的少量候选仍由 Event/V2 产生正式 run 和完整审计。
6. 自动规划器只在能力完整匹配时选择 Fast Matrix；不能证明时进入 Event/V2。
7. 继续支持时间分块和内存硬门，不用整月全市场 dense matrix 换取速度。

## 4. 非目标

- 不支持任意 Python 策略回调、tick、订单簿、部分成交或完整交易所撮合。
- 第一版不支持止盈止损、移动止损、冷却、事件优先级和同 bar 风险冲突。
- 第一版不支持依赖当前账户状态的动态保证金、连续加仓和持仓替换。
- 不从旧 trades 反推新权益，不修改旧 run，不让 Fast Matrix 覆盖 V2 artifact。
- 不把 IC、分层收益直接当成可交易组合收益。
- 不保证所有 V2 策略都能转换；受限能力是正确性边界，不是临时警告。
- 不在设计阶段虚构全年吞吐或固定加速倍数。

## 5. 目标架构

```text
DatasetSnapshot
  └─ Universe / Factor / Label
       ├─ ResearchDiagnostics：IC、分层、覆盖、Rank 稳定性
       └─ AnalysisSnapshot
            └─ SignalSnapshot / TargetSchedule
                 ├─ FastMatrixBackend
                 │    └─ MatrixResearchRun（研究结果）
                 └─ EventBackend（当前 V2）
                      └─ FormalRun（正式不可变结果）

LegacyV1Backend：仅历史复现与兼容测试，不进入新策略默认工作流
```

关键分层：

- **策略语义版本**：继续由 `config_version` 描述配置合同；新能力默认建立在 V2 合同上。
- **执行后端**：`fast_matrix` 与 `event` 描述如何执行同一组已支持的经济语义。
- **性能模式**：`in_memory`/`chunked` 继续描述数据如何分块，不与后端名称混用。

不得把新引擎命名成 V3，因为“配置版本”和“执行算法”是不同维度。

## 6. 用户工作流

### 6.1 单纯因子诊断

```text
Factor → IC / Rank IC / Quantile Return / Coverage / Rank Turnover
```

不创建账户，不调用任何交易执行后端。

### 6.2 常规截面策略

```text
ResearchDiagnostics
  → Fast Matrix 批量比较
  → 用户选择少量候选
  → Event/V2 正式确认与报告
```

Fast Matrix 结果必须显示“研究结果 / Research Run”，不能使用正式 `a17-*` run ID。

### 6.3 复杂事件策略

策略能力检查不通过时直接使用 Event/V2，不运行 Fast Matrix。显式请求 `fast_matrix` 时应
失败并列出原因；`auto` 模式可以选择 Event，但必须在 manifest/终端记录选择原因。

## 7. 配置与后端规划

建议在 V2 `backtest` 配置下新增独立段，最终字段名在 A31 固定：

```yaml
config_version: v2

engine:
  backend: auto        # auto | fast_matrix | event
  purpose: research    # research | formal
  equivalence_audit: false

performance:
  mode: chunked        # in_memory | chunked
```

规则：

- `purpose=formal` 第一版只允许 `event`；Fast Matrix 候选须提升后再正式运行。
- `backend=fast_matrix` 遇到任何不支持能力必须失败关闭。
- `backend=event` 保持当前 V2 行为。
- `backend=auto` 根据可机器验证的能力清单选择，并将 reason codes 写入结果。
- 旧配置没有 `engine` 段时保持当前 V1/V2 分派，不允许升级后悄然改变历史结果。

建议建立显式能力对象，而不是散落 `if`：

```text
ExecutionRequirements
ExecutionCapabilities
BackendDecision(selected_backend, supported, reason_codes)
```

初始 reason code 示例：

```text
UNSUPPORTED_SYMBOL_EXIT
UNSUPPORTED_PORTFOLIO_EXIT
UNSUPPORTED_STATE_DEPENDENT_SIZING
UNSUPPORTED_PARTIAL_FILL
UNSUPPORTED_LIQUIDATION
UNSUPPORTED_EVENT_ARBITRATION
UNSUPPORTED_DYNAMIC_REENTRY
UNSUPPORTED_DATA_GAP_POLICY
```

## 8. 第一版 Fast Matrix 能力边界

### 8.1 必须支持

- point-in-time Universe 和当时可见因子；
- 固定 factor/rebalance 时钟；
- 当前截面的 count/quantile Top/Bottom 选择；
- `equal`、可严格定义的 `score` 和 `inverse_volatility` 权重；
- 目标 gross/net exposure 和静态单合约上限；
- `target_weight`，目标由账户外部信号完整确定；
- `signal_delay_bars >= 1` 与 `next_bar_open`；
- fixed-bps 或 zero fee/slippage；
- `trade_close`，以及有完整输入时的 `mark_close`；
- funding 的真实时点现金流和显式缺失政策；
- 固定调仓间持有、权重漂移、下一次调仓的真实 delta/turnover；
- 左闭右开区间、预热、合约上市/下市和期末估值；
- long-short、long-only 和 short-only 的静态目标权重组合。

### 8.2 可以共享但不阻止矩阵化的上游状态

Rank 缓冲、连续 Rank、Rank 递减等选择状态如果能够在 SignalSnapshot 中完全确定、且不读取
账户/持仓结果，可以作为 Fast Matrix 输入。Fast Matrix 不负责重新实现这些选择状态机，
只消费确定性的 `TargetSchedule`。

### 8.3 第一版明确拒绝

- fixed margin/notional 或 equity fraction 导致的账户依赖 sizing；
- 已持仓才执行、加仓次数、替换和 reverse policy；
- 单合约/组合止盈止损、trailing、drawdown exit；
- cooldown、风险退出后的 reentry 和事件抑制；
- partial fill、liquidation、盘口冲击和非线性成本；
- 同一时点需要 arbitrator 决定顺序的多事件；
- 不能证明完整的缺 bar 成交或估值政策。

后续扩展必须逐项增加等价证明，不能仅删除拒绝条件。

## 9. TargetSchedule：共享执行输入

现有 `SignalSnapshot` 主要保存 selections。Fast Matrix 和 Event 要共享经济输入，需要增加
规范化目标日程：

```text
signal_time
fill_time
symbol
target_weight
source_signal_id
factor_version
universe_version
portfolio_version
```

要求：

- 同一 `fill_time + symbol` 唯一；
- 未被选择的旧持仓必须有明确归零语义，不能靠后端猜测；
- long/short 符号、gross/net 和静态约束已确定；
- `fill_time` 由 signal delay 和真实基础 bar 规则确定；
- 身份绑定 Analysis/Signal manifest hash、选择/权重配置和实现版本；
- Event/V2 也能够消费同一目标日程，避免两套组合构建逻辑漂移。

TargetSchedule 可以成为 SignalSnapshot 的新表或独立子产物；A31 需比较失效边界后固定，
不能在实现中临时塞进 MatrixResearchRun。

## 10. 经济计算模型

### 10.1 不能使用“权重恒定乘收益”的错误近似

调仓后持有的是数量，价格变化会使实际权重漂移。下一次调仓的 delta 应基于调仓前真实
数量、开盘价和账户权益，而不是简单计算两个目标权重之差。

因此 Fast Matrix 仍有沿时间方向的组合递推：

```text
PortfolioState_t = F(PortfolioState_t-1, market_t, target_t, funding_t)
```

与 V2 的区别不是“完全没有时间状态”，而是：

- 状态是紧凑的现金、数量向量、权益峰值和累计器；
- 横截面价格、收益、目标和 delta 使用列式/数组运算；
- 不创建 Python 级 instruction/trade/risk 对象并逐事件仲裁；
- 无调仓区间可以批量计算持仓 PnL；
- 时间分块只传递紧凑矩阵检查点。

### 10.2 每个基础时点的顺序

必须与 Event/V2 固定共同语义：

1. 读取真实 open；
2. 执行到期 TargetSchedule；
3. 根据调仓前数量和权益计算目标数量及 delta；
4. 以 reference open 和不利滑点记录成本口径；
5. 扣除手续费/滑点；
6. 应用区间内到期 funding；
7. 使用 close 或 mark close 估值；
8. 输出组合收益、权益、敞口、换手和回撤；
9. 进入下一时点。

时间戳、open/close 边界和 funding 顺序必须通过 V2 fixture 锁定，不能因矩阵实现方便而改变。

### 10.3 计算表示

第一版优先使用分块长表和每块活动 symbol 索引，不构造“整月 × 全历史所有合约”的 dense
矩阵：

```text
当前时间块 LazyFrame
  → 活动 symbol 稳定索引
  → open/close/funding/target 的列式块
  → 紧凑 quantity 向量与 portfolio scalar state
  → returns/turnover/cost 的块输出
```

允许使用 Polars/Arrow/NumPy 原生数组；禁止在 Python 中双重遍历每根 bar 的每个 symbol。
若必须沿时间循环，应是每个组合时点一次的紧凑向量运算，并在基准中证明其不是主要瓶颈。

## 11. 成本、Funding 与缺失行情

- fee/slippage 金额必须以真实 `abs(delta_notional)` 计算，不按目标权重粗估。
- `fill_price` 的审计值继续使用 reference open 加不利滑点；经济成本不得重复扣除。
- funding 使用当时持仓数量和 funding price，收入为正、支出为负，符号与 V2 一致。
- 合约已持有而当前缺 bar时，第一版若不能严格复现 V2 carry-forward 估值和禁止伪造成交，
  后端规划器必须拒绝该数据/策略组合，而不是填充后继续。
- 合约上市、退市、Universe 移除和目标归零必须有 fixture；不能把缺行等同于平仓。

## 12. 结果与产物分层

建议新增研究产物类型 `MatrixResearchRun`：

```text
data/backtest/research_runs/
  fm-<identity>/
    manifest.json
    resolved_config.json
    backend_decision.json
    metrics.json
    attribution.json
    performance.json
    tables/
      returns.parquet
      target_schedule.parquet       # 可配置保存
      rebalance_summary.parquet
      factor_diagnostics.parquet    # 引用或摘要
```

原则：

- `fm-*` 与正式 `a17-*` 身份空间分离；
- manifest 绑定 Dataset/Analysis/Signal/TargetSchedule hash、引擎版本和配置 hash；
- 默认不保存每分钟每合约 positions 和逐事件审计表；
- equivalence audit 模式可在小样本输出必要的逐调仓明细；
- 成功后不可覆盖，临时 sweep 可使用独立 workspace，选定摘要再发布研究结果；
- MatrixResearchRun 不能直接改名为正式 run；提升必须启动 Event/V2。

报告可提供轻量研究报告，但页面必须显著标记“矩阵研究结果，未经过 Event 正式确认”。

## 13. 批量研究与缓存

Fast Matrix 需要复用 A27–A29，而不是建立另一套 cache：

- 相同 Dataset/Universe 的多个因子尽量共享最小行情扫描；
- 相同 AnalysisSnapshot 的多个选择变体复用因子；
- 相同 TargetSchedule 的成本/报告变化不得重算因子；
- 不同 factor identity 正确失效，但批处理器可以共享底层市场块；
- 每个候选有独立数量、现金、权益、成本和结果 hash；
- 候选数、矩阵宽度、RSS 和输出大小有硬门；
- batch 只生成研究摘要，不能自动选优或自动进入正式 run。

未来的多候选研究应一次规划共享数据块，而不是机械启动多个彼此不知情的冷进程；具体因子、
参数和策略族不在引擎开发阶段预设。

## 14. 建议代码目录

实现时遵循现有职责，不建立顶层杂乱脚本：

```text
src/bfbt/
  engine/
    fast_matrix/
      __init__.py
      capabilities.py      # 支持矩阵与 reason codes
      planner.py           # auto/explicit 后端决策
      target_schedule.py   # 规范化执行输入
      kernel.py            # 单组合向量经济核心
      batch.py             # 多候选共享输入
      checkpoint.py        # chunk 状态与恢复
      result.py            # MatrixResult 合同
    v2.py                  # 保持 Event/V2，不搬迁无关代码
    vectorized.py          # Legacy V1，冻结新增能力
  application/
    matrix.py              # 研究运行编排与提升请求
    run.py                 # 正式 Event 入口与后端分派
  artifacts/
    matrix.py              # MatrixResearchRun manifest/store
    reuse.py               # 继续负责 Analysis/Signal，不复制
  config/
    backtest.py            # engine/backend 合同
  research/
    evaluator.py           # 现有快速研究层
    matrix_sweep.py        # 研究摘要接口；不放经济 kernel
```

测试和文档：

```text
tests/acceptance/test_acceptance_31_*.py
tests/acceptance/test_acceptance_32_*.py
...
docs/acceptance/A31.md
docs/acceptance/A32.md
...
```

禁止：

- 在 `strategies/` 放执行代码；
- 在 `research/` 复制一套成本/资金费公式；
- 在 `fast_matrix` 复制 factor、Rank 或 Dataset 读取器；
- 将大矩阵、报告或研究结果提交 Git；
- 为追求速度绕过 manifest/hash/时点完整性。

## 15. CLI 与提升流程

建议最终用户入口保持意图清晰，准确命令在 A31 固定：

```text
bfbt research factor-evaluate ...
bfbt research matrix-run ...
bfbt research matrix-sweep ...
bfbt run ...                         # 默认正式 Event/V2
```

提升流程：

```text
fm-研究结果 + 精确 candidate config
  → 用户确认
  → backend=event, purpose=formal
  → 新的正式 a17-* run
  → 逐时点等价摘要写入正式报告
```

Fast Matrix 不直接触发 Event，不自动替用户选择候选。

### 15.1 研究报告分层

快速研究与 Fast Matrix 是两个独立筛选阶段，不得再把全部明细混成一张静态总表：

- 快速研究报告一行对应 `study_id / period / factor / horizon`，只保留 Rank IC、首尾分位差、
  覆盖率、Rank 换手和方向命中比例，用于在大量因子中快速筛选；
- Fast Matrix 索引一行对应 `study_id / period / factor / cost_variant / fm_run_id`，展示收益、
  回撤、累计换手和费用，并链接到单 run 报告；
- 单个 Fast Matrix 报告是简化版 Event 报告，至少说明因子身份、目标组合、调仓/成交/估值
  语义、手续费/滑点/funding、敞口、换手、净值和不可变来源身份；
- 研究项目的 `report.html` 只负责阶段导航，机器批处理继续读取 `summary.json` 和 Parquet；
- 表格必须支持按索引键搜索，并按月份、因子、预测周期或成本口径筛选及按列排序。

`fm-*` 目录保持不可变。报告布局升级不能覆盖旧 run；旧产物通过
`bfbt research study-report` 在研究项目目录生成可重建展示报告，新发布 run 才在不可变目录
内原生使用新版报告。

## 16. 等价性合同

共同支持场景要求以下字段逐时点一致，浮点容差必须在 A32 逐字段固定：

- 生效 target weight 和 fill time；
- 调仓前数量、目标数量和 delta notional；
- reference price、fee、slippage 和 funding cashflow；
- turnover、gross/net exposure；
- gross price return、成本贡献、funding return、net return；
- equity、drawdown、ending positions；
- 缺失数据 warning/reason code 的经济含义。

运行 ID、生成时间、后端诊断和完整审计事件不要求相同。若 Fast Matrix 不发布某张 V2 明细
表，equivalence audit fixture 必须能从内核暴露足够证据进行比较。

## 17. 性能与资源验收

正确性优先于速度，但没有实质加速也不能宣布阶段完成：

1. A31 先冻结代表性基线、硬件、冷/热缓存状态和测量命令；
2. 单因子单月比较 Fast Matrix 与 Event/V2 的 wall time、CPU、读取行数/字节和峰值 RSS；
3. 多候选 batch 比较共享扫描与相同数量的独立运行；
4. 记录计算、I/O、artifact 写入和报告各阶段耗时；
5. 在看到基线后由用户确认最低加速门，设计阶段暂以单策略至少 5×、批量边际成本显著低于
   N 倍作为候选目标，不作为已经承诺的事实；
6. chunked 结果不得因块长变化而改变，峰值 RSS 继续受绝对硬门监督；
7. 不以关闭费用、funding、hash 校验或减少真实区间换取基准数字。

## 18. 兼容与迁移

- 旧 V1/V2 配置缺少 `engine` 段时，行为和 run identity 保持原版本规则。
- Legacy V1 代码冻结功能，只修复安全、读取和历史兼容问题。
- 既有 V1/V2 artifacts、报告重建和 acceptance 回归继续通过。
- 新用户文档只讲 `fast_matrix` 与 `event`；V1 放入 legacy/历史说明。
- 旧正式 run 永不迁移成 `fm-*`，`fm-*` 也不伪装成旧正式 run。

## 19. 开发前仍需冻结的决定

A31 开始实现前，应在验收文档中最终确认：

1. `engine` 配置字段名和旧配置默认行为；
2. TargetSchedule 是 SignalSnapshot 新表还是独立子产物；
3. MatrixResearchRun 的根目录、identity 和最小表集合；
4. 第一版是否同时支持 `mark_close` 与 funding `exclude_symbol`；
5. 浮点逐字段容差或 exact/ULP 政策；
6. 代表性性能基线与最低加速门；
7. 第一次真实端到端性能验收使用的中性验收配置、区间和数据范围；该配置不登记为用户策略。

这些决定必须先写入 A31，不应在编码过程中由实现细节偶然决定。
