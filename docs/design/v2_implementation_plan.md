# 第二版实施路线与完成标准

本文把《第二版架构设计》拆成可独立验收、可独立提交的开发阶段。第二版从 A12
开始编号，不重写已经验收的第一版能力。

## 1. 实施原则

- 第一版配置和经济结果是兼容基线；第二版功能由 `config_version: v2` 显式开启。
- 每个阶段先固定配置、schema、reason code 和 golden fixture，再实现行为。
- 一个阶段只交付表中约定的能力，不提前混入下一阶段。
- 默认验收完全离线、数据量小且确定；联网和真实数据只放在最终全链路阶段。
- 分块与非分块必须产生相同经济结果，状态必须有明确复杂度和硬上限。
- 不新增外部回测框架；继续复用现有 Polars、PyArrow、DuckDB、Pydantic 和账本。
- 每阶段完成代码后补充 `docs/acceptance/Axx.md`、固定 fixture 和独立验收脚本。

## 2. 依赖关系

```text
A12 配置与契约
  └─ A13 精确 Rank
       └─ A14 历史 Rank
            └─ A15 增量仓位与资金模型
                 └─ A16 止盈止损状态机
                      └─ A17 统一事件、产物与报告
                           └─ A18 兼容回归与真实全链路
```

严格按顺序开发。A13 和 A14 看似可以并行，但先固定当前 Rank 产物后，历史状态的
语义和 golden 结果更容易审计。

### 2.1 代码落点

第二版不复制一个 `bianbt_v2` 包，也不从回测子项目再次独立出去。它是现有
`src/bianbt` 的兼容演进，可以复用已经验收的数据、账本、分块、artifact
和报告能力；仍与仓库中的实盘 `bianbot` 保持隔离。

```text
src/bianbt/
├── config/backtest.py          # V1 adapter、V2 配置和跨字段校验
├── data/schemas.py             # rankings/instructions/risk events schema
├── portfolio/
│   ├── ranking.py              # 新增：稳定 Rank 快照和有界历史状态
│   ├── selection.py            # 扩展：精确/历史 Rank 选择
│   ├── instructions.py         # 新增：目标与增量仓位指令
│   └── constraints.py          # 扩展：敞口、保证金和缩放/拒绝
├── engine/
│   ├── state.py                # 新增：持仓、资金、冷却和跨块状态
│   ├── risk.py                 # 新增：止盈止损与组合风险规则
│   ├── events.py               # 新增：意图优先级和冲突仲裁
│   ├── execution.py            # 扩展：指令引用和 next-open 成交
│   └── streaming.py            # 扩展：确定性事件循环
├── performance/
│   ├── chunks.py               # 扩展：V2 状态边界
│   └── spool.py                # 扩展：新明细表有界落盘
├── artifacts/store.py          # 扩展：schema/version/原子发布
├── reports/renderer.py         # 扩展：V2 双语交互审计
└── application/
    ├── run.py                  # V1/V2 分派
    └── chunked.py              # V2 分块编排
```

文件名是开发前目标，可在不改变职责边界的前提下微调。核心接口依次为
`RankSnapshotBuilder`、`SelectionPolicy`、`PositionInstructionBuilder`、
`ConstraintEngine`、`RiskRule`、`EventArbitrator` 和 `ExecutionLedger`。
上游只生成不可变的意图，下游账本才产生实际成交；报告只读取正式产物，不参与决策。

## 3. A12：V2 配置与数据契约

### 实现

- 增加 `config_version`，实现 V1 兼容适配和 V2 严格分派。
- 增加 selection、sizing、capital、risk、performance 配置模型及跨字段校验。
- 定义 rankings、position instructions、risk events 的 Arrow schema 和版本。
- 固定 event priority、reason code、resolved config 和 manifest 字段。
- 为旧配置生成不改变旧执行语义的内部标准形式。

### 验收

- V1 配置加载后的经济字段与当前版本一致。
- V2 合法示例可以解析、解析后再序列化且指纹稳定。
- Rank 重叠、非法 lag、缺少金额、非法风险频率和 mark 数据缺失等配置在扫描数据前
  失败。
- schema 字段、空值政策、枚举和版本具有固定 golden。

### 暂不包含

不改变选币、仓位或事件循环；该阶段只有契约与兼容层。

## 4. A13：当前快照精确 Rank

### 实现

- 生成确定性的 raw score、ordinal Rank、percentile Rank 和 sample count。
- 实现多空独立的 Rank 列表与闭区间选择。
- 支持单边持仓、Rank 越界和当前 universe 交集 reason code。
- 发布 `rankings.parquet`，并让第一版 Top/Bottom 适配到连续 Rank 区间。

### 验收

