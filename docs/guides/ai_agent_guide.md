# Use BFBT through an AI Agent — no programming required

[简体中文](ai_agent_guide.zh-CN.md)

This guide is for a researcher who has access to a server where BFBT and an AI Agent are already
set up, but does not want to write code, edit JSON, or use a terminal. You describe the research in
ordinary language, review the important choices, and authorize each consequential stage. The Agent
handles BFBT's files and commands.

The [beginner tutorial](beginner_tutorial.md) serves a different purpose: it teaches a user or
operator to run BFBT manually from a terminal. You do not need to complete it when an operator has
already prepared the server and Agent integration.

## Before you start

Confirm with the server operator that:

- BFBT is installed and the market-data workspace is configured;
- the Agent can work inside the BFBT repository and run the installed `bfbt` application;
- the Agent cannot access exchange accounts, credentials, private order streams, or live trading;
- the Agent will ask before downloading data, writing research state, or starting a formal run;
- long jobs are recorded so that you can leave and ask for their status later.

BFBT does not contain its own chat model or web chat screen. A normal chat website that cannot
access the server is not enough. You need a server-connected Agent such as a supervised coding or
operations Agent. BFBT remains the deterministic research engine and permission boundary beneath
that Agent.

## 1. Start a research session

Paste this message into a new Agent session:

```text
Use the BFBT installation in this server workspace for my research. Read the repository guidance
and inspect the installed system, available data, and recorded job state before planning anything.

I do not write code. Translate my request into a reviewable BFBT research plan and explain every
economically important choice in plain language. Ask me about unresolved choices instead of
guessing. Do not download data, write research state, start research, or run a formal Event
backtest until I explicitly authorize that exact stage. Never access an exchange account or place
an order.

For long jobs, start the recorded background job, give me its ID, and return control. Do not keep
monitoring it; I will ask for status later.
```

The Agent should first report the current system, data, and job facts. Inspection is not permission
to execute research.

## 2. Describe what you want to learn

You may begin with an incomplete idea. For example:

```text
I want to investigate whether medium-term momentum works across Binance USDT perpetual futures.
I am not sure which lookback window, rebalance frequency, or number of positions is sensible.
Offer two or three reasonable designs, explain their trade-offs, and wait for my choice.
```

If you already know the intended design, include as many of these items as you can:

- the research question or hypothesis;
- market and contract universe;
- start and end dates;
- bar interval;
- factor formula, direction, and lookback;
- long, short, or long–short portfolio;
- ranking and number or percentage of positions;
- decision and rebalance frequency;
- fill timing;
- fees, slippage, funding, and leverage;
- risk exits and end-of-run handling;
- whether you want factor diagnosis, portfolio research, or a formal backtest.

You do not have to understand every item. Say “recommend options and explain them” for anything you
do not know. The Agent must not silently fill an economically material gap.

## 3. Review the freeze sheet

Before any execution, ask the Agent to show one plain-language freeze sheet containing:

- the exact question being tested;
- data period, universe, bar interval, and point-in-time rules;
- when the factor becomes knowable, when the decision is made, and when the simulated fill occurs;
- ranking, portfolio, sizing, leverage, and risk rules;
- fees, slippage, funding, and estimated turnover drag;
- the selected BFBT layer and why it is suitable;
- missing data, warnings, assumptions, and unresolved choices;
- every action that would require your permission.

Ask questions until the sheet matches your intent. Useful questions include:

- “Explain next-bar execution without technical terminology.”
- “What could create look-ahead bias in this design?”
- “How much return would estimated trading costs consume?”
- “Which choices are research assumptions rather than facts?”

BFBT rejects unresolved material ambiguities. If you change the plan, the Agent must produce a new
freeze sheet; an approval for the old plan does not transfer automatically.

## 4. Authorize one stage at a time

Use narrow approvals in ordinary language. For example:

```text
I approve the factor definition, timing, and cost assumptions in the current freeze sheet. You may
prepare the missing public historical data and run Quick Research. This does not authorize Fast
Matrix or a formal Event backtest.
```

