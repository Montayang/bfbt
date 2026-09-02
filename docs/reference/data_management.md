# 本地数据管理设计

## 1. 设计原则

- Raw 不可变：下载到的原始 ZIP、CSV、JSON 和 CHECKSUM 永不就地修改。
- Parquet 是标准化行情的事实来源；DuckDB catalog 是可重建的索引和审计层。
- 所有正式数据集都有 schema version 和 dataset version。
- 导入、标准化和压缩操作必须幂等。
- 更新正式分区时先写临时目录，校验通过后原子替换。
- 数据目录不进入 Git；Git 只保存 schema、配置和处理代码。

## 2. 推荐目录

回测数据统一放在仓库根目录的 `data/backtest/`，但按生命周期分层，不再把 Raw、
标准化数据、工作配置、run 和外部报告混在一个 E2E 目录中：

```text
data/backtest/
├── datasets/
│   └── <dataset_name>/
│       ├── raw/
│       ├── normalized/
│       ├── curated/
│       ├── manifests/{raw,partitions}/
│       ├── quality/
│       └── dataset-snapshot.json
├── catalogs/
│   └── <catalog_name>.duckdb
├── workspaces/
│   └── <workspace_name>/
│       ├── configs/
│       └── logs/
├── runs/
│   ├── <run_id>/{manifest.json,metrics.json,performance.json,report*.html,tables/*.parquet}
│   ├── .staging/
│   └── .work/
└── reports/
    └── <run_id>/
        └── <report_name>.html
```

各目录职责固定：

- `datasets` 只保存可复用的数据事实、manifest 和质量报告，不保存策略 run。
- `catalogs` 是可重建控制面；一个 catalog 可以登记多个 DatasetSnapshot 和 run。
- `workspaces` 保存可编辑配置和日志，可以复制或删除，不具有不可变产物语义。
- `runs/<run_id>` 是全局集中、不可变的正式运行产物，不再嵌套在数据集下面。
- `reports/<run_id>` 只保存从 run 重建出的外部展示版本，可以覆盖或生成多个变体。

`runs/<run_id>/report.html` 与 `reports/<run_id>/*.html` 含义不同：前者是发布 run 时
写入 manifest 的冻结报告，不得修改；后者是后来使用 `bfbt report` 从不可变 artifact
重建的展示文件。任何重建报告都不得再散落在 dataset 或 workspace 根目录。

A09 只在集中 runs 根目录的 `.staging` 下准备目录，完整校验后原子改名；A10 的
`.work/a10-*` 保存当前分块运行的中间 Parquet。正常或异常退出会清理；遗留目录也
必须同时满足 marker、死亡 PID 和年龄阈值才可在显式 `--apply` 下删除。已发布
`<run_id>` 永远不属于清理集合。

分块过程不会把年度全合约面板累计在 Python 对象中；内存只保留当前分析/执行块、
小型跨块账本状态和最终逐时间组合收益。

Raw 内部路径继续尽量镜像 Binance 归档；Normalized 路径围绕截面查询设计。

## 3. 为什么标准化层不按 symbol 分区

截面回测的典型查询是“读取某个月全部合约的某些字段”，而不是“读取一个合约的全部历史”。如果按 symbol 分区，一次截面回测需要打开数百个文件。

因此标准化 K 线采用：

```text
dataset / schema_version / dataset_version / interval / year / month
```

每个分区内部按 `(open_time, symbol)` 排序，并控制单文件和 row group 大小。这样时间过滤和列裁剪可以生效，同时避免过多小文件。

Raw 层仍保留 Binance 的 symbol/month 组织，因为它的用途是追溯和重建，不是直接回测。

## 4. 分区与文件大小

初始建议：

- 1m bars：按月分区；数据量过大时同月拆为多个 `part`。
- 5m 及更高频率：仍按月分区，避免年文件过大。
- funding 和 contracts：按年/月分区。
- 因子和 universe：按版本、年、月分区。
- 目标单个 Parquet 文件约 128–512 MiB，避免大量 KB 级小文件。
- Parquet 使用 ZSTD；row group 初始目标 128K–512K 行，后续以实际扫描性能调整。

这些值必须作为存储配置，而不是散落在实现代码中。

## 5. 数据版本

### 5.1 Schema version

