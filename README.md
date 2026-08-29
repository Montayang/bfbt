# bianbt

`bianbt` 是独立的 Binance 永续合约研究与回测框架。它只处理公开历史市场数据和本地
回测产物，不包含交易账户 Client、实盘下单代码或私有凭据。

框架包含快速因子研究、Fast Matrix 常规截面组合回测，以及支持时序状态、风险事件、
可恢复 chunk 和不可变审计产物的 Event 引擎。公开验收规格位于 `docs/acceptance/`。

## 隔离边界

- 不包含或导入任何实盘 Client，不访问账户、余额或下单接口。
- 不读取 `.env`；市场历史数据来自公开归档或用户提供的本地数据仓库。
- 使用独立的 `pyproject.toml`、包名 `bianbt`、测试目录和配置文件。
- 回测数据和产物统一写入仓库根目录 `data/backtest/`，并按 `datasets`、`catalogs`、`workspaces`、`runs`、`reports` 分层；全部不提交 Git。
- 与其他系统集成时只应交换纯数据结构或策略信号协议，不能引入有副作用的交易 Client。

## 第一版预定范围

- Binance USDⓈ-M、USDT 保证金、永续合约。
- 以 1 分钟 K 线为基础数据，支持向更高周期重采样。
- OHLCV、成交额、成交笔数、主动买入量和资金费率。
- 时点化合约池、截面因子、IC/Rank IC、分层收益、多空组合。
- 手续费、滑点、换手和资金费率成本。

## 文档

- [`strategies/README.md`](strategies/README.md)：用户实际策略工作区，以及策略规格到正式回测报告的协作流程。

- [`docs/README.md`](docs/README.md)：按用途分类的完整文档导航。
- [`docs/guides/beginner_tutorial.md`](docs/guides/beginner_tutorial.md)：第一次使用时照着复制命令即可完成真实数据回测的傻瓜式教程。
- [`docs/guides/custom_factor_tutorial.md`](docs/guides/custom_factor_tutorial.md)：实现、注册、测试全新截面因子并在教程数据集上回测。
- [`docs/guides/user_manual.md`](docs/guides/user_manual.md)：当前版本的安装、真实数据、配置、正式回测、结果解读和故障排查手册。
- [`docs/design/system_design.md`](docs/design/system_design.md)：系统目标、输入输出、功能分层、时序和正确性约束。
- [`docs/design/architecture.md`](docs/design/architecture.md)：模块结构和端到端数据流摘要。
- [`docs/reference/data_contract.md`](docs/reference/data_contract.md)：各事实表、派生表和运行产物的 schema 契约。
- [`docs/reference/interfaces.md`](docs/reference/interfaces.md)：模块之间的 Protocol 和职责边界。
- [`docs/reference/data_management.md`](docs/reference/data_management.md)：本地目录、分区、版本、增量更新和质量管理。
- [`docs/reference/dependencies_and_sources.md`](docs/reference/dependencies_and_sources.md)：Python 包、Binance 数据源和 REST 接口选型。
- [`docs/reference/configuration.md`](docs/reference/configuration.md)：配置分层、字段语义、默认值和校验规则。
- [`docs/design/implementation_plan.md`](docs/design/implementation_plan.md)：第一版技术实施顺序和完成标准。
- [`docs/design/v2_design.md`](docs/design/v2_design.md)：第二版架构、配置、事件语义和内存边界。
- [`docs/design/v2_implementation_plan.md`](docs/design/v2_implementation_plan.md)：第二版 A12–A18 实施路线。
- [`docs/design/v3_low_memory_design.md`](docs/design/v3_low_memory_design.md)：第三阶段低内存分块、恢复和全市场策略设计。
- [`docs/acceptance/plan.md`](docs/acceptance/plan.md)：第一版用户验收里程碑。
- [`docs/acceptance/v2_plan.md`](docs/acceptance/v2_plan.md)：第二版验收和提交规则。
- [`docs/acceptance/v3_plan.md`](docs/acceptance/v3_plan.md)：第三阶段 A19–A24 验收路线。
- [`docs/acceptance/A01.md`](docs/acceptance/A01.md)：配置层环境搭建、测试命令和预期结果。
- [`docs/acceptance/A02.md`](docs/acceptance/A02.md)：Arrow schema 与 manifest 验收步骤。
- [`docs/acceptance/A03.md`](docs/acceptance/A03.md)：DuckDB catalog 的环境、自动验收和人工 CLI 步骤。
- [`docs/acceptance/A04.md`](docs/acceptance/A04.md)：公开归档、REST 分页和原始数据发布验收步骤。
- [`docs/acceptance/A05.md`](docs/acceptance/A05.md)：标准化、质量门、Parquet 发布和 DataStore 验收步骤。
- [`docs/acceptance/A06.md`](docs/acceptance/A06.md)：多周期重采样和时点化合约池验收步骤。
- [`docs/acceptance/A07.md`](docs/acceptance/A07.md)：因子、标签和研究评估验收步骤。
- [`docs/acceptance/A08.md`](docs/acceptance/A08.md)：组合构建、成本、资金费率和账本验收步骤。
- [`docs/acceptance/A09.md`](docs/acceptance/A09.md)：指标、正式产物、报告重建和失败状态验收步骤。
- [`docs/acceptance/A10.md`](docs/acceptance/A10.md)：分块等价、内存预算、清理和全年容量验收步骤。
- [`docs/acceptance/A11.md`](docs/acceptance/A11.md)：双语交互报告和快照审计验收步骤。
- [`docs/acceptance/A12.md`](docs/acceptance/A12.md)：V2 配置、schema、事件与 manifest 契约验收。
- [`docs/acceptance/A20.md`](docs/acceptance/A20.md)：V2 独立时间块 worker、恢复和经济等价验收。
- [`docs/acceptance/A21.md`](docs/acceptance/A21.md)：V2 流式归并、指标、报告和正式原子发布验收。

A01 引入 Pydantic、PyYAML、Typer 和 pytest，A02 增加 PyArrow，A03 增加 DuckDB 和 pytz，A04 增加 HTTPX，A05 增加 Polars。其余计算依赖在对应验收阶段再加入。