- 明确覆盖“做多 Rank 2、做空 Rank 1”。
- 覆盖多 Rank、区间、单边、样本数变化、并列分数和 Rank 越界。
- 并列值始终按 `score DESC, symbol ASC` 得到相同结果。
- V1 Top/Bottom 经适配后，targets 及下游经济列与兼容基线相同。
- rankings 的行数、唯一键、排序和 run 归属可审计。

### 暂不包含

仅支持 `lag=0`；不做跨快照状态，不改变 target-weight 仓位语义。

## 5. A14：跨快照 Rank 与有界状态

### 实现

- 支持 `clock=factor|rebalance` 和固定非负 `lag`。
- 使用有界环形缓冲保存最近 L 个 Rank 快照。
- 在 chunk 边界序列化和恢复最小状态。
- 保存 decision time、rank source time、lag、clock 和缺失原因。
- 实现 `max_rank_lag`、`max_rank_state_rows` 与 RSS 预算门。

### 验收

- 明确覆盖“当前时刻做多上一个快照 Rank 1”且没有前视。
- factor clock 与 rebalance clock 在 1h 信号、4h 调仓下得到预期的不同来源时刻。
- 历史 Rank 合约当前不 eligible 时跳过且不补位。
- 第一段没有足够历史时产生明确原因，不读取未来快照。
- chunk 边界前后不重选、不漏选；分块与非分块经济列一致。
- `lag=1` 状态规模为 O(N)，固定 L 为 O(N×L)；超限在 OOM 前明确失败。

### 暂不包含

执行引擎不提供任意历史宽矩阵。完整 Rank 历史只作为分区 Parquet 研究数据按需扫描。

## 6. A15：增量仓位与资金模型

### 实现

- 将 selection 与 position instruction 解耦。
- 保留 `target_weight`，新增 fixed margin、fixed notional、equity fraction 和
  position fraction 四种增量模式。
- 实现 initial equity、simple-cross 初始保证金、可用保证金和成本缓冲。
- 实现 flatten only、flatten then open、net delta 三种反向政策。
- 实现总敞口、净敞口、单合约、连续加仓、换手和可用保证金约束。
- 发布 requested/constrained delta 与 reason code。

### 验收

- 相同方向的连续信号按配置重复加仓，不被误解释成目标权重。
- 四种模式的基数、杠杆、舍入和零仓位政策有独立 fixture。
- 多空总敞口和净敞口均可与调仓前不同，且只受显式约束限制。
- 反向政策在过零点、余额不足和约束缩放时符合固定顺序。
- 成交、持仓、现金、未实现盈亏、成本和保证金恒等式逐 bar 成立。
- V1 target-weight 回归不变。

### 暂不包含

不模拟 Binance 维持保证金阶梯、真实强平、ADL、逐仓转账或统一账户。

## 7. A16：止盈止损与风险状态机

### 实现

- 在基础 K 线时钟上独立执行单合约止损、止盈和移动止损。
- 支持组合止损、止盈和最大回撤退出。
- 实现 close 与 reduce-fraction 动作、冷却和重新入场政策。
- 固定 trade/mark 触发源、next-open 成交、跳空和 OHLC 双触发政策。
- 为每个持仓维护均价、有利极值、风险线、触发计数和冷却状态。

### 验收

- 覆盖多头/空头、止盈/止损/移动止损和组合回撤。
- 覆盖 1m 风险检查、1h 因子和 4h 调仓三个独立时钟。
- 同一 K 线双触发按 worst-case 或 error 处理，不根据未知盘中路径乐观选择。
- 跳空不按已经穿越的理想触发价成交。
- 加仓后平均入场价和风险线更新正确；平仓后状态不会污染下一笔持仓。
- chunk 边界不会重复触发、漏触发或重置冷却。

### 暂不包含

不实现盘口 stop-market 撮合、部分成交或交易所真实强平。

## 8. A17：统一事件、产物和交互报告

### 实现

- 将定时策略、universe 退出和风险意图送入统一优先级仲裁器。
- 生成 position instructions、risk events，并用稳定引用关联 trades。
- 扩充 positions、metrics、manifest 和失败诊断字段。
- 报告区分因子、Rank、调仓、风险四个时钟，并展示 Rank 来源、保证金、风险线、
  触发原因和抑制意图。
- 保持详细表和快照 payload 有界，低频内容默认折叠。

### 验收

- 同时发生风险退出和定时开仓时，高优先级退出生效且同一时刻不重新开回。
- 每次请求、约束、抑制、成交和风险触发都能从产物双向追溯。
- 报告可交互查看某时间点的持仓、Rank 来源、关联成交与风险事件。
- 双语名称和自然语言策略说明覆盖新字段，不直接暴露难懂变量名作为主要内容。
- 报告重建不重跑回测，旧 schema run 仍能用兼容模板重建。

