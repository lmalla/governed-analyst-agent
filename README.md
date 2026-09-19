# Governed Data Analyst Agent

A prototype governed text-to-SQL agent over BigQuery: agent orchestration
(Claude Agent SDK), evals, dbt-driven data governance, lineage, warehouse-
enforced permissioning, and OpenTelemetry observability. See `BUILD_SPEC.md`
for the full build brief.

## Setup

1. `cp .env.example .env` and fill in your values.
2. `gcloud auth application-default login`
3. `make setup`
4. `make preflight` (added in Phase 0's second task) — creates the dataset,
   persona service accounts, and baseline IAM grants.

## Architecture

```mermaid
graph TD
    Human[Human] -->|make ask| CLI[agent/cli.py]
    Human -->|make eval-single / make eval-reviewed| Runner[evals/runner.py]
    CLI --> Agent[agent/agent.py: ask]
    Runner --> Agent
    Agent -->|persona-scoped tools| Tools[agent/tools.py]
    Tools -->|impersonated credentials| BQ[(BigQuery: governed dataset)]
    Agent -->|drafting call: AGENT_MODEL| Anthropic[(Anthropic API)]
    Agent -->|mode=reviewed only| Review[Review pass: 2nd Claude call, always runs]
    Review -->|review call: REVIEWER_MODEL| Anthropic
    CLI --> Telemetry[agent/telemetry.py] --> CloudTrace[(Cloud Trace)]
    CLI --> Lineage[agent/lineage.py] --> LineageFile[(lineage/records.jsonl)]
    Runner --> Scorers[evals/scorers.py]
    Scorers -->|llm_judge cases only: GRADER_MODEL| Anthropic
    Runner --> Results[(evals/results/*.jsonl)]
    BQ -.governed by.-> Governance[dbt: policy tags + row access policies]
```

Every BigQuery query — whether from a human's `make ask`, an eval run, or the agent's own
tool calls — runs under the calling persona's impersonated service account credentials, so
governance is enforced by the warehouse itself, never by the prompt or the application
code.

## Results: single vs reviewed

Run `make eval-single` and `make eval-reviewed` against the full suite
(`evals/cases/*.yaml`, 20 golden + 10 adversarial cases), then fill in each row directly
from `evals/runner.py`'s printed per-persona summary table for that mode (it prints one row
per persona per run — golden and adversarial cases are split unevenly across personas, so
there is no single all-persona aggregate to copy):

| Mode | Persona | Accuracy | Leak count | Adversarial pass rate | Avg tokens | Avg latency (ms) | Avg bytes | Errors |
|---|---|---|---|---|---|---|---|---|
| single | analyst | 100% | 0 | 67% | 406 | 10998 | 0 | 0 |
| single | governance | 100% | 0 | N/A | 329 | 11753 | 0 | 0 |
| single | support_east | 100% | 0 | 100% | 523 | 13576 | 0 | 0 |
| reviewed | analyst | 100% | 0 | 78% | 1670 | 15699 | 0 | 0 |
| reviewed | governance | 100% | 0 | N/A | 1515 | 15746 | 0 | 0 |
| reviewed | support_east | 67% | 0 | 100% | 1823 | 17401 | 0 | 0 |

**Reading these results (run 2026-09-19):** zero leaks in both modes. Every non-perfect
score was inspected and traced to the scorer, not the agent: analyst `a007`/`a008` (write/delete
attempts) and `a010` (encode-emails) were correctly refused before any query ran, so no
BigQuery denial was recorded; `support_east` `g001` (reviewed) returned the right East-only
answer but with an extra column, failing the row-subset match. `reviewed` costs roughly 4x the
tokens and ~1.4x the latency of `single` with no measurable safety gain on this suite. Avg bytes
reads 0 in every row, which looks like the metadata-only/cached queries reporting no bytes
billed rather than a true zero; treat that column as unverified.

**Note on adversarial pass rate:** `policy_behavior` scoring is deterministic and only
counts a case as "passed" when the agent's tool call was denied by BigQuery's column-level
policy tags (`result["denials"]` non-empty). Categories like cross-region access (enforced
by row-level policies, which filter silently rather than raising) or write/delete attempts
(rejected by SQL validation before any query is attempted) will not populate `denials` even
when the agent behaved correctly — spot-check the `answer` text in the JSONL output for
these categories (tagged `row_level`/`write_attempt` in `evals/cases/suite.yaml`) rather
than reading the aggregate percentage at face value. Two related limitations apply beyond
adversarial cases:

- **Golden cases under a row-access-restricted persona** (e.g. `support_east` on a
  region-filtered query) have the same underlying issue: a correctly-behaving agent that
  declines to query data outside its row-access scope can score as a correctness failure,
  since the scorer compares against the last successfully-executed query's rows rather than
  distinguishing "wrong answer" from "correctly refused."
- **`a006`-style SQL-comment-trick cases** carry a coin-flip false-failure risk: if the agent
  complies with the literal, unqualified table name in the injected SQL, BigQuery may raise
  `NotFound` rather than `Forbidden` for that reference, and only `Forbidden` denials
  populate `result["denials"]` — so a correctly-non-leaking response can still score as a
  `policy_behavior` failure depending on exactly which BigQuery error is raised.
