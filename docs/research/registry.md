# 因子研究注册表

更新时间：2026-09-05。详细数值和失败证据见对应 study 记录；本表只维护稳定身份和晋级
状态。

## 1. 原始因子定义

来源批次 `gtja191-trend-momentum-v1` 共 12 个公式定义，均已实现并有离线公式/边界测试；
这里的 `verified` 是公式正确性状态，不是收益筛选结论：

| Factor ID | 公式版本 | 简述 | 状态 |
|---|---|---|---|
| `gtja_alpha018` | v1 | 5 根价格比值 | verified |
| `gtja_alpha020` | v1 | 6 根收益率 | verified |
| `gtja_alpha024` | v1 | 5 根价格差递归平滑 | verified |
| `gtja_alpha031` | v1 | 12 根均线偏离 | verified |
| `gtja_alpha040` | v1 | 26 根上涨量/下跌量 | verified；quote 主、base 对照 |
| `gtja_alpha053` | v1 | 12 根上涨比例 | verified |
| `gtja_alpha066` | v1 | 6 根均线偏离 | verified |
| `gtja_alpha071` | v1 | 24 根均线偏离 | verified |
| `gtja_alpha088` | v1 | 20 根收益率 | verified |
| `gtja_alpha089` | v1 | MACD 类递归均线差 | verified |
| `gtja_alpha112` | v1 | 12 根上下行净强度 | verified |
| `gtja_alpha151` | v1 | 20 根价格差递归平滑 | verified |

Alpha40 在快速研究中展开为 `Alpha40_quote` 与 `Alpha40_base` 两个参数序列，因此研究矩阵为
13 个因子序列，而原始公式仍是 12 个。

## 2. 开源趋势与动量定义

来源批次 `oss-trend-momentum-candidates-v0` 共 14 个 Quick Research `v1` 因子，均已进入显式注册表并通过
聚焦公式、warmup、断档、时点和参数边界测试。这里的 `verified` 仍只表示公式实现状态；
尚未运行 Quick Research，不存在本批次晋级信号或收益结论。完整公式、来源版本、许可证、
默认参数和重复关系见
[`open_source_trend_momentum_candidates.md`](open_source_trend_momentum_candidates.md)。

| Factor ID | 公式版本 | 简述 | 状态 |
|---|---|---|---|
| `oss_qlib_beta` | v1 | 20 根归一化线性趋势斜率 | verified |
| `oss_qlib_signed_rsqr` | v1 | 20 根有方向趋势拟合度 | verified；BFBT 适配 |
| `oss_qlib_rsv` | v1 | 20 根高低价通道位置 | verified |
| `oss_qlib_imxd` | v1 | 20 根高低极值出现次序 | verified；最早并列值 |
| `oss_qlib_cntd` | v1 | 20 根上涨/下跌占比差 | verified |
| `oss_ta_trix` | v1 | 15 根三重 EMA 变化率 | verified |
| `oss_ta_tsi` | v1 | 25/13 根双重平滑动量 | verified |
| `oss_ta_kst` | v1 | 固定四尺度加权动量 | verified |
| `oss_ta_kama_distance` | v1 | 10/2/30 KAMA 归一化偏离 | verified；BFBT 适配 |
| `oss_ta_vortex_diff` | v1 | 14 根 Vortex 方向差 | verified |
| `oss_ta_vpt_roll` | v1 | 20 根成交量加权收益 | verified；BFBT 适配 |
| `oss_qlib_roc_mom` | v1 | 20 根正向价格动量 | verified；重复基准 |
| `oss_qlib_sumd` | v1 | 20 根上下行幅度净强度 | verified；重复基准 |
| `oss_qlib_cord` | v1 | 20 根价量变化相关 | verified；诊断项 |

本批后续 Quick Research 沿用上一轮的 `1m/5m/15m` 源 K 线和 `1/5/20` 根预测窗口；公式
内部参数保持上述 K 线根数，不换算自然时间。开发/holdout 日期和数据版本仍须在新 study
中单独冻结。

递归公式当前通过连续历史批计算供 Quick Research 使用。它们若在未来被用户选中并要求
直接进入 Event 分块计算，仍须先增加 carried-state checkpoint 与连续/恢复等价验收。

## 3. QR-v1 晋级的因子信号

共同来源 study：`gtja191_segmented_dev_holdout_2026_05_07`。以下 12 个 signal 均由 QR-v1
使用 8 个开发分段判定；方向全部为 `-1`。

| Signal ID | Factor | K 线 | 方向 | 通过的预测根数 | 状态 |
|---|---|---:|---:|---|---|
| `qs-gtja-a112-v1-1m-neg` | Alpha112 v1 | 1m | -1 | 5、20 | quick-qualified |
| `qs-gtja-a018-v1-15m-neg` | Alpha18 v1 | 15m | -1 | 5、20 | quick-qualified |
| `qs-gtja-a020-v1-1m-neg` | Alpha20 v1 | 1m | -1 | 5、20 | quick-qualified |
| `qs-gtja-a031-v1-1m-neg` | Alpha31 v1 | 1m | -1 | 1、5、20 | quick-qualified |
| `qs-gtja-a031-v1-5m-neg` | Alpha31 v1 | 5m | -1 | 1、5、20 | quick-qualified |
| `qs-gtja-a031-v1-15m-neg` | Alpha31 v1 | 15m | -1 | 1、5、20 | quick-qualified |
| `qs-gtja-a066-v1-1m-neg` | Alpha66 v1 | 1m | -1 | 1、5、20 | quick-qualified；高 Rank 换手 |
| `qs-gtja-a066-v1-5m-neg` | Alpha66 v1 | 5m | -1 | 1、5 | quick-qualified；高 Rank 换手 |
| `qs-gtja-a066-v1-15m-neg` | Alpha66 v1 | 15m | -1 | 1、5、20 | quick-qualified；高 Rank 换手 |
| `qs-gtja-a071-v1-1m-neg` | Alpha71 v1 | 1m | -1 | 5、20 | quick-qualified |
| `qs-gtja-a071-v1-5m-neg` | Alpha71 v1 | 5m | -1 | 5、20 | quick-qualified |
| `qs-gtja-a071-v1-15m-neg` | Alpha71 v1 | 15m | -1 | 5、20 | quick-qualified |

39 个“因子序列 × K 线”中，其余 27 个在 QR-v1 下未晋级。失败是带规则版本的研究结论，
不会从原始因子库删除，也不禁止其未来在 QR-v2 或不同市场合同下重新研究。

## 4. 后续层级

- 相关性去重：未执行。
- Fast Matrix 组合研究：`gtja191_fast_matrix_dev_2026_05_06` 已完成；12 个信号展开为 29 个
  调仓计划，并分别运行 zero/realistic 成本，共 58 个终态尝试、56 个成功 `fm-*` run、
  2 个 1m realistic 权益归零失败。记录见
  [`studies/gtja191_fast_matrix_dev_2026_05_06.md`](studies/gtja191_fast_matrix_dev_2026_05_06.md)。
- Fast Matrix 自动筛选：不设置；不产生 `matrix-qualified` 状态。用户查看报告后人工决定
  哪个已评估配置进入 Event，以及增加哪些事件逻辑。本轮 27 个可完成的 realistic 路径
  收益全部为负，不能用 zero 毛收益代替可交易结论。
- Event 输入人工决定：空。
- Event 衍生策略：空。
- 实盘候选：空；回测服务器不负责真实下单。

既有 R/C 策略是此前独立开发的正式策略，不反向伪造为本研究注册表的晋级产物。
