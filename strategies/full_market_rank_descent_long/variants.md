# 策略变体注册表

以下名称是后续交流、配置和运行索引使用的稳定身份。

| 编号 | 正式简称 | `variant_id` | 因子 | 仓位 | 状态 |
|---|---|---|---|---|---|
| R1 | `R1-24H动量全仓` | `rdl_momentum_24h_equity_full` | 1 分钟更新的 24H 收益率 | 当时权益 100% 保证金 | 已回测 |
| R2 | `R2-24H动量固定1/3` | `rdl_momentum_24h_fixed_margin_third` | 1 分钟更新的 24H 收益率 | 固定为初始权益 1/3 的保证金 | 已回测 |
| R3 | `R3-15M EMA7/25固定1/3` | `rdl_ema_7_25_15m_intrabar_fixed_margin_third` | 15 分钟 EMA7/EMA25，因子与 Rank 每分钟更新 | 固定为初始权益 1/3 的保证金 | 已回测 |
| R4 | `R4-15M EMA7/25 Rank7固定1/3` | `rdl_ema_7_25_15m_intrabar_rank7_fixed_margin_third` | R3 因子；Rank 从至少 7 非上升到 1 | 固定为初始权益 1/3 的保证金 | 已回测 |
| R5 | `R5-相位均值比正向Rank5固定1/3` | `rdl_sampled_mean_ratio_15m12_pos_fixed_margin_third` | 每分钟用 `t、t-15m…t-165m` 12 点价格均值比，原始方向 | 固定为初始权益 1/3 的保证金 | 七月 6-profile Event 研究已完成 |
| R6 | `R6-相位均值比反向Rank5固定1/3` | `rdl_sampled_mean_ratio_15m12_neg_fixed_margin_third` | R5 因子直接取负，作为独立因子身份 | 固定为初始权益 1/3 的保证金 | 七月 6-profile Event 研究已完成，2 组权益非正 |

## 可接受的口头简称

- `R1`、`24H动量全仓策略` → `rdl_momentum_24h_equity_full`
- `R2`、`24H动量三分之一策略` → `rdl_momentum_24h_fixed_margin_third`
- `R3`、`15分钟EMA三分之一策略` → `rdl_ema_7_25_15m_intrabar_fixed_margin_third`
- `R4`、`15分钟EMA Rank7策略` → `rdl_ema_7_25_15m_intrabar_rank7_fixed_margin_third`
- `R5`、`相位均值比正向策略` → `rdl_sampled_mean_ratio_15m12_pos_fixed_margin_third`
- `R6`、`相位均值比反向策略` → `rdl_sampled_mean_ratio_15m12_neg_fixed_margin_third`

R3 的精确盘中 EMA 状态、无前视和跨 chunk 语义见
[`ema_7_25_15m_intrabar_spec.md`](ema_7_25_15m_intrabar_spec.md)。
R4 的唯一配置变化和对照边界见
[`ema_7_25_15m_intrabar_rank7_spec.md`](ema_7_25_15m_intrabar_rank7_spec.md)。
R5/R6 的相位采样公式、双因子身份、风险 profiles 和父子报告边界见
[`sampled_mean_ratio_15m12_rank5_spec.md`](sampled_mean_ratio_15m12_rank5_spec.md)。
