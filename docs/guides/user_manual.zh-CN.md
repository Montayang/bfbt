# BFBT 用户使用手册

[English](user_manual.md)

本文面向需要在本地使用 Binance USDⓈ-M 永续合约数据进行截面因子研究和正式
回测的用户。当前版本号为 `0.1.0`，A01–A10 功能已经实现，并已用 8 个真实合约、
真实 1 分钟行情和资金费率完成全链路验收。

## 1. 先了解边界

bfbt 是离线研究系统，不是实盘下单程序：

- 只使用 Binance 公共市场数据，不需要 API key，也不会读取仓库根目录 `.env`。
- 不包含实盘交易 Client，不会访问账户、余额或下单接口。
- 当前支持 Binance USD-M、USDT 保证金、PERPETUAL 合约。
- 基础事实数据为 1m trade bars、mark bars、funding 和 contracts；可派生更高周期。
- 支持时点化合约池、内建截面因子、研究诊断、多空组合、下一根 K 线成交、
  手续费、滑点、资金费率、mark 估值、分块运行和不可变结果发布。
- 当前没有“任意范围一条命令完成下载到 DatasetSnapshot”的通用命令。下载、
  标准化和 Catalog CLI 已实现，但自定义数据集仍需准备脚本组合分区并生成
  `DatasetSnapshotManifest`。现有真实小样本准备器可作为模板。
- 一年全市场属于容量验收，不是当前低内存服务器的日常流程。

## 2. 核心概念

正式回测不是直接读取一批 CSV，而是使用一条不可变的数据链：

```text
Binance archive/REST
  → Raw 文件 + RawObjectManifest
  → 标准化 Parquet + QualityReport + PartitionManifest
  → DuckDB Catalog
  → DatasetSnapshot（精确绑定四类数据版本和分区）
  → bfbt run
  → 不可变 run artifacts
```

需要区分三个版本：

- `schema_version`：字段、类型和语义版本，当前事实表为 `v1`。
- `dataset_version`：由来源对象 checksum、标准化代码和参数共同决定。
- `DatasetSnapshot.dataset_version`：一次正式运行所使用的 bars、mark bars、
  funding 和 contracts 的组合版本。

系统不接受 `latest` 作为正式版本。这样同一数据、配置、代码和依赖可以定位到
同一个 run；任一输入变化都会形成新身份。

所有时间区间采用 UTC 左闭右开语义 `[start, end)`。配置和命令中的时间必须带
`Z` 或明确 UTC offset。

## 3. 安装与环境

进入克隆后的仓库根目录：

```bash
cd /path/to/bfbt
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[test]"
bfbt --help
```

以后每次新终端只需：

```bash
cd /path/to/bfbt
source .venv/bin/activate
```

主要依赖包括 Polars、PyArrow、DuckDB、Pydantic、HTTPX、PyYAML 和 Typer。
数据和产物统一放在仓库的 `data/backtest/`，不提交 Git。

## 4. 最短可用路径：复现真实小样本回测

仓库不附带行情数据。完成入门教程的小样本下载，或在相同目录放置已有的兼容数据后，
所有路径按数据集、catalog、工作配置、run 和外部报告分层：

```text
data/backtest/
├── datasets/tutorial/
├── catalogs/tutorial.duckdb
├── workspaces/tutorial/{configs,logs}/
├── runs/<run_id>/
└── reports/<run_id>/
```

### 4.1 设置路径

```bash
cd /path/to/bfbt
source .venv/bin/activate

DATA_ROOT=data/backtest
DATASET_ROOT="$DATA_ROOT/datasets/tutorial"
DB="$DATA_ROOT/catalogs/tutorial.duckdb"
CONFIG_ROOT="$DATA_ROOT/workspaces/tutorial/configs"
LOG_ROOT="$DATA_ROOT/workspaces/tutorial/logs"
RUN_ROOT="$DATA_ROOT/runs"
REPORT_ROOT="$DATA_ROOT/reports"
```

如果 Raw 已存在但尚未标准化，使用独立参数生成其余层：

