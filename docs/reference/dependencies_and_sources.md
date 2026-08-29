# 依赖与外部数据源设计

## 1. 选型结论

第一版不依赖 `bianbot`、币安交易 SDK、CCXT 或 vectorbt。回测数据全部来自公开归档和公开市场数据 REST；计算内核采用 Parquet + PyArrow + DuckDB + Polars。

## 2. 生产依赖

### 2.1 数据与计算

| 包 | 用途 | 是否核心 |
| --- | --- | --- |
| `polars` | 惰性扫描、rolling、group by、join、截面排序和流式执行 | 是 |
| `pyarrow` | Arrow schema、Parquet dataset 写入和元数据 | 是 |
| `duckdb` | Catalog、SQL 审计、Parquet coverage 查询和临时分析 | 是 |
| `pytz` | DuckDB `TIMESTAMPTZ` 与 Python aware `datetime` 转换 | A03 运行时 |
| `numpy` | 数值数组、随机数和部分性能敏感计算 | 是 |
| `pandas` | 与研究生态和小型报告表互操作，不处理主面板 | 辅助 |
| `scipy` | Spearman、统计检验等研究指标 | 是 |

Polars 的 Lazy API 支持 projection/predicate pushdown 和 streaming，适合只读取所需时间与字段；DuckDB 能直接扫描 Parquet，并同样支持列裁剪和过滤下推；PyArrow 负责稳定 schema 和分区数据集写入。

A05 正式引入 `polars>=1.30,<2`。`ParquetDataStore` 使用 `scan_parquet` 返回 LazyFrame，并在收集前组合时间、symbol 和列投影；PyArrow 使用 ZSTD、统计信息和可配置 row group 写入不可变 part。

### 2.2 配置与模型

| 包 | 用途 |
| --- | --- |
| `pydantic` | 配置、manifest 和接口模型校验 |
| `pydantic-settings` | 非敏感运行参数的环境变量覆盖；回测不读取实盘 `.env` |
| `PyYAML` | 加载 YAML 配置；加载后必须交给 Pydantic 校验 |

### 2.3 网络与 CLI

| 包 | 用途 |
| --- | --- |
| `httpx` | A04 公共 HTTP、连接池、超时、流式下载和可注入测试 transport |
| `tenacity` | 后续复杂重试策略候选；A04 未引入 |
| `typer` | 类型化 CLI |
| `rich` | 下载进度、表格和结构化终端输出 |

ZIP、hash、路径和并发控制优先使用 Python 标准库：`zipfile`、`hashlib`、`pathlib`、`concurrent.futures`、`asyncio`。

A04 采用项目内小型同步重试策略：只重试明确的瞬时 HTTP 状态和 transport 错误，次数、指数退避上限及 `Retry-After` 均有界；没有为了这一项额外引入 `tenacity`。

### 2.4 报告

| 包 | 用途 |
| --- | --- |
| `jinja2` | HTML 报告模板 |
| `plotly` | 可交互净值、回撤、IC 和分层图 |
| `matplotlib` | 静态图和测试基线，可选 |

### 2.5 开发依赖

| 包 | 用途 |
| --- | --- |
| `pytest` | 单元和集成测试 |
| `pytest-cov` | 覆盖率 |
| `hypothesis` | 重采样、权重和成本不变量的性质测试 |
| `ruff` | lint 和格式检查 |
| `mypy` | 静态类型检查 |

## 3. 暂不采用的依赖

- `vectorbt`：适合快速矩阵回测，但动态 universe、资金费率和自定义数据版本仍需自建核心；可作为结果对照工具而非架构基座。
- `backtrader`：事件式单标的模型不是本项目截面批量研究的最佳基础。
- `ccxt`：统一交易所接口会丢失 Binance 永续特有字段，且第一版只有一个交易所。
- Binance 交易 SDK：数据均为公开端点，直接 HTTP 更轻、更易固定原始响应；同时可彻底避免引入账户和下单能力。
- `dask`/`ray`/`spark`：第一版先使用单机列式和 out-of-core 处理，确认单机瓶颈后再引入分布式复杂度。

## 4. Binance 数据源

### 4.1 Binance Public Data

用途：历史批量回填的第一来源。A04 已实现 USD-M `klines`、`markPriceKlines` 的日/月归档和 `fundingRate` 月归档。

