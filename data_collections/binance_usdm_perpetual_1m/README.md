# Binance USD-M 永续合约 1 分钟数据集合

- `collection_id`：`binance_usdm_perpetual_1m`
- 市场：Binance USD-M、USDT 保证金、`PERPETUAL`
- 基础粒度：1 分钟 trade bars
- 附属数据：资金费率与 `exchangeInfo` 合约元数据
- 本机数据：`data/backtest/datasets/binance_usdm_perpetual_1m/`
- 本机 catalog：`data/backtest/catalogs/binance_usdm_perpetual_1m.duckdb`

该集合当前覆盖 2026-05、2026-06 与 2026-07 的 Raw/Normalized 数据。五月用于因子研究，
六月和七月同时支持正式回测；当前保存三份 DatasetSnapshot：

- `dataset-snapshot-2026-05-research.json`：市场维度 dataset ID
  `binance-usdm-perpetual-1m-2026-05-research`
- `dataset-snapshot-2026-06.json`：历史 dataset ID
  `binance-usdm-full-market-rank-descent-2026-06`
- `dataset-snapshot-2026-07.json`：历史 dataset ID
  `binance-usdm-full-market-rank-descent-2026-07`

上述 dataset ID 因审计兼容保留旧名称，但数据本身不属于该策略。后续策略只要所需市场、
字段、1 分钟粒度和区间被某份快照覆盖，就直接复用，不需要重新下载。新发布的 snapshot
应使用市场维度 ID，例如 `binance-usdm-perpetual-1m-<period>`。

`download_2026_05_archives.sh` 与 `prepare_2026_05_research_dataset.py` 是五月研究数据的可复现
入口；下载使用 Binance 公开归档，包含 4 月 30 日预热日。`download_2026_06_archives.sh` 与
`prepare_2026_06_dataset.py` 是 A24 首次建库的历史、可复现入口；后者同时生成了当时首个
策略的固定配置，因此不是所有新策略的配置模板。
