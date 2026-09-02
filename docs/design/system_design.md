# 回测系统总体设计

## 1. 目标与边界

`bfbt` 的目标是建立一个面向 Binance USDⓈ-M 永续合约的、可复现的截面因子研究与组合回测系统。第一版解决的是分钟级及以上的研究问题，不是逐笔或订单簿级仿真。

第一版必须支持：

- 在任意历史时点构造当时可交易的永续合约池。
- 一次读取同一时刻全部合约的量价等数据，计算截面因子。
- 独立设置基础数据频率、因子频率、调仓频率和持有周期。
- 计算 IC、Rank IC、分层收益、因子覆盖率和换手率。
- 将因子分数转换为多头、空头或多空组合权重。
- 模拟下一根 K 线成交、手续费、滑点、换手和资金费率。
- 保存完整配置、数据版本、中间结果和最终报告。

第一版明确不做：

- 秒级逐笔撮合、盘口排队和市场冲击的订单簿仿真。
- 真实强平、ADL、保证金阶梯的完整交易所风控复刻。
- 现货、COIN-M、期权或多个交易所的统一回测。
- 在回测循环中访问实盘 Client、账户 API 或下单 API。
- 自动把研究策略部署到实盘。

## 2. 系统输入

一次回测由五类输入共同确定。

### 2.1 数据快照 `DatasetSnapshot`

指定本次运行使用的数据版本，而不是含糊地读取“当前最新文件”：

```text
dataset_id
dataset_version
schema_version
available_from / available_to
source_manifest_hash
quality_report_id
```

它解析为以下只读数据集：

- 成交价格 K 线。
- 标记价格 K 线。
- 资金费率。
- 合约元数据历史快照。
- 可选的指数价格、溢价指数、持仓量和聚合成交。

### 2.2 合约池配置 `UniverseSpec`

来自 `configs/universe.yaml`，至少包括：

- 市场类型：USDⓈ-M、`PERPETUAL`、USDT 保证金。
- 上市冷启动天数。
- 最少有效历史长度。
- 滚动成交额门槛。
- 缺失数据容忍度。
- 显式排除列表。

输出必须是按时间变化的资格表，不能是单个静态 symbols 列表。

### 2.3 因子定义 `FactorSpec`

因子定义由代码和配置组成：

```text
factor_name
factor_version
required_columns
lookback_bars
compute_interval
parameters
preprocess_pipeline
```

因子实现只允许读取传入的历史数据窗口，不允许自行访问网络或读取未来标签。

### 2.4 组合与执行配置 `BacktestSpec`

来自 `configs/backtest.yaml`：

- 回测起止时间和时区。
- 因子观察频率、调仓频率、持有周期。
- 多空分位数和权重方法。
- 总敞口、净敞口、单币权重和换手约束。
- 成交价格、信号延迟、手续费和滑点模型。
- 是否计入资金费率和使用标记价格估值。

### 2.5 代码版本与随机种子

运行清单必须保存 Git commit、Python/依赖版本和随机种子。相同代码、数据快照和配置应产生相同结果。

## 3. 系统输出

每次运行生成独立的 `run_id` 目录：

```text
data/backtest/runs/<run_id>/
├── manifest.json
├── resolved_config.json
├── summary.json
├── equity_curve.parquet
├── returns.parquet
├── positions.parquet
├── trades.parquet
├── costs.parquet
├── factor_values.parquet
├── factor_diagnostics.parquet
├── ic.parquet
├── quantile_returns.parquet
├── universe.parquet
├── warnings.jsonl
└── report.html
```

### 3.1 必须输出

- `manifest.json`：代码、数据、配置和环境指纹。
- `summary.json`：收益、风险、换手、成本和覆盖率摘要。
- `equity_curve.parquet`：净值、收益、回撤、总/净敞口。
- `positions.parquet`：每个时点、每个 symbol 的目标权重和实际权重。
- `trades.parquet`：因调仓产生的权重变化、成交价和成交金额。
- `costs.parquet`：手续费、滑点和资金费率分项。
- `universe.parquet`：每个时点的可交易资格及被排除原因。

### 3.2 因子研究输出

- 因子覆盖率、缺失率和截面分布。
- Pearson IC 和 Spearman Rank IC 时间序列。
- IC 均值、标准差、IR、正 IC 比例。
- 分位数组合收益和多空价差。
- 因子自相关、分组迁移和换手率。
- 因子值与未来收益标签的对齐审计。

### 3.3 组合回测输出

- 毛收益、净收益、年化收益和波动率。
- Sharpe、Sortino、Calmar 和最大回撤。
- 多头、空头、资金费率和交易成本贡献。
- 总敞口、净敞口、单币集中度和持仓数量。
- 按 symbol、月份和市场状态的收益归因。

## 4. 端到端工作流

```text
1. discover
   发现公共归档文件、REST 增量范围和合约元数据
                              ↓
2. ingest
   下载原始文件、校验 checksum、登记 manifest
                              ↓
3. normalize
   解析字段、统一 UTC 和类型、去重、写入临时分区
                              ↓
4. validate + compact
   质量检查、原子替换正式 Parquet 分区、更新 catalog
                              ↓
5. build universe
   根据历史状态、上市时间、成交额和数据完整度生成资格表
                              ↓
6. compute factor
   读取必要列和窗口、计算因子、截面变换、生成未来收益标签
                              ↓
7. evaluate factor
   IC、Rank IC、分层收益、覆盖率、稳定性和换手诊断
                              ↓
8. construct portfolio
   选择多空标的、生成目标权重、应用暴露和换手约束
                              ↓
9. simulate
   信号延迟、下一根 K 线成交、价格 PnL、手续费、滑点、资金费率
                              ↓
10. report
    固化运行清单、Parquet 明细、指标摘要和 HTML 报告
```

