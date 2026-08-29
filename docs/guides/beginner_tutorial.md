# bianbt 傻瓜式入门教程

这份教程只做一件事：让第一次接触本项目的人，用真实 Binance 永续合约数据跑出
一份完整回测报告。

你不需要 API key，不会连接交易账户，也不会真实下单。先不要修改策略、因子或
数据结构，完整跑通一次后再研究高级配置。

如果你已经熟悉命令行和数据版本，请阅读更完整的
[`user_manual.md`](user_manual.md)。

## 最终会得到什么

完成后会得到一个回测目录，其中包括：

- `report.html`：最容易阅读的网页报告。
- `metrics.json`：收益、回撤、Sharpe 等汇总指标。
- `tables/trades.parquet`：每笔模拟成交。
- `tables/positions.parquet`：每分钟持仓。
- `tables/returns.parquet`：每分钟收益和净值。
- `manifest.json`：证明本次回测使用了哪些数据、配置和代码。

教程固定使用以下真实数据：

- 8 个合约：BTC、ETH、BNB、SOL、XRP、DOGE、ADA、LINK。
- 1 分钟 trade K 线和 mark-price K 线。
- 真实资金费率。
- 回测时间：所选月份的第 8 日至第 15 日，共 7 天。
- 策略：24 小时 momentum 截面排名，每 4 小时调仓，同时做多 2 个、做空 2 个。
- 成交：信号出现后，使用下一根 1 分钟 K 线开盘价成交。
- 成本：4 bps 手续费、2 bps 滑点和真实资金费率。

这些参数只用于验证系统能否正确工作，不代表推荐策略。

## 第一步：打开正确的目录

打开终端，复制：

```bash
cd /path/to/bianbt
```

确认位置：

```bash
pwd
```

应该看到：

```text
/path/to/bianbt
```

后面的命令都在这个目录执行。

## 第二步：启动 Python 环境

### 已经创建过 `.venv`

直接复制：

```bash
source .venv/bin/activate
```

命令行左侧通常会出现 `(.venv)`。再确认 CLI 可用：

```bash
bianbt --help
```

只要能看到 `run`、`data`、`catalog` 等命令，就可以继续。

### 首次安装还没有 `.venv`

依次复制：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[test]"
bianbt --help
```

如果安装依赖时失败，先不要继续，查看文末“常见错误”。

## 第三步：选择数据来源

只选择下面的一条路线。

### 路线 A：复用已有本地数据

如果你已经按相同目录保存了教程 Raw 数据，可以直接复用：

```bash
DATA_ROOT=data/backtest
SOURCE="$DATA_ROOT/datasets/tutorial"
TARGET="$DATA_ROOT/datasets/tutorial"
DB="$DATA_ROOT/catalogs/tutorial.duckdb"
CONFIG_ROOT="$DATA_ROOT/workspaces/tutorial/configs"
LOG_ROOT="$DATA_ROOT/workspaces/tutorial/logs"
RUN_ROOT="$DATA_ROOT/runs"
REPORT_ROOT="$DATA_ROOT/reports"
```

确认源数据存在：

```bash
test -d "$SOURCE/raw" && echo "数据存在，可以继续"
```

显示“数据存在，可以继续”后，直接跳到第四步。如果没有输出，请使用路线 B。

### 路线 B：为全新 checkout 下载小样本（推荐）

这条路线需要访问 Binance 公共数据服务，但不需要 API key。新下载、标准化结果、
工作配置、run 和外部报告仍分别进入固定目录：

```bash
DATA_ROOT=data/backtest
SOURCE="$DATA_ROOT/datasets/tutorial"
TARGET="$DATA_ROOT/datasets/tutorial"
DB="$DATA_ROOT/catalogs/tutorial.duckdb"
CONFIG_ROOT="$DATA_ROOT/workspaces/tutorial/configs"
LOG_ROOT="$DATA_ROOT/workspaces/tutorial/logs"
RUN_ROOT="$DATA_ROOT/runs"
REPORT_ROOT="$DATA_ROOT/reports"
RAW_ROOT="$SOURCE/raw"
RAW_MANIFESTS="$SOURCE/manifests/raw"