```bash
python tests/live/prepare_real_backtest_smoke.py \
  "$DATASET_ROOT" "$DATASET_ROOT" \
  --database "$DB" \
  --config-root "$CONFIG_ROOT" \
  --runs-root "$RUN_ROOT"
```

准备器会重新校验 Raw 文件大小和 SHA-256，但不会重新联网下载。

### 4.2 正式运行

将准备器输出的精确版本填入命令：

```bash
DATASET_ID=binance-usdm-real-e2e-smoke-2026-06
DATASET_VERSION=live-smoke-替换为实际值

bfbt run \
  "$DATASET_ID" "$DATASET_VERSION" momentum \
  --database "$DB" \
  --data-config "$CONFIG_ROOT/data.json" \
  --universe-config "$CONFIG_ROOT/universe.json" \
  --factor-config "$CONFIG_ROOT/factor.json" \
  --backtest-config "$CONFIG_ROOT/backtest.json" \
  --verify-hashes \
  | tee "$LOG_ROOT/run-momentum.log"
```

成功输出 `status=succeeded`、全局唯一的 `run_id` 和 `run_path`。数据集目录不会出现
run；正式产物统一发布到 `data/backtest/runs/<run_id>`。

### 4.3 验证结果

```bash
RUN_ID=a09-替换为实际值

python tests/live/validate_real_backtest_smoke.py \
  "$RUN_ROOT/$RUN_ID"

bfbt performance inspect "$RUN_ID" \
  --output-root "$RUN_ROOT"
```

run 内的 `report.html` 是默认英文不可变发布产物；同一新 run 还包含显式英文
`report.en.html` 和独立简体中文 `report.zh-CN.html`。需要用新版渲染器重建时写到集中报告
目录，命令会同时生成三者：

```bash
mkdir -p "$REPORT_ROOT/$RUN_ID"
bfbt report "$RUN_ID" \
  --output-root "$RUN_ROOT" \
  --output "$REPORT_ROOT/$RUN_ID/report.html"
```

## 5. 四份配置文件

默认模板位于 `configs/`。模板中的 `null` 表示用户尚未做出决定，正式运行前必须
补齐；不要直接把草稿当作可运行配置。

运行前校验：

```bash
bfbt config validate \
  --data configs/data.yaml \
  --universe configs/universe.yaml \
  --factor configs/factor.yaml \
  --backtest configs/backtest.yaml \
  --run-ready
```

使用 `bfbt config show` 可查看默认值展开、路径稳定化后的完整配置。建议每次
正式运行都保留四份配置，不用命令行临时逻辑替代配置。

### 5.1 data.yaml

关键字段：

- `time.base_interval`：当前正式流程通常使用 `1m`。
- `time.start/end`：研究核心区间，不包含因子历史 overlap 和末端成交尾部。
- `derived_intervals`：允许派生的更高周期，如 `5m`、`1h`、`4h`。
- `storage.root/normalized/metadata`：本地数据位置。
- `validation.max_partition_missing_ratio`：标准化分区缺失率门。
- `source.allow_authenticated_endpoints`：当前应保持 `false`。

### 5.2 universe.yaml

合约池在每个 schedule 时点重建。主要过滤项：上市年龄、最少历史 bars、滚动成交
额、滚动缺失率和显式排除列表。

`use_contract_snapshots=true` 只适用于确实拥有覆盖回测时段的历史 exchangeInfo
快照。若只有今天抓取的 exchangeInfo 而回测过去行情，应设置：

```yaml
point_in_time:
  enabled: true
  use_contract_snapshots: false
  use_first_last_valid_bar: true

filters:
  trading_status_only: false
```

此时上市/退市边界和 symbol 集合从当时有效 trade bars 推导；当前快照不会被
回填到历史。不要伪造历史 snapshot_time。

### 5.3 factor.yaml

当前内建因子均为 `v1`：

```bash
bfbt research list-factors
```

可用名称：

- `momentum`：过去收益，可配置 `lookback` 和 `skip_recent`。
- `reversal`：短期反转。
- `realized_volatility`：已实现波动率。
- `quote_volume`：滚动成交额。
- `taker_buy_ratio`：主动买入成交额占比。
- `amihud_illiquidity`：单位成交额对应的绝对价格变化滚动均值。