下载、标准化和回测是三个独立命令。回测运行期间禁止为了补数据临时请求 Binance。

## 5. 核心功能分层

### 5.1 数据采集层

- 枚举历史归档和目标合约。
- 支持月包为主、日包补尾部、REST 修复小缺口。
- 并发下载、超时、有限重试和请求限速。
- checksum 校验和幂等跳过。
- 保存 HTTP 状态、文件大小、checksum 和采集时间。

### 5.2 标准化与质量层

- 将 Binance CSV/JSON 转为固定 Arrow schema。
- 所有时间转为 UTC，内部使用毫秒精度。
- 以数据集主键去重并排序。
- 检查 OHLC 关系、非正价格、负成交量、时间间隔和重复数据。
- 区分“合约未上市”“合约无交易”和“数据缺失”。
- 输出机器可读质量报告，严重错误阻断分区发布。

### 5.3 数据目录与查询层

- Catalog 管理数据集版本、分区、时间范围、schema 和质量状态。
- DataStore 根据字段、时间、symbols 和版本返回惰性查询。
- DuckDB 用于目录查询、审计和临时 SQL 分析。
- Polars LazyFrame 用于因子和面板计算。

### 5.4 时点合约池

资格计算顺序：

1. 元数据表明该时刻已上市且属于永续合约。
2. 该时刻没有已知下架或暂停状态。
3. 上市时间达到冷启动要求。
4. 观察窗口数据完整度达到门槛。
5. 滚动成交额等流动性指标达到门槛。
6. 不在显式排除名单中。

每条不合格记录都要保存 `reason_code`，便于审计生存者偏差。

### 5.5 因子计算与研究

- 支持单因子和多个因子批量计算。
- 支持时间序列 rolling 和同一时刻 cross-sectional 操作。
- 标准预处理：缺失过滤、去极值、截面排序/标准化、可选中性化。
- 标签计算和因子计算分离。
- 允许缓存稳定的因子结果，缓存键包含数据版本、因子版本和参数。

### 5.6 组合构建

第一版支持：

- Top/Bottom 固定数量。
- Top/Bottom 分位数。
- 等权、因子分数权重和波动率倒数权重。
- 多头、空头、多空市场中性。
- 总敞口、净敞口、单币上限和最大换手。

权重构建必须在当期有效 universe 内完成，并在约束后重新归一化。

### 5.7 向量化回测引擎

第一版采用目标权重驱动，而非订单事件驱动：

```text
factor scores → target weights → delayed fills → realized positions → PnL/costs
```

基本时序约定：

- 因子使用截至 `t` K 线收盘已经可见的数据。
- 默认在 `t+1` K 线开盘成交。
- `t` 与 `t+1` 之间不得使用 `t+1` 的成交量或最高/最低价决定权重。
- 交易收益按成交价格到下一估值时点计算。
- 资金费率只在真实 `funding_time` 且当时持有仓位时计入。

资金费率现金流使用统一符号：

```text
funding_cashflow = -signed_notional * funding_rate
```

其中多头 `signed_notional > 0`，正资金费率时多头支付、空头收取。

### 5.8 报告与可复现性

- 所有最终表都写 Parquet，JSON/YAML 只保存小型清单和摘要。
- 报告读取已经固化的结果，不重新运行回测。
- warning 不能只打印到终端，必须写入运行目录。
- 失败运行保留状态和错误信息，但不得发布不完整结果为成功 run。

## 6. 正确性约束

### 6.1 无未来函数

- rolling 窗口右端不得超过因子时间。
- 使用收盘价形成的信号至少延迟一根 bar。
- universe、成交额过滤和截面标准化只使用当期可见样本。
- future return 标签与因子输入分表保存。

### 6.2 无生存者偏差

- 不允许用今天的 `exchangeInfo` symbols 直接代表历史合约池。
- 使用元数据快照和第一/最后有效 K 线推断历史可用区间。
- 已下架合约的历史数据必须保留。

### 6.3 成本不重复

- 权重变化只在成交时产生手续费和滑点。
- 资金费率不能同时计入价格收益与独立现金流两次。
- 毛收益、各成本项和净收益必须能逐行对账。

### 6.4 确定性

- 相同输入重复运行，核心 Parquet 输出的内容 hash 应一致。
- 同一数据分区的重复导入必须幂等。
- 数据和结果的正式发布使用临时文件加原子重命名。

## 7. 性能设计

- 标准数据使用长表，不保存“一个 symbol 一列”的永久宽表。
- Raw 层按 Binance 原始组织保留；Normalized 层按数据类型、interval、年月分区。
- Normalized 分区内按 `(open_time, symbol)` 排序，便于同时读取一个历史截面。
- 不按 symbol 细分标准化 Parquet，避免截面读取打开数百个小文件。
- 通过 Polars `scan_parquet` 和 DuckDB projection/filter pushdown 仅读取所需列与时间范围。
- 计算按月或按可配置时间块执行，跨块 rolling 时额外读取 lookback overlap。
- 只有单个截面或最终小结果才允许转成 Pandas；大面板保持 Arrow/Polars。

## 8. 第一版验收场景

系统完成第一版时，应能稳定执行以下场景：

1. 下载并标准化指定年份全部 USDT 永续合约的 1m K 线。
2. 从 1m 数据确定性生成 5m、15m、1h 和 4h 数据。
3. 构建每小时变化的 point-in-time universe。
4. 计算一个 24 小时动量因子和一个成交额因子。
5. 输出 Rank IC、五分位收益和 Top/Bottom 多空组合。
6. 模拟下一根 K 线开盘成交、双边手续费、固定滑点和资金费率。
7. 在相同 commit、数据版本和配置下重复运行并得到一致结果。
