# 已确认但尚未完成的开发与研究清单

更新时间：2026-09-29。

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

### BL05 — 研究治理补强

- 状态：QR-v1 和不可变研究身份已经存在，治理仍为 `partial`。
- 未完成：因子相关性/重复性矩阵；开发、验证、留出期的统一合同；标签 horizon 对分段边界
  的 purge/隔离。
- 已有但需沿用：A43 已提供 Fast Matrix 人工选择的结构化 decision artifact 和 Event
  promotion 的可校验来源链；治理补强不得另建冲突合同。
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
- DE-v1 数据需求、无副作用计划、记录式恢复、精确 Snapshot 就绪与最小血缘（BL02，
  `94204cb`，A42）。
- Fast Matrix 第二阶段共享准备、联合边界执行、区间估值和稀疏持仓（BL03，`7cef70a`，
  A41）。
- 通用受监督 AI Agent 工作流、安全因子表达式、人工晋级与证据解释（BL04，`9800438`，
  A43）。

## 5. 建议推进顺序

在用户没有另行调整优先级时：

1. 完成 BL01 开源因子 Quick Research，补足当前量化研究故事；
2. 逐步完成 BL05 研究治理补强；
3. 在最终工程级版本前完成 BL06 发布补强。

每条主线启动时仍需当前任务授权；本文不授权测试、网络、下载、研究、正式回测或外部
变更。