预处理按配置顺序执行，支持分位数 winsorize、z-score 和 rank。预处理顺序、窗口
和 compute interval 都进入版本指纹。标签只用于研究评价，不允许进入因子输入。

### 5.4 backtest.yaml

正式运行必须明确填写：

- `run.start/end/dataset_version`。
- 手续费模型和 bps，或明确选择 `zero`。
- 滑点模型和 bps，或明确选择 `zero`。
- funding 是否启用及缺失策略。
- output root 和性能预算。

组合支持：

- `long_short_quantile`：填写 long/short quantile，count 必须为空。
- `long_short_count`：填写 long/short count。
- weighting：`equal`、`score` 或 `inverse_volatility`。

当前成交模型固定为 `next_bar_open`，不支持 partial fill。`signal_delay_bars=1`
表示信号不会用同一根 K 线立即成交。

资金费率缺失策略：

- `error`：正式研究推荐；缺失即失败。
- `exclude_symbol`：排除没有资金费率输入的合约。
- `assume_zero`：将缺失视为 0，只适合明确接受该偏差的场景。

估值可选择 `mark_close` 或 `trade_close`。使用 `mark_close` 时 DatasetSnapshot 必须
包含覆盖运行区间和成交尾部的 mark bars。

## 6. 从零获取公共数据

以下命令不需要 API key。建议先用一个 symbol、一个日包验证网络和目录，再扩大
范围。低内存服务器将 `--workers` 设为 1。

### 6.1 初始化 Catalog

```bash
DATA_ROOT=data/my-dataset
RAW_ROOT="$DATA_ROOT/raw"
RAW_MANIFESTS="$DATA_ROOT/manifests/raw"
DB="$DATA_ROOT/catalog.duckdb"

bfbt catalog init --database "$DB"
bfbt catalog info --database "$DB"
```

### 6.2 规划和下载 archive

离线查看候选对象：

```bash
bfbt data archive-plan bars BTCUSDT \
  2025-01-01T00:00:00Z 2025-02-01T00:00:00Z \
  --interval 1m --frequency monthly
```

真实下载并验证官方 checksum、ZIP CRC、文件 hash：

```bash
bfbt data archive-sync bars BTCUSDT \
  2025-01-01T00:00:00Z 2025-02-01T00:00:00Z \
  --interval 1m --frequency monthly --workers 1 \
  --raw-root "$RAW_ROOT" --manifest-root "$RAW_MANIFESTS" \
  --database "$DB"

bfbt data archive-sync mark_bars BTCUSDT \
  2025-01-01T00:00:00Z 2025-02-01T00:00:00Z \
  --interval 1m --frequency monthly --workers 1 \
  --raw-root "$RAW_ROOT" --manifest-root "$RAW_MANIFESTS" \
  --database "$DB"

bfbt data archive-sync funding BTCUSDT \
  2025-01-01T00:00:00Z 2025-02-01T00:00:00Z \
  --frequency monthly --workers 1 \
  --raw-root "$RAW_ROOT" --manifest-root "$RAW_MANIFESTS" \
  --database "$DB"
```

覆盖率检查不会联网：

```bash
bfbt data archive-coverage bars BTCUSDT \
  2025-01-01T00:00:00Z 2025-02-01T00:00:00Z \
  --interval 1m --frequency monthly \
  --raw-root "$RAW_ROOT" --manifest-root "$RAW_MANIFESTS"
```

### 6.3 REST 补增量和元数据

archive 尚未发布的最近数据可通过 `rest-klines`、`rest-funding` 分页补取。每页
响应都作为不可变 Raw JSON 保存。用 `--help` 查看完整分页参数：

```bash
bfbt data rest-klines --help
bfbt data rest-funding --help
```

抓取当前公开快照：

```bash
bfbt data snapshot exchange-info \
  --raw-root "$RAW_ROOT" --manifest-root "$RAW_MANIFESTS" \
  --database "$DB"

bfbt data snapshot funding-info \
  --raw-root "$RAW_ROOT" --manifest-root "$RAW_MANIFESTS" \
  --database "$DB"
```

exchangeInfo 是采集时刻的快照，不是历史真相。

### 6.4 标准化

