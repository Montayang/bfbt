# R3：15M EMA7/25 固定三分之一保证金

- `family_id`：`full_market_rank_descent_long`
- `variant_id`：`rdl_ema_7_25_15m_intrabar_fixed_margin_third`
- 正式简称：`R3-15M EMA7/25固定1/3`
- 首次正式回测：`R3-202607-r01`

## 因子与时点

R3 使用 15 分钟 K 线的 EMA7 与 EMA25：

```text
factor(t) = EMA7_live(t) / EMA25_live(t) - 1
alpha(span) = 2 / (span + 1)
```

因子、全市场 Rank 和调仓频率均为 1 分钟。每个已完成 1 分钟 K 线的 close 作为当前
未收盘 15 分钟 K 线的临时 close；临时 EMA 始终从上一根已完整收盘的 15 分钟 EMA
计算，不把同一根慢 K 线的前一分钟临时值递归写回。15 分钟收盘时才提交 EMA 状态。

每合约跨 chunk 只保存 EMA7、EMA25、连续样本数和最后提交时点。至少 25 个连续完整
15 分钟样本后才有效；缺口重置该合约 EMA 连续状态。信号使用已完成分钟的信息，在下一
根 1 分钟 K 线开盘成交。

## 继承 R2 的经济规则

- 初始权益 10,000 USDT；每次开仓固定保证金为初始权益三分之一，即
  `3,333.3333333333335 USDT`。
- 5 倍杠杆、单多仓、Rank 从至少 5 非上升到 1 触发、持平保留、上升重置。
- 止盈 3.6%、止损 2.0%，1 分钟 OHLC、`same_bar_trigger`；同 K 线双触发按止损。
- taker 手续费 4 bps、固定滑点 1 bps、真实资金费率；缺失按 0 并记录 warning。

实现与离线验收见 `docs/acceptance/A26.md`，正式结果见 [`runs.md`](runs.md)。
