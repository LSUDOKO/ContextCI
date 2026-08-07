# CLAUDE.md

ContextCI — an autonomous DataOps SRE agent that gates schema changes in pull requests by
tracing their blast radius through DataHub, and writes governance tags back to the catalog.
Built for the "Build with DataHub: The Agent Hackathon". Apache 2.0.

## Commands

```bash
# Environment (Python 3.14 has no pydantic-core wheels; pin 3.11)
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt

.venv/bin/python -m pytest tests/ -q        # full suite, ~0.2s
.venv/bin/python -m src.main                # run the gate (needs the env vars below)

# Local DataHub
DATAHUB_MAPPED_GMS_PORT=8081 .venv/bin/datahub docker quickstart
.venv/bin/datahub docker ingest-sample-data
```

Port 8080 is occupied on this machine by an Envio `generated-graphql-engine-1` container, so
the local DataHub quickstart is mapped to **8081**. `~/.datahubenv` points at
`http://localhost:8081` with an empty token (the quickstart is unauthenticated).

## Required environment

| Variable | Notes |
| --- | --- |
| `GITHUB_TOKEN`, `GITHUB_REPOSITORY`, `PR_NUMBER` | Supplied by the Action; required locally |
| `DATAHUB_MCP_URL` (or `DATAHUB_GMS_URL`) | GMS endpoint; the client degrades gracefully if unreachable |
| `DATAHUB_GMS_TOKEN` | Optional for a local quickstart |
| `ANTHROPIC_API_KEY` | Without it, the deterministic rule-based analyzer runs instead |
| `CONTEXTCI_AUTOFIX`, `CONTEXTCI_MENTION_OWNERS` | Both default to `false` |

## Architecture

Four phases, one module each, wired together in `src/main.py`:

1. `diff_parser.py` — parses unified-diff patches (not whole files) for schema changes across
   raw SQL DDL, Alembic `op.*` calls, dbt `schema.yml`, and dbt model select lists.
2. `datahub_mcp_client.py` — resolves table names to dataset URNs, reads column-level lineage
   and governance, and later writes tags and notes back. Backed by `DataHubGraph` from the
   `acryl-datahub` SDK, which is the same API surface the DataHub MCP Server exposes.
3. `blast_analyzer.py` + `code_generator.py` — Claude returns a structured verdict via
   `client.messages.parse(output_format=...)`; a rule-based path covers the no-key case.
4. `github_reporter.py` — one sticky PR comment, optional auto-fix commits.

`models.py` holds the Pydantic contracts every phase exchanges. Change those first when
adding a field; the rest follows.

## Conventions that matter here

- **Absent lineage is not evidence of safety.** A table missing from DataHub, or a downstream
  asset whose column-level lineage is unconfirmed, produces a warning — never an approval.
- **Everything is idempotent.** The PR comment is keyed by an HTML marker and edited in place;
  DataHub tags are read before write; the pending-change note is keyed by the PR URL.
- **Nothing crashes the build except a real block verdict.** DataHub failures, missing API
  keys and GitHub errors all degrade to warnings. Exit code 1 is reserved for
  `recommended_action == "block"`.
- **Only added diff lines count as DDL.** Deleting an old migration is not a schema change.
- **Owner @-mentions stay opt-in.** DataHub owner names are not GitHub handles; guessing wrong
  pings a stranger on every PR.

## DataHub skills

This repo has the `datahub-skills` plugin installed. Use `/datahub-search` to explore the
catalog, `/datahub-lineage` for lineage questions, and `/datahub-setup` for connectivity
problems, rather than writing ad-hoc `datahub` CLI invocations. The plugin's
`catalog-*` skills are the same commands under different names.

## Testing notes

Tests are pure — no network, no DataHub, no Anthropic API. The LLM path is exercised
end-to-end against a real PR; the rule table in `_analyze_with_rules` is what the unit tests
pin down. When changing risk escalation, update `tests/test_blast_analyzer.py` in the same
commit — the thresholds are the product.
