# 个人研究者数据处理子系统目标与下一阶段规划

更新时间：2026-09-08。

## 1. 文档状态

本文冻结开发目标、范围和验收边界。DE-v1 的 D0–D3 已于 2026-09-29 实现并通过 A42
离线验收；D4 受控真实数据记录仍需单独授权。当前事实以 `CURRENT_STATE.md`、
`docs/design/data_prepare_de_v1.md` 和 `docs/acceptance/A42.md` 为准。

BFBT 不以建设企业级数据平台为目标。目标是在一台个人研究机器上，把 Binance 公开历史
数据可靠地准备成 Quick Research、Fast Matrix 和 Event Engine 可以共同消费的不可变数据
快照，并让不熟悉内部命令的用户也能知道“已有何种数据、缺少什么、将执行什么、结果是否
可用”。

## 2. 产品定位

数据子系统是研究和回测引擎的上游生产者，逻辑上独立，但在可预见阶段继续与 BFBT 保持
同一仓库、同一安装包和同一本地存储根。除非以后出现多个独立消费者、长期在线更新或多机
协作需求，不拆成单独服务。

目标工作流：

```text
研究所需数据说明
  -> 无副作用的覆盖与资源计划
  -> 用户授权下载或构建
  -> Raw 留存、校验、标准化和质量门
  -> 必要周期与时点合约池派生
  -> 不可变 DatasetSnapshot
  -> 面向三层引擎的数据就绪证明
```

边界必须保持：

- 回测和因子计算不得在运行中访问 Binance 或自动补数据。
- Raw、标准化分区、派生数据和 Snapshot 不得被静默覆盖。
- 数据修订产生新版本和差异记录，不改写旧研究使用的事实。
- 每个字段必须有明确事件时间、最早可见时间和完整性语义。
- 默认按需准备研究范围内的数据，不维护全市场、全历史、全周期的永久副本。
- 缺失和异常默认失败关闭；不能用 forward-fill 伪造可成交行情。

## 3. 当前已有基础

当前系统已经具备以下可复用组件，不应重写另一套实现：

- Binance USD-M Archive 与公开 REST 数据源；
- Bars、Mark Bars、Funding 和 Contracts 四类事实数据；
- 不可变 Raw 对象、checksum、内容 hash 和来源 manifest；
- 有界标准化、确定性质量报告、分区 Parquet 与原子发布；
- DuckDB Catalog、显式 dataset version 和组合式 DatasetSnapshot；
- UTC 右开 K 线、重采样和 Point-in-time Universe；
- Polars LazyFrame 的时间、symbol 和列投影扫描；
- 分块读取、资源硬门以及研究/正式 run 的数据身份绑定。

当前主要问题不是缺少另一种计算框架，而是这些能力暴露为多个偏底层步骤。用户必须自行
推导预热、标签尾部、数据覆盖、命令顺序和版本选择，也缺少统一的可恢复准备记录。

## 4. 长期开发目标

### DE01 — 统一数据需求合同

增加版本化 `ResearchDataRequirement`，至少表达：

- 市场、合约范围、研究核心区间和基础周期；
- Bars、Mark Bars、Funding、Contracts 的必要性；
- 因子最大历史预热、Universe 历史窗口和标签未来尾部；
- 所需派生周期及每种数据的最早可见时间；
- 使用目的：Quick Research、Fast Matrix 或 Event；
- 原始用户需求 hash、假设、未决项和精确配置来源。

该合同只描述需求，不隐式授权联网、下载、构建或运行研究。

### DE02 — 无副作用的数据计划

由需求合同生成稳定的 `DataPlan`，一次说明：

- 本地已经满足的覆盖和可复用 Snapshot；
- 缺失日期、合约、数据类型、周期和分区；
- 将读取、下载、生成和写入的对象；
- 预计远端对象数、下载字节、标准化行数、磁盘占用和峰值内存区间；
- 数据质量风险、Point-in-time 限制和需要用户确认的假设；
- 每一步所属授权类别。

计划和检查必须不联网、不写数据，也不把 `latest` 当作正式版本。

### DE03 — 幂等的一键准备编排

在显式授权后，用一个 application service 串联现有的 archive/REST、Raw、normalize、
quality、Catalog、resample、Universe 和 Snapshot 能力。它必须：

- 已存在且 hash 一致的对象直接复用；
- 中断后可从已验证步骤继续，不从头重复下载；
- 冲突、上游修订或质量失败时停止并给出稳定原因码；
- 保存 job manifest、计划 hash、日志、步骤状态和最终 Snapshot 身份；
- 长任务启动后允许 Agent 交还控制，之后按用户要求查询状态。

记录式 job 的公共状态机应与 `AI_AGENT_READINESS.md` 的 AG05 共用，不能各建一套。

### DE04 — 可解释的数据质量与修订

把现有质量报告汇总成用户可以直接判断的数据就绪结果：

- 核心区间、预热区间和执行尾部分开报告覆盖率；
- 区分缺文件、缺 bar、未完成 bar、异常 OHLC、无效价格和合约状态缺失；
- 显示受影响的日期、symbol、研究用途和可行处理，而不只给总计数；
- 上游归档内容发生变化时生成旧/新 checksum 与受影响 Snapshot 清单；
- 不自动修复价格，不允许用删除坏行掩盖质量失败。

### DE05 — 派生数据血缘与最小复用

记录以下最小依赖图：

```text
Raw object
  -> normalized partition
  -> derived interval / Point-in-time Universe
  -> optional Factor or Analysis Snapshot
  -> DatasetSnapshot / research artifact / formal run
```

