# AI Agent 介入能力与开发欠缺

更新时间：2026-09-30。

Showcase 保留受控展示薄切片；通用工作流现由 A43 的 `AgentResearchIntent`、冻结单、统一
预检、授权、阶段 evidence、人工晋级和解释合同承担。两者不能互相冒充事实来源。

个人研究者的数据准备目标与下一阶段见 `DATA_ENGINE_PLAN.md`。其中的
ResearchDataRequirement、DataPlan、记录式 job 和 DataReadiness 应分别与 AG03、AG05、
AG07、AG08 共用合同，不得由数据入口和 Agent 入口重复实现。

## 目标工作流

系统的特殊产品目标不是让 Agent 直接拼接命令，而是让不懂代码的用户用自然语言完成一条
可解释、可审计、需要时可暂停确认的研究链路：

```text
自然语言研究想法
  -> 结构化 ResearchIntent 与歧义清单
  -> 因子/组合/执行语义冻结
  -> 数据覆盖、能力、成本和资源预检
  -> Quick Research
  -> Fast Matrix 组合研究
  -> 用户人工选择
  -> Event/V2 正式确认
  -> 不可变产物、报告和基于证据的解释
```

Agent 是控制面和协作者，不是新的计算引擎。经济计算必须继续由现有确定性模块完成；Agent
不能绕开配置校验、时点语义、成本警告、授权边界、不可变身份或 Event 正式运行要求。

## 已有基础

- Quick Research、Fast Matrix 和 Event/V2 三层职责已经明确，Fast Matrix 不支持的语义会
  失败关闭或显式转入 Event。
- 配置、DatasetSnapshot、因子版本、源码/依赖环境和 run artifact 已有稳定身份与校验。
- Catalog、数据准备、研究预览、Fast Matrix、正式 run、报告和性能诊断已经有 CLI 入口。
- 内置因子注册表包含公式、版本、依赖列及中英文解释；报告可以展示研究指标、成本、换手、
  持仓、成交和风险事件。
- 正式 Event 运行具备分块、有限内存、checkpoint、恢复、失败 artifact 和原子发布能力。
- 系统不包含账户 Client、凭据或下单入口，这应继续作为 Agent 部署的硬边界。

这些基础足以支持熟悉仓库的人或受监督的 Codex 会话完成研究，但还不等于通用的无代码
Agent 产品入口。

## 欠缺登记

状态取值：`missing`、`partial`、`ready`。优先级 P0 表示形成安全最小闭环前必须解决，P1
表示工程级可用所需，P2 表示规模化与开放生态增强。

