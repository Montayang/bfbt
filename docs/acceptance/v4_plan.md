# 第四阶段 A27–A30 验收计划

第四阶段实现 [`../design/v4_reusable_analysis_and_fast_replay.md`](../design/v4_reusable_analysis_and_fast_replay.md)
中的分层复用目标，并增加全市场逐合约因子交叉策略能力。

| 阶段 | 目标 | 状态 |
|---|---|---|
| A27 | AnalysisSnapshot/SignalSnapshot 身份、不可变发布、父 hash 和失效边界 | 已完成 |
| A28 | 信号快照热回放、稀疏行情读取、冷/热与全量/稀疏经济等价 | 已完成 |
| A29 | 共享输入 risk/仓位 sweep；in-memory 与逐块多状态路径 | 已完成 |
| A30 | 分钟盘中因子真正穿越、LONG/FLAT 指令、独立多仓和交叉报告 | 已完成 |

共同正确性门：继续使用 V2 chunked，不修改旧 run，不允许缓存 hash/父级不符时降级，
不改变无前视、next-open、成本、风险、恢复和内存硬门。具体证据见对应 Axx 文档。

测试、真实回测、联网和 Git 操作仍由用户当次授权控制。本阶段没有引入实盘 Client 或
账户访问；正式 C1 复用既有六月、七月 DatasetSnapshot，不下载新数据。