上游变化时能够回答哪些派生对象可能失效。第一阶段不建设通用 Feature Store，只复用已有
AnalysisSnapshot、SignalSnapshot 和因子身份；只有实际重复计算证据证明值得物化时，才增加
新的缓存层。

### DE06 — 有界资源与个人维护成本

- 所有下载、标准化和派生都按时间、symbol 或文件分区有界执行；
- 提供磁盘预算、临时空间、行数和 RSS 硬门；
- 支持 dry-run、resume、verify 和保守清理；
- 不要求常驻数据库服务、消息队列、对象存储或集群；
- 不引入 Spark、Kafka、Airflow 等仅为架构外观服务的依赖；
- 本地目录损坏或迁移到另一台机器时，可以用 manifest 重建 Catalog 和覆盖索引。

### DE07 — 稳定的消费者边界

Quick Research、Fast Matrix 和 Event 只消费已验证 Snapshot 或其有身份的派生对象。数据
就绪证明至少包含：

- 精确 Snapshot 和成员数据集版本；
- 核心、预热和尾部覆盖；
- 数据质量状态及所有保留警告；
- Point-in-time 合约信息的已知限制；
- 预计扫描规模和适用引擎；
- 与 ResearchIntent/配置的绑定 hash。

三层引擎不得各自实现下载、缺口填补或不同的数据可见性解释。

## 5. 下一阶段：DE-v1 本地数据准备闭环

实现状态：D0–D3 完成；D4 待另行授权的真实公开数据操作证据。

下一阶段只解决“普通用户能安全准备一次研究所需数据”，不扩充新市场或高频数据类型。

### D0 — 冻结合同与验收样本

- 定义 `ResearchDataRequirement`、`DataPlan`、`DataReadiness` 和记录式 job schema；
- 固定核心区间、历史预热、标签尾部和执行尾部的计算规则；
- 用完全离线的小型 fixture 覆盖已有数据、部分缺失、质量失败、上游冲突和未决语义；
- 先写 acceptance 计划，不改现有 DatasetSnapshot 和 run identity。

完成标志：同一需求与同一 Catalog 状态产生字节稳定的计划，且计划无副作用。

### D1 — `data plan` 与 `data inspect`

- 从研究配置或显式 requirement 生成覆盖计划；
- 展示可复用 Snapshot、精确缺口、预计资源和授权动作；
- 提供机器可读 JSON 与面向用户的中英文摘要；
- 明确拒绝无法从本地事实确定的假设。

完成标志：用户无需手工计算 warmup/future tail，也能在下载前知道将发生什么。

### D2 — `data prepare` 记录式编排

- 复用现有数据源、标准化、质量门和 Catalog，不复制底层实现；
- 将每一步写入可恢复 job；
- 重复运行保持幂等，成功后只发布一个精确 Snapshot；
- 网络和写入必须在计划之后按授权开始；长任务启动后不要求 Agent 持续轮询。

完成标志：从空的任务目录到可用 Snapshot 有一条可恢复命令；失败不会留下可被研究误用的
半发布版本。

### D3 — 就绪报告与派生血缘

- 汇总质量、覆盖、版本、限制、资源和派生关系；
- 让三个研究层在启动前校验同一份 `DataReadiness`；
- 记录上游变化可能影响的 Snapshot、AnalysisSnapshot、研究 run 和正式 run，但绝不自动
  删除或改写旧产物。

完成标志：用户和 Agent 都能回答“这份数据为什么可用、缺什么、哪些结果依赖它”。

### D4 — 真实数据验收

- 先用少量高流动性合约和短区间完成真实 Archive 验收；
- 再用已有全市场分钟数据验证只读规划、幂等复用、恢复和资源预算；
- 只有用户另行授权时才下载数据或运行研究；
- 不把一年全市场构建设为下一阶段完成的默认门槛。

完成标志：离线 fixture 全部通过，并有一次受控真实数据准备记录；测试、下载和运行证据分别
注明，不能互相替代。

## 6. 明确不进入下一阶段

- 秒级 Futures K 线、`trades`/`aggTrades` 接入和微观结构执行；
- 多交易所、股票、期权或链上数据；
- 常驻定时同步、在线数据 API、Web 管理台和告警服务；
- 企业级调度、集群计算、数据权限和多租户配额；
- 自动修复市场价格或用前值伪造成交；
- 全量通用 Feature Store；
- 让 Agent 在没有计划和授权时自动下载数据。

这些方向只有出现明确研究需求和容量证据后才能建立独立计划。

## 7. 与其他开发主线的关系

- 开源趋势/动量因子的下一轮 Quick Research 可以使用现有数据能力，不以 DE-v1 为前置。
- DE01–DE03 为 Agent 的 AG03、AG05、AG07、AG08 提供共同数据边界，应共享合同与 job，
  避免重复建设。
- Fast Matrix 的候选维度优化消费同一 Snapshot，但不应进入数据准备编排。
- 研究期/验证期/留出期划分属于研究治理；数据子系统负责执行边界和防止标签跨界，不替用户
  做因子晋级决定。
- 开源发布工程只打包代码和小型 fixture；真实市场数据、Catalog 和 Snapshot 继续保持
  untracked。

## 8. 开发启动条件

开始 D0 前仍需当前任务明确授权。实现阶段必须从同步、干净的 `main` 创建功能分支，并将
合同、聚焦测试、acceptance 文档、用户文档和维护状态一起更新。本文本身不授权测试、联网、
下载、数据构建、研究运行或正式回测。
