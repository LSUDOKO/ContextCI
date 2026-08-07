# ContextCI

**Context-Aware CI. Zero Breaking Changes.**

ContextCI is an autonomous DataOps SRE agent that runs as a GitHub Action. When a pull
request alters a database schema, it traces the change through DataHub's column-level
lineage, decides whether anything downstream breaks, writes the backward-compatible
migration, comments on the PR, and **tags the affected datasets back in DataHub** so the
catalog records that a change is in flight.

DataHub is treated as a two-way operating system, not a read-only catalog: ContextCI reads
lineage, ownership, glossary terms and tags, then mutates the graph with
`Schema-Change-Pending`, `Blast-Risk-{level}` and a pending-change note linked to the PR.

---

## The problem

A developer drops a column. CI is green — the schema change is valid SQL. Three days later
a dbt model produces NULLs, an ML feature table silently drifts, and an executive dashboard
shows the wrong revenue number. Nothing errored; the breakage was in a system the PR never
mentioned.

The lineage that would have caught it already exists in DataHub. ContextCI puts it in the
pull request, before the merge.

---

## The four phases

| Phase | Module | What happens |
| --- | --- | --- |
| 1. Parse | `src/diff_parser.py` | Extract schema changes from the PR diff — raw SQL DDL, Alembic migrations, dbt `schema.yml` column removals, dbt model select-list drops |
| 2. Context | `src/datahub_mcp_client.py` | Resolve each table to a dataset URN, pull column-level lineage, ownership, glossary terms, tags |
| 3. Analyze | `src/blast_analyzer.py`, `src/code_generator.py` | Claude judges the blast radius against the real lineage and writes runnable migrations |
| 4. Act | `src/github_reporter.py`, `src/datahub_mcp_client.py` | Post the PR comment, optionally commit fixes, tag the affected datasets in DataHub, fail the build on a block verdict |

### What makes a change "breaking"

The analyzer weighs evidence rather than counting rows:

- **Column-level lineage confirmed** — DataHub's fine-grained lineage proves a downstream
  asset reads the specific column. Strongest signal.
- **Table-level only** — the asset reads the table, but DataHub cannot prove it touches the
  column. Weighed lower, never ignored: absent lineage is not evidence of safety.
- **Asset type** — dashboards and ML models fail *silently*, so they escalate risk further
  than another table would.
- **Governance** — `PII`, `GDPR-Sensitive` and `Revenue-Critical` glossary terms escalate.

`block` fails the Action (exit 1) and stops the merge. `warn` comments and passes. `approve`
is silent.

---

## Quick start for judges

### 1. Point ContextCI at a DataHub instance

Local quickstart (the DataHub CLI ships with it):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install acryl-datahub
datahub docker quickstart          # GMS on :8080, UI on :9002
```

If port 8080 is taken, run `DATAHUB_MAPPED_GMS_PORT=8081 datahub docker quickstart` and use
that port below. The quickstart needs about 13 GB of free Docker disk space.

Load the sample data so there is lineage to trace:

```bash
datahub docker ingest-sample-data
```

### 2. Configure the repository

Add these repository **secrets** (Settings → Secrets and variables → Actions):

| Secret | Purpose |
| --- | --- |
| `DATAHUB_MCP_URL` | DataHub GMS endpoint, e.g. `http://localhost:8081` or `https://<instance>.acryl.io/gms` |
| `DATAHUB_GMS_TOKEN` | DataHub personal access token (omit for an unauthenticated local quickstart) |
| `ANTHROPIC_API_KEY` | Enables LLM analysis and code generation; without it ContextCI falls back to deterministic rules |

`GITHUB_TOKEN` is provided by Actions automatically.

Optional repository **variables**:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATAHUB_PLATFORM` | `postgres` | Platform used when resolving a table name to a dataset URN |
| `DATAHUB_ENV` | `PROD` | Environment segment of the URN |
| `CONTEXTCI_AUTOFIX` | `false` | Commit generated migrations to the PR branch |
| `CONTEXTCI_MENTION_OWNERS` | `false` | @-mention DataHub owners in the comment (see the note below) |
| `DATAHUB_FRONTEND_URL` | — | Base URL used to link assets in the comment, e.g. `http://localhost:9002` |

The workflow at `.github/workflows/contextci-gate.yml` is already wired up — it runs on every
pull request against `main`.

### 3. Open a breaking PR

```sql
-- migrations/007_drop_customer_id.sql
ALTER TABLE analytics.orders DROP COLUMN customer_id;
```

ContextCI comments with the blast radius, the migration, and the owners; the check fails; the
`Schema-Change-Pending` tag appears on the dataset in the DataHub UI.

`examples/sample_pr_comment.md` and `examples/sample_dbt_fix.sql` show real output.

### 4. Run it locally

```bash
pip install -r requirements.txt
export GITHUB_TOKEN=ghp_...
export GITHUB_REPOSITORY=owner/repo
export PR_NUMBER=128
export DATAHUB_MCP_URL=http://localhost:8081
export ANTHROPIC_API_KEY=sk-ant-...
python -m src.main
```

Or containerised:

```bash
docker build -t contextci .
docker run --rm --network host \
  -e GITHUB_TOKEN -e GITHUB_REPOSITORY -e PR_NUMBER \
  -e DATAHUB_MCP_URL -e ANTHROPIC_API_KEY contextci
```

---

## Design decisions worth knowing

**Owner @-mentions are opt-in.** DataHub owner names are not necessarily GitHub handles, and
a wrong guess pings an unrelated person on every pull request. ContextCI renders owners as
plain names until you set `CONTEXTCI_MENTION_OWNERS=true`.

**Idempotent by construction.** The PR comment is keyed by an HTML marker and edited in
place, so pushing to the branch never stacks duplicates. DataHub tags are read before write,
and the pending-change note is keyed by the PR URL, so re-running mutates nothing twice.

**Degrades instead of crashing.** If DataHub is unreachable the client stays constructed but
reports `available = False`; reads return empty, writes return `False`, and the PR comment
carries a "degraded run" banner. If no `ANTHROPIC_API_KEY` is set, the rule-based analyzer
takes over. The gate always produces a verdict.

**Only added lines count as DDL.** Deleting an old migration file is not a schema change
being shipped, and commented-out DDL is ignored.

---

## Development

```bash
pip install -r requirements-dev.txt
pytest -q
```

36 tests cover diff parsing across four dialects, the risk-escalation rules, and comment
rendering.

```
src/
├── main.py                 # orchestrates the four phases; exits 1 on block
├── models.py               # Pydantic contracts shared between phases
├── diff_parser.py          # phase 1
├── datahub_mcp_client.py   # phase 2 reads + phase 4 writes
├── blast_analyzer.py       # phase 3 verdict (LLM + rule-based fallback)
├── code_generator.py       # phase 3 migrations (prompt + templates)
└── github_reporter.py      # phase 4 PR comment and auto-fix commits
```

---

## License

Apache 2.0 — see [LICENSE](LICENSE).