Later, after reviewing the result:

```text
I approve Fast Matrix research for the candidates that passed the reviewed Quick Research rule.
Use the same frozen data, timing, and cost assumptions. Do not choose an Event candidate for me.
```

And only after you have selected a candidate:

```text
I select candidate fm-... because .... Record this as my decision. Show me the final Event
configuration and required authorization before starting the formal backtest.
```

Data/network access, data writes, research execution, formal Event execution, tests, and source
control are separate capabilities. “Continue” is not a blanket approval to expand the plan.

## 5. Understand the three stages

The route depends on the question; not every request needs all three layers.

1. **Quick Research** asks whether the factor contains cross-sectional information. It reports
   coverage, IC/Rank IC, quantile returns, and Rank turnover without pretending to simulate a full
   account.
2. **Fast Matrix** asks whether conventional portfolios built from selected signals remain
   interesting after turnover, fees, slippage, funding, and valuation. These are research results,
   not formal strategy confirmation.
3. **Event Engine** is the formal chronological simulation. It is required for exact fills,
   account and margin state, path-dependent exits, event arbitration, and trade-level audit.

The Agent may explain and compare Fast Matrix candidates, but it must not choose one on your behalf.
Your selection and rationale become part of the evidence trail.

## 6. Leave long jobs and return later

For a long authorized operation, the Agent should start a recorded background job and then tell you:

- the job ID;
- the current stage;
- what was authorized;
- where status and logs are recorded;
- what evidence will indicate success.

It should then return control instead of continuously polling. In a later session, say:

```text
Check the status of job .... Do not restart it. If it finished, verify its immutable artifacts
before reporting success; if it failed, explain the recorded cause and the safest next action.
```

Process exit alone is not proof of a valid result. The Agent must verify the recorded evidence and
artifact hashes.

## 7. Read the result with the Agent

Ask for separate English and Simplified-Chinese reports when both are useful. Request a summary
that clearly separates:

- verified facts from the run;
- warnings and data qualifications;
- research interpretation;
- assumptions and sample limitations;
- claims that the evidence does **not** support;
- links or paths to the exact reports and immutable evidence.

A positive backtest does not prove that a strategy will remain profitable. Ask the Agent to discuss
cost sensitivity, turnover, drawdown, regime dependence, factor overlap, and development-versus-
holdout evidence before considering further research.

## A complete example request

```text
Research a cross-sectional 24-hour momentum factor on Binance USDT-margined perpetual futures from
January through December 2025 using 15-minute bars. Consider a market-neutral portfolio that is
long the top 10% and short the bottom 10%, rebalanced every four hours. Use point-in-time contract
eligibility, next-bar fills, observed funding, and realistic fees and slippage.

First check the available data and propose the precise factor, ranking, sizing, leverage, and risk
semantics. Explain any missing choice and estimate turnover drag. After I approve the freeze sheet,
run only Quick Research. If the reviewed evidence justifies continuing, ask separately before Fast
Matrix. Never select an Event candidate automatically. A formal Event run requires my named
candidate, rationale, reviewed configuration, and a new authorization.

Run long work as a recorded background job and stop monitoring after launch. Produce separate
English and Simplified-Chinese reports and explain the final evidence in plain language.
```

## Current boundaries

The supervised Agent workflow is implemented, but BFBT is not a standalone one-click chat product.
The external Agent still performs natural-language interpretation and invokes the existing BFBT
services. Generic process heartbeat, safe cancellation, shared-user queues, and a hosted chat UI are
not complete product features.

BFBT deliberately does not:

- access credentials, balances, private exchange APIs, or live orders;
- execute arbitrary model-generated Python or shell as a research specification;
- guess unresolved economic semantics;
- silently download data or broaden authorization;
- automatically promote a Fast Matrix candidate;
- present research output as investment advice.

For manual commands and configuration details, see the [user manual](user_manual.md). For the exact
control-plane contracts, see the [Agent workflow design](../design/agent_workflow.md).
