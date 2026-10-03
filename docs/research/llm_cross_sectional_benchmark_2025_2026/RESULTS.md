# LLM cross-sectional benchmark — formal results

These are the immutable formal Event results for benchmark `llm-cs-202509-202608-v1`. They compare
four one-shot model submissions; they are not evidence of live profitability or a recommendation
to trade any strategy.

## Frozen identity

- Evaluation interval: `[2025-09-01T00:00:00Z, 2026-09-01T00:00:00Z)`, followed by the frozen
  terminal-liquidation event.
- Initial equity: 100,000 USDT per isolated account.
- Execution friction: 5 bp taker fee plus 2 bp slippage per traded notional; observed funding is
  applied separately.
- BFBT source commit: `2b1ca8f6826866fcf70e1cc39fefae4c8a668c7f`.
- DatasetSnapshot SHA-256:
  `a49846e623dee5ef16cebe28a48a12e384c07582237a782eeef311208ab75e96`.
- All four runs contain 525,601 minute observations and bind the original-response and
  implementation-audit hashes in their manifests.

The first execution attempt on source `809c2dd` failed before publication when a sparse audit field
was inferred from leading nulls. Its evidence remains local and separate. The corrected clean-source
attempt passed the exact regression and complete 393-test offline suite before producing the runs
below.

## Leaderboard

The frozen ranking rule places positive-total-return strategies above non-positive strategies, then
uses after-cost Sharpe, Calmar, lower drawdown and lower turnover. Under that rule:

| Rank | Submission | Strategy | Total return | Sharpe | Max drawdown | Turnover | Trades | Immutable run ID |
|---:|---|---|---:|---:|---:|---:|---:|---|
| 1 | ChatGPT 6 Astra Ultra | Buffered Relative Momentum | +42.32% | 1.04 | -24.67% | 196.14× | 24,058 | `evt-a01175782a0fa6997144d0da` |
| 2 | Grok 4.7 XHigh | Residual Momentum-Carry Buffer | -3.91% | 0.16 | -43.88% | 270.03× | 32,200 | `evt-c7bdf2892da257893f46be6c` |
| 3 | Kimi K3 Max | Tri-Blend Carry-Momentum-Reversal | -47.06% | -1.55 | -53.15% | 535.46× | 53,600 | `evt-498d8f170a3ee9272dbbb66c` |
| 4 | Claude Fable 5.1 Max | Trailing-Funding Carry Reversal | -38.69% | -1.76 | -49.03% | 538.54× | 115,847 | `evt-ed4f9aeb18febee2690b12d4` |

Kimi ranks above Claude because the frozen rule is Sharpe-first within the non-positive group; it
does not imply that Kimi lost less capital.

## Return attribution

The fields below are additive period contributions from the metrics identity. Fee and slippage are
shown as positive drag magnitudes; compounded total return differs from the additive net
contribution.

| Submission | Gross price | Funding | Fee drag | Slippage drag | Net contribution |
|---|---:|---:|---:|---:|---:|
| ChatGPT 6 Astra Ultra | +19.19% | +38.96% | 9.81% | 3.92% | +44.43% |
| Grok 4.7 XHigh | -65.02% | +91.41% | 13.50% | 5.40% | +7.49% |
| Kimi K3 Max | -102.73% | +83.35% | 26.77% | 10.71% | -56.85% |
| Claude Fable 5.1 Max | -107.57% | +99.69% | 26.93% | 10.77% | -45.58% |

Funding was material for every submission. The winning strategy therefore should not be described
as a pure price-momentum result. Turnover and the common 7 bp one-way friction were also first-order
economic features, especially for Kimi and Claude.

## Audit qualifications

- Model and effort labels are owner-supplied experimental metadata, not claims about current model
  availability.
- Each model received one opportunity to answer. There was no follow-up clarification, parameter
  search or result-informed repair.
- Mechanical resolutions of ambiguous or conflicting submission text are retained in each local
  `implementation-audit.json`; the common contract took precedence where explicitly recorded.
- Gross exposure can move beyond the target between repair events. Observed peak gross exposure was
  1.00× for ChatGPT, 1.07× for Grok, 1.15× for Kimi and 1.07× for Claude; these excursions remain in
  the minute ledger and reports.
- Complete historical exchange-status snapshots are unavailable at every timestamp. Eligibility is
  inferred point-in-time from observed bars, listing/history boundaries and trailing liquidity.
- The execution model does not reproduce liquidation tiers, ADL, order-book queueing or tick-level
  fills.
- BFBT is independent and has no affiliation with Binance. Binance USD-M appears only as the factual
  public-data and market compatibility boundary used in this experiment.

## Generated evidence

Formal Parquet artifacts, manifests and bilingual HTML reports remain untracked under
`data/backtest/experiments/llm-cs-202509-202608-v1/`. Git records the protocol, implementation,
acceptance contract and immutable result identities, not the large generated files.
