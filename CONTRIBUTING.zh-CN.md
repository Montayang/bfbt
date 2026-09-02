# 为 BFBT 贡献代码

[English](CONTRIBUTING.md)

感谢参与。当前项目优先保证因果时序、经济语义、有限内存和不可变审计，而不是扩大命令或
因子数量。

## 开发流程

1. 从同步且已验证的 `main` 创建短期功能分支。
2. 在 issue 或变更说明中冻结行为、数据/配置身份和兼容边界。
3. 通用能力进入 `src/bfbt/`；真实策略规格与 run 映射进入 `strategies/`。
4. 同时更新聚焦测试、验收文档和维护状态。
5. 运行相关测试，再运行完整离线 suite：

```bash
python -m pip install -e ".[test]"
python -B -m pytest -q
```

6. 提交前运行 `git diff --check`，确认没有数据、凭据、绝对本机路径或生成 run 被跟踪。

## 不可破坏的合同

- 保持 `Quick Research -> Fast Matrix -> Event 引擎` 的职责分离。
- 保持时点化数据、下一根 K 线成交、显式成本和 UTC `[start, end)` 语义。
- Event 引擎的正式全市场运行必须分块、有限内存、可 checkpoint/恢复，并与连续执行经济等价。
- 不覆盖成功或失败的终态 run；修订必须获得新身份。
- 报告压缩曲线时仍须保留每笔成交、持仓变化和风险事件。
- 不加入账户 Client、API 凭据、下单入口或依赖 `.env` 的行为。

## 数据与联网测试

`data/backtest/`、数据集、catalog、checkpoint、workspace、run 和派生报告均不提交 Git。
默认测试必须离线且使用小型确定性 fixture。需要下载公开市场数据或执行长回测的验收，应在
变更说明中单独列出，并保存精确数据版本和产物身份。

## 新因子

新因子必须声明依赖列、窗口/warmup、可用时点、缺口政策、有限值政策和版本；必须有无前视、
跨 chunk 及边界 fixture。不要把任意 `eval`/`exec` 或 Agent 生成代码作为公共因子接口。
