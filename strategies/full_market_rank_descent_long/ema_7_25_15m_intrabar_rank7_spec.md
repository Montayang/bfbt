# R4：15M EMA7/25 Rank7 固定三分之一保证金

- `family_id`：`full_market_rank_descent_long`
- `variant_id`：`rdl_ema_7_25_15m_intrabar_rank7_fixed_margin_third`
- 正式简称：`R4-15M EMA7/25 Rank7固定1/3`

R4 完整继承 R3 的因子、分钟时点、成交、仓位、成本和风险语义，仅修改 Rank 递减路径：

- `start_rank_at_least`：`5 → 7`；
- `entry_rank`：仍为 `1`；
- `equal_policy`：仍为 `keep`；Rank 数字变大仍重置；
- `audit_top_n`：`5 → 7`，确保报告可审计整个触发范围。该字段只控制发布表，真正
  改变交易的是 `start_rank_at_least`。

正式别名为 `R4-202606-r01`、`R4-202607-r01`，结果见 [`runs.md`](runs.md)。旧 R3
正式 run 在未提交源码状态下生成；R4 使用提交 `183fbbc` 的干净源码。虽然两组
resolved config 除上述字段外完全一致，但部分缺口恢复时点的有效 Rank 集合不同；因此
当前结果可以分别解释，不应把 R3/R4 的全部差额严格视为单参数因果效应。纯参数对照需要
在当前干净源码上另跑 R3 的新修订。