mkdir -p "$DATA_ROOT/catalogs" "$LOG_ROOT"
bianbt catalog init --database "$DB"
```

下载 8 个合约的 2026 年 6 月数据。整段复制即可。你也可以把三处时间同时改成
另一个完整月份，但 bars、mark bars 和 funding 必须使用同一个月：

```bash
for SYMBOL in BTCUSDT ETHUSDT BNBUSDT SOLUSDT XRPUSDT DOGEUSDT ADAUSDT LINKUSDT
do
  echo "正在下载 $SYMBOL trade bars"
  bianbt data archive-sync bars "$SYMBOL" \
    2026-06-01T00:00:00Z 2026-07-01T00:00:00Z \
    --interval 1m --frequency monthly --workers 1 \
    --raw-root "$RAW_ROOT" --manifest-root "$RAW_MANIFESTS" \
    --database "$DB" || break

  echo "正在下载 $SYMBOL mark bars"
  bianbt data archive-sync mark_bars "$SYMBOL" \
    2026-06-01T00:00:00Z 2026-07-01T00:00:00Z \
    --interval 1m --frequency monthly --workers 1 \
    --raw-root "$RAW_ROOT" --manifest-root "$RAW_MANIFESTS" \
    --database "$DB" || break

  echo "正在下载 $SYMBOL funding"
  bianbt data archive-sync funding "$SYMBOL" \
    2026-06-01T00:00:00Z 2026-07-01T00:00:00Z \
    --frequency monthly --workers 1 \
    --raw-root "$RAW_ROOT" --manifest-root "$RAW_MANIFESTS" \
    --database "$DB" || break
done
```

每个成功对象会显示 `downloaded`，已经存在的对象会显示 `skipped`。两者都正常。
如果中途出现错误并停止，先解决该错误，再原样执行一次；已经校验过的数据不会
重复下载。

最后下载公开合约信息：

```bash
bianbt data snapshot exchange-info \
  --raw-root "$RAW_ROOT" \
  --manifest-root "$RAW_MANIFESTS" \
  --database "$DB"
```

确认三类行情各有 8 个 manifest：

```bash
find "$RAW_MANIFESTS" -name 'archive-bars-*.json' | wc -l
find "$RAW_MANIFESTS" -name 'archive-mark_bars-*.json' | wc -l
find "$RAW_MANIFESTS" -name 'archive-funding-*.json' | wc -l
```

三行都应该显示 `8`。如果不是 8，不要进入下一步，重新检查下载日志。

## 第四步：把 Raw 数据转换成回测数据

确认当前仍在仓库根目录，并且命令行左侧有 `(.venv)`。

复制：

```bash
mkdir -p "$LOG_ROOT"
python tests/live/prepare_real_backtest_smoke.py "$SOURCE" "$TARGET" \
  --database "$DB" \
  --config-root "$CONFIG_ROOT" \
  --runs-root "$RUN_ROOT" \
  | tee "$LOG_ROOT/prepare.log"
