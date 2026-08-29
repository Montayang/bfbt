# R5/R6：相位采样均值比 Rank5 固定三分之一保证金

## 1. 稳定身份

- R5：`rdl_sampled_mean_ratio_15m12_pos_fixed_margin_third`，正向因子。
- R6：`rdl_sampled_mean_ratio_15m12_neg_fixed_margin_third`，反向因子。
- 研究区间：`[2026-07-01T00:00:00Z, 2026-08-01T00:00:00Z)`。
- R5 与 R6 是两个独立因子和策略身份，不用一个方向字段事后翻转解释。

## 2. 因子与时点

对每个合约，在已完成 1 分钟 K 线的收盘时点 `t` 取12个价格：

```text
close(t), close(t-15m), close(t-30m), ..., close(t-165m)
mean12(t) = mean(上述12点)
R5_raw(t) = close(t) / mean12(t) - 1
R6_raw(t) = -R5_raw(t)
```

这里不构造自然15分钟 K 线，也不使用 Alpha31 的12根自然慢 K 线窗口。所有点来自同一套
1分钟 close，按15个基础 bar 的固定相位行距抽样。任一抽样点缺失、不完整、非正或时间差
不精确时，该时点因子无效；不会用相邻行跨越缺口。

因子、截面 Rank 和调仓决策频率均为1分钟。信号只使用已经完成的分钟信息，在下一分钟
真实开盘成交。R5 因子值越高表示当前价格相对采样均值越强；R6 因子值越高表示越弱。

## 3. Rank、仓位与执行

- Rank 按各自因子值从高到低，稳定 symbol 次序打破并列。
- Rank 从至少5开始；随后 Rank 数字下降或持平，首次到达1触发做多。
- Rank 上升、缺失或退出时点 Universe 时重置；事件消费后重新等待新序列。
- 全账户单多仓；新信号先平旧仓再开新仓。
- 初始权益10,000 USDT，每次固定保证金3,333.333333 USDT，5倍杠杆。
- taker 手续费4 bps、固定滑点1 bp、真实 Funding；缺失 Funding 按0并保留 warning。
- 估值使用 trade close；风险触发使用1分钟 trade OHLC。

## 4. 固定风险 profiles

| Profile | 止盈 | 止损 |
|---|---:|---:|
| `BASE` | 关闭 | 关闭 |
| `F1` | 2.4% | 1.2% |
| `F2` | 3.6% | 2.0% |
| `F3` | 4.8% | 2.4% |
| `F4` | 5.8% | 2.8% |
| `F5` | 7.2% | 3.6% |

硬退出使用 `same_bar_trigger`；跳空止损取更差可成交价，同一分钟止盈止损双触发按止损。
风险退出优先，同一分钟不重新开仓。动态止盈不在本研究范围。

## 5. 研究、run 与报告身份

R5 和 R6 各有一个父 Event study；每个父 study 下包含六个 profile。风险参数不影响上游
AnalysisSnapshot/SignalSnapshot，因此同一因子的六组回放复用因子、Rank 和 Rank 递减信号。
R5/R6 因子身份不同，各自生成独立 AnalysisSnapshot 和 SignalSnapshot。

每个 profile 发布独立不可变 Event run 和完整子报告，别名格式为
`R5-F2-202607-r01` / `R6-F2-202607-r01`。父 `report.html` 使用类似 sheets 的页签统一索引
六个子报告，但不改变子 run 的配置哈希、经济路径或审计事实。

若账户权益非正，子 run 按引擎规则以 `failed` 终止：保留不可变失败 artifact 和专用失败
子页，不伪造成 -100% 的成功回测，也不生成虚构收益、回撤或成交指标。父研究在六个参数
身份都到达终态且至少一个失败时标记为 `completed_with_failures`。

七月按用户的 Event 决策口径作为此前未用于规则选择的评估区间；查看本研究全部结果后，
七月不再能用于同一批参数的再次未见验证。

## 6. 七月正式结果

- R5 父研究：`r5_sampled_mean_ratio_pos_2026_07`，6/6 成功；BASE 收益 +472.18%、最大
  回撤 -91.00%，F4 收益 +316.93%、最大回撤 -67.59%。
- R6 父研究：`r6_sampled_mean_ratio_neg_2026_07`，4 个完整成功、2 个权益非正失败；四个
  完整 run 收益为 -99.57% 至 -100.00%。
- 两份父报告分别位于 `data/backtest/event_studies/<study_id>/report.html`；精确 run ID
  与全参数结果登记于 `runs.md`。
