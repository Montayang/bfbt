# 第五阶段 A31–A35 验收计划：Fast Matrix 工作流

## 1. 状态

本计划对应 [`../design/v5_fast_matrix_engine.md`](../design/v5_fast_matrix_engine.md)。
2026-08-23 已按用户授权完成 A31–A35；逐阶段证据见 `A31.md`–`A35.md`。

| 阶段 | 目标 | 状态 |
|---|---|---|
| A31 | 后端合同、能力规划器、TargetSchedule 与研究产物身份 | 完成 |
| A32 | Fast Matrix 单组合经济内核及与 Event/V2 的逐时点等价 | 完成 |
| A33 | funding、估值、缺失边界、chunked checkpoint 与资源门 | 完成 |
| A34 | 多候选共享输入、研究产物和正式提升流程 | 完成 |
| A35 | 代表性真实数据基准与 Event 对照验证 | 完成 |

## 2. 共同原则

- `fast_matrix` 是 V2 语义下的受限执行后端，不是 `config_version=v3`。
- `event` 是当前 V2 事件执行后端；V1 仅保留 legacy 兼容。
- Fast Matrix 不支持的配置必须失败或由 `auto` 明确选择 Event，不能静默忽略字段。
- MatrixResearchRun 与正式 run 使用不同身份、目录和页面标识。
- 共同支持场景以 Event/V2 为正确性基准，速度不能牺牲经济等价。
- 继续使用不可变 Dataset/Analysis/Signal、hash 验证、时间分块和内存硬门。
- 每阶段同步代码、自动验收和 `docs/acceptance/Axx.md`；本计划不替代阶段证据。
- 测试、真实回测、联网和 Git 操作按用户当次授权执行，历史授权不继承。

## 3. A31：合同与规划器

### 目标

- 将配置版本、执行后端和性能模式拆成三个正交概念；
- 定义 `auto|fast_matrix|event`、research/formal 目的和兼容默认值；
- 建立声明式 capabilities/requirements 与稳定 reason codes；
- 固定 TargetSchedule schema、身份和 SignalSnapshot 关系；
- 定义 `fm-*` MatrixResearchRun manifest、路径安全、原子发布和 hash；
- 冻结性能基线、第一版支持表和 A32 浮点政策。

### 完成门

1. 旧 V1/V2 配置不增加字段时解析和分派不变；
2. 显式 Fast Matrix 对不支持语义逐项失败，错误包含稳定 reason code；
3. `auto` 的后端选择确定、可序列化并进入 identity；
4. TargetSchedule 拒绝重复 key、越界 fill、隐式旧持仓和父 hash 不符；
5. MatrixResearchRun 拒绝路径逃逸、半发布、篡改、身份碰撞和覆盖；
6. 本阶段不实现经济计算，也不伪造性能结果。

## 4. A32：单组合经济内核

### 初始支持面

- 固定调仓时钟；
- long-short/long-only `target_weight`；
- next-bar-open；
- trade-close；
- fixed-bps/zero fee 与 slippage；
- 无 funding、无风险退出、连续完整 bars；
- in-memory 小型 fixture。

### 完成门

1. 不使用 Python `for time × for symbol` 双重事件循环；
2. 目标生效、数量漂移、下一次 delta、换手和成本不是权重差近似；
3. 逐时点 target、delta、价格、成本、收益、权益、敞口和回撤与 Event/V2 等价；
4. 空截面、仅一侧、目标归零、符号反转、Universe 变化和期末持仓有固定 fixture；
5. equivalence audit 能定位首个不一致时点、symbol 和字段；
6. MatrixResearchRun 只发布最小研究表，不能生成 V2 完整审计的假替代品。

## 5. A33：真实经济边界与低内存

### 目标

- 真实 funding 时点和缺失政策；
- mark-close/trade-close；
- 上市、退市、缺 bar、前值估值和禁止伪造成交；
- 时间 chunk、checkpoint、恢复、原子块和绝对 RSS 门；
- 不同 chunk interval 结果等价。

### 完成门

1. funding 金额、符号和计入时点与 Event/V2 一致；
2. 任何暂不支持的缺失政策由 planner 拒绝，不在 kernel 中近似；
3. checkpoint 只保存紧凑组合状态、identity 和必要计数器；
4. 中断恢复与连续运行逐时点等价；
5. 块大小不改变经济结果；
6. 市场块、候选矩阵和输出受配置硬门，超限失败关闭且不发布成功结果。

## 6. A34：批量研究与提升

### 目标

- 多因子、多窗口或多组合候选共享一次市场块读取；
- 每个候选经济状态严格隔离；
- 复用 Analysis/Signal/TargetSchedule，按最早依赖失效；
- `factor-evaluate → matrix-run/sweep → event formal` 用户入口；
- 轻量研究报告与正式报告视觉/身份隔离；
- 用户确认后的候选配置可无歧义提升到 Event/V2。

### 完成门

1. N 个候选不会读取或常驻 N 份共享行情；
2. 批量每个候选与单独 Fast Matrix 运行等价；
3. batch 不能自动选择、自动正式发布或覆盖旧研究结果；
4. 提升保持 dataset、factor、selection、target、成本和区间身份，可解释的后端字段除外；
5. Event 正式 run 报告包含来源 `fm-*` 和逐时点等价摘要；
6. CLI 帮助、用户手册和错误信息明确区分研究结果与正式结果。

## 7. A35：代表性真实数据验收与性能门

### 代表性范围

- 复用现有 Binance USD-M 1m 数据，不下载已经存在且兼容的数据；
- 使用只覆盖 Fast Matrix 第一版能力的中性验收配置，不建立策略族、不分配用户策略简称；
- 配置应包含固定因子/调仓时钟、常规 Top/Bottom 目标权重、当前手续费/滑点和真实 funding；
- 精确因子、参数、区间和候选数在 A35 开始前基于性能基线单独确认，不写入本长期计划；
- 验收结果只证明引擎正确性与性能，不代表用户决定使用该因子。

### 测量

分别记录：

- 研究诊断；
- 单候选 Fast Matrix 冷/热；
- 六候选共享 batch；
- 相同单候选 Event/V2；
- wall/CPU、读取行数/字节、峰值 RSS、输出字节、缓存命中和各阶段耗时。

### 完成门

1. 固定 fixture 逐时点等价，既有真实日数据的期末权益与 Event/V2 等价；
2. 真实 24 合约日分片测得约 3.21×，不将单次数字写成永久承诺；
3. 六候选 batch 的边际成本显著低于六个独立冷运行，且有实际数字；
4. 累计换手、手续费和滑点诊断在 Event 对照执行前产生；
5. 提升机制由固定 fixture 完整验收，A35 不要求把真实验收配置登记成用户正式策略；
6. 报告、文档、研究/对照身份和 Git 交接完整。

完整月度全市场 Event 对照属于具体候选的正式使用成本，不作为引擎代码发布前的重复门；
真实开发验收范围和数字见 `A35.md`。

## 8. 前置维护

当前交互报告对高持仓/高事件 run 存在按时间排序后施加全局 limit、导致后段快照饿死的
已知缺陷。它不属于 Fast Matrix 核心，但应在 A34 研究/正式报告联调前修复并回归；修复只
重建既有报告，不重新执行经济回测。

## 9. 阶段外事项

- 任意 Python 策略插件系统；
- tick/订单簿与非线性冲击模型；
- 分布式集群；
- 自动因子挖掘、自动选优和自动上线；
- 全年全市场压力测试；
- 将 legacy V1 artifacts 迁移成新身份。
