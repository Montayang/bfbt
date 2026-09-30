# 扩展与兼容政策

[English](extension_policy.md)

BFBT 优先采用显式、可审计的研究合同，而不是隐式运行时插件。本文说明外部用户现在可以
依赖哪些接口，以及哪些扩展必须通过正常源码贡献进入项目。

## 当前支持的扩展路线

1. **安全因子表达式。** `bfbt-factor-expression/v1` 是受支持的零代码公式入口，只允许文档
   列出的因果字段和函数，不允许 import、任意 Python、shell、凭据或网络。
2. **仓库内版本化因子。** Python 因子可以贡献到 `src/bfbt/factors/`，但必须声明身份、输入、
   warmup、可见时间、缺口/有限值政策、来源、fixture 和因果测试；经过审阅和发布后才成为
   受支持能力。
3. **外部编排。** Agent 或本地程序可以调用有文档的 CLI，并交换版本化 JSON/artifact 合同；
   必须保留计划、授权、不可变证据和人工晋级门。

BFBT 当前**不会**通过 Python entry point 自动发现第三方包，不会从插件目录加载代码，也不
承诺任意内部模块 import 的稳定性。即使 monkey-patch 引擎或修改 artifact 文件暂时能运行，
也不属于受支持扩展。

## 兼容面

- 版本化 schema 及其校验器在该 schema 版本内保持稳定；
- 不可变 artifact 按记录的 schema/manifest 合同保持可读；
- 有文档的 CLI 和机器可读字段遵循软件包发布政策；
- 安全表达式在 `bfbt-factor-expression/v1` 内保持兼容；会改变含义的新语法必须使用新版本；
- 报告 HTML 外观和 DOM 是展示细节，不是插件 API；机器可读 artifact 才是集成入口；
- 未明确记录为公共接口的 Python 对象可能在 `0.x` 次版本中变化。

外部包应声明 BFBT 上界，例如 `bfbt>=0.1,<0.2`，测试每个支持的 Python/BFBT 组合，并在
遇到未知 schema 或 capability 版本时失败关闭。兼容性声明必须有确定性 fixture，不能只看
是否 import 成功。

## 安全与经济边界

扩展不得加入交易所凭据、实盘下单 Client、隐式联网、自动 Fast Matrix 晋级、未经审阅的
生成代码、未来数据访问或 artifact 改写。数据获取、研究执行、Event 正式回测、测试和源码
控制继续属于彼此独立的授权类别。

因子必须保持时点可见性、下一根 K 线成交边界、确定性身份、缺失数据行为和显式成本。路径
依赖行为必须进入 Event 引擎，不能在 Fast Matrix 中静默近似。

## 未来插件 API

只有真实外部需求证明源码贡献和安全表达式不足时，才考虑通用插件加载器。届时必须先建立
版本化 entry-point 合同、隔离配置命名空间、能力声明、路径/资源限制、确定性 fixture、兼容
矩阵、弃用政策和安全审查。仅把模块放进 `PYTHONPATH` 永远不构成受支持插件合同。