字段名称、类型或语义变化时提升，例如 `bars/v1` → `bars/v2`。不同 schema version 不允许静默混读。

### 5.2 Dataset version

同一 schema 下，数据内容变化形成新 dataset version。版本指纹至少包含：

```text
schema_version
source object IDs and checksums
normalizer code version
normalizer parameters
```

Partition 的文件字节 hash 单独保存在 Partition manifest；由 DatasetSnapshot 绑定具体 Partition 及其 manifest hash，避免把下游文件 hash 循环地放回上游 dataset version。

### 5.3 Curated version

派生数据版本包含上游 dataset version 以及派生算法和参数。例如 5m K 线的版本由 1m 数据版本和重采样规则决定。

## 6. Manifest

每个原始对象一条记录：

```text
object_id
source_uri
dataset_name
symbol
interval
available_from / available_to
byte_size
checksum_sha256 / upstream_checksum_sha256
etag
retrieved_at
http_status
```

每个标准化分区一条记录：

```text
partition_path
schema_version
dataset_version
row_count
min_time / max_time
symbols_count
content_hash
source_manifest_ids
quality_report_id
published_at
```

Manifest 使用 JSON Lines 或 Parquet 保存，Catalog 将其注册为可查询表。

A04 延续 A02 的 portable RawObject manifest，不把机器绝对路径写进 manifest。归档本地路径由配置的 `raw_root + remote.relative_path` 确定；REST 路径由 endpoint、symbol、interval、请求 URI 指纹和响应 hash 确定。操作结果 `FetchResult.path` 返回本机实际路径，manifest 通过来源 URI、覆盖区间、字节数和 checksum 绑定内容。

REST 每一页保存为独立 JSON 原始对象，保留 HTTP 响应体字节，尚不解析成标准化事实表。exchangeInfo/fundingInfo 每次采集使用观察时间形成不同对象，避免用当前元数据覆盖历史快照。

A05 的标准化批次先根据 manifest 反推出 portable Raw 路径，并在解析前重新校验文件大小和 SHA-256。一个批次不能跨 UTC 月；同月可以有多个不可变 part。 跨月运行由上层显式组合各月版本并分块扫描。质量报告先于 Parquet 发布，失败批次只留下 `quality/v1` 报告，不登记 Partition。通过批次写入临时 Parquet 后原子改名，再写 Partition manifest 并登记 Catalog。

正式路径中的 `dataset_version` 由 schema fingerprint、normalizer 代码版本与参数、全部来源对象 ID/checksum 确定，不使用 `latest`。A05 DataStore 先从 Catalog 解析这个精确版本的 Partition manifests，再用 Polars LazyFrame 对时间、symbol 和列做过滤；只有调用方 `collect` 时才物化结果。

## 7. DuckDB Catalog

A03 将 catalog 定义为可丢弃、可重建的控制面数据库，当前 schema version 为 `1`。它保存：

- 四个内置 Arrow schema 的名称、精确版本、fingerprint 和逻辑描述。
- RawObject 的业务 ID、来源/覆盖摘要、内容 hash 和完整规范 manifest。
- Partition 的 dataset/schema/version、路径、行数、时间范围、symbol 数、内容 hash、RawObject 与质量报告引用。
- DatasetSnapshot 及其成员数据集、精确 Partition 和质量报告引用。
- Run 及其 DatasetSnapshot hash、schema、质量、因子和 artifact 引用。

所有登记都以 manifest 业务 ID 为键：完全相同内容的重复登记返回幂等成功，同一 ID 对应不同规范内容时拒绝覆盖。Partition、DatasetSnapshot 和 Run 在写入前校验上游引用；关联表和主体写入处于同一事务，失败不得留下半条登记。

版本解析只接受显式 `dataset_id/dataset_version`，不提供 `latest` 别名。Coverage 来自已登记 Partition 元数据，不扫描 Parquet，返回分区数、总行数、最早/最晚数据时刻、单分区最大 symbol 数和质量报告集合。

Catalog 不存放主要行情事实表，行情仍在 Parquet。`catalog rebuild` 递归读取 JSON manifests，按 RawObject → Partition → DatasetSnapshot → Run 的依赖顺序建立唯一临时数据库；全部登记成功后才原子替换目标。任何验证或引用失败都保留原 catalog，并清理本次精确命名的临时文件。

