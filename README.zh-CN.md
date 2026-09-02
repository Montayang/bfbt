# BFBT

**Binance Futures Backtesting Framework**——面向 Binance USDⓈ-M 永续合约截面因子研究的
离线研究与回测系统。

[English](README.md) · [文档导航](docs/README.md) · [Showcase 指南](showcase/README.md) ·
[贡献指南](CONTRIBUTING.zh-CN.md)

> BFBT 是独立的开源研究项目，与 Binance 不存在隶属、背书、赞助或任何利益关系。项目只
> 使用公开历史市场数据，不包含账户 Client 或实盘下单路径，也不构成投资建议。

`bfbt` 是面向 Binance USDⓈ-M 永续合约的离线截面因子研究与回测框架。它把快速因子
诊断、常规组合研究和路径依赖的正式事件回测分层处理，并为数据、配置、源码、成交和报告
保留可验证身份。

项目只处理公开历史市场数据和本地研究产物，不包含交易账户 Client、API 凭据或实盘下单
代码。历史模拟不构成投资建议。

## 为什么分成三层

```mermaid
flowchart LR
    A[自然语言研究想法] --> B[ResearchIntent / 语义冻结]
    B --> C[Quick Research<br/>IC · 分层 · 覆盖 · 换手]
    C --> D[Fast Matrix<br/>常规截面组合研究]
    D --> E{用户人工选择}
    E --> F[Event / V2<br/>路径状态 · 风险仲裁 · 正式产物]
    F --> G[不可变 run · 报告 · 逐笔审计]
```

- **Quick Research** 不模拟账户，用于因子 IC、分层收益、覆盖率和 Rank turnover 诊断。
- **Fast Matrix** 处理目标权重、固定调仓和线性成本边界内的列式组合研究，发布 `fm-*`
  研究产物，不充当正式策略真相。
- **Event/V2** 按时间维护账户、仓位、保证金和风险状态，负责移动止损、事件仲裁、滚仓、
  checkpoint/恢复及不可变正式 run。
- Fast Matrix 不支持的行为失败关闭或明确提升到 Event，不做静默近似。V1 仅保留兼容性。

## Showcase

仓库包含一个受控的 Agent/研究展示入口。它把自然语言请求、冻结语义、多月结果、滚仓保证金
轨迹和不可变证据连成一个离线页面；所有数字从逐文件验证后的 run artifact 读取。

![bfbt 三个月 Showcase 预览](docs/assets/showcase-preview.svg)

在已准备好本地 H2 产物的机器上：

```bash
.venv/bin/bfbt showcase prepare \
  --spec showcase/r5_t4_h2_rolling_202605_202607.json
```

生成页面位于：

```text
data/backtest/showcases/r5-t4-h2-rolling-202605-202607-r01/index.html
```

默认 `index.html` 为英文入口；同目录还会生成显式英文 `index.en.html` 与独立简体中文
`index.zh-CN.html`。其他面向人的 HTML 报告也遵循相同语言合同，机器可读证据不复制。

只读检查，不生成页面：

```bash
.venv/bin/bfbt doctor \
  --spec showcase/r5_t4_h2_rolling_202605_202607.json
```

市场数据和正式 run 按设计不提交 Git，因此全新 checkout 不会自带这三份真实结果。展示规格、
合同、渲染器和离线 fixture 测试均在仓库内；完整演示步骤见
[`showcase/README.md`](showcase/README.md)。

## 当前能力

- Binance USD-M、USDT 保证金、永续合约；1m trade/mark bars、funding 与合约元数据。
- 不可变 Raw、标准化 Parquet、质量报告、DuckDB Catalog 和精确 DatasetSnapshot。
- 时点化合约池、无前视因子/标签、预处理、IC/Rank IC、分层收益和 turnover。
- 内建 momentum、reversal、波动率、成交量、主动买入、Amihud、EMA、采样均值比和已登记
  GTJA191 因子；精确清单由 `bfbt research list-factors` 输出。
- Fast Matrix 列式经济内核、funding/mark、分块 checkpoint、批量研究与 Event promotion。
- Event/V2 下一根 K 线成交、显式手续费/滑点/资金费率、增量仓位、杠杆/敞口限制、固定与
  移动风险退出、滚仓保证金和统一事件优先级。
- 全市场分钟级 bounded-memory chunk、原子 checkpoint、失败恢复和连续/恢复经济等价。
- 不可变成功/失败 artifact、源码与依赖指纹、双语交互报告，以及完整成交、持仓变化和风险
  事件导航。
- 当前公开验收覆盖 A01–A40；BFBT 公开发布候选的完整离线 suite 为 334 项通过，精确环境
  与历史基线见 [`CURRENT_STATE.md`](docs/maintainer/CURRENT_STATE.md)。

## 安装

需要 Python 3.10 或更高版本：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[test]"
bfbt --help
bfbt doctor
```

真实数据首次使用需要访问 Binance 公共市场数据服务，但不需要 API key。详细安装、数据准备
和第一份真实小样本回测见
[`beginner_tutorial.md`](docs/guides/beginner_tutorial.md)；完整配置与故障排查见
[`user_manual.md`](docs/guides/user_manual.md)。

## 正确性和身份边界

- 所有时间区间采用 UTC 左闭右开 `[start, end)`；因子、Rank、决策、成交、风险、funding
  和估值拥有显式时钟。
- 正式运行拒绝 `latest`，必须固定数据集 ID/版本、完整配置、因子版本、源码和依赖环境。
- 成功和失败的终态 artifact 都不可变；修改策略或重跑使用新 alias/revision 和新 run ID。
- 曲线可以压缩展示点，但每笔成交、每次持仓变化和风险事件必须留在审计导航中。
- 路径依赖策略必须使用 Event/V2；不以快速矩阵结果冒充正式事件回测。

## 明确不支持

- 实盘账户、余额、订单、API key 或 `.env` 访问。
- 交易所完整强平阶梯、ADL、订单簿排队和 tick 级成交。
- Agent 自动替用户选择 Fast Matrix 候选。
- 任意 LLM 生成 Python、shell 或因子表达式的直接执行。
- 当前 Showcase 的 ResearchIntent 是受控薄切片，不等于通用无代码 Agent 平台已经完成。

## 项目导航

- [`docs/README.md`](docs/README.md)：设计、参考、验收、研究和使用文档总入口。
- [`docs/maintainer/START_HERE.md`](docs/maintainer/START_HERE.md)：维护任务入口和授权规则。
- [`docs/maintainer/SHOWCASE_PLAN.md`](docs/maintainer/SHOWCASE_PLAN.md)：展示版本范围和验收门。
- [`docs/maintainer/AI_AGENT_READINESS.md`](docs/maintainer/AI_AGENT_READINESS.md)：通用自然语言研究工作流欠缺清单。
- [`strategies/README.md`](strategies/README.md)：稳定策略身份、规格与正式 run 映射。
- [`docs/design/architecture.md`](docs/design/architecture.md)：模块与端到端数据流。
- [`docs/reference/data_contract.md`](docs/reference/data_contract.md)：事实表与产物 schema。

## 参与和安全

开发约定见 [`CONTRIBUTING.md`](CONTRIBUTING.md)，安全边界与漏洞报告见
[`SECURITY.md`](SECURITY.md)，版本变化见 [`CHANGELOG.md`](CHANGELOG.md)。项目采用
[`MIT License`](LICENSE)。