官方仓库说明公共数据以 daily/monthly 文件发布，支持 USD-M Futures，K 线包含 OHLC、volume、quote volume、成交笔数、taker buy volume 和 taker buy quote volume，并提供 CHECKSUM。

入口：

- [Binance Public Data GitHub](https://github.com/binance/binance-public-data)
- [Binance Data Collection](https://data.binance.vision/)

典型路径约定：

```text
https://data.binance.vision/data/futures/um/monthly/klines/<SYMBOL>/<INTERVAL>/<FILE>.zip
https://data.binance.vision/data/futures/um/daily/klines/<SYMBOL>/<INTERVAL>/<FILE>.zip
```

实际支持的数据类型和日期必须通过 discovery 探测，不能仅凭拼接 URL 假定存在。

### 4.2 公开 REST

用途：元数据快照、归档尾部增量和小范围修复，不承担多年全量回填。A04 已实现表中成交 K 线、标记价格 K 线、资金费率历史、exchangeInfo 和 fundingInfo；其他行仍是后续候选。

| 数据 | Endpoint | 第一版用途 | 关键限制 |
| --- | --- | --- | --- |
| 合约信息 | `GET /fapi/v1/exchangeInfo` | 当前合约与 filter 快照 | 只反映查询时状态，必须历史化保存 |
| 成交 K 线 | `GET /fapi/v1/klines` | 尾部增量、缺口修复 | 单次最多 1500 根，请求权重随 limit 增加 |
| 标记价格 K 线 | `GET /fapi/v1/markPriceKlines` | 估值、风险和资金费率附近价格 | 单次最多 1500 根 |
| 指数价格 K 线 | `GET /fapi/v1/indexPriceKlines` | 可选基差/溢价研究 | 单次最多 1500 根 |
| 溢价指数 K 线 | `GET /fapi/v1/premiumIndexKlines` | 可选资金费率因子 | 归档覆盖需单独核查 |
| 资金费率历史 | `GET /fapi/v1/fundingRate` | 真实资金费率现金流 | 单次最多 1000；与 fundingInfo 共享 500/5min/IP |
| 资金费率规则 | `GET /fapi/v1/fundingInfo` | 记录 interval/cap/floor 调整 | 需要定期快照 |
| 持仓量统计 | `GET /futures/data/openInterestHist` | 二期 OI 因子 | REST 只提供最近一个月 |
| 聚合成交 | `GET /fapi/v1/aggTrades` | 二期亚分钟或冲击研究 | 数据量大，不纳入第一版主路径 |

官方参考：

- [USDⓈ-M Market Data](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)
- [Kline/Candlestick Data](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Kline-Candlestick-Data)
- [Mark Price Kline](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Mark-Price-Kline-Candlestick-Data)
- [Funding Rate History](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Get-Funding-Rate-History)
- [Open Interest Statistics](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Open-Interest-Statistics)

## 5. 数据优先级

### 第一版必须

1. 成交 K 线 1m。
2. 标记价格 K 线 1m。
3. 资金费率历史。
4. exchangeInfo 历史快照。

### 第一版建议

5. 指数价格 K 线。
6. fundingInfo 快照。

### 第二阶段

7. OI、taker buy/sell ratio、global/top trader long-short ratio。
8. aggTrades。
9. 更精细的手续费和保证金阶梯历史。

OI 和多空比类 REST 历史窗口很短，因此如果未来需要长历史，应尽早建立每日采集任务；没有历史归档时不能用当前值填充过去。

## 6. 包职责边界

- `httpx` 只存在于 `data.sources`，研究和 engine 不得导入它。
- `duckdb` 主要存在于 catalog、coverage 和审计查询。
- `pyarrow` 负责 schema、Parquet 写入和跨库交换。
- `polars` 是主计算接口。
- `pandas` 只允许出现在 reports、notebooks 或小型兼容层。
- Plotly/Jinja2 不得反向成为 engine 依赖。

## 7. 官方技术参考

- [Polars Lazy API](https://docs.pola.rs/user-guide/lazy/using/)
- [Polars query optimizations](https://docs.pola.rs/user-guide/lazy/optimizations/)
- [DuckDB Parquet overview](https://duckdb.org/docs/stable/data/parquet/overview)
- [PyArrow Parquet](https://arrow.apache.org/docs/python/parquet.html)
- [PyArrow write_dataset](https://arrow.apache.org/docs/python/generated/pyarrow.dataset.write_dataset.html)