| ID | 优先级 | 状态 | 欠缺 | 完成边界 |
|---|---|---|---|---|
| AG01 | P0 | ready | Agent 面向的研究意图合同 | A43 的 `AgentResearchIntent` 绑定自然语言原文 hash、目标、假设、歧义、决定、数据、因子、语义、成本、输出与结果引用，可区分诊断、组合研究、正式回测和结果查询。 |
| AG02 | P0 | ready | 语义冻结与确认协议 | 通用计划冻结因子方向、四类时钟、Rank、组合、成交、仓位、成本、风险与期末处理；歧义失败关闭，语义确认码必须进入 plan-bound grant。 |
| AG03 | P0 | ready | 统一预检与行动计划 | A43 组合 DE-v1 数据计划、合法后端、成本拖累、资源、阶段路线、写入和全部授权动作，且规划无副作用。 |
| AG04 | P0 | ready | 授权门控合同 | `AuthorizationGrant` 把单个动作、确认码、批准人和有效期绑定到精确 plan hash；只读、网络、下载、写入、研究、Event、测试和 Git 仍为独立类别。 |
| AG05 | P0 | partial | 记录式后台任务服务 | DE-v1 已有 plan-hash 绑定的四步 job manifest、终态和幂等恢复；通用 PID、日志、心跳、安全取消及研究/Event 接入仍缺。 |
| AG06 | P0 | ready | 通用因子表达入口 | `bfbt-factor-expression/v1` 只允许白名单字段、算术、因果 lag、rolling/EMA、abs/log；禁止任意代码，按 symbol 与连续段计算，缺口重置，非有限值失败并产生内容版本。 |
| AG07 | P0 | ready | 端到端研究编排服务 | `AgentWorkflowStore` 按 intent/plan hash 记录 Data → Quick → Matrix → 人工选择 → Event → 解释阶段，跨会话恢复已完成证据；具体计算继续委托现有确定性服务。 |
| AG08 | P1 | ready | 数据需求规划与版本选择 | DE-v1 根据核心区间、warmup、Universe 历史、标签/执行尾部及所需事实生成覆盖计划，拒绝 `latest` 和含糊多版本，并产出精确 Snapshot 就绪证据。 |
| AG09 | P1 | ready | 换手、成本和可行性硬门 | 组合/Event 计划强制要求预估调仓换手，按调仓次数、手续费和滑点计算拖累；超过绝对或预期毛收益占比阈值时必须显式确认并进入授权记录。 |
| AG10 | P1 | partial | 研究治理自动化 | QR-v1 和研究注册表已有稳定规则与血缘；A43 已提供 Fast Matrix 人工选择 decision artifact 与可校验 Event 来源合同。相关性去重、开发/验证/留出隔离和标签 horizon purge 尚未实现。人工选择本身必须保留，不能改成黑箱自动晋级。 |
| AG11 | P1 | ready | Agent 可消费的结果解释接口 | `AgentEvidenceSummary` 区分事实、资格说明和警告；每条 claim 强制引用已经接纳的 SHA-256 evidence，结果查询也会重新校验显式引用文件。 |
| AG12 | P1 | missing | 安全的生成与扩展沙箱 | 需要路径白名单、资源限额、生成因子静态检查、确定性 fixture、代码评审门及禁止凭据/网络/下单依赖的自动检查。生成代码与运行研究必须是两个独立授权动作。 |
| AG13 | P1 | ready | 部署与环境自检 | A44 增加 Python 3.10–3.12 通用 hash 锁、精确生成器与输入/输出 manifest、严格锁定安装和全新环境验收；结合现有 `bfbt doctor`，安装与本地就绪合同完整。 |
| AG14 | P1 | ready | Agent 工作流验收 | A39 保留展示薄切片；A43 覆盖通用意图、歧义、数据/成本计划、受限表达式、授权、暂停恢复、证据链和人工 promotion，且完全离线。 |
| AG15 | P2 | ready | 开源工程化配套 | A44 在既有双版本 CI、MIT 与双语公共入口上增加固定 SHA 的 Actions、review-only 依赖更新、tag/version/changelog/package 门、GitHub Release 自动化、checksum 和双语扩展兼容政策。 |
| AG16 | P2 | missing | 多用户与配额模型 | 当前是单机本地工作区语义。若未来提供服务，需要项目/用户隔离、并发与存储配额、任务排队、审计主体和 artifact 访问控制；不能让共享服务直接沿用单用户路径假设。 |

## 推荐实施顺序

1. 将 AG05 的通用 PID、心跳、日志和安全取消作为工程增强，与已经存在的 DE/Event 恢复
   语义复用，不建设第二套任务计算引擎。
2. 在研究治理任务中补齐 AG10 的相关性、开发/验证/留出隔离和 purge 合同；保留已经实现的
   人工 promotion artifact。
3. AG12 继续补路径白名单、资源限额和扩展审查；安全表达式本身已经禁止任意代码。
4. 只有出现真实共享服务需求后再规划 AG16 多用户隔离与配额。

## 不应采取的捷径

- 不让 LLM 直接生成 shell 命令作为公共 API，也不把任意 Python `eval`/`exec` 当因子 DSL。
- 不因自然语言中出现“回测”就自动下载数据或启动正式 Event run。
- 不自动替用户选择 Fast Matrix 候选，不把 QR-v1 晋级或 zero-cost 正收益解释成可交易结论。
- 不用 Fast Matrix 近似路径依赖风险，不让 Agent 改写旧 run、manifest 或报告中的经济事实。
- 不让解释层访问账户、凭据、实盘客户端或真实订单流。

## 验证记录边界

独立仓库 pytest 是否通过仍以 `CURRENT_STATE.md` 的验证基线为准。维护者发现新欠缺时，应
更新本表的状态和完成边界；只有代码、验收和文档一并落地后，才能把项目改为 `ready`。