## 8. 数据更新策略

### 8.1 首次回填

1. 读取目标时间范围和数据类型。
2. 发现所有月度归档对象。
3. 下载 ZIP 和 CHECKSUM。
4. 校验后登记 Raw manifest。
5. 标准化为临时月分区。
6. 质量检查并发布。
7. 日包或 REST 只补月包尚未覆盖的尾部。

A04 的发布顺序是：探测 ZIP/CHECKSUM 对 → checksum 文本校验 → ZIP 流式写入同目录 `.part` → SHA-256 与 ZIP CRC 校验 → 原子改名 → 原子写 manifest → 可选登记 catalog。下载批次必须全部成功后，编排服务才开始顺序登记 catalog；这避免下载失败时出现该批次的部分 catalog 记录。

### 8.2 日常增量

- 每日归档可用后下载上一日文件。
- REST 只补归档尚未发布的最近区间。
- 月包发布后，可用经过 checksum 验证的月包重建当月正式分区。
- 同一主键发生内容变化时不能静默覆盖，必须记录 revision。

A04 对已存在且 hash/manifest 完全一致的 Raw 对象返回 `skipped`；本地文件、manifest 或上游 checksum 任一发生冲突都会失败且不覆盖。由于 Binance 官方可能修订归档，A04 先把这种情况显式暴露为 immutable conflict，revision 接纳策略在标准化阶段实现后再加入。

本地归档覆盖率状态为 `missing`、`partial`、`unmanifested`、`orphan_manifest`、`verified` 或 `conflict`，只检查本地文件和 manifest，不发起网络请求。

### 8.3 缺口修复

- 先判断缺口属于未上市、暂停交易还是数据异常。
- 小范围缺口可走 REST。
- 修复后重新生成整个受影响的标准化分区，避免原地追加产生重复。
- 所有修复写入 revision log。

## 9. 数据质量规则

### 9.1 K 线

- 主键 `(open_time, symbol, interval)` 唯一。
- `open_time < close_time`。
- `low <= min(open, close) <= max(open, close) <= high`。
- 价格严格大于 0。
- volume、quote volume、trades 非负。
- taker buy volume 不得显著大于总 volume；允许微小浮点容差。
- 同一 symbol 相邻 bar 的时间间隔符合 interval。

### 9.2 资金费率

- 主键 `(funding_time, symbol)` 唯一。
- `funding_rate` 可正可负，但必须是有限数。
- funding time 必须与该 symbol 的有效交易区间相交。
- 不硬编码八小时一次，因为 Binance 可能调整 funding interval。

### 9.3 合约元数据

- 每个 snapshot_time、symbol 唯一。
- `contractType`、quote asset、margin asset 必须显式保存。
- filter 中的 tick size 和 step size 作为交易约束来源，不用 pricePrecision 替代。

## 10. 覆盖率与缺失语义

覆盖率表至少包含：

```text
dataset, symbol, interval, date,
expected_rows, actual_rows, duplicate_rows,
missing_rows, first_time, last_time, quality_status
```

缺失分为：

- `NOT_LISTED`：当时尚未上市。
- `DELISTED`：已经结束交易。
- `NO_TRADES`：存在但该 bar 无成交；是否生成空 bar 由明确规则决定。
- `SOURCE_MISSING`：官方源缺失。
- `INGEST_FAILED`：下载或解析失败。
- `FILTERED`：质量规则主动排除。

回测不得把这些状态全部简单填充为 0。

## 11. 重采样与时间边界

- 所有窗口以 UTC 对齐。
- 1m → Nm 只使用完整、连续的源 bar。
- 月/日分区边界不等于 rolling 窗口边界；计算时读取前一分区的 lookback overlap。
- `open_time` 表示 bar 左边界，`close_time` 表示数据可用边界。
- 因子默认在 close_time 之后才可见。

## 12. 备份与清理

- Raw 和 manifests 是最重要的可恢复资产，应定期备份。
- Normalized/curated 可由 Raw 和代码重建，但重建成本较高，可按容量选择备份。
- `tmp` 中断后可安全清理；正式分区不得由通配符递归删除。
- 清理旧 dataset version 前，先检查是否仍被某个 run manifest 引用。
