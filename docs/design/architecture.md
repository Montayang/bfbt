# Architecture

## 数据流

```text
Binance public archive / REST incremental data
                    ↓
              raw immutable files
                    ↓
       normalize + validate + deduplicate
                    ↓
        partitioned long-form Parquet data
                    ↓
 point-in-time universe + factor calculation
                    ↓
 portfolio construction + execution assumptions
                    ↓
          metrics + reproducible reports
```

## 模块职责

- `data`: 数据源适配、下载编排、标准化、校验和数据目录。
- `universe`: 按历史时点构造可交易永续合约池。
- `factors`: 因子协议、因子库和截面变换。
- `research`: IC、Rank IC、分层收益、换手和诊断。
- `portfolio`: 标的选择、权重生成和组合约束。
- `engine`: 向量化回测、成交假设、费用、滑点和资金费率。
- `metrics`: 组合绩效和风险指标。
- `reports`: 将一次运行的配置、数据版本和结果汇总为报告。

## 强制边界

1. 数据下载与回测执行分离，回测循环不得临时请求 Binance API。
2. 因子只接收无副作用的历史数据，不接收实盘 Client。
3. `t` 时刻收盘后形成的信号最早在 `t+1` 成交。
4. 合约池、标准化样本和流动性过滤均必须按历史时点计算。
5. 每次运行保存完整配置和数据版本，以保证结果可复现。

## 设计文档关系

本文是架构摘要。完整输入输出、执行语义和验收场景见 [`system_design.md`](system_design.md)；接口、数据管理、依赖和实施细节见同目录其他专题文档。

## 控制面与数据面

- 控制面：配置模型、Catalog、dataset/run manifest、CLI 和日志。
- 数据面：Raw 文件、Parquet 数据集、Polars LazyFrame、因子值、权重、成交和收益流水。

Catalog 只负责发现和版本索引，Parquet 才是标准化行情的事实来源。DuckDB 用于 catalog 和 SQL 审计，Polars 用于主计算，PyArrow 用于 schema 与 Parquet 写入。

## 依赖方向

```text
cli/reports
    ↓
application services / engine
    ↓
factors ─ portfolio ─ research
    ↓          ↓
universe ─ data store
              ↓
catalog / parquet
              ↓
sources / raw files
```

下层不能导入上层；`data.sources` 可以使用 HTTP，但 factor、portfolio、engine 和 reports 不能访问网络。`bianbt` 的任何模块都不能导入 `bianbot.Clients`。

## 两条运行路径

数据路径：

```text
discover → download → normalize → validate → compact → publish dataset version
```

研究路径：

```text
resolve dataset → build universe → compute factor/labels
→ evaluate → construct portfolio → simulate → publish run
```

两条路径通过不可变 `DatasetSnapshot` 连接。回测不会自动补数据。

## 额外架构约束

- 价格 K 线、标记价格、资金费率和合约元数据使用不同数据集，不隐式混用。
- 失败的数据发布和失败的 run 只能保留为临时或 failed 状态，不能冒充成功版本。
- 报告只读取已经固化的 run artifacts，不重新计算策略结果。

## 目标包结构

A01–A10 已实现并通过本地功能验收；A10 真实一年全市场容量仍需在用户目标机器验收。

```text
src/bianbt/
├── application/
│   ├── run.py                 # A09/A10 模式分派和 terminal failure
│   ├── planning.py            # A10 history overlap 与 final tail
│   └── chunked.py             # A10 分块分析、执行和发布编排
├── config/
│   ├── common.py              # 严格不可变模型和 UTC 公共校验
│   ├── data.py                # 数据源、dataset、storage 配置
│   ├── universe.py            # 时点化合约池配置
│   ├── factor.py              # 因子、预处理和标签配置
│   ├── backtest.py            # 组合、执行、风险和输出配置
│   ├── bundle.py              # 跨文件约束和 run-ready 校验
│   ├── durations.py           # duration 与基础周期换算
│   ├── loader.py              # 安全 YAML、路径和环境覆盖
│   └── fingerprint.py         # 规范序列化和配置指纹
├── data/
│   ├── schemas.py             # Arrow schemas 和 schema version
│   ├── catalog.py             # DuckDB catalog
│   ├── storage.py             # A05 版本固定 Polars DataStore
│   ├── publisher.py           # A05 质量门和原子 Parquet 发布
│   ├── resample.py            # A06 UTC 多周期重采样
│   ├── manifests.py           # raw/dataset partition manifest
│   ├── sources/
│   │   ├── base.py            # 强类型请求、远端对象、结果和错误
│   │   ├── http.py            # 无认证公共 HTTP 与有界重试
│   │   ├── binance_archive.py # 归档发现、checksum、覆盖率和发布
│   │   └── binance_rest.py    # K 线/资金费率分页和元数据快照
│   ├── ingest/
│   │   ├── raw_store.py       # REST 原始响应原子发布
│   │   └── service.py         # 并行下载、成功后顺序登记 catalog
│   ├── normalize/
│   │   ├── core.py            # A05 四类标准化与版本指纹
│   │   └── service.py         # A05 标准化发布编排
│   └── validation/
│       └── reports.py         # A05 确定性质量报告
├── universe/
│   ├── contracts.py          # A06 合约历史投影
│   ├── filters.py            # A06 稳定原因码
│   └── point_in_time.py      # A06 schedule、滚动指标和时点资格
├── factors/
│   ├── base.py
│   ├── registry.py            # A07 显式 name/version 注册表
│   ├── transforms.py          # A07 eligible-only 截面变换
│   ├── momentum.py
│   ├── volatility.py
│   └── liquidity.py
├── labels/
│   └── forward_returns.py     # A07 显式 signal/entry/exit 标签
├── research/
│   ├── evaluator.py           # A07 lazy 研究输出组合
│   ├── ic.py                  # A07 IC/Rank IC
│   ├── quantiles.py           # A07 分层收益
│   ├── turnover.py            # A07 rank turnover
│   └── diagnostics.py         # A07 覆盖率和对齐数
├── portfolio/
│   ├── base.py                 # A08 组合结果和版本契约
│   ├── selection.py            # A08 count/quantile 选币
│   ├── weighting.py            # A08 equal/score/inverse-vol
│   └── constraints.py          # A08 静态约束和 target version
├── engine/
│   ├── vectorized.py           # A08 有状态逐 bar 账本
│   ├── execution.py            # A08 fill 和 turnover
│   ├── streaming.py            # A10 跨块持仓/净值状态
│   ├── costs.py                # A08 fee/slippage
│   └── funding.py              # A08 funding cashflow
├── performance/
│   ├── chunks.py               # A10 左闭右开时间块和 overlap
│   ├── diagnostics.py          # A10 row/RSS 预算门
│   └── spool.py                # A10 临时 Parquet parts 与安全清理
├── metrics/
│   ├── performance.py         # A09 收益和风险调整指标
│   ├── risk.py                # A09 exposure/turnover
│   ├── attribution.py         # A09 return contribution
│   └── summary.py             # A09 稳定 JSON 摘要
├── reports/
│   └── renderer.py            # A09 artifact-only standalone HTML
├── artifacts/
│   ├── environment.py         # A09 Git/源码/依赖指纹
│   └── store.py               # A09 terminal run 原子发布
└── cli.py
```

其中 `application` 负责用例编排，domain-style 计算模块保持无 I/O；`artifacts` 与 `data.storage` 分开，前者保存某次运行结果，后者保存可跨运行复用的市场数据。
