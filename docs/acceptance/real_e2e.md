# 真实币安永续回测全链路验收

## 验收目标

本验收只验证系统逻辑，不承担全年全市场容量测试。固定使用 8 个高流动性
USDT 永续合约（BTC、ETH、BNB、SOL、XRP、DOGE、ADA、LINK）在
`[2025-01-08, 2025-01-15)` 的真实数据。

链路覆盖：官方 archive Raw 与 checksum、标准化、质量报告、Parquet、Catalog、
DatasetSnapshot、时间点合约池、24h momentum 截面排名、每 4h 调仓、下一根 K 线
开盘成交、手续费、滑点、资金费率、mark price 估值、净值与指标、原子 artifact、
HTML 报告、hash 校验和相同配置复跑。

当前 exchangeInfo 是未来快照，因此配置明确关闭历史合约快照回填；上市边界由
首末有效 trade bar 推导。测试不需要 API key。

## 数据准备

准备器复用 A10 联网流程已经从 Binance 下载并校验的 2025-01 Raw ZIP，不会重新
下载，也不会继续处理一年全市场数据：

```bash
cd backtest
source .venv/bin/activate
DATA_ROOT=data/backtest
SOURCE="$DATA_ROOT/datasets/capacity-2025"
TARGET="$DATA_ROOT/datasets/real-e2e"
DB="$DATA_ROOT/catalogs/real-e2e.duckdb"
CONFIG_ROOT="$DATA_ROOT/workspaces/real-e2e/configs"
RUN_ROOT="$DATA_ROOT/runs"

python tests/live/prepare_real_backtest_smoke.py \
  "$SOURCE" "$TARGET" \
  --database "$DB" \
  --config-root "$CONFIG_ROOT" \
  --runs-root "$RUN_ROOT"
```

`TARGET` 只包含 normalized Parquet、quality、partition manifests 和 DatasetSnapshot；
Catalog、四份 JSON 配置与 runs 分别写入 `DB`、`CONFIG_ROOT` 和 `RUN_ROOT`。

## 正式运行

使用准备器输出的 `dataset_version`：

```bash
bianbt run binance-usdm-real-e2e-smoke-2025-01 DATASET_VERSION momentum \
  --database "$DB" \
  --data-config "$CONFIG_ROOT/data.json" \
  --universe-config "$CONFIG_ROOT/universe.json" \
  --factor-config "$CONFIG_ROOT/factor.json" \
  --backtest-config "$CONFIG_ROOT/backtest.json" \
  --verify-hashes
```

结果层验证：

```bash
python tests/live/validate_real_backtest_smoke.py \
  "$RUN_ROOT/RUN_ID"
```

运行采用 1 天分块、25 万行/块和 512 MiB 增量 RSS 硬门，适配当前服务器。

## 通过条件

- 四类真实数据均完成标准化，覆盖 8 个 symbol 和运行所需 25h 历史/成交尾部。
- 正式 run 状态为 `succeeded`，并存在 factor、universe、targets、trades、
  positions、costs、returns、metrics、performance、manifest 和 HTML 报告。
- universe 同时出现 eligible 记录，factor/targets/trades/positions 均非空。
- costs 中手续费、滑点和资金费率路径均有真实记录；returns 和 metrics 数值有限。
- manifest 中所有 artifact 的大小和 SHA-256 校验通过。
- `performance.json` 通过行数与 512 MiB 增量 RSS 预算。
- 完全相同的命令复跑得到相同 run ID 和 `already_published`。

## 实测结果（2026-07-31）

最终稳定环境下的 run ID 为 `a09-f56d4c47a988c81ab7f63cc9`；紧接着使用完全
相同的 source fingerprint、依赖、数据和配置复跑，返回同一 run ID、
`publication=already_published` 和 `catalog=already_registered`。

- Raw 来源：Binance 官方 2025-01 monthly archive，以及真实 exchangeInfo REST。
- 标准化：trade bars 357,120 行、mark bars 357,120 行、funding 744 行、
  contracts 850 行。
- 回测产物：universe 1,344 行、factor 1,344 行（有效 1,288）、targets 140 行、
  trades 180 行、positions 39,364 行、costs 264 行、returns 10,082 行。
- 成本路径：fee `0.008966456590972866`、slippage
  `0.004483228295486433`、funding cashflow `0.00007597486589486231`，均非零。
- 初始权益 `1.0`，结束权益 `1.0340539178535315`，区间收益
  `0.03405391785353151`。这些只是逻辑验收观测值，不代表策略有效性。
- 14 个发布 artifact 的集合、大小与 SHA-256 全部通过；每个分析块均读取
  850 行真实 exchangeInfo，所有块低于 25 万行，512 MiB 增量 RSS 门通过。
- A10 定向自动回归：15 passed。

首次正式尝试暴露了“关闭历史 contract snapshots 时仍强制按历史区间扫描当前
exchangeInfo”的问题；修复后，未来快照只满足 typed contracts 输入，不会进入
历史 backward as-of 状态，历史 symbol 集合只从当时有效 trade bars 推导。另将
毫秒时间列的半开上界从 `+1us` 修正为 `+1ms`，避免上界比较取整后漏掉快照行。