一个标准化批次应属于同一 UTC 月。示例：

```bash
bfbt data normalize bars \
  "$RAW_MANIFESTS/archive-bars-BTCUSDT-1m-monthly-2025-01.json" \
  --raw-root "$RAW_ROOT" \
  --normalized-root "$DATA_ROOT/normalized" \
  --partition-manifest-root "$DATA_ROOT/manifests/partitions" \
  --quality-root "$DATA_ROOT/quality" \
  --database "$DB"
```

命令会再次验证 Raw 大小/SHA-256，解析 schema，运行质量门，原子发布 Parquet、
QualityReport 和 PartitionManifest，并登记 Catalog。重复运行相同输入应返回
`already_published`。

上面的 CLI 示例适合单批次。跨 symbol 或跨月分批处理同一逻辑数据集时，不能让
每一批各自生成互不相同的 dataset version 后再强行合并。准备脚本应先调用
`build_normalization_release()`，用全部 Raw object ID/checksum 固定一个 release，
再让各有界批次共享该 release。真实小样本和年度准备器都展示了这一做法。

预览标准化数据：

```bash
bfbt data normalized-scan bars DATASET_VERSION \
  2025-01-01T00:00:00Z 2025-01-02T00:00:00Z \
  --interval 1m --columns open_time,symbol,close \
  --normalized-root "$DATA_ROOT/normalized" \
  --database "$DB" --verify-hashes
```

### 6.5 生成 DatasetSnapshot

正式 `bfbt run` 只接受已经登记到 Catalog 的 DatasetSnapshot。当前应编写一个
项目级准备脚本，完成以下工作：

1. 为 bars、mark_bars、funding、contracts 选择精确 dataset version。
2. 绑定明确的 PartitionManifest 和 QualityReport ID。
3. 确保 bars 覆盖最大因子/universe 历史窗口及成交尾部。
4. 确保 mark/funding 覆盖核心运行区间和成交尾部。
5. 写出 `dataset-snapshot.json`，调用 `catalog.register_dataset()`，并生成四份配置。

参考实现：`tests/live/prepare_real_backtest_smoke.py`。全年多批次参考实现：
`tests/live/prepare_capacity_2025.py`，但不建议在当前低内存服务器直接运行。

## 7. 正式回测与产物

正式命令的一般形式：

```bash
bfbt run DATASET_ID DATASET_VERSION FACTOR_NAME \
  --database /path/to/catalog.duckdb \
  --data-config /path/to/data.yaml \
  --universe-config /path/to/universe.yaml \
  --factor-config /path/to/factor.yaml \
  --backtest-config /path/to/backtest.yaml \
  --verify-hashes
```

成功目录：

```text
<output_root>/<run_id>/
├── manifest.json
├── resolved_config.json
├── environment.json
├── run_metadata.json
├── metrics.json
├── performance.json            # chunked 且 collect_diagnostics=true
├── warnings.json
├── report.html                 # render_html=true
└── tables/
    ├── universe.parquet
    ├── factor_values.parquet
    ├── targets.parquet
    ├── trades.parquet
    ├── positions.parquet
    ├── costs.parquet
    └── returns.parquet
```

`manifest.json` 绑定 Git commit、未提交源码 fingerprint、Python、依赖、配置、
DatasetSnapshot、schema、factor 和所有 artifact hash。不要手工修改成功 run 目录。

失败的正式 run 也会发布 terminal manifest 和 error/config/environment，便于复盘；
它不会伪装成成功产物。

## 8. 如何阅读结果

新版 `report.html` 会直接说明因子公式和中文含义、截面选多空与期末持仓；核心指标
直接显示。交互净值曲线支持悬停查看收益、回撤、敞口、换手和成本，点击有界快照点
可以查看当时持仓与关联成交。数据版本、完整参数、详细指标、成交和警告按需展开，
指标与表头均使用“中文 / English”。
期末持仓表示最后估值时刻仍在账面的仓位，当前引擎不会为了报告美观而在回测边界
伪造一笔强制平仓交易。

旧的不可变 run 可以使用本节末尾的 `bfbt report` 命令重建到外部 HTML。

先看 `report.html` 和 `metrics.json`，再按问题读取 Parquet：

