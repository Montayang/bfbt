# BFBT Showcase

这套展示面向 8–12 分钟的本地演示。它只读取并验证既有不可变回测产物，生成派生静态页面；
不会下载数据、运行回测、访问交易账户或发送订单。

## 演示前准备

在仓库根目录运行：

```bash
.venv/bin/bfbt showcase prepare \
  --spec showcase/r5_t4_h2_rolling_202605_202607.json
```

命令会先执行只读 readiness 检查，逐文件验证三个 run，然后生成：

```text
data/backtest/showcases/r5-t4-h2-rolling-202605-202607-r01/index.html
```

`index.html` 是英文兼容入口；同目录还会生成显式英文 `index.en.html` 和独立简体中文
`index.zh-CN.html`。三者共享同一份语言中立 `evidence.json`。

演示现场可以直接用浏览器打开该文件。页面不引用 CDN、远程字体或外部脚本，因此断网可用。
如果只想检查而不写派生页面：

```bash
.venv/bin/bfbt doctor \
  --spec showcase/r5_t4_h2_rolling_202605_202607.json

.venv/bin/bfbt showcase inspect \
  --spec showcase/r5_t4_h2_rolling_202605_202607.json
```

## 建议讲解顺序

1. 用一句自然语言提出“两小时采样、三个月、滚仓保证金轨迹”的研究请求。
2. 展示 ResearchIntent 和冻结单，强调 Agent 只翻译意图，经济计算仍由确定性引擎完成。
3. 说明 Quick Research、Fast Matrix、Event/V2 的职责；本策略因为路径依赖直接使用 Event。
4. 同屏比较三个月收益、回撤、成本和换手，明确七月为亏损，避免收益挑选偏差。
5. 展示每次开仓保证金轨迹，再进入六月深度报告，点击一笔成交查看相邻持仓和风险事件。
6. 打开配置、指标和 manifest，说明数据版本、配置哈希、源码与每个文件都可验证。
7. 指出黄色 provenance 徽标：当前 `r01` 记录为 `git_dirty=true`，没有被页面隐藏。
8. 结束时重申：这是离线历史研究系统，不连接账户，结果不构成投资建议。

## 无网络排练清单

- `bfbt doctor` 返回 `ready=true`；warning 可以存在，但必须在讲解中说明。
- 三个 run 均逐文件验证成功，展示页的 `verified_runs=3`。
- 页面中不存在绝对本机路径，深度报告与五类证据链接均能打开。
- 桌面宽屏和窄窗口均可阅读；Tab 键能访问导航、详情和证据链接。
- 不启动正式回测，不运行下载命令，不依赖本地静态服务器。
- 准备一张入口页和一张审计页截图作为浏览器故障时的备用材料。

## 已知限定

当前本机只有 H2 `r01` 三份产物，它们的环境均记录 `git_dirty=true`。内部预览允许保留醒目
限定；对外录屏或正式展示更适合在展示代码提交后，另行授权生成干净的 `r02` 证据。旧 run
保持不可变，不得覆盖或修改。
