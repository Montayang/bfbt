# 实施路线与验收标准

## 0. 实现原则

- 先完成数据正确性，再写因子和回测。
- 每个阶段都产生可独立验证的命令和产物。
- 不以 notebook 中“能跑”为完成标准，核心逻辑必须进入 `src/bianbt` 并有测试。
- 第一版只做一个执行语义清晰的向量化引擎，不同时开发事件引擎。

用户验收按 [`acceptance_plan.md`](../acceptance/plan.md) 进一步拆分：Phase 1 的配置、schema、catalog 分别对应 A01、A02、A03，均已验收并推送。Phase 2 对应 A04，Phase 3 对应 A05，Phase 4 对应 A06，Phase 5 对应 A07，均已验收。Phase 6 对应 A08，已完成验收。

## 1. Phase 1：配置、schema 与 catalog

实现：

- Pydantic 配置模型和 YAML 加载。
- Arrow schemas：bars、mark bars、funding、contracts、manifest。
- DuckDB catalog 初始化和迁移。
- run manifest、dataset reference 和内容 hash。

验收：

- 非法 interval、时间范围、分位数和敞口配置会在运行前失败。
- schema 可序列化并带版本。
- catalog 可从空目录创建，并能登记一个测试分区。

## 2. Phase 2：历史数据采集

实现：

- Binance archive discovery。
- ZIP/CHECKSUM 下载和验证。
- REST 分页、限速和增量补齐。
- exchangeInfo/fundingInfo 快照。
- Raw manifest。

验收：

- 重复执行不会重复下载 checksum 相同的文件。
- 网络中断留下的临时文件不会被登记为成功。
- 可列出指定 symbol、interval、月份的下载覆盖率。

具体环境、14 个离线用例和可选联网核验见 [`acceptance_A04.md`](../acceptance/A04.md)。

## 3. Phase 3：标准化、校验与本地存储

实现：

- K 线、mark K 线、funding、contracts normalizer。
- 质量规则和确定性质量报告。
- Parquet part 写入和原子发布。
- DuckDB Catalog 解析与 Polars LazyFrame DataStore。

验收：

- 重复主键、坏 OHLC 和 checksum 错误能被检测。
- 同一输入重复标准化得到相同内容 hash。
- 可扫描一个月全部 symbols 的指定三列，而无需加载完整表。

A05 先允许同月存在多个不可变 part，由 DataStore 统一扫描；跨 part compact 和正式 revision 接纳策略在性能与数据维护阶段实现，不在本验收点静默合并文件。

## 4. Phase 4：重采样与 point-in-time universe

实现：

- 1m → 5m/15m/1h/4h 的确定性聚合。
- 历史 listing/trading 区间推断。
- 上市冷启动、成交额、历史长度和缺失率过滤。
- universe reason code。

验收：

- OHLCV 聚合与手工样例一致。
- 月边界 rolling 正确读取上月 overlap。
- 尚未上市和已经下架的 symbol 不会出现在错误时点。

## 5. Phase 5：因子与研究工具

A07 已实现本节的首批因子、标签、截面变换、研究指标和有界预览 CLI；正式缓存发布留待后续 artifact 阶段。

实现：

- Factor Protocol 和 registry。
- returns、rolling、rank、winsorize、zscore。
- forward return labeler。
- IC/Rank IC、分层收益、覆盖率和换手。
- 因子缓存和版本键。

首批示例因子：

- 24h/7d momentum。
- 短期 reversal。
- realized volatility。
- rolling quote volume/liquidity。
- taker buy ratio。

验收：

- 人工构造数据能证明没有使用未来 bar。
- 因子与标签错一根 bar 的测试会失败。
- 因子只在当期 eligible universe 内标准化。

## 6. Phase 6：组合与回测引擎

A08 已实现并验收本节组合构造、成交和五张账本；正式 artifact 与指标汇总仍留在 Phase 7。


实现：

- Top/Bottom 数量和分位数组合。
- 等权、分数权重、波动率倒数权重。
- 下一 bar 开盘成交。
- fixed bps fee/slippage。
- 资金费率现金流。
- target/actual position、trade 和 cost ledger。

