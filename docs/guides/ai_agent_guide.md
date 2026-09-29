# Use BFBT through an AI Agent — no programming required

[简体中文](ai_agent_guide.zh-CN.md)

> **You need a Linux server before starting.** BFBT is not a website and does not run inside a
> normal browser chat. The simplest supported starting point is a 64-bit Ubuntu 22.04 or 24.04 LTS
> server with internet access and a server-connected AI Agent.

This guide starts from obtaining that server and ends with a completed, evidence-backed research
workflow. You do not need to write Python, edit JSON, or understand terminal commands. There is one
short copy-and-paste installation step if your Agent has not yet been connected; after that, you
describe research in ordinary language, review important choices, and authorize each consequential
stage.

The [beginner tutorial](beginner_tutorial.md) serves a different purpose: it teaches a user or
operator to run BFBT manually from a terminal. You do not need to complete it when an operator has
already prepared the server and Agent integration.

## Part A — put BFBT on a server

### 1. Obtain a Linux server

Create a server at a cloud provider or use a Linux machine you already control. When the provider
asks for an operating system, choose **Ubuntu 24.04 LTS, 64 bit**. Ubuntu 22.04 LTS also works.

A reasonable starting size for learning and bounded studies is:

- 4 virtual CPU cores;
- 16 GiB memory;
- 100 GiB SSD storage;
- outbound internet access to GitHub, Python package services, and—only when you approve a market-
  data download—Binance public data services.

Larger full-market minute studies can need 8 or more cores, 32 GiB memory, and 500 GiB or more
storage. Actual storage depends on symbols, dates, intervals, and retained datasets. A GPU, domain
name, exchange API key, inbound public web port, and trading account are not required.

Cloud servers normally cost money while running, and extra disk storage may be billed separately.
Deleting the server can also delete its datasets and reports. Use the provider's spending alerts
and snapshots or another backup method for research you want to retain. Do not open public ports
other than the access method required by your server or Agent provider.

Use a normal non-root login account. Keep the provider's password or SSH key private; never paste
it into a research request or commit it to this repository.

### 2. Connect an AI Agent, or use the server console once

Choose an AI coding or operations Agent that can open a workspace on the server and run commands
there. Follow that product's own instructions to connect it to the server; BFBT does not bundle or
require a particular model vendor.

If the Agent can already operate the server, give it this deployment request:

```text
Install the public repository https://github.com/Montayang/bfbt on this Ubuntu server under my
normal user account. Follow its no-programming Agent guide and run scripts/install_ubuntu.sh.
Do not access credentials, download market data, run research, or start a backtest. Report the
checkout path and the final bfbt doctor result.
```

If no Agent is connected yet, open the cloud provider's browser-based terminal or SSH console and
paste these four lines exactly:

```bash
sudo apt-get update
sudo apt-get install -y git
git clone https://github.com/Montayang/bfbt.git
cd bfbt && bash scripts/install_ubuntu.sh
```

The server may ask for its login password while installing the standard Ubuntu packages. The BFBT
[`scripts/install_ubuntu.sh`](../../scripts/install_ubuntu.sh) helper then creates a private Python
environment inside the checkout, installs runtime
dependencies, prepares ignored local data directories, and runs the read-only readiness check. It
does **not** download market data, start research, run a backtest, open a network port, or access an
exchange account.

Successful output ends with text similar to:

```text
ready=true
BFBT is installed in /home/your-user/bfbt.
No market data was downloaded and no backtest was started.
```

If it stops, copy the complete error into the Agent and ask it to diagnose the installation without
deleting an existing `.venv` or changing anything outside the BFBT checkout without your approval.

### 3. Point the Agent at the BFBT workspace

Set the Agent's workspace to the checkout path printed by the installer, commonly
`/home/your-user/bfbt`. The Agent must be able to read the repository, run `.venv/bin/bfbt`, and
write only the ignored `data/backtest/` workspace when authorized.

“Deployment” here does not mean starting a permanent web service. BFBT is an offline research
application. The Agent invokes it when you request work, and long operations use recorded
background jobs. Do not expose BFBT or its data directory directly to the public internet.

### 4. Confirm that deployment is ready

Ask the Agent to confirm that:

- BFBT is installed, its local workspace is writable, and the Agent has reported whether market
  data is already present or still needs separate authorization;
- the Agent can work inside the BFBT repository and run the installed `bfbt` application;
- the Agent cannot access exchange accounts, credentials, private order streams, or live trading;
- the Agent will ask before downloading data, writing research state, or starting a formal run;
- long jobs are recorded so that you can leave and ask for their status later.

BFBT does not contain its own chat model or web chat screen. A normal chat website that cannot
access the server is not enough. You need a server-connected Agent such as a supervised coding or
operations Agent. BFBT remains the deterministic research engine and permission boundary beneath
that Agent.

## Part B — run research by conversation

### 1. Start a research session

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

### 2. Describe what you want to learn

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

### 3. Review the freeze sheet

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

### 4. Authorize one stage at a time

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

### 5. Understand the three stages

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

### 6. Leave long jobs and return later

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

### 7. Read the result with the Agent

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

### A complete example request

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
