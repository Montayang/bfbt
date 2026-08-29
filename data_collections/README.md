# 市场数据集合

本目录保存可复用的市场数据采集、标准化和 DatasetSnapshot 准备说明，不保存大体积
行情文件。实体数据仍位于被 Git 忽略的 `data/backtest/datasets/`。

## 组织原则

- `data_collections/<collection_id>/` 按交易所、市场、合约类型和基础粒度命名，不按策略命名。
- `strategies/<strategy_id>/` 只描述策略逻辑及其正式运行，不拥有行情数据。
- DatasetSnapshot 是一次运行实际引用的不可变数据边界；同一 snapshot 可以供多个策略使用。
- 时间覆盖不足、基础粒度不同或数据语义不同才需要扩充/新建集合，不因策略不同而重复下载。
- 历史 dataset ID 与成功 run 保持不可变；目录整理不得伪造或重写既有审计身份。

当前集合：

- [`binance_usdm_perpetual_1m`](binance_usdm_perpetual_1m/README.md)：Binance USD-M、
  USDT 永续合约、1 分钟 trade bars、资金费率与合约元数据。