```

这个过程会：

1. 再次检查下载文件大小和 SHA-256。
2. 把 ZIP/JSON 转换成标准化 Parquet。
3. 检查数据质量。
4. 建立本地 DuckDB Catalog。
5. 生成四份已经填好的回测配置。

在低内存机器上，看到内存短时间升高是正常的。不要同时启动第二份准备任务。

成功时最后会显示类似：

```text
dataset_id=binance-usdm-real-e2e-smoke-2025-01
dataset_version=live-smoke-xxxxxxxxxxxxxxxxxxxxxxxx
database=/.../data/backtest/catalogs/tutorial.duckdb
config_root=/.../data/backtest/workspaces/tutorial/configs
runs_root=/.../data/backtest/runs
```

准备器会自动选择源目录中唯一完整的月份；如果源目录有多个月份，应在命令最后添加
例如 `--month 2026-01` 的参数明确指定月份。

最重要的是前两行。复制完整的 `dataset_id` 和 `dataset_version`。

如果 `TARGET` 已被以前一次不完整的测试使用，最简单的处理方式不是删除，而是换
一个新名字后重试，例如：

```bash
TARGET="$DATA_ROOT/datasets/tutorial-02"
DB="$DATA_ROOT/catalogs/tutorial-02.duckdb"
CONFIG_ROOT="$DATA_ROOT/workspaces/tutorial-02/configs"
```

然后重新执行本步骤。

## 第五步：填写刚才复制的数据集和版本

下面等号右侧只是例子，必须替换为你在第四步看到的两个完整值：

```bash
DATASET_ID="binance-usdm-real-e2e-smoke-把这里替换成实际月份"
DATASET_VERSION="live-smoke-把这里替换成实际值"
```

检查是否替换成功：

```bash
echo "$DATASET_ID"
echo "$DATASET_VERSION"
```

两行输出中都不应该再有中文“把这里替换成实际值/月份”。

## 第六步：正式运行回测

整段复制：

```bash
bianbt run \
  "$DATASET_ID" \
  "$DATASET_VERSION" \
  momentum \
  --database "$DB" \
  --data-config "$CONFIG_ROOT/data.json" \
  --universe-config "$CONFIG_ROOT/universe.json" \
  --factor-config "$CONFIG_ROOT/factor.json" \
  --backtest-config "$CONFIG_ROOT/backtest.json" \
  --verify-hashes \
  | tee "$LOG_ROOT/run-momentum.log"
```

成功时最后会显示：

```text
run_id=a09-xxxxxxxxxxxxxxxxxxxxxxxx
status=succeeded
run_path=/.../runs/a09-xxxxxxxxxxxxxxxxxxxxxxxx
publication=published
catalog=inserted
```

只要 `status=succeeded`，完整回测就已经完成。

复制第一行中 `a09-...` 的完整值，并填写：

```bash
RUN_ID="a09-把这里替换成实际值"
```

检查：

```bash
echo "$RUN_ID"
```

如果显示 `status=failed`，不要只看最后一行。向上查找 `Run failed:` 后面的错误，
也可以查看：

```bash
cat "$LOG_ROOT/run-momentum.log"
```

## 第七步：一键检查结果是否完整

复制：

```bash
python tests/live/validate_real_backtest_smoke.py \
  "$RUN_ROOT/$RUN_ID" \
  | tee "$LOG_ROOT/validate.log"
