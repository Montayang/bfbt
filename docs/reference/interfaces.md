# 核心接口契约

本文定义模块之间的边界。未标注阶段的示例仍是接口草图；第 2 节数据源边界已在 A04 落地，第 3–4 节的标准化发布与版本固定 DataStore 已在 A05 落地。

## 1. 通用数据类型

核心模块交换 Arrow/Polars 长表，不交换任意嵌套字典。配置进入运行层前必须验证为不可变模型。

```python
class TimeRange:
    start: datetime
    end: datetime

class DatasetRef:
    name: str
    version: str
    schema_version: str

class RunContext:
    run_id: str
    dataset: DatasetRef
    git_commit: str
    random_seed: int
```

时间范围统一采用左闭右开 `[start, end)`，避免分页和分区边界重复。

## 2. 数据源接口

A04 的精确类型是 `ArchiveDiscoveryRequest`、`RemoteArchiveObject`、`FetchResult` 和 `RestPage`。归档源实现 discover/fetch；REST 源以迭代器返回经过端点契约验证的 page，再交给 `RawRestStore` 发布。

```python
class BinanceArchiveSource:
    def discover(self, request: ArchiveDiscoveryRequest) -> list[RemoteArchiveObject]: ...
    def fetch(self, obj: RemoteArchiveObject, *, raw_root: Path,
              manifest_root: Path, catalog: DuckDBCatalog | None = None) -> FetchResult: ...
```

约束：

- `discover` 不下载正文。
- `fetch` 只写 Raw 层，不直接写标准化数据。
- `FetchResult` 包含对象 ID、本机路径、HTTP 状态、字节数、checksum、ETag 和时间；URL 固化在 RawObject manifest。
- 已存在且 checksum 一致时返回 `SKIPPED`，保持幂等。
- 时间范围统一为 UTC 左闭右开；调用 Binance 使用包含式 `endTime` 时转换为 `end - 1ms`。
- 公共 HTTP 层拒绝 `Authorization` 和 `X-MBX-APIKEY`，接口不接收 API key 参数。
- REST 分页必须验证行数、symbol、时间单调性、边界和游标推进。

具体适配器：

- `BinanceArchiveSource`：日/月 ZIP 和 CHECKSUM。
- `BinanceRestSource`：最近数据增量和小缺口修复。
- `BinanceRestSource.exchange_info/funding_info`：保存公开元数据快照。

## 3. 标准化接口

```python
class Normalizer(Protocol):
    dataset_name: str
    schema_version: str

    def normalize(self, raw_objects: list[RawObject]) -> NormalizedBatch: ...

class Validator(Protocol):
    def validate(self, batch: NormalizedBatch) -> QualityReport: ...
```

`NormalizedBatch` 必须带：

- 固定 Arrow schema。
- 目标分区键。
- 原始文件引用。
- 行数、最小/最大时间和内容 hash。

只有 `QualityReport.status == PASS` 的 batch 才能发布到正式分区。

## 4. Catalog 与 DataStore

```python
class Catalog(Protocol):
    def register_raw(self, result: FetchResult) -> None: ...
    def publish_partition(self, partition: PartitionManifest) -> None: ...
    def resolve(self, dataset: str, version: str) -> DatasetSnapshot: ...
    def coverage(self, dataset: str) -> CoverageTable: ...

class MarketDataStore(Protocol):
    def scan_bars(
        self,
        dataset: DatasetRef,
        time_range: TimeRange,
        interval: str,
        columns: list[str],
        symbols: list[str] | None = None,
    ) -> pl.LazyFrame: ...

    def scan_funding(...) -> pl.LazyFrame: ...
    def scan_contracts(...) -> pl.LazyFrame: ...
```

DataStore 返回惰性查询。调用者不能假定数据已经完整加载到内存。

A05 的实际实现为 `NormalizationService`、`ParquetPublisher` 和 `ParquetDataStore`。当前 DataStore 直接解析指定 `dataset_name/dataset_version` 的 Partition manifests；`DatasetSnapshot` 仍由后续运行编排层组合多个数据集版本，A05 不提供浮动 `latest` 或扫描时自动下载。

## 5. 重采样接口

A06 已实现本节接口语义。实际入口 `resample_bars` 返回 `ResampleResult(frame=LazyFrame, dataset_version=...)`；不完整窗口保留并明确标记，调用方不得在因子计算中静默使用。

