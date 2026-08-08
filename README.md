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
| 2. Context | `src/datahub_mcp_client.py` | Resolve each table to a dataset URN, pull column-level lineage, ownership, glossary terms, tags, the dataset profile, and real query history |
| 3. Analyze | `src/blast_analyzer.py`, `src/code_generator.py` | Claude judges the blast radius against the real lineage and writes runnable migrations; a compliance gate blocks regulated changes outright |
| 4. Act | `src/github_reporter.py`, `src/datahub_mcp_client.py` | Post the PR comment, optionally commit fixes, tag the affected datasets **and the changed column** in DataHub, fail the build on a block verdict |

### What DataHub gives it

| DataHub feature | How ContextCI uses it |
| --- | --- |
| Column-level lineage | `searchAcrossLineage` finds downstream assets; each one's `upstreamLineage.fineGrainedLineages` is checked for the changed column's `schemaField` URN, so "reads this table" and "reads this column" are reported differently |
| Ownership | Owners of the breaking assets are listed in the PR comment, and @-mentioned when you opt in |
| Glossary terms and tags | PII / GDPR / PHI / Tier-1 markers drive the security gate; other terms escalate risk |
| Dataset profile | Row count, size, and the changed column's null fraction and distinct count size the migration — a backfill on four million rows is not the same as on a staging stub |
| Usage stats and query history | `topSqlQueries` and per-column `fieldCounts` ground generated migrations in SQL people actually run, and prove how heavily the column is queried |
| Tag mutations | `Schema-Change-Pending`, `PR-Under-Review`, `Security-Review-Required` on the dataset, `Blast-Risk-{level}` downstream, plus a field-level tag via `editableSchemaMetadata` |
| Institutional memory | The pending-change note is attached as a link keyed by the PR URL, so the catalog records why the dataset is flagged |

### The compliance gate

Risk heuristics decide engineering impact. They do not get to wave through a
compliance decision. A **destructive** change to a column carrying a PII, GDPR,
PHI, HIPAA, PCI, Tier-1, regulated or SOX marker — on the dataset itself or on
anything downstream — forces a block and a security review, whatever the blast
radius says. Additive changes never gate, and the gate can raise a verdict but
never soften one.

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
| `TOOLS_IS_MUTATION_ENABLED` | `true` | Set `false` for a read-only run — every graph write logs what it would have done and changes nothing. Same switch name the DataHub MCP Server uses |

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

### 4. Try it without a pull request

`--diff` runs phases 1–3 against a diff on disk and prints the exact comment it
would post. Graph writes stay off unless you ask for them:

```bash
python -m src.main --diff examples/breaking_change.diff
```

Against a live catalog, pointing at a dataset that really has lineage:

```bash
DATAHUB_GMS_URL=http://localhost:8081 \
DATAHUB_PLATFORM=hive \
TOOLS_IS_MUTATION_ENABLED=true \
python -m src.main --diff my_change.diff
```

This is the run recorded under "Verified end to end" below.

### 5. Run it locally

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

## Verified end to end

Run against a DataHub v1.7.0 quickstart (GMS on `:8081`, UI on `:9002`) loaded
with the standard `bootstrap_mce.json` sample metadata — 105 events, 7 datasets
with real lineage, ownership and ML features.

Input diff: `ALTER TABLE SampleHiveDataset DROP COLUMN field_foo;`

| Phase | Result |
| --- | --- |
| 1 | Change parsed from the diff: `drop_column SampleHiveDataset.field_foo` |
| 2 | Resolved to `urn:li:dataset:(urn:li:dataPlatform:hive,SampleHiveDataset,PROD)`; 13 downstream assets found — 2 dbt-style datasets, 2 data jobs, 4 ML features, 3 ML primary keys, an S3 backup; owners `John Doe`, `DataHub` |
| 3 | Verdict **critical / block**, with a staged-deprecation compatibility view generated |
| 4 | `17/17` graph mutations applied |

What landed in DataHub, read back from the graph afterwards:

```
dataset tags:     Legacy, Schema-Change-Pending, PR-Under-Review   ← existing tag preserved
field tag:        field_foo → Schema-Change-Pending                ← column-level write-back
downstream tags:  fct_users_created → Blast-Risk-Critical
note:             file:///…/live_demo.diff → "Pending …: drop column on `field_foo`.
                  Blast radius: 13 downstream asset(s)…"
```

Re-running holds at `17/17` with no duplicated tags and the note updated in
place, not appended.

**Not verified:** the LLM analysis path. No `ANTHROPIC_API_KEY` was available on
this machine, so the run above exercised the deterministic rule-based analyzer.
The LLM path is unit-tested at its boundaries but has not been exercised against
the live API here.

**Quickstart gotchas hit on the way**, in case you hit them too:

- The quickstart refuses to start below 13 GB of free Docker disk.
- If another process already holds host port `3306`, the quickstart's MySQL
  container can end up detached from `datahub_network`, and the system-update job
  then fails with `Timeout waiting for TCP mysql:3306`.
- Below ~5% free disk, OpenSearch sets a persistent `cluster.blocks.create_index`,
  and system-update fails at `BuildIndicesStep` with `403 index_create_block_exception`.
  Free disk, then clear it:
  `curl -XPUT localhost:9200/_cluster/settings -H 'Content-Type: application/json' -d '{"persistent":{"cluster.blocks.create_index":null}}'`

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

**ContextCI ignores its own tags.** `Blast-Risk-Critical` written by a previous run
would otherwise match the `critical` Tier-1 marker and gate a change that was never
regulated. Every tag ContextCI writes is excluded from governance evaluation; a real
`Business-Critical` tag still gates.

**No LangChain.** The analysis is a single structured-output call —
`client.messages.parse(output_format=...)` — so a chain framework would add a
dependency and an abstraction layer without removing a line of code.

---

## Development

```bash
pip install -r requirements-dev.txt
pytest -q
```

63 tests cover diff parsing across four dialects, the risk-escalation rules, the
compliance gate, the DataHub write path (against a fake graph that records emitted
aspects), local diff splitting, and comment rendering. No network, no DataHub, no
Anthropic API.

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
