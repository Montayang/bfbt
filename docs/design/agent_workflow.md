# General supervised AI Agent workflow

BFBT's Agent surface is a deterministic control plane around the existing data, research, and
Event engines. It does not embed a particular language model and does not accept generated shell or
Python as its public API. An integrating Agent translates natural language into a strict contract;
BFBT decides whether that contract is complete, safe, authorized, and supported.

## Workflow

```text
natural-language request
  -> AgentResearchIntent + original-text hash + ambiguity list
  -> semantic freeze + data/capability/cost/action plan
  -> plan-bound authorization grants
  -> DE-v1 exact DatasetSnapshot
  -> Quick Research evidence
  -> Fast Matrix candidate-set evidence
  -> structured human promotion decision
  -> Event formal evidence
  -> claim-level evidence summary
```

The route is objective-dependent. Factor diagnostics stop after Quick Research; portfolio research
stops after Fast Matrix; formal backtests require the complete route and a human selection. Result
queries can only cite exact, hash-verified references supplied in the intent.

## Contracts and failure boundaries

`agent-research-intent/v1` freezes the original text, objective, factor identity, data requirement,
all clocks, ranking, portfolio, fill, sizing, costs, risk, terminal handling, assumptions, decisions,
ambiguities, and outputs. Unresolved ambiguity blocks execution.

`agent-workflow-plan/v1` is side-effect-free. It embeds the DE-v1 plan, selects the legal backend,
estimates rebalance cost drag, emits confirmation codes, and classifies every action. It never
grants permission.

`agent-authorization/v1` binds one action class and acknowledgement set to one exact plan hash and
expiry. Network, download, data writes, research, formal Event execution, tests, and source control
remain separate actions. Read-only inspection does not broaden authority.

`agent-workflow-job/v1` records stage status and verified hand-offs. It never runs arbitrary
commands. An Agent may start an existing recorded data/research/Event operation, return control,
and later attach its immutable evidence. Succeeded stages are retained across sessions.

## Safe factor extension

An intent may reference the built-in registry or `bfbt-factor-expression/v1`. The expression
language permits only market fields, finite numeric constants, arithmetic, `abs`, `log`, causal
`lag`, bounded rolling functions, and EMA. It rejects attributes, imports, indexing, comprehensions,
conditionals, keyword calls, unknown functions, and negative/future lags. Compilation uses Python's
AST only for parsing; it never calls `eval` or `exec`.

Expression execution is grouped by symbol, resets rolling history at missing/incomplete bars,
uses close-time availability, joins only the point-in-time eligible universe, rejects non-finite
outputs, and receives a content-derived factor version.

## Evidence and human control

- DE evidence must match a succeeded DE-v1 job and the reviewed embedded data-plan hash.
- Quick evidence must be a succeeded study summary.
- Matrix evidence is a hash-pinned candidate set; every `fm-*` manifest and referenced file is
  verified by `MatrixResearchStore`.
- Promotion can select only a run in that verified set and records rationale, person, time, and
  Event override hash.
- Event evidence must be a succeeded immutable run whose resolved configuration names the selected
  Matrix source run.
- Final claims use `agent-evidence-summary/v1`; every fact, warning, or qualification cites one or
  more admitted SHA-256 evidence objects.

The workflow never automatically chooses a candidate, turns research into formal truth, downloads
data from a read-only plan, accesses credentials, or sends orders.