```python
class BarResampler(Protocol):
    def resample(
        self,
        bars: pl.LazyFrame,
        source_interval: str,
        target_interval: str,
    ) -> pl.LazyFrame: ...
```

聚合规则固定为：

- `open`：第一条。
- `high`：最大值。
- `low`：最小值。
- `close`：最后一条。
- volume、quote volume、trades、taker buy volume：求和。
- 目标 bar 只有在所需源 bar 完整时才标为完整。

## 6. UniverseBuilder

A06 已实现 `build_schedule` 和 `build_point_in_time_universe`。合约状态和 bar 指标均使用 backward as-of 连接；精确相等的 close/snapshot 时点可见，未来记录不可见。输出还包含稳定原因码、滚动指标、上游版本和 universe version。

```python
class UniverseBuilder(Protocol):
    def build(
        self,
        contracts: pl.LazyFrame,
        bars: pl.LazyFrame,
        spec: UniverseSpec,
        schedule: pl.LazyFrame,
    ) -> pl.LazyFrame: ...
```

输出 schema：

```text
timestamp, symbol, is_eligible, reason_code,
listing_age_days, history_bars, rolling_quote_volume, missing_ratio
```

`reason_code` 使用稳定枚举，例如：

```text
NOT_LISTED, NOT_PERPETUAL, WRONG_MARGIN_ASSET, NOT_TRADING,
WARMUP, INSUFFICIENT_HISTORY, ILLIQUID, MISSING_DATA, EXPLICITLY_EXCLUDED
```

## 7. 因子接口

A07 实际入口为 `compute_factor`，使用显式 registry 和版本固定的 bars/universe；只输出当期 eligible 样本，invalid 行保留原因码。五个 v1 名称为 `momentum`、`reversal`、`realized_volatility`、`quote_volume` 和 `taker_buy_ratio`。

```python
class Factor(Protocol):
    name: str
    version: str
    required_columns: tuple[str, ...]
    lookback_bars: int

    def compute(
        self,
        bars: pl.LazyFrame,
        universe: pl.LazyFrame,
        params: Mapping[str, Any],
    ) -> pl.LazyFrame: ...
```

输出严格为：

```text
timestamp, symbol, factor_name, factor_version, value, is_valid
```

截面变换作为独立 pipeline：

```python
class FactorTransform(Protocol):
    def apply(self, values: pl.LazyFrame, universe: pl.LazyFrame) -> pl.LazyFrame: ...
```

标准实现包括 winsorize、rank、zscore 和可选回归中性化。

## 8. 标签与研究接口

A07 实际入口为 `compute_forward_returns` 和 `evaluate_factor`。标签明确输出 entry/exit 时间及价格；Evaluator 返回 lazy 的 IC、分层收益、coverage 和 rank turnover，成功对齐数保存在 `sample_count`/`aligned_valid_count`。

```python
class ForwardReturnLabeler(Protocol):
    def compute(
        self,
        bars: pl.LazyFrame,
        signal_delay_bars: int,
        horizon_bars: int,
        entry_field: str,
        exit_field: str,
    ) -> pl.LazyFrame: ...

class FactorEvaluator(Protocol):
    def evaluate(
        self,
        factors: pl.LazyFrame,
        labels: pl.LazyFrame,
        universe: pl.LazyFrame,
    ) -> FactorEvaluation: ...
```

标签表不能作为 Factor 输入。Evaluator 必须报告因子和标签成功对齐的样本数。

## 9. 组合构建接口

A08 实际入口为 `construct_portfolio`：输入必须固定 `factor_version` 和 `universe_version`，返回 `PortfolioResult(frame, portfolio_version, ...)`。构建器实现 count/quantile、equal/score/inverse-vol 和静态单币上限；依赖实际持仓的最大换手由执行层处理。下列 Protocol 保留为职责说明。


```python
class PortfolioConstructor(Protocol):
    def construct(
        self,
        scores: pl.LazyFrame,
        universe: pl.LazyFrame,
        previous_weights: pl.LazyFrame,
        spec: PortfolioSpec,
    ) -> pl.LazyFrame: ...
```

输出：

```text
signal_time, symbol, score, side, target_weight,
unconstrained_weight, constraint_flags
```

构建器只产生目标权重，不负责决定成交价格或成本。

## 10. 执行与成本接口

