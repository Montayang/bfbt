# 正式运行索引

| 回测别名 | 变体 | 区间 | DatasetSnapshot | run ID | 状态 |
|---|---|---|---|---|---|
| `C1-202606-r01` | `ema_7_25_15m_intrabar_fixed_margin_10` | `[2026-06-01, 2026-07-01)` | `live-11a24daad7b1e9a5f3643039` | `a17-581483f40a44b006c081b2d1` | 成功 |
| `C1-202607-r01` | `ema_7_25_15m_intrabar_fixed_margin_10` | `[2026-07-01, 2026-08-01)` | `live-fd672b8e69b458d1c0076d74` | `a17-feab7dab13fa96503072b728` | 成功 |

配置位于本机忽略 Git 的
`data/backtest/workspaces/ema_7_25_15m_intrabar_fixed_margin_10/configs/<月份>/`。
正式完成后在这里登记不可变 run、manifest hash、报告、资源和关键结果。

## `C1-202606-r01`

- resolved config hash：`2bdb76ad1c368db56e8592cda2361b184c7b386cb7ad6ba6ec3d22cc92718f3b`
- run manifest hash：`f526c6b525643695811d03f5d1b197090dfe9480a5786342ed107b12a166c9f1`
- 报告：`data/backtest/reports/a17-581483f40a44b006c081b2d1/report.html`
- 结果：期末权益 876.4398 USDT，总收益 -91.2356%，最大回撤 -91.5267%，
  hit rate 49.4375%；219,574 笔成交，最大同时持仓 472，期末持仓 94。
- 资源：31 个执行块，30 分 47.94 秒，worker 峰值 4,588.90 MiB；Analysis/Signal
  均命中缓存 `analysis-c09cba7b63809ffb6a408d49` / `signal-e88dbf97789bc258ceb3f0fa`。
- 99,756 个真实资金费缺失按显式策略记零；`HUMAUSDT` 在尾部两分钟缺 bar，按最后
  真实 close 估值并产生 2 条 warning，没有伪造成交或月末强平。

## `C1-202607-r01`

- resolved config hash：`1695413ce2c0b5799eaa88d30c4eeaeee75766db030b5340a1707e2cfeda4421`
- run manifest hash：`fba6f628e2cae55d67a39d90edae026ce464d8ff6fa19cff291bfbb2fbda765a`
- 报告：`data/backtest/reports/a17-feab7dab13fa96503072b728/report.html`
- 结果：期末权益 2,085.6644 USDT，总收益 -79.1434%，最大回撤 -81.3643%，
  hit rate 49.1107%；269,249 笔成交，最大同时持仓 476，期末持仓 209。
- 资源：32 个执行块，含冷分析共 39 分 43.96 秒，worker 峰值 4,858.81 MiB；新建
  `analysis-0495ebb9842e093bd8890a0c` / `signal-26db58a9b8e277798e23258a`。
- 136,937 个真实资金费缺失按显式策略记零；无止盈止损事件，无前值估值事件。

两次正式 run 都绑定干净源码提交 `811e6958e0c9d74ad9aa4c4d276a7c01ffed7c0b`。
