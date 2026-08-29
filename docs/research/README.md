# 因子研究与晋级登记

本目录维护从因子定义到事件策略的研究血缘。它记录“为什么进入下一层”，不复制本机
`data/backtest/` 中的大体积结果，也不把研究候选混入正式策略目录。

## 对象层级

| 层级 | 核心单位 | Git 中的事实来源 | 当前状态 |
|---|---|---|---|
| 因子定义 | 公式 ID + 公式版本 + 参数 | 因子实现、测试、来源文档和 `registry.md` | 已登记首批 GTJA191 12 因子 |
| 快速研究记录 | 因子序列 × K 线 × 研究合同 | `studies/` 与本机不可变研究汇总 | 首批研究完成 |
| 因子信号候选 | 因子版本 × K 线 × 冻结方向 | `registry.md` 中带规则版本的晋级记录 | QR-v1 晋级 12 个组合 |
| 组合研究 | 因子信号 + 组合构建 + 执行配置 | Fast Matrix study 与 `fm-*` 产物 | 29 个调仓计划已完成双成本研究 |
| Event 输入决定 | Fast Matrix 已评估配置 + 用户人工选择 | study/run 引用与人工决定记录 | 尚无选择 |
| Event 策略 | 人工选择结果 + 状态机 + 风险和执行语义 | `strategies/<family_id>/` 与正式 run | 本研究批次尚无衍生策略 |

“通过”是某个对象在某个规则版本、研究合同和数据版本下的结论，不是对象可被移动或覆盖的
固有属性。规则升级后保留旧结论并新增记录，不从库中删除失败者。

因子定义层的 `proposed / implemented / verified` 只表示公式实现与校验状态，不是收益筛选；
所有 `verified` 因子在进入快速研究前仍属于“未经表现筛选”的因子库。只有 `verified` 因子
可以形成正式快速研究记录。

## 身份与血缘

- 原始因子使用稳定的 `factor_id:factor_version`，例如 `gtja_alpha071:v1`。
- 快速研究晋级后生成 `signal_id`；方向作为字段记录，取负不复制成新因子公式。
- 预测根数是筛选证据，不进入 `signal_id`。
- Fast Matrix 已评估结果必须有组合和执行身份；它不再只是“因子 × K 线”。
- Fast Matrix 不设自动筛选规则或 `matrix-qualified` 状态；是否进入 Event 由用户看报告后
  人工决定。
- Event 策略必须引用来源 Fast Matrix study、`fm-*` run 和人工决定；只有 Event/V2 可以
  形成正式 `run_id`。
- 快速研究晋级记录绑定研究 ID、规则版本、数据合同和结果位置。

## 目录

- [`registry.md`](registry.md)：当前各层对象与晋级状态总表。
- [`rules/QR-v1.md`](rules/QR-v1.md)：当前快速研究筛选规则。
- [`studies/gtja191_segmented_dev_holdout_2026_05_07.md`](studies/gtja191_segmented_dev_holdout_2026_05_07.md)：
  首批 12 因子的研究合同、结果和 QR-v1 决策。
- [`studies/gtja191_fast_matrix_dev_2026_05_06.md`](studies/gtja191_fast_matrix_dev_2026_05_06.md)：
  12 个晋级信号、29 个调仓计划的五月—六月连续 Fast Matrix 双成本研究。
- [`gtja191_trend_momentum_candidates.md`](gtja191_trend_momentum_candidates.md)：原报告来源、候选池与
  公式迁移说明。

本机 HTML、`summary.json`、缓存和未来 `fm-*` 产物继续放在 `data/backtest/`，不提交 Git。
Git 文档保存身份、规则、结论与本机产物定位。

## 当前推进边界

当前已完成快速研究整理和统一 Fast Matrix 组合口径的实际运行。Fast Matrix 不设置自动
筛选规则；Event 策略设计尚未开始，等待用户查看报告后人工指定。不能把 QR-v1 晋级记录、
Fast Matrix 已评估状态或 zero 毛收益描述为可实盘策略。
