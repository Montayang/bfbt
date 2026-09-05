# 开源趋势与动量因子候选库

## 1. 文档状态

- 候选批次：`oss-trend-momentum-candidates-v0`
- 整理日期：2026-09-05
- 状态：14 个 Quick Research 因子均已实现并通过聚焦公式/边界测试；尚未运行研究
- 目标：为下一轮 `Quick Research -> Fast Matrix -> Event` 全链路研究建立一组来源可审计、
  适合 Binance 永续合约截面研究的趋势与动量候选。

本文是研究准备，不是收益结论。公式已经取得稳定的 `factor_id:v1` 并完成聚焦测试；后续
仍须由独立的 study 冻结数据、K 线、预测期、方向和筛选规则。本文中的
“优先级”只表示研究信息增量和实现价值，不表示预期盈利能力。

## 2. 来源审阅与许可证边界

### 2.1 纳入的来源

| 来源 | 固定版本 | 许可证 | 本文用途 |
|---|---|---|---|
| [Microsoft Qlib](https://github.com/microsoft/qlib/tree/79633dd9506ea689e5400dea0197717b5b3d74b7) | `79633dd` | [MIT](https://github.com/microsoft/qlib/blob/79633dd9506ea689e5400dea0197717b5b3d74b7/LICENSE) | Alpha158 的滚动趋势、区间位置、方向持续率与价量确认公式 |
| [`bukosabino/ta`](https://github.com/bukosabino/ta/tree/a890410710a6e483c9ba08da7f3dd5089e4b9dff) | `a890410` | [MIT](https://github.com/bukosabino/ta/blob/a890410710a6e483c9ba08da7f3dd5089e4b9dff/LICENSE) | TSI、TRIX、KST、KAMA、Vortex 与 VPT 的参考实现 |

只借鉴公开公式和行为定义；BFBT 不新增这些项目为运行依赖。未来实现应使用 BFBT 的
Polars、时点数据、warmup 和 chunk 状态合同，并在项目许可证要求下保留适当来源说明。

### 2.2 已审阅但不纳入的 QuantZone

[Paradex QuantZone](https://github.com/tradeparadex/QuantZone/tree/4525d85460959a18a7f4e60d1065c53cd9dd46d3)
是面向 Paradex 永续合约、参考外部交易所价格的做市策略，而不是截面因子库。其公开说明
涉及双边报价、账户仓位、订单刷新和认证。它不提供本轮需要的独立趋势/动量因子目录，且
真实交易客户端不属于 BFBT 的安全研究边界，因此本轮不读取或迁移其交易代码。

### 2.3 原式与 BFBT 适配式

来源公式和 BFBT 适配公式必须保持不同身份：

- `source-exact`：在 BFBT 数据字段和数值边界允许时复现来源公式；
- `bfbt-adaptation`：为截面可比性、有限窗口或方向解释而改写，不能沿用来源的原始因子名；
- 方向翻转不制造新公式身份，由 Quick Research 的冻结方向字段表达；
- 来源库的默认填充值不照搬。warmup 不足、零分母、缺 K 线或非有限值一律输出无效值，
  不能用 `0/1/均值` 静默填充。

## 3. 记号与统一数据口径

在合约 `s`、已闭合 K 线 `t` 上：

- `O_t, H_t, L_t, C_t`：开、高、低、收；
- `V_t`：默认使用 `quote_volume`。涉及成交量的公式另保留 `base volume` 对照参数，不能
  把不同币种的 base volume 直接当作可比较的横截面尺度；
- `R_t = C_t / C_{t-1} - 1`，`Delta C_t = C_t - C_{t-1}`；
- `SMA_d(X)_t`、`EMA_d(X)_t`、`SUM_d(X)_t` 分别是包含 `t` 的尾部 `d` 根滚动统计；
- `Slope_d(C)_t` 与 `R2_d(C)_t` 是用 `x=0,...,d-1` 对窗口价格做含截距 OLS 的斜率和
  决定系数；
- `eps` 只用于防止浮点零除。真实分母为零时因子仍应标记无效，不用 `eps` 人造排序；
- 所有因子只读取 `t` 及以前的数据，在 `t` 收盘后形成信号，未来收益与成交继续遵守
  BFBT 的 next-bar 合同。

实现前必须明确 Qlib `IdxMax/IdxMin` 的索引方向、重复极值取第一个还是最后一个，并用固定
样例对照来源；这是 `IMXD` 进入 `verified` 的前置条件。

## 4. 候选总表

| Proposed ID | 来源身份 | 类型 | 优先级 | 数据 | 实现级别 | 与现有 12 因子关系 |
|---|---|---|---:|---|---:|---|
| `oss_qlib_beta` | Qlib `BETA` | 线性趋势速度 | A | C | R1 | 新增趋势几何信息 |
| `oss_qlib_signed_rsqr` | BFBT 组合适配 | 有方向趋势质量 | A | C | R1 | 新增趋势质量维度 |
| `oss_qlib_rsv` | Qlib `RSV` | 通道位置 | A | HLC | R1 | 与均线偏离不同 |
| `oss_qlib_imxd` | Qlib `IMXD` | 极值时序 | A | HL | R1* | 接近 Aroon；需冻结并列语义 |
| `oss_qlib_cntd` | Qlib `CNTD` | 方向持续率 | A | C | R1 | Alpha53 的对称版本 |
| `oss_ta_trix` | `ta` TRIX | 平滑趋势速度 | A | C | R2 | 比 Alpha89 多一层平滑 |
| `oss_ta_tsi` | `ta` TSI | 平滑有符号动量 | A | C | R2 | 与 Alpha112 平滑结构不同 |
| `oss_ta_kst` | `ta` KST | 多周期动量 | A | C | R1 | 新增多尺度组合 |
| `oss_ta_kama_distance` | BFBT KAMA 适配 | 自适应趋势偏离 | A | C | R2 | 新增噪声自适应状态 |
| `oss_ta_vortex_diff` | `ta` Vortex diff | 高低价方向运动 | A | HLC | R1 | 新增 true-range 归一化方向 |
| `oss_ta_vpt_roll` | BFBT VPT 适配 | 量价确认 | A | CV | R1 | Alpha40 之外的量价联合形式 |
| `oss_qlib_roc_mom` | BFBT Qlib ROC 适配 | 纯价格动量基准 | B | C | R0 | 与 Alpha18/20/88 高度重复 |
| `oss_qlib_sumd` | Qlib `SUMD` | 上下行幅度强度 | B | C | R1 | 与 Alpha112 高度重复 |
| `oss_qlib_cord` | Qlib `CORD` | 价量变化相关 | B | CV | R1 | 非独立方向信号，适合诊断/交互 |

`R0/R1/R2` 延续 GTJA191 候选文档的含义；`R1*` 表示普通滚动算子足够，但必须先完成行为
校准。A 组强调相对现有因子的新增信息，B 组主要作为实现校验、重复性基准或后续交互项。

## 5. Qlib Alpha158 候选公式

来源定位：
[Qlib `Alpha158DL.get_feature_config`](https://github.com/microsoft/qlib/blob/79633dd9506ea689e5400dea0197717b5b3d74b7/qlib/contrib/data/loader.py#L89)。
Qlib 默认窗口为 `5/10/20/30/60`，但本文不把股票日频窗口直接冻结成加密货币 K 线窗口。

### 5.1 `oss_qlib_beta` — 归一化线性趋势速度

来源原式：

```text
BETA_d(t) = Slope_d(C)_t / C_t
```

高值表示窗口内单位时间上涨速度相对当前价格更高。本批固定 `d=20`，与 Qlib 默认窗口组
的中间尺度一致；价格为正时按 `source-exact` 实现。

### 5.2 `oss_qlib_signed_rsqr` — 有方向的趋势质量

Qlib 分别定义：

```text
BETA_d(t) = Slope_d(C)_t / C_t
RSQR_d(t) = R2_d(C)_t
```

BFBT 适配式：

```text
SIGNED_RSQR_d(t) = sign(BETA_d(t)) * RSQR_d(t)
```

`RSQR` 单独只能衡量线性程度，无法分辨上涨或下跌；组合后才是可直接参与截面方向排序的
候选。它是 `bfbt-adaptation`，不是 Qlib 原生 Alpha158 字段。

### 5.3 `oss_qlib_rsv` — 近期通道位置

```text
RSV_d(t) = (C_t - min_d(L)_t) / (max_d(H)_t - min_d(L)_t)
```

取值通常位于 `[0, 1]`，高值表示更接近近期高位。若窗口振幅为零则无效，不填为中性值。

### 5.4 `oss_qlib_imxd` — 高低极值的时间次序

来源原式：

```text
IMXD_d(t) = (IdxMax_d(H)_t - IdxMin_d(L)_t) / d
```

Qlib 源码注释将大值解释为更强的向下动量。BFBT 保留原始数值，不在公式层擅自取负；
Quick Research 同时评估冻结的 `+1/-1` 方向。与 Aroon Oscillator 信息高度接近，因此二者
只保留一个，避免伪造因子数量。

### 5.5 `oss_qlib_cntd` — 上涨/下跌 K 线占比差

```text
CNTD_d(t) = mean_d(1[C_i > C_{i-1}]) - mean_d(1[C_i < C_{i-1}])
```

平收对两项均贡献零。它只衡量方向是否持续，不衡量单根涨跌幅，取值在 `[-1, 1]`。

### 5.6 `oss_qlib_sumd` — 上下行幅度净强度

令 `U_i=max(Delta C_i,0)`、`D_i=max(-Delta C_i,0)`：

```text
SUMD_d(t) = (SUM_d(U)_t - SUM_d(D)_t) / (SUM_d(|Delta C|)_t)
```

这是 Qlib 对称强度公式，与现有 `gtja_alpha112` 高度重复，只作为来源交叉校验和相关性对照，
不应被包装成新的量化故事。

### 5.7 `oss_qlib_roc_mom` — 纯动量基准

Qlib 原式实际为：

```text
ROC_SOURCE_d(t) = C_{t-d} / C_t
```

其高值方向与常见正动量相反。为避免名称误导，BFBT 候选使用单独身份：

```text
ROC_MOM_d(t) = C_t / C_{t-d} - 1
```

它与已验证的 GTJA Alpha18/20/88 仅是窗口或单调变换差异，定位是 R0 基准，不作为新增
独立证据。

### 5.8 `oss_qlib_cord` — 价量变化相关

Qlib 原式：

```text
CORD_d(t) = Corr_d(C_t / C_{t-1}, log(V_t / V_{t-1} + 1))
```

它描述价格相对变化与成交量相对变化是否同向，不天然提供上涨/下跌方向。第一轮若纳入，
应把它标为诊断/交互候选，不能仅因高相关就解释成趋势跟随收益信号。

## 6. `ta` 候选公式

### 6.1 `oss_ta_trix` — 三重 EMA 的变化率

来源定位：[`TRIXIndicator`](https://github.com/bukosabino/ta/blob/a890410710a6e483c9ba08da7f3dd5089e4b9dff/ta/trend.py#L232)。

```text
E1_t = EMA_d(C)_t
E2_t = EMA_d(E1)_t
E3_t = EMA_d(E2)_t
TRIX_d(t) = 100 * (E3_t / E3_{t-1} - 1)
```

三层递归 EMA 必须跨 chunk 保存状态；初始化和 warmup 必须显式，不能采用来源库为了绘图
设置的均值填充值。

### 6.2 `oss_ta_tsi` — True Strength Index

来源定位：[`TSIIndicator`](https://github.com/bukosabino/ta/blob/a890410710a6e483c9ba08da7f3dd5089e4b9dff/ta/momentum.py#L58)。

```text
M_t = Delta C_t
NUM_t = EMA_fast(EMA_slow(M))_t
DEN_t = EMA_fast(EMA_slow(|M|))_t
TSI(t) = 100 * NUM_t / DEN_t
```

来源默认 `slow=25, fast=13`。分母为零时无效。它同时平滑方向和绝对运动，适合与不平滑的
Alpha112 比较。

### 6.3 `oss_ta_kst` — 多周期加权动量

来源定位：[`KSTIndicator`](https://github.com/bukosabino/ta/blob/a890410710a6e483c9ba08da7f3dd5089e4b9dff/ta/trend.py#L407)。

令 `ROC_r(t)=C_t/C_{t-r}-1`，则：

```text
KST(t) = 100 * (
    SMA_n1(ROC_r1)_t
  + 2 * SMA_n2(ROC_r2)_t
  + 3 * SMA_n3(ROC_r3)_t
  + 4 * SMA_n4(ROC_r4)_t
)
```

来源默认 `r=(10,15,20,30)`、`n=(10,10,10,15)`。它把多个趋势尺度压成一个因子；参数多，
第一轮只允许一组基准参数，不能大网格搜索后只展示赢家。

### 6.4 `oss_ta_kama_distance` — KAMA 自适应偏离

来源定位：[`KAMAIndicator`](https://github.com/bukosabino/ta/blob/a890410710a6e483c9ba08da7f3dd5089e4b9dff/ta/momentum.py#L262)。

```text
ER_t = |C_t - C_{t-d}| / SUM_d(|Delta C|)_t
FAST = 2 / (p_fast + 1)
SLOW = 2 / (p_slow + 1)
SC_t = (ER_t * (FAST - SLOW) + SLOW)^2
KAMA_t = KAMA_{t-1} + SC_t * (C_t - KAMA_{t-1})
KAMA_DISTANCE_t = C_t / KAMA_t - 1
```

来源只输出价格尺度的 `KAMA`；最后一行是 BFBT 为截面可比性增加的适配公式，身份必须是
`oss_ta_kama_distance`。来源默认 `d=10, p_fast=2, p_slow=30`。递归状态需要 checkpoint/
resume 等价测试。BFBT 的因果冷启动以 `KAMA_{d-1}=C_{d-1}` 为种子，取得第 `d` 个完整
价格变化后才产生首个值；不复用来源实现中 `numpy.roll` 在序列开头形成的环绕值。

当前实现面向 Quick Research 的连续历史批计算。若未来绕过 Fast Matrix schedule、直接在
Event 分块路径中递归计算 KAMA，必须先增加 carried-state checkpoint 和连续/恢复等价验收；
本次公式测试不能替代该正式执行合同。

### 6.5 `oss_ta_vortex_diff` — Vortex 方向差

来源定位：[`VortexIndicator`](https://github.com/bukosabino/ta/blob/a890410710a6e483c9ba08da7f3dd5089e4b9dff/ta/trend.py#L803)。

```text
TR_t  = max(H_t-L_t, |H_t-C_{t-1}|, |L_t-C_{t-1}|)
VM+_t = |H_t-L_{t-1}|
VM-_t = |L_t-H_{t-1}|
VI+_d(t) = SUM_d(VM+)_t / SUM_d(TR)_t
VI-_d(t) = SUM_d(VM-)_t / SUM_d(TR)_t
VORTEX_DIFF_d(t) = VI+_d(t) - VI-_d(t)
```

高值表示正向方向运动占优。来源默认 `d=14`；true range 总和为零时无效。

### 6.6 `oss_ta_vpt_roll` — 有限窗口量价趋势

来源 [`VolumePriceTrendIndicator`](https://github.com/bukosabino/ta/blob/a890410710a6e483c9ba08da7f3dd5089e4b9dff/ta/volume.py#L238)
定义累计序列：

```text
VPT_t = cumulative_sum(V_t * R_t)
```

累计值依赖数据起点，且其绝对尺度不适合跨合约排序。BFBT 候选使用有限窗口适配：

```text
VPT_ROLL_d(t) = SUM_d(V_i * R_i)_t / SUM_d(V_i)_t
```

它可解释为窗口内成交额加权收益。这里 `V` 默认使用 `quote_volume`；base volume 只作为对照。
该式是受 VPT 启发的 `bfbt-adaptation`，不是来源库的原始 VPT。

## 7. 明确排除的高重复项

| 候选 | 不进入本批的原因 |
|---|---|
| `ta` ROC | 与 `oss_qlib_roc_mom` 及 GTJA Alpha18/20/88 重复 |
| `ta` PPO / MACD | 与已实现 GTJA Alpha89 的经济结构高度重复 |
| `ta` RSI / StochRSI | 与 GTJA Alpha112、Qlib SUMD 及随机区间类高度重复 |
| `ta` Aroon | 与 Qlib IMXD 都由高低极值出现位置构成，本批只保留 IMXD |
| `ta` SMA/EMA/WMA | 移动均线本身有价格尺度；已有 GTJA Alpha31/66/71 覆盖均线偏离 |
| Parabolic SAR | 路径状态、反转规则和参数更多，更适合进入 Event 策略组件而非首轮截面因子 |
| ADX 单值 | 衡量趋势强度但无方向；若未来研究，应与方向项组合并使用新适配身份 |

## 8. 实现结果与研究顺序

### Phase A：低歧义、无递归

已实现并完成聚焦公式/边界校验：

```text
oss_qlib_beta
oss_qlib_signed_rsqr
oss_qlib_rsv
oss_qlib_cntd
oss_ta_kst
oss_ta_vortex_diff
oss_ta_vpt_roll
```

同时实现 `oss_qlib_roc_mom` 作为已知重复基准，用来检查新 study 与旧 GTJA 研究的方向、
标签和排序口径，而不是制造一条“新发现”。

### Phase B：语义或递归校准

以下语义或递归项也已实现并完成聚焦校验：

```text
oss_qlib_imxd
oss_ta_trix
oss_ta_tsi
oss_ta_kama_distance
```

`IMXD` 固定为 Qlib 的 1-based、窗口内最早并列极值语义；递归项遇到断档会重置，且不采用
来源库面向绘图的静默填充值。`oss_qlib_sumd` 和 `oss_qlib_cord` 也已实现，但仍只作为
相关性/交互诊断项，不应优先占用正式候选名额。

当前实现默认参数如下，全部按各自源 K 线的根数解释：

| 因子组 | 默认参数 |
|---|---|
| Qlib `BETA/SIGNED_RSQR/RSV/IMXD/CNTD/ROC_MOM/SUMD/CORD` | `window_bars=20` |
| TRIX | `window_bars=15` |
| TSI | `slow_span=25, fast_span=13` |
| KST | `roc_bars=[10,15,20,30]`, `smooth_bars=[10,10,10,15]` |
| KAMA distance | `efficiency_bars=10, fast_span=2, slow_span=30` |
| Vortex diff | `window_bars=14` |
| rolling VPT | `window_bars=20`, `volume_field=quote_volume` |

## 9. Quick Research 前必须冻结的事项

1. **K 线与自然时间**：沿用上一轮 `1m/5m/15m` 源 K 线；公式参数按各源 K 线根数解释，
   不换算成等自然时间。
2. **预测窗口**：沿用上一轮源 K 线之后 `1/5/20` 根；信号收盘后以下一根开盘进入标签。
3. **候选参数预算**：第一轮只用第 8 节默认参数，不做参数网格；KST/KAMA 尤其禁止事后
   扩网格。
4. **统一市场样本**：使用同一 point-in-time universe、合约生命周期和缺口规则。
5. **分段边界**：开发/holdout 日期与重叠标签截断在新 study 中显式冻结。
6. **方向规则**：原值和方向字段分离；开发集选方向，holdout 不重新选方向。
7. **相关性去重**：至少输出候选间及相对现有 12 因子的 Spearman 相关矩阵；同簇不得全部
   晋级来夸大因子数量。
8. **换手与成本前置**：Quick Research 报告除 Rank IC、分位差和覆盖率外，继续记录 Rank
   换手；进入 Fast Matrix 前先估计调仓和成本拖累。
9. **失败关闭**：warmup、零分母、非有限值、窗口缺口和递归恢复不一致必须显式失败。

## 10. 下一步边界

本文完成后，下一步仍不是直接批量回测，而是：

1. 冻结新 study 的开发/holdout 日期、数据版本与研究矩阵规模；
2. 获得当次明确授权后再运行 Quick Research；
3. Quick Research 结果只产生研究证据，不能直接描述为可交易策略；
4. 只有用户从 Fast Matrix 结果中选定需要直接进入 Event 的递归因子时，才为该因子增加
   Event carried-state 合同，避免为未晋级因子制造不必要的正式执行复杂度。
