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

Architecture diagram and results table land here in Phase 3.
