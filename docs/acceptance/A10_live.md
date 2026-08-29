# A10 真实联网与年度全市场容量验收

## 1. 范围

本手册是 `acceptance_A10.md` 的联网补充，不属于默认 pytest。测试访问
Binance USD-M 公共 REST 和 `data.binance.vision`，不读取 `.env`，不需要
API key。

本轮固定研究区间为左闭右开的
`[2025-01-01T00:00:00Z, 2026-01-01T00:00:00Z)`，正好 365 天。Raw 下载额外覆盖
2024-12 和 2026-01，用于 25 小时 factor/universe overlap 和末端成交/估值。

## 2. 本地位置

真实数据不提交 Git，容量数据也遵循统一分层：

```text
data/backtest/
├── datasets/capacity-2025/{raw,normalized,manifests,quality}/
├── catalogs/capacity-2025.duckdb
├── workspaces/capacity-2025/{configs,logs}/
├── runs/<run_id>/
└── reports/<run_id>/
```

下载和运行均在独立 tmux 窗口执行。年度数据量很大，但不得再把 catalog、配置、
日志和 run 写进 dataset 根目录。

## 3. 全市场口径

symbol 清单来自测试当日的真实 `/fapi/v1/exchangeInfo`：

- `contractType=PERPETUAL`
- `quoteAsset=USDT`
- `marginAsset=USDT`
- `onboardDate < 2026-01-01`
- `deliveryDate=0` 或 `deliveryDate > 2025-01-01`

该口径得到 582 个在 2025 年任一时点可能存在的 USDT 永续合约。当前
exchangeInfo 不是历史快照，因此年度运行明确设置
`use_contract_snapshots=false`，可交易起止时点由首末有效 trade bar 保守推导，
不会把 2026 年快照状态回填到 2025 年。

## 4. 下载与补缺

三类 archive 使用项目 CLI 下载，所有对象逐个验证官方 SHA-256、ZIP CRC，并以
原子方式发布 Raw 和 manifest：

```bash
bianbt data archive-sync DATASET SYMBOL \
  2024-12-01T00:00:00Z 2026-02-01T00:00:00Z \
  --frequency monthly --workers 4 ...
```

真实市场包含 Unicode symbol `币安人生USDT`。Binance 为它提供 trade 和 funding
archive，但没有 mark-price archive。本轮不排除该合约，而是通过公共
`/fapi/v1/markPriceKlines` 补齐 mark bars。REST 页若跨 UTC 月界，会重新按月界
拆成不重叠页；原跨月 Raw 保留审计，但准备器输出 `quarantine` 并不纳入月分区
release。

## 5. 有界标准化 release

完整年度不能一次放进 Python 内存。`build_normalization_release()` 先把某数据集
全部 Raw object ID、SHA-256、schema、normalizer 代码版本和参数绑定为一个不可变
`dataset_version`，随后允许多个有界批次共享该版本。

每个批次只能使用 release 已绑定来源；注入额外 Raw 会失败。年度准备器按
`UTC month + 4 symbols` 处理 trade/mark，funding 使用更大的小批次：

```bash
DATA_ROOT=data/backtest
DATASET_ROOT="$DATA_ROOT/datasets/capacity-2025"
DB="$DATA_ROOT/catalogs/capacity-2025.duckdb"
CONFIG_ROOT="$DATA_ROOT/workspaces/capacity-2025/configs"
RUN_ROOT="$DATA_ROOT/runs"

python tests/live/prepare_capacity_2025.py \
  "$DATASET_ROOT" \
  --database "$DB" \
  --config-root "$CONFIG_ROOT" \
  --runs-root "$RUN_ROOT"
```

每批仍执行 Raw 大小/SHA-256 复核、解析、schema 校验、质量门、Parquet 原子发布
和 Catalog 登记。准备完成后生成 DatasetSnapshot 和四份正式运行配置。

## 6. 年度正式运行

```bash
bianbt run binance-usdm-full-market-2025 DATASET_VERSION momentum \
  --database "$DB" \
  --data-config "$CONFIG_ROOT/data.json" \
  --universe-config "$CONFIG_ROOT/universe.json" \
  --factor-config "$CONFIG_ROOT/factor.json" \
  --backtest-config "$CONFIG_ROOT/backtest.json" \
  --verify-hashes
```

容量配置固定为：

```json
{
  "mode": "chunked",
  "chunk_interval": "1d",
  "max_input_rows_per_chunk": 5000000,
  "max_incremental_rss_mib": 1536,
  "collect_diagnostics": true
}
```

机器只有约 1.9 GiB RAM，因此使用一天块和 1536 MiB 增量 RSS 硬门。若超预算，
验收必须失败并缩短块长，不能提高预算掩盖问题。

## 7. 通过条件

- 真实 archive/REST 下载命令退出码均为 0。
- 582-symbol 清单和 Raw 数量、字节数有记录。
- 中文合约 trade、mark、funding 都能进入标准化数据。
- DatasetSnapshot 四成员版本明确，覆盖所需历史和执行尾部。
- 正式 run 覆盖 365 天，状态为 `succeeded` 并发布完整 artifacts。
- `performance.json` 所有块低于 500 万输入行，且
  `memory_budget_passed=true`。
- run manifest hash 校验通过，`.work` 无残留活动 part。
- 同配置复跑得到相同 run ID 和 `already_published`。

## 8. 其他真实联网链路

除年度 archive 全链路外，本轮还执行：

1. `exchange-info` 与 `funding-info` 公共 REST 快照到 Raw/Catalog。
2. 最近已收盘 BTCUSDT 1m REST 分页，经 Raw、标准化、质量门、Parquet、
   Catalog 和带 hash 扫描。
3. Unicode 合约 archive checksum/manifest 与缺失 mark 的 REST 月界补取。

最终实测数字和 run ID 在本轮完成后追加到本手册。