- `universe`：每个时点每个 symbol 是否可交易及 reason code。
- `factor_values`：raw value、预处理 value、是否有效及无效原因。
- `targets`：截面分组后的目标方向和权重。
- `trades`：信号时间、成交时间、参考价、含滑点成交价和换手。
- `positions`：数量、名义敞口、实际权重、mark price 和未实现盈亏。
- `costs`：fee、slippage、funding cashflow 和总成本。
- `returns`：gross、成本、funding、net、equity、drawdown、敞口和 turnover。

成本字段是对组合净值的贡献。fee/slippage 以正成本记录并从净收益扣除；funding
收入为正、支出为负。

`metrics.json` 包含：

- performance：总收益、年化收益/波动率、Sharpe、Sortino、最大回撤、Calmar、
  命中率和观测数。
- risk：平均/最大 gross exposure、absolute net exposure 和 turnover。
- attribution：gross price、fee、slippage、funding 与 net contribution，并用
  `maximum_identity_error` 检查收益恒等式。

短区间的年化指标会非常夸张，只能用于链路检查，不应作为策略有效性的证据。

重建报告不会重跑回测，且会先验证 artifact：

```bash
bfbt report RUN_ID \
  --output-root /path/to/runs \
  --output /path/outside/run/rebuilt.html
```

输出文件必须位于不可变 run 目录之外。

## 9. 研究和回测预览

预览命令适合调试单个阶段，不发布正式 run：

- `bfbt data resample-preview`：验证 1m 到更高周期的 UTC 聚合。
- `bfbt universe preview`：查看时点合约池和过滤 reason code。
- `bfbt research preview`：查看因子、label、IC、quantile、coverage、turnover。
- `bfbt backtest preview`：查看 targets、trades、positions、costs、returns。

这些命令要求显式提供 `history_start`，研究/回测预览还要求 `future_end`。CLI 不会
发现历史不足后自动联网补数据。完整参数以各命令 `--help` 为准；可复制的已验证
示例位于 `docs/acceptance/A06.md` 至 `docs/acceptance/A08.md`。

## 10. 性能模式和低内存建议

`performance.mode`：

- `in_memory`：适合很小的数据集和作为分块结果的基准。
- `chunked`：正式运行推荐，按时间块计算 factor/universe/targets，并用跨块账本
  执行交易。

当前约 2 GiB RAM 的服务器建议从下面的保守值开始：

```yaml
performance:
  mode: chunked
  chunk_interval: 1d
  max_input_rows_per_chunk: 250000
  max_incremental_rss_mib: 512
  collect_diagnostics: true
```

先缩短时间或减少 symbol 验证逻辑，再扩大规模。不要为了通过测试直接提高内存门。
正式一年全市场回测应迁移到内存更充足的机器。

查看分块计划和成功诊断：

```bash
bfbt performance plan \
  2025-01-01T00:00:00Z 2025-02-01T00:00:00Z \
  --chunk-interval 1d --overlap-seconds 90000

bfbt performance inspect RUN_ID --output-root /path/to/runs
```

清理异常退出遗留的临时 workspace 时，先 dry-run：

```bash
bfbt performance clean-work \
  --output-root /path/to/runs \
  --older-than-hours 24 --dry-run
```

确认目标后才使用 `--apply`。该命令只识别带 marker、进程已死亡且达到年龄阈值的
`.work/a10-*`，不会删除已发布 run。

## 11. Catalog 管理

常用命令：

```bash
bfbt catalog info --database /path/catalog.duckdb
bfbt catalog coverage bars DATASET_VERSION --database /path/catalog.duckdb
bfbt catalog resolve DATASET_ID SNAPSHOT_VERSION --database /path/catalog.duckdb
```

Catalog 是可重建控制面，不存放主要行情。数据库损坏或迁移时可从 manifests 重建：

```bash
bfbt catalog rebuild /path/to/manifests \
  --database /path/to/new-catalog.duckdb
```

重建按 Raw → Partition → DatasetSnapshot → Run 校验引用，并在全部成功后原子替换。

## 12. 常见问题

### `config validate --run-ready` 失败

