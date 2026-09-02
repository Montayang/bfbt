# Security policy

[简体中文](SECURITY.zh-CN.md)

BFBT is an independent open-source research project. It is not affiliated with, endorsed by,
sponsored by, or financially connected to Binance.

## Supported scope

The latest version on `main` is the currently supported security-fix target. BFBT remains in `0.x`
development and does not yet promise long-term support for earlier versions.

## Reporting a vulnerability

Do not disclose credential exposure, path traversal, arbitrary code execution, supply-chain, or
data-integrity vulnerabilities in a public issue. Report them privately through the maintainer
email published in `pyproject.toml`, including the affected version, minimal reproduction,
potential impact, and suggested mitigation. Do not publish exploit code before acknowledgement.

## Security boundary

- BFBT is an offline research and backtesting system; it must not contain exchange account clients,
  API keys, or order-entry paths.
- It must not read `.env`, account balances, or private order streams.
- Network behavior is limited to explicitly requested public market-data retrieval; formal runs bind
  immutable local data versions.
- Reports and Showcase pages read only verified artifacts and reject path traversal or hash mismatch.
- Agent-generated code and research execution are separate authorization actions; arbitrary
  generated-code execution is not supported.

Treat any behavior that crosses these boundaries as a security vulnerability, not a normal feature
request.
