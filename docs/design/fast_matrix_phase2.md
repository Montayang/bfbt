# Fast Matrix 第二阶段：联合区间执行与复杂度合同

状态：已实现（2026-09-29）。验收证据见 `docs/acceptance/A41.md`。

## 1. 目标与不变边界

本阶段只优化 Fast Matrix 的多候选研究路径，不扩大它的策略能力。Quick Research → Fast
Matrix → Event 的职责、TargetSchedule、next-bar open、真实数量漂移、手续费、滑点、funding、
mark/trade close、不可变身份和人工 Event 晋级均不改变。路径依赖风控仍必须进入 Event。

优化目标是减少重复工作，而不是删除时间因果：行情只准备一次，多个候选联合执行；状态只在
调仓或资金费等真实边界递推；两个边界之间的所有时点一次列式估值；持仓状态只保存非零仓位。

## 2. 复杂度基线

定义：

- `T`：市场时点数；`S`：市场 symbol 数；`C`：候选数；
- `R`：联合调仓/funding 状态边界数；
- `K_c,r`：候选 `c` 在区间 `r` 的非零持仓数，`H_r = Σ_c K_c,r`。

第一版 batch 只共享一次 LazyFrame 物化，随后调用单候选内核 `C` 次：

```text
市场校验、分组和连接：O(C × T × S)
Python 时间控制：     O(C × T)
候选状态：            O(C × S) 的逻辑宽度
```

第二阶段联合路径：

```text
共享市场准备：         O(T × S)
不可省略的输出：       O(C × T)
区间稀疏估值：         O(Σ_r interval_length_r × H_r)
调仓状态更新：         O(Σ candidate rebalance (K_old + K_target))
顺序状态深度：         O(R)，而不是每个候选各自 O(T)
活动持仓状态：         O(max_r H_r)
```

当 `K << S` 时，主要候选估值项由 `O(C × T × S)` 收敛为 `O(C × T × K)`；共享行情输入的
`O(T × S)` 和必须发布的 `O(C × T)` return rows 仍不可消除。若所有候选持有所有 symbol，
最坏情况仍为 `O(C × T × S)`；本设计不声称在稠密最坏情况下改变算术复杂度。

区间计算的瞬时表还与当前区间长度和活动持仓数有关。它不构造整段历史的
`T × C × S` dense cube；大范围运行仍应使用已有 chunk 边界控制市场输入内存。

## 3. 执行布局

1. 一次收集并验证 trade bars；mark bars 和 funding 也各自只准备一次。
2. 建立共享市场时钟、真实 open 查找和按 symbol 的估值历史。
3. 合并所有候选的调仓时点与 funding 所在 bar，形成联合状态边界。
4. 在边界上按候选更新稀疏 `quantity / average_entry_price / cash`；未出现在完整目标快照中的
   旧持仓明确归零。已持仓但当前没有真实 open 时保留原数量，禁止伪造成交。
5. funding 在调仓后、收盘估值前应用，顺序与单候选/Event 合同一致。
6. 对边界至下一边界的整段时钟，把活动持仓与 carry-forward close 做 as-of 连接；所有候选
   的 equity、exposure、return 和 drawdown 在同一列式批次产生。
7. 每个候选仍产生独立 `fm-*` 身份、returns、rebalance audit、checkpoint 和结果哈希。

单候选和 chunked API 保留为兼容、恢复和等价对照路径。联合 batch 不自动选择候选，不发布
正式 run，也不覆盖旧产物。

## 4. 真实性与失败语义

- 结果逐字段与同配置的独立 `run_fast_matrix` 对照，容差 `1e-12`；checkpoint 和 result hash
  必须完全相同。
- `trade_close`、`mark_close`、fee、slippage、funding 和缺失活动 funding 的失败策略均保留。
- 调仓仍使用调仓前权益和真实 open；费用仍使用真实 `abs(delta_notional)`。
- carry-forward 只用于已持仓估值或无法成交时的持仓标记，不能给新仓制造开盘价。
- 每个市场快照仍必须有一致 close time；每个 trade 时点仍必须存在估值快照。

## 5. 基准合同

可复现离线命令：

```bash
.venv/bin/python -B tests/benchmarks/benchmark_fast_matrix_phase2.py
```

固定合成形状：360 个市场时点、64 个 symbol、23,040 行、6 个候选、每候选 8 个活动持仓、
每 30 bars 调仓；费用和滑点开启。基准比较：

- 同一内存行情上的 6 次独立单候选运行；
- 一次第二阶段联合 batch；
- 基准在输出性能数字前先证明每个候选的 checkpoint 和 result hash 完全相同；
- 记录 wall/CPU、联合状态边界数、稀疏状态峰值和 dense 对照宽度。

2026-09-29 本机一次结果：独立运行 4.488 秒，联合 batch 0.296 秒，约 15.14×；联合路径
13 个状态边界，稀疏状态峰值 48 行，对照 dense `candidate × symbol` 为 384 cells（12.5%）。
这是可复现实验事实，不是跨机器、数据形状或版本的固定性能承诺。真实研究仍应在实际数据
形状上重新测量，且不得为速度关闭费用、funding、身份或审计。

## 6. 诊断合同

`MatrixBatchResult.diagnostics` 公开：候选数、共享行情加载/预处理次数、市场行数与时点数、
状态边界/估值区间数、稀疏持仓峰值、dense 对照 cells、稀疏比例和总耗时。每个候选诊断标记
`execution_mode=joint_batch`，但经济结果和身份仍与独立执行相同。
