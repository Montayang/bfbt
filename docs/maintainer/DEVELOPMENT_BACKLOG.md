# 已确认但尚未完成的开发与研究清单

更新时间：2026-09-08。

本文汇总已经在用户目标、维护规划或研究记录中明确出现，但尚未完成的工作。它用于避免
跨会话遗漏，不把所有“未来可以做”的想法都提升为当前任务。准确实现状态仍以 Git、不可变
artifact 和各专题文档为准。

## 1. 已确认的近期主线

### BL01 — 开源趋势与动量因子的 Quick Research

- 状态：公式实现完成，研究未开始。
- 已有：14 个来源固定的 Qlib/`ta` 因子、稳定 `v1` 身份、公式/因果/缺口测试；上一轮
  `1m/5m/15m` K 线和未来 `1/5/20` 根标签口径已经冻结。
- 未完成：冻结新 study 的开发/验证/留出日期与 DatasetSnapshot；运行 Quick Research；
  应用明确规则；生成并登记中英文报告和结论。
- 后续：先做相关性/重复信息审阅，再由用户决定哪些信号进入 Fast Matrix。不得因为公式
  `verified` 就声称因子有效。
- 事实来源：`docs/research/open_source_trend_momentum_candidates.md`、
  `docs/research/registry.md`。

### BL02 — DE-v1 个人研究数据准备闭环

- 状态：目标与下一阶段计划已冻结，尚未开发。
- 范围：ResearchDataRequirement、无副作用 DataPlan、覆盖/资源估计、记录式可恢复
  `data prepare`、DataReadiness 和最小派生血缘。
- 原则：复用现有 Archive/REST、Raw、标准化、质量门、Catalog 和 Snapshot；不建设企业级
  平台，不把秒级数据纳入下一阶段。
- 事实来源：`DATA_ENGINE_PLAN.md`。

### BL03 — Fast Matrix 第二阶段性能架构

- 状态：现有单候选横截面已列式化；多候选批量仍逐候选执行，仅共享一次行情物化。
- 未完成：在开发前冻结复杂度与真实性能基线；研究 `candidate × symbol` 状态布局、调仓区间
  批处理、稀疏持仓 `K << S` 和共享成本变体；证明逐时点经济结果继续与 Event 等价。
- 目标不是删除时间因果，而是把市场预处理从逐候选重复降为一次、将必须串行的状态边界从
  每个市场时点尽量收敛到调仓/资金费等真实状态边界。
- 必须先形成独立设计和验收计划，不能直接重写当前内核或用关闭费用、funding、审计换速度。
- 事实来源：`docs/design/v5_fast_matrix_engine.md` 第 13、17 节及当前实现
  `src/bfbt/engine/fast_matrix/batch.py`。

### BL04 — 通用自然语言 Agent 研究闭环

- 状态：Showcase 只有受控薄切片，通用能力尚未完成。
- 未完成：通用 ResearchIntent、语义冻结、统一预检、授权绑定、记录式后台任务、安全因子
  规格、端到端编排、数据需求计划、成本硬门、人工 promotion artifact、结构化结果解释和
  安全验收。
- 顺序：AG01–AG04，之后 AG05/AG07，再做 AG06/AG08/AG09、AG10/AG11 和 AG12–AG15。
- 事实来源：`AI_AGENT_READINESS.md`。AG16 多用户配额是条件性远期方向，不属于个人本地
  工作流的近期完成门槛。

### BL05 — 研究治理补强

- 状态：QR-v1 和不可变研究身份已经存在，治理仍为 `partial`。
- 未完成：因子相关性/重复性矩阵；开发、验证、留出期的统一合同；标签 horizon 对分段边界
  的 purge/隔离；用户 Fast Matrix 人工选择的结构化 decision artifact；Event promotion 的
  可校验来源链。
- 自动化只能整理证据，不能代替用户选择 Fast Matrix 候选。
- 事实来源：`AI_AGENT_READINESS.md` 的 AG10，以及 `docs/research/registry.md`。

### BL06 — 工程级开源发布补强

- 状态：仓库、CI、MIT License、双语入口、治理模板和 GitHub Pages 已公开可用。
- 未完成：依赖锁与可复现安装、正式 release/tag 自动化、依赖更新策略、插件/扩展兼容政策。
- 这些是最终工程级发布质量工作，不阻塞当前公开仓库和报告实例继续使用。
- 事实来源：`AI_AGENT_READINESS.md` 的 AG13/AG15 和 `SHOWCASE_PLAN.md`。

## 2. 已有结果但等待用户决定

### BL07 — GTJA191 Fast Matrix 后的 Event 选择

12 个 QR-v1 信号已经展开成 29 个调仓计划并完成 Fast Matrix 双成本研究；27 个可完成的
realistic 路径全部为负。没有自动晋级，Event 输入决定仍为空。只有用户基于报告明确指定
候选和事件逻辑后才继续；这不是系统自行推进的待办。

### BL08 — Clean H2 `r02` 展示证据

现有 H2 `r01` 带 `git_dirty=true` 的来源资格；另有干净来源的 1x May Event 报告作为当前
Pages 示例。May–July H2 `r02` 重跑仍是可选证据改进，需要单独冻结身份并授权正式回测，
不是当前 Showcase 或公开仓库的缺陷。

### BL09 — 一年全市场容量复验

A10 保留了目标机器上的一年全市场容量验收手册。已有 598 合约单月、6 GiB 约束下的正式
Event 实测，因此一年复验只在需要声明相应容量或评估新数据/Fast Matrix 架构时启动，不是
日常开发的默认门槛。

## 3. 条件性或明确延期的方向

以下事项有记录，但没有被确认为近期开发承诺：

- Binance Futures `trades`/`aggTrades` 接入与 1s/5s/15s 派生 K 线；
- tick/order-book fill、完整 liquidation tiers、ADL 和非线性市场冲击；
- 多交易所、多资产数据源；
- 常驻在线数据服务、Web 控制台和自动告警；
- 多用户、租户隔离、配额和云端任务队列；
- 实盘账户、凭据、下单或交易客户端。

出现明确用户需求后，应先单独冻结数据语义、维护成本和验收边界。实盘能力继续保持系统
硬边界之外。

## 4. 已完成，不应继续列为待办

- BFBT/`bfbt` 完整改名与 `Montayang/bfbt` 公共仓库；
- Python 3.10/3.12 CI 兼容修复；
- 英文主入口、独立中文文档与双语 HTML；
- 三层真实报告实例的 GitHub Pages 发布；
- Showcase S0–S5、只读 doctor 和受控 ResearchIntent 薄切片；
- R5-T4-H2-ROLLING May/June/July 正式 run；
- 1x May Event 展示 run 和 Pages 报告替换；
- 14 个开源趋势/动量因子的代码实现与公式级验证。

## 5. 建议推进顺序

在用户没有另行调整优先级时：

1. 完成 BL01 开源因子 Quick Research，补足当前量化研究故事；
2. 为 BL03 Fast Matrix 第二阶段另写设计/复杂度/基准合同，再决定是否实现；
3. 启动 BL02 的 D0–D1，把数据需求和无副作用计划变成普通用户入口；
4. 将 BL02 的记录式 job 与 BL04 的 AG05/AG07 合并建设；
5. 逐步完成 BL05 研究治理与 BL04 Agent 闭环；
6. 在最终工程级版本前完成 BL06 发布补强。

每条主线启动时仍需当前任务授权；本文不授权测试、网络、下载、研究、正式回测或外部
变更。