通常是 start/end/dataset_version、手续费或滑点仍为 null，或者组合 construction 与
quantile/count 字段混用。先执行 `bfbt config show` 查看展开配置。

### `no partitions overlap the requested constraints`

检查 DatasetSnapshot 的精确版本、时间覆盖、基础 interval 和 Catalog 中的 Partition
时间边界。若是历史回测配当前 exchangeInfo，确认 `use_contract_snapshots=false`，
不要伪造快照时间。

### `dataset member ... does not cover ...`

Snapshot 没有覆盖因子历史或成交尾部。bars 起点必须早于 run start 至少最大
lookback/universe window；末端必须覆盖 signal delay 后的下一根 K 线。

### `target input has no rows`

检查 universe 是否全部被 WARMUP、MISSING_DATA、ILLIQUID 等原因排除；也要确认
截面数量足以满足 long/short quantile 或 count。

### funding 缺失导致失败

优先补齐真实 funding 数据。只有明确接受研究偏差时才改为 `exclude_symbol` 或
`assume_zero`。

### Binance 返回 403/451、DNS 或超时

这是网络/地区访问问题，不等同于本地计算失败。Raw 下载支持重复执行，已校验对象
会跳过；不要用账户 API key 绕过公共历史数据流程。

### run ID 与上次不同

检查 Git 工作区、未提交文件、依赖版本、配置规范化结果和 DatasetSnapshot。当前
实现会把仓库内未提交 diff 与 untracked 文件加入 source fingerprint；
仅新增文档或测试文件也可能改变正式 run ID。

### 服务器内存不足

停止年度全市场任务，改用少量 symbol/短区间验证；选择 chunked、缩短
`chunk_interval`、降低单块行数，并将标准化拆成小批次。不要在实盘下单服务器上
同时运行大规模标准化和回测。

## 13. Fast Matrix 常规截面研究

Fast Matrix 是 Event 引擎经济语义下的研究后端，不是正式回测的替代品。配置使用
`engine.backend=fast_matrix`、`engine.purpose=research`；准备好版本化行情 parquet、规范化
TargetSchedule parquet、完整调仓时间 JSON 和父 SignalSnapshot hash 后运行：

```bash
bfbt research matrix-run targets.parquet bars.parquet \
  --rebalance-times rebalance_times.json \
  --parent-manifest-sha256 <sha256> \
  --market-identity <dataset-identity> \
  --backtest-config backtest.yaml
```

结果发布在 `data/backtest/research_runs/fm-*/`，报告会明确标记为研究结果。选定候选后应将
后端改为 `event`、用途改为 `formal` 并运行正式流程；动态保证金、止盈止损、冷却或事件仲裁
策略会由能力规划器拒绝 Fast Matrix。

一次研究项目包含大量因子时，快速研究与 Fast Matrix 分开查看。成功项目含 `summary.json`
后可重建报告：

```bash
bfbt research study-report data/backtest/research_studies/<study_id> \
  --matrix-runs-root data/backtest/research_runs
```

输出约定：

- `report.html`：只做工作流导航；
- `quick_research.html`：按 `study / period / factor / horizon` 搜索、筛选和排序；
- `fast_matrix.html`：按 `study / period / factor / cost / fm-run` 检索组合结果；
- `fast_matrix_reports/<fm-run>.html`：含因子、执行口径、成本、敞口、净值和身份审计的单
  run 增强报告。

已有 `fm-*` 是不可变产物，重建展示报告不会覆盖其文件或改变 manifest。

## 14. 进一步文档

- `docs/acceptance/real_e2e.md`：已完成的真实全链路验收和实测数字。
- `docs/reference/configuration.md`：全部配置字段及校验规则。
- `docs/reference/data_contract.md`：事实表、账本表、manifest 和 artifact 契约。
- `docs/reference/data_management.md`：数据目录、分区、版本和 Catalog 原则。
- `docs/reference/interfaces.md`：模块接口和职责边界。
- `docs/acceptance/A01.md` 至 `docs/acceptance/A10.md`：分阶段测试手册。

查看任意命令的当前参数时，以本地安装版本为准：

```bash
bfbt --help
bfbt data --help
bfbt run --help
```