```

成功时会输出一段 JSON，并且不出现 `AssertionError` 或 `Traceback`。重点看：

```text
"status": "succeeded"
"memory_budget_passed": true
"trades": 180
```

使用完整月度数据时，正常情况下还能看到：

- `universe`：1,344 行。
- `factor_values`：1,344 行。
- `targets`：140 行。
- `trades`：180 行。
- `positions`、`costs`、`returns`：均非空；具体行数可能随月份变化。

收益数字可能与已有验收结果一致，但不要把这 7 天的收益当成策略表现证明。

## 第八步：找到 HTML 报告

报告位置：

```bash
echo "$RUN_ROOT/$RUN_ID/report.html"
```

确认文件存在：

```bash
test -f "$RUN_ROOT/$RUN_ID/report.html" && echo "报告已生成"
```

如果服务器没有图形界面，可以通过 VS Code 文件浏览器找到该文件并下载到电脑，
再用浏览器打开。不要直接编辑 run 目录里的报告或其他文件。

## 第九步：验证相同输入不会产生第二份结果

原样重新执行第六步的 `bianbt run` 命令。

如果代码、数据和配置完全没变，应该看到：

```text
run_id=与第一次相同
publication=already_published
catalog=already_registered
```

这表示回测可复现，并且系统没有重复覆盖已有结果。

如果 run ID 变了，最常见的原因是你修改、新增了仓库内的文件。系统会把
未提交代码和文档也纳入环境指纹。这不是随机误差。

## 第十步：先看懂四个最重要的结果

第一次使用时只看下面四项即可。

### 1. `metrics.json`

```bash
cat "$RUN_ROOT/$RUN_ID/metrics.json"
```

重点字段：

- `total_return`：这段时间的总收益。
- `ending_equity`：从初始净值 1.0 运行后的最终净值。
- `max_drawdown`：最大回撤，通常是负数。
- `sharpe_ratio`：风险调整收益；7 天样本的年化数值不可靠。

### 2. `report.html`

用于快速查看净值和总结，不需要理解 Parquet。

### 3. `tables/trades.parquet`

保存所有模拟成交。当前验收应有 BUY 和 SELL，成交时间应晚于信号时间。

### 4. `tables/costs.parquet`

保存手续费、滑点和资金费率。三者非零才说明成本路径确实参与了回测。

## 第十一步：想改策略时先改什么

确认原始教程完整跑通后，再复制一份目标目录或重新准备一个新目标目录，不要修改
已经发布的 `runs/<run_id>/`。

最常改的是：

```text
$CONFIG_ROOT/factor.json
$CONFIG_ROOT/universe.json
$CONFIG_ROOT/backtest.json
```

简单例子：

- 改 momentum 窗口：编辑 `factor.json` 的 `lookback`。
- 改调仓频率：编辑 `backtest.json` 的 `rebalance_interval`。
- 改手续费和滑点：编辑 `taker_bps`、`bps`。
- 改多空数量：保持 `construction=long_short_count`，修改 `long_count` 和
  `short_count`。

改完后重新运行 `bianbt run`。配置变化会产生新的 run ID，这是正确行为。

不要一开始就修改：

- DatasetSnapshot 或 manifest JSON。
- 标准化 Parquet。
- 已发布 run 目录。
- 原项目的实盘 Client 和策略脚本。

## 常见错误

### `bianbt: command not found`

通常是没有启动虚拟环境：

```bash
cd /path/to/bianbt
source .venv/bin/activate
bianbt --help
```

### `No such file or directory`

先检查当前目录：

```bash
pwd
```

必须是 `/path/to/bianbt`。再检查变量：

```bash
echo "$SOURCE"
echo "$TARGET"
```

### 第四步提示缺少 Raw manifest

路线 A 的源目录不完整，或者路线 B 有合约没有下载成功。执行：

```bash
find "$SOURCE/manifests/raw" -type f | sort
```

检查 8 个 symbol 是否都有 bars、mark_bars、funding，并且存在
`rest-contracts-exchangeInfo-...json`。

### Binance 返回 403、451、DNS 或 timeout

这是网络或地区访问限制，不是策略代码错误。换到可以访问 Binance 公共数据的
网络，之后原样重跑下载命令即可。

### `no partitions overlap the requested constraints`

通常是 `TARGET`、`DATASET_ID`、`DATASET_VERSION` 或配置文件来自不同一次准备结果。
不要混用两个目标目录；重新复制第四步输出的两个值。

### `target input has no rows`

所有合约都被 universe 过滤掉了，或者截面合约数量不足。先恢复教程自动生成的
配置，确认基准流程能通过，再逐项修改过滤条件。

### 服务器变慢或内存升高

不要同时运行两个准备器或两个回测。教程已经使用单进程下载、8 个合约、7 天
回测和 1 天分块。如果机器仍然吃紧，先停止其他大数据任务，不要启动一年全市场
测试。

### 我需要 API key 吗？

不需要。教程只访问公共 archive 和公共 exchangeInfo。不要把交易账户 API key
写入回测配置。

## 完成检查表

依次确认：

- [ ] `bianbt --help` 能正常显示。
- [ ] 第四步输出了 `dataset_id=...` 和 `dataset_version=live-smoke-...`。
- [ ] 第六步输出了 `status=succeeded`。
- [ ] 第七步没有 AssertionError，并显示 `memory_budget_passed=true`。
- [ ] `report.html` 文件存在。
- [ ] 第二次运行显示 `already_published`。

六项全部完成，就说明你已经成功执行了一次真实 Binance 永续合约全链路回测。

后续需要自定义数据集、研究 IC、配置更多因子或理解全部表结构时，再阅读：

- [`user_manual.md`](user_manual.md)：完整用户手册。
- [`acceptance_real_e2e.md`](../acceptance/real_e2e.md)：真实验收记录。
- [`configuration.md`](../reference/configuration.md)：所有配置字段。
- [`data_contract.md`](../reference/data_contract.md)：数据和结果字段定义。
