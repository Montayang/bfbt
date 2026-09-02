# bfbt 文档导航

文档按用途分类。第一次使用系统从“使用指南”开始；开发或审阅引擎时从“架构设计”与
“参考资料”开始。

## 维护与交接

- [`maintainer/START_HERE.md`](maintainer/START_HERE.md)：新维护任务与 Codex 会话入口。
- [`maintainer/CURRENT_STATE.md`](maintainer/CURRENT_STATE.md)：当前能力和验证基线。
- [`maintainer/ACTIVE_WORK.md`](maintainer/ACTIVE_WORK.md)：未完成或未合并工作。
- [`maintainer/SHOWCASE_PLAN.md`](maintainer/SHOWCASE_PLAN.md)：工程级开源前的受控展示版本范围、验收与实施顺序。
- [`maintainer/ARCHITECTURE_DECISIONS.md`](maintainer/ARCHITECTURE_DECISIONS.md)：跨会话架构决策。
- [`maintainer/OPERATIONS.md`](maintainer/OPERATIONS.md)：数据、run、报告和后台任务操作规则。

## 实际策略工作区

- [`../strategies/README.md`](../strategies/README.md)：用户实际策略的规格、配置与正式回测索引；不属于框架验收。

## 研究候选

- [`research/README.md`](research/README.md)：因子定义、快速研究、组合候选与 Event 策略的
  晋级对象、身份和目录入口。
- [`research/registry.md`](research/registry.md)：当前因子库、QR-v1 晋级信号及后续层状态。
- [`research/rules/QR-v1.md`](research/rules/QR-v1.md)：当前版本化快速研究筛选规则。
- [`research/gtja191_trend_momentum_candidates.md`](research/gtja191_trend_momentum_candidates.md)：
  国泰君安 Alpha191 中趋势、动量、量能确认与趋势强度候选的来源、实现分级和首轮选取依据。

## 使用指南

- [`guides/beginner_tutorial.md`](guides/beginner_tutorial.md)：从真实数据下载开始，按步骤跑出第一份回测报告。
- [`guides/custom_factor_tutorial.md`](guides/custom_factor_tutorial.md)：实现、注册、测试一个全新截面因子，并在入门教程数据集上回测。
- [`guides/user_manual.md`](guides/user_manual.md)：完整命令、配置、结果解读与排错手册。

## 参考资料

- [`reference/configuration.md`](reference/configuration.md)：配置字段、默认值和校验规则。
- [`reference/data_contract.md`](reference/data_contract.md)：事实表、派生表和运行产物 schema。
- [`reference/data_management.md`](reference/data_management.md)：本地目录、分区、版本和 Catalog 管理。
- [`reference/interfaces.md`](reference/interfaces.md)：模块接口和职责边界。
- [`reference/dependencies_and_sources.md`](reference/dependencies_and_sources.md)：依赖包、Binance 数据源和公开接口。

## 架构设计

- [`design/system_design.md`](design/system_design.md)：系统目标、输入输出、时序和正确性约束。
- [`design/architecture.md`](design/architecture.md)：模块结构和端到端数据流摘要。
- [`design/implementation_plan.md`](design/implementation_plan.md)：第一版实施阶段与完成标准。
- [`design/v2_design.md`](design/v2_design.md)：Event 引擎的功能语义、事件模型、状态边界和配置草案。
- [`design/v2_implementation_plan.md`](design/v2_implementation_plan.md)：Event 引擎 A12–A18 实施路线与完成标准。
- [`design/v3_low_memory_design.md`](design/v3_low_memory_design.md)：第三阶段全市场分钟回测的时间分块、恢复和 6 GiB 内存设计。
- [`design/v4_reusable_analysis_and_fast_replay.md`](design/v4_reusable_analysis_and_fast_replay.md)：第四阶段可复用分析、分层失效、稀疏交易回放和参数扫描设计。
- [`design/v5_fast_matrix_engine.md`](design/v5_fast_matrix_engine.md)：第五阶段 Fast Matrix
  常规截面回测后端、能力边界、经济等价、研究产物和目录设计。

## 验收记录

- [`acceptance/plan.md`](acceptance/plan.md)：第一版 A01–A11 验收规划和当前状态。
- [`acceptance/v2_plan.md`](acceptance/v2_plan.md)：Event 引擎 A12–A18 协作、测试和提交规则。
- [`acceptance/v3_plan.md`](acceptance/v3_plan.md)：第三阶段 A19–A25 低内存执行与真实策略验收路线。
- [`acceptance/v4_plan.md`](acceptance/v4_plan.md)：第四阶段 A27–A30 可复用分析、快速回放、参数 sweep 与因子交叉验收路线。
- [`acceptance/v5_plan.md`](acceptance/v5_plan.md)：第五阶段 A31–A35 Fast Matrix 合同、
  经济内核、批量研究、正式提升和真实性能验收路线。