验收：

- 零成本、单币、恒定价格等极简场景可手工对账。
- 正资金费率下，多头现金流为负、空头为正。
- `gross_return - fees - slippage + funding == net_return` 逐行成立。
- 相同输入重复运行输出 hash 一致。

## 7. Phase 7：指标、报告和 CLI

A09 已实现并验收本节指标、terminal run 原子发布、artifact-only HTML、Catalog 登记以及正式 `run/report` CLI。


实现：

- 收益风险指标和归因。
- HTML 报告。
- run artifact 原子发布。
- data/factor/run/report CLI。
- 结构化日志和退出码。

验收：

- 报告可完全从 run artifacts 重建，不重跑回测。
- 失败 run 不会被标记为成功。
- run manifest 能定位到具体 commit、数据版本和配置。

## 8. Phase 8：性能与稳健性

A10 已实现本节分块/overlap、有状态账本、临时 Parquet spool、行数/RSS 预算门、诊断 artifact 和保守清理，并通过本地自动、回归和正式双路径验收。标准化 Parquet 已使用可配置 row group；真实一年全市场容量必须在用户目标机器和本地 DatasetSnapshot 上验收。

实现：

- 分块计算和 lookback overlap。
- Parquet 文件大小和 row group 调优。
- 查询计划和内存峰值观测。
- 缓存清理和版本引用保护。

验收：

- 能在目标机器上处理至少一年全部 USDⓈ-M 永续 1m 数据。
- 峰值内存受配置约束，不要求将完整面板载入 RAM。
- 分块与非分块的小样本结果一致。

## 9. 测试矩阵

### Unit

- schema 和配置校验。
- 时间边界和 interval 工具。
- 重采样聚合。
- factor rolling 和截面变换。
- 权重归一化和约束。
- fee、slippage 和 funding 符号。

### Integration

- ZIP → normalized Parquet。
- REST 分页 → 去重分区。
- catalog → DataStore scan。
- factor → portfolio → engine → artifacts 小型闭环。

### Golden data

在 `tests/fixtures` 保存少量匿名、固定行情和预期输出。Golden data 必须足够小，不能依赖本地完整数据仓库。

### Property tests

- 重采样后 high 不小于 open/close，low 不大于 open/close。
- 约束后总/净敞口满足配置。
- 无交易时 costs 为 0。
- 资金费率多空现金流互为相反数。

## 10. 第一版完成定义

只有同时满足以下条件才算第一版完成：

- 可从空数据目录通过 CLI 建立一年行情数据仓库。
- 可生成时点化 universe。
- 可注册并运行至少三个截面因子。
- 可输出因子诊断和多空组合回测。
- 可计入交易成本和资金费率。
- 所有输出带数据/代码/配置指纹。
- 核心正确性测试通过。
- 全流程不需要实盘 API Key，也不会导入 `bianbot.Clients`。

## 11. 后续版本需求

本节是开发过程中记录的第二版需求来源。现已细化并迁移到
[`v2_design.md`](v2_design.md) 和 [`v2_implementation_plan.md`](v2_implementation_plan.md)；
发生歧义时以这两份第二版文档为准，本节保留用于追踪原始需求。

### 11.1 增量加减仓与保证金规模

当前第一版只支持目标权重调仓。后续版本必须保留该模式，并增加显式的增量仓位
指令，至少覆盖：

- `fixed_margin`：每次信号按固定保证金币种金额加仓或减仓。
- `fixed_notional`：每次信号按固定合约名义价值加仓或减仓。
- `equity_fraction`：每次按调仓时组合净值的一定比例增减仓。
- `position_fraction`：每次按该合约调仓前持仓的一定比例增减仓。

增量模式不能复用目标权重的含义伪装实现。设计和验收时必须同时明确：初始资金与
计价币、杠杆和保证金换算、可用保证金、单合约及组合累计仓位上限、最大连续加仓
次数、反向信号的减仓/平仓语义、零仓位时的比例基数，以及手续费、滑点和资金费率
对后续可用资金的影响。