A08 实际入口为 `run_vectorized_backtest` 及 `costs.py`、`execution.py`、`funding.py` 中的纯函数。成交固定为下一 bar open，fixed-bps 缺值会失败；资金费现金流收入为正、支出为负。


```python
class ExecutionModel(Protocol):
    def execute(
        self,
        targets: pl.LazyFrame,
        bars: pl.LazyFrame,
        previous_positions: pl.LazyFrame,
    ) -> ExecutionResult: ...

class FeeModel(Protocol):
    def compute(self, trades: pl.LazyFrame) -> pl.LazyFrame: ...

class SlippageModel(Protocol):
    def compute(self, trades: pl.LazyFrame, market: pl.LazyFrame) -> pl.LazyFrame: ...

class FundingModel(Protocol):
    def compute(self, positions: pl.LazyFrame, funding: pl.LazyFrame) -> pl.LazyFrame: ...
```

执行输出至少包含：

```text
signal_time, fill_time, symbol, old_weight, target_weight,
filled_weight, turnover, fill_price, notional, status
```

## 11. 回测引擎接口

A08 的 `BacktestResult` 返回 version-pinned 的 targets、trades、positions、costs、returns 五个 LazyFrame，以及 run ID、结果 hash 和 warnings。A10 的 `StreamingLedger` 按有序市场块调用 `process`，在块间只保留数量、平均成本、请求权重、上一标记价格、净值/峰值、序号和 warnings；重复或倒序块会失败。正式 runner 根据 `backtest.performance.mode` 选择内存或分块路径，两者保持同一五表契约。分块路径另返回确定性 diagnostics 和 `presorted` 标志，供 artifact writer 流式发布。


```python
class BacktestEngine(Protocol):
    def run(
        self,
        context: RunContext,
        data: DatasetSnapshot,
        factor: Factor,
        universe_spec: UniverseSpec,
        backtest_spec: BacktestSpec,
    ) -> BacktestResult: ...
```

引擎负责调度，不应该内置具体因子。它必须逐项返回：

- gross price return。
- fee cost。
- slippage cost。
- funding cashflow。
- net return。
- positions 和 trades。
- warnings 和 coverage。

## 12. Reporter 与 ArtifactStore

A09/A10 实际实现为 `RunArtifactStore`、`capture_environment` 和 `render_report_from_artifacts`。成功与失败均只发布 terminal manifest；成功目录先在同根 staging 写全并校验，再原子改名和登记 Catalog。表格使用 Polars streaming Parquet sink；分块结果已按块有序，不再做全年全局排序。报告只读取 `metrics.json`、`run_metadata.json` 与 `tables/returns.parquet`，不会调用引擎。A10 的 `performance.json` 记录确定性的块边界、行数、预算和通过状态，实际耗时/RSS 只参与本次进程预算门，不进入可复现 artifact。


```python
class ArtifactStore(Protocol):
    def begin_run(self, manifest: RunManifest) -> RunHandle: ...
    def write_table(self, handle: RunHandle, name: str, table: pa.Table) -> None: ...
    def commit_run(self, handle: RunHandle) -> None: ...
    def fail_run(self, handle: RunHandle, error: RunError) -> None: ...

class Reporter(Protocol):
    def render(self, result: BacktestResult, target: Path) -> None: ...
```

ArtifactStore 使用临时目录，只有全部产物成功后才原子发布为正式 `run_id`。

## 13. CLI 边界

A09 新增顶层 `bianbt run DATASET_ID DATASET_VERSION FACTOR` 和 `bianbt report RUN_ID`。正式 run 只接受 Catalog 中精确 DatasetSnapshot；report 先验证 artifact 集合、大小和 hash，并禁止写回不可变 run 目录。A10 增加 `bianbt performance plan`、`inspect` 和 `clean-work`；清理默认 dry-run，只识别已死亡、达到年龄阈值且带 marker 的 `.work/a10-*`。


预定命令：

```text
bianbt data discover
bianbt data download
bianbt data normalize
bianbt data validate
bianbt data compact
bianbt data coverage
bianbt universe build
bianbt factor compute
bianbt factor evaluate
bianbt run
bianbt report
bianbt performance plan
bianbt performance inspect
bianbt performance clean-work
```

CLI 只负责解析配置、调用 application service 和返回退出码，不放置业务计算逻辑。