- [`acceptance/real_e2e.md`](acceptance/real_e2e.md)：真实联网全链路验收记录。
- [`acceptance/A01.md`](acceptance/A01.md) 至 [`acceptance/A10.md`](acceptance/A10.md)：各阶段环境、步骤、预期结果与故障定位。
- [`acceptance/A10_live.md`](acceptance/A10_live.md)：一年全市场容量测试补充手册。
- [`acceptance/A11.md`](acceptance/A11.md)：双语交互报告的离线与真实 run 重建验收。
- [`acceptance/A12.md`](acceptance/A12.md)：Event 配置分派、artifact schema、事件契约和 manifest 验收。
- [`acceptance/A13.md`](acceptance/A13.md)：当前快照精确 Rank、V1 兼容适配和 rankings 产物验收。
- [`acceptance/A14.md`](acceptance/A14.md)：跨快照 Rank、双时钟、有界状态和 chunk 恢复验收。
- [`acceptance/A15.md`](acceptance/A15.md)：四种增量仓位、反向政策、simple-cross 资金与敞口约束验收。
- [`acceptance/A16.md`](acceptance/A16.md)：止盈止损、移动止损、组合回撤、next-open 与冷却状态验收。
- [`acceptance/A17.md`](acceptance/A17.md)：统一事件仲裁、三表原子发布和交互审计报告验收。
- [`acceptance/A18.md`](acceptance/A18.md)：Event 正式 CLI、四场景代表性真实联网和兼容回归验收。
- [`acceptance/A19.md`](acceptance/A19.md)：Event 可恢复分块工作区、检查点完整性和绝对 RSS 门验收。
- [`acceptance/A20.md`](acceptance/A20.md)：Event 独立时间块 worker、跨块经济状态、宕机恢复和 in-memory 等价验收。
- [`acceptance/A21.md`](acceptance/A21.md)：Event 惰性审计归并、流式指标、有界交互报告和正式原子发布验收。
- [`acceptance/A22.md`](acceptance/A22.md)：可配置 Rank 递减路径、O(合约数) 状态和 Top N 审计裁剪验收。
- [`acceptance/A23.md`](acceptance/A23.md)：单持仓替换、权益保证金 5 倍名义和同 K 线硬退出验收。
- [`acceptance/A24.md`](acceptance/A24.md)：598 合约真实一个月全链路、正式报告和 6 GiB 实测验收。
- [`acceptance/A25.md`](acceptance/A25.md)：明亮报告工作台、事件导航、内部页签和响应式排版验收。
- [`acceptance/A26.md`](acceptance/A26.md)：15 分钟 EMA7/25 分钟盘中状态和正式 R3/R4 回测。
- [`acceptance/A27.md`](acceptance/A27.md)：不可变 AnalysisSnapshot/SignalSnapshot、身份、父 hash 和失效边界。
- [`acceptance/A28.md`](acceptance/A28.md)：缓存热回放、稀疏行情依赖闭包与经济等价。
- [`acceptance/A29.md`](acceptance/A29.md)：共享输入的内存/逐块多参数回放 sweep。
- [`acceptance/A30.md`](acceptance/A30.md)：逐合约因子真正穿越、策略 FLAT 和独立多仓。
- [`acceptance/A31.md`](acceptance/A31.md)：执行后端合同、能力规划器与 TargetSchedule。
- [`acceptance/A32.md`](acceptance/A32.md)：Fast Matrix 列式经济内核与 Event 引擎等价。
- [`acceptance/A33.md`](acceptance/A33.md)：funding、mark 估值、chunked checkpoint 与硬门。
- [`acceptance/A34.md`](acceptance/A34.md)：共享行情 batch、不可变 `fm-*` 产物和正式提升。
- [`acceptance/A35.md`](acceptance/A35.md)：全量回归、真实数据性能与发布证据。
- [`acceptance/A36.md`](acceptance/A36.md)：相位采样正反因子、Event 风险 profiles 与多 sheets
  父子报告。
- [`acceptance/A37.md`](acceptance/A37.md)：R5-T4 激活式移动止盈、净盈亏滚仓保证金与
  固定/滚仓两次正式结果。
- [`acceptance/A38.md`](acceptance/A38.md)：所有曲线报告的完整成交时点、持仓变化导航与
  真实相邻账本状态。
- [`acceptance/A39.md`](acceptance/A39.md)：受控 ResearchIntent、只读 doctor、不可变证据
  验证与离线 Showcase 页面。
- [`acceptance/A40.md`](acceptance/A40.md)：BFBT 公开身份、英文主入口与独立中英文 HTML
  产物合同。
