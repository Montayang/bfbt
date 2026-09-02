# 实际策略工作区

这里保存用户实际希望运行的策略，不属于框架测试、验收用例或通用架构设计。

## 目录约定

共享机制相同的策略使用稳定 `family_id` 建立策略族目录；因子、仓位等经济语义不同的
版本使用稳定 `variant_id` 登记：

```text
strategies/
  <family_id>/
    README.md         # 共享机制与身份规则
    variants.md       # 策略变体的稳定名称、variant_id 和状态
    <factor>_spec.md  # 已确认变体的公式、成交语义和边界
    runs.md           # 回测别名、区间、修订号与不可变 run_id 映射
```

大型数据、Parquet 表和 HTML 报告不提交 Git，仍存放在 `data/backtest/`。`runs.md`
记录正式 `run_id`、数据集版本、配置路径和报告路径，使自然语言需求、执行配置和不可变
产物可以互相追溯。

策略族目录不拥有行情数据。可被多种策略复用的采集、标准化和快照准备说明统一放在
`data_collections/<collection_id>/`；本机实体数据位于
`data/backtest/datasets/<collection_id>/`。不同策略只要市场、K 线粒度、时间覆盖和数据
语义相容，就应引用同一不可变 DatasetSnapshot，不应按策略重复下载。

## 推荐工作流

1. 用户用自然语言描述希望实际回测的策略。
2. 对符合目标权重、固定时钟和线性成本边界的常规截面策略，先用 Fast Matrix 发布
   `fm-*` 研究结果；复杂事件策略直接使用 Event/V2。
3. Codex 在对应策略族中登记 `variant_id` 并整理规格，把歧义和当前能力缺口明确列出。
4. 用户确认经济语义后，Codex 实现缺失的通用能力；可复用代码仍进入 `src/bfbt/`，
   不能藏在策略文档目录中。
5. 每次正式运行前按 `variant_id` 保存版本化 Event 配置，并在 `runs.md` 增加计划记录。
6. Codex 完成数据准备、正式回测、产物校验和报告重建。
7. 成功后把 `run_id` 和报告位置写回 `runs.md`，用户直接查看报告。

数据准备与策略运行是两个独立步骤：缺少时间覆盖时扩充数据集合并发布新快照；已有合适
快照时，新策略直接复用其 `dataset_id` 与 `dataset_version`。

框架自身的功能测试仍放在 `tests/`，阶段验收文档仍放在 `docs/acceptance/`；不得把
用户实际策略伪装成验收 fixture 或测试场景。

## 当前策略族

- [`full_market_rank_descent_long`](full_market_rank_descent_long/README.md)：
  全市场 Rank 递减做多策略族，包含 R1–R4 四个已回测变体。
- [`full_market_ema_crossover_long`](full_market_ema_crossover_long/README.md)：
  全市场逐合约 EMA 金叉开多、死叉平多的独立多仓策略族。
