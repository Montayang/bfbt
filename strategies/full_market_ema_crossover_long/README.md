# 全市场 EMA 交叉做多策略族

- `family_id`：`full_market_ema_crossover_long`
- 中文族名：`全市场 EMA 交叉做多`
- 运行索引：[`runs.md`](runs.md)
- 变体注册表：[`variants.md`](variants.md)

本策略族对 point-in-time universe 中的每个合约独立维护 EMA 交叉状态，不做横截面 Rank
选币。金叉产生开多信号，死叉产生策略主动平仓信号；不同合约可以同时持仓。策略使用
V2 chunked、下一根基础 K 线开盘成交、显式成本/资金费及不可变正式 run。

策略身份继续使用 `family_id → variant_id → 回测别名 → run_id`。时间区间不进入
`variant_id`；同一变体同一区间重跑时递增 `r02`，不得覆盖旧结果。