### 暂不包含

不增加实盘下单接口，不让报告执行任意查询或加载完整历史到浏览器。

## 9. A18：兼容回归与真实全链路

### 实现和验收

- 执行 A12–A17 的完整离线回归和 V1 golden 回归。
- 使用已有真实 Binance 教程数据集完成标准化后到报告的代表性全链路。
- 至少执行：当前精确 Rank、lag=1 Rank、固定保证金增量仓位、止盈止损冲突四种
  小型真实数据场景。
- 每种场景复跑得到相同 run identity 或明确的幂等状态，经济产物一致。
- 记录峰值 RSS、Rank 状态行数、持仓状态数、执行时间和磁盘占用。
- 验证 terminal failed、临时目录清理、报告重建和 catalog 发布。

本服务器的 A18 目标是逻辑正确和代表性真实数据闭环，不做一年全市场压力测试。以后
迁移到更大内存服务器时，另开容量验收，不改变第二版功能完成定义。

## 10. 验收脚本规划

| 阶段 | 计划验收脚本 | 主要 fixture |
|---|---|---|
| A12 | `test_acceptance_12_v2_contracts.py` | V1/V2 配置、非法配置、schema golden |
| A13 | `test_acceptance_13_exact_rank.py` | 并列、越界、单边和精确 Rank 截面 |
| A14 | `test_acceptance_14_historical_rank.py` | 双时钟、多 chunk、universe 变化 |
| A15 | `test_acceptance_15_position_sizing.py` | 四种 sizing、反向、保证金不足 |
| A16 | `test_acceptance_16_risk_events.py` | 多空 OHLC、跳空、双触发、冷却 |
| A17 | `test_acceptance_17_event_report.py` | 冲突事件、三张新表、报告快照 |
| A18 | `test_acceptance_18_v2_e2e.py` | V1 golden 与小型真实 DatasetSnapshot |

每份验收文档必须列出环境、命令、测试意图、预期结果、相关代码和失败定位。测试默认
由用户执行；只有用户针对该阶段明确授权后，Codex 才在保留的 tmux 验收会话内运行。

## 11. 依赖和接口决策

第二版默认不增加第三方运行依赖：

- Pydantic 负责配置和跨字段校验。
- Polars 负责分块扫描、截面 Rank 和列式转换。
- PyArrow/Parquet 负责有 schema 的有界明细发布。
- DuckDB 负责 catalog、coverage 和研究查询。
- 现有 CLI、artifact store、ledger、renderer 继续作为外部入口。

不引入 Backtrader、Zipline、vectorbt 或事件总线框架。若开发中确实需要新包，必须先
记录用途、版本、许可证、内存影响和不用现有依赖实现的理由，再进入对应验收阶段。

## 12. 第二版完成定义

同时满足以下条件才称为第二版完成：

1. A12–A18 逐阶段验收并按用户指示提交。
2. 用户提出的精确 Rank、历史 Rank、内存保护、增量仓位和止盈止损全部可配置、
   可执行、可审计。
3. V1 配置无需改写即可运行，经济结果通过 golden 兼容门。
4. 分块与非分块一致，历史 Rank 和风险状态不会改变内存复杂度数量级。
5. 真实 Binance 数据完成代表性全链路，报告可解释全部策略和风险动作。
6. 文档、配置参考、数据契约、用户教程和报告说明与最终代码一致。

## 13. 用户需求追踪

| 已提出需求 | 设计位置 | 实现/验收阶段 |
|---|---|---|
| 任意指定多空 Rank，例如多 Rank 2、空 Rank 1 | V2 设计 4.2 | A13 |
| 使用上一或前 N 个快照的 Rank | V2 设计 4.3 | A14 |
| 历史 Rank 不得造成全历史常驻内存 | V2 设计 4.4、8 | A14、A18 |
| 每次增加固定保证金、名义金额或比例，而非只能目标仓位 | V2 设计 5.1 | A15 |
| 调仓后总敞口、净敞口不要求与调仓前一致 | V2 设计 5.3 | A15 |
| 按止盈、止损、移动止损或组合风险调仓 | V2 设计 6、7 | A16、A17 |
| 区分因子、调仓和风险检查频率 | V2 设计 3、6、10 | A16、A17 |
| 报告保留公式、中文说明、区间、执行细节和双语名称 | V2 设计 10 | A17 |
| 快照持仓和关联成交折叠并纵向展示 | V2 设计 10 | A17 |
| 数据集、run、report 目录保持隔离 | V2 设计 9 | A17、A18 |

这张表是第二版范围检查清单。若开发中发现用户需求只能通过改变这里的边界实现，应先
更新设计并说明影响，再进入代码阶段。