成交产物必须同时保存调仓前仓位、请求增量、约束后的实际增量和调仓后仓位；分块
执行与非分块执行必须在固定样本上完全等价。该需求完成前，配置和用户手册必须继续
明确标注系统仅支持 `target_weight` 目标权重模式。

### 11.2 精确 Rank 选币与跨快照 Rank

后续版本必须扩展当前只能选择连续 Top/Bottom 区间的无状态选币器，至少支持：

- 按统一且显式的排名方向选择单个 rank、rank 列表和 rank 区间。
- 为多头和空头分别配置任意且互不重叠的 rank 集合。
- 使用当前快照或前 N 个快照的 rank 生成目标仓位。
- 明确 `rank_lag` 基于因子快照还是调仓快照。
- 明确并稳定处理并列值、有效合约数变化、新上市、下架和数据缺失。
- 在产物中保存可审计的原始分数、整数 rank、百分位 rank、样本数、被引用的
  历史快照时间和最终选币原因。

实现跨快照 rank 前必须先完成内存与复杂度评估，不能默认把全部时间和全部合约展开
为常驻内存的稠密矩阵。优先采用以下有界方案：

- 固定 `rank_lag=L` 只保留每个合约最近 `L` 个所需 rank 状态，内存目标为
  `O(N × L)`，其中 `N` 是当期合约数，而不是 `O(T × N)` 全历史常驻内存。
- 只需要上一快照时使用双缓冲或按合约状态表，额外内存目标为 `O(N)`。
- 分块模式必须在 chunk 边界显式携带最小 rank 历史状态，且结果与非分块模式等价。
- 只有研究输出确实需要时才落盘完整 rank 历史；使用分区 Parquet 和列裁剪读取，
  不得为了回测执行一次性 collect 全部历史。
- 对允许的最大 lag、滚动窗口、单块行数和增量 RSS 设置配置门限，超限时明确失败，
  不得依赖系统 OOM。

验收必须同时覆盖精确 rank 语义、跨快照无前视、chunk 边界连续性、分块等价性和
峰值内存门。性能报告需列出合约数、快照数、rank 状态行数、理论状态复杂度和实测
增量 RSS；若固定 lag 的实现复杂度或内存超过上述数量级，必须先完成优化再验收。

### 11.3 止盈止损与事件触发调仓

后续版本必须在固定时间调仓之外增加事件触发的风险检查和调仓机制，至少支持：

- 按单合约持仓设置固定比例止损和止盈。
- 按组合净值设置组合级止损、止盈和最大回撤保护。
- 可选移动止损，并保存随行情更新的触发基准。
- 分别为多头和空头定义对称或不同的触发阈值。
- 明确触发后是仅平仓、部分减仓、反向，还是立即按选币规则补足空缺仓位。
- 明确止盈止损后的冷却期、重新入场条件和最大触发次数。
- 明确定时调仓、止盈止损、合约退出 universe、资金不足和未来强平规则同时发生时
  的优先级。

风险检查频率必须与因子计算频率和定时调仓频率解耦。因子可以使用小时级 K 线，
止盈止损可以使用更细的基础 K 线逐根检查；配置和报告必须分别展示这三个频率。
实现必须明确触发价格与成交语义，包括使用成交价格或标记价格、使用 close 还是
high/low、触发价成交或下一根开盘成交，以及手续费和滑点。若同一根 K 线同时触及
止盈与止损且缺少更细粒度数据，必须采用显式且保守的冲突规则并在报告中告警，不能
自行假定盘中先后顺序。

引擎必须为每笔持仓维护入场均价、最高/最低有利价格、当前止盈止损线、触发原因和
冷却状态。分块模式只携带必要状态，并保证与非分块模式等价；不得为了检查触发条件
将全部历史行情常驻内存。事件产物和报告必须能够审计信号时间、触发时间、触发依据、
触发阈值、参考价格、成交时间、成交价格、调仓前后仓位及成本。

验收必须覆盖多头和空头、止盈和止损、跳空、同 K 线双触发、定时调仓冲突、回测
边界、分块边界、无前视和内存门。使用小时 K 线无法确定盘中路径时，报告必须明确
标注结果依赖冲突处理假设；需要更高精度时，应使用分钟级或更细的风险检查数据。
