# ContextCI

**Context-Aware CI. Zero Breaking Changes.**

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/LSUDOKO/ContextCI?quickstart=1&machine=standardLinux32gb)

## The 3am page

A backend engineer renames `user_id` to `account_id` in a migration. Every application
test passes — it is valid SQL, and nothing in that repository mentions data.

Twelve hours later the nightly dbt run produces NULLs, an executive dashboard shows the
wrong revenue number, and a recommendation model quietly starts training on empty values.
Nobody gets an error. The on-call data engineer spends the morning grepping commit history
across ten repositories to find out who changed what.

**The lineage that would have caught this already exists in DataHub.** It is just on the
wrong side of the merge button.

ContextCI moves it. It runs as a GitHub Action: when a pull request alters a schema, it
traces the change through DataHub's column-level lineage, decides whether anything
downstream breaks, writes the backward-compatible migration, comments on the PR, and
**tags the affected datasets back in DataHub** so the catalog records that a change is in
flight.

DataHub is treated as a two-way operating system, not a read-only catalog.

---

## Try it — one command, nothing to install

Click the Codespaces badge above (pick the **4-core** machine), then:

```bash
make demo
```

That boots DataHub, ingests sample metadata with real column-level lineage, runs the gate
against a breaking `DROP COLUMN`, and prints the exact PR comment it would post. Roughly
10–15 minutes on a cold start, almost all of it pulling DataHub's images.

```
4/4  Running the gate against a breaking schema change
      ALTER TABLE SampleHiveDataset DROP COLUMN field_foo;

ContextCI verdict: block (risk: critical)
  Exit code 1 — 1 means the gate would block the merge.
  DataHub UI:  https://…-9002.app.github.dev   (login datahub / datahub)
```

Then open the DataHub UI it prints and search `SampleHiveDataset` — the tags ContextCI
just wrote are on the dataset, on the `field_foo` column itself, and on every downstream
asset. That round trip, catalog → decision → catalog, is the whole idea.

`make help` lists the rest (`make test`, `make gate DIFF=…`, `make token`).

To let **GitHub Actions** reach that DataHub — so CI resolves real lineage instead of
degrading to diff-only — see [Hosting DataHub on Codespaces](docs/CODESPACES-DATAHUB.md).

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

### 1b. Turn on real authentication (recommended)

The quickstart ships with `METADATA_SERVICE_AUTH_ENABLED=false`, which is why
`DATAHUB_GMS_TOKEN` can be left blank — the metadata service accepts anonymous
calls. No real deployment runs that way. To make ContextCI authenticate the way it
would in production, flip the flag on GMS and mint a token:

```bash
# 1. GMS with metadata auth on (state lives in MySQL/OpenSearch, so recreating GMS is safe)
docker inspect datahub-datahub-gms-quickstart-1 > /tmp/gms.json   # keep its 74 env vars
# recreate with METADATA_SERVICE_AUTH_ENABLED=true and the datahub-gms network alias

# 2. Mint a personal access token straight into .env
python scripts/make_datahub_token.py
```

`scripts/make_datahub_token.py` logs into the DataHub frontend, creates a
`PERSONAL` access token via GraphQL, and writes it to `.env` as
`DATAHUB_GMS_TOKEN`, replacing any existing or commented-out line. Options:
`--frontend`, `--user`, `--password`, `--duration`, `--name`.

Verify the token is genuinely required:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8081/api/graphql \
  -H 'Content-Type: application/json' -d '{"query":"{ me { corpUser { username } } }"}'
# 401 without a token; 200 with `Authorization: Bearer $DATAHUB_GMS_TOKEN`
```

Once the token is in `.env`, every read and every mutation ContextCI performs is
authenticated — nothing else in the project changes.

> The `datahub-gms` network alias matters: the frontend resolves GMS by that name,
> and a recreated container that only carries `datahub-gms-quickstart` will make
> login fail with `UnknownHostException: datahub-gms`.

### 2. Configure the repository

Add these repository **secrets** (Settings → Secrets and variables → Actions):

| Secret | Purpose |
| --- | --- |
| `DATAHUB_MCP_URL` | DataHub GMS endpoint, e.g. `http://localhost:8081` or `https://<instance>.acryl.io/gms` |
| `DATAHUB_GMS_TOKEN` | DataHub personal access token (omit for an unauthenticated local quickstart) |
| `ANTHROPIC_API_KEY` | Enables LLM analysis and code generation with Claude |
| `GROQ_API_KEY` | Alternative provider. Used when no Anthropic key is set; without either, ContextCI falls back to deterministic rules |

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

## Reasoning providers

| Provider | Env var | Default model | Notes |
| --- | --- | --- | --- |
| Anthropic | `ANTHROPIC_API_KEY` | `claude-opus-5` | Structured output via `messages.parse` |
| Groq | `GROQ_API_KEY` | `openai/gpt-oss-120b` | Override with `CONTEXTCI_GROQ_MODEL` |
| None | — | — | Deterministic rule-based analyzer |

Anthropic wins when both keys are set. A provider failure is never fatal —
ContextCI logs it and falls through to the rules.

**Groq free-tier limit.** The free tier allows 8000 tokens per minute and counts
`max_tokens` against that budget, so ContextCI asks Groq for 4000 rather than the
16000 it asks Claude for. Raise it with `CONTEXTCI_GROQ_MAX_TOKENS` on a paid
tier. Not every Groq model supports strict JSON schema (`openai/gpt-oss-120b`
does, `llama-3.3-70b-versatile` does not); ContextCI detects the rejection and
retries in JSON mode automatically.

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

The **LLM path is verified too**, via Groq (`openai/gpt-oss-120b`) against the
same catalog. Three consecutive runs were served by the model with no rate-limit
failures. On this data it returns **medium / warn** where the rules return
critical / block — a defensible disagreement: there are 13 downstream assets but
DataHub confirms column-level lineage for none of them, and the model weighs that
uncertainty rather than counting neighbours. Its summary and generated view:

> Dropping `field_foo` may break downstream assets that read it; column is used
> in queries but no confirmed lineage.

```sql
CREATE OR REPLACE VIEW SampleHiveDataset_compat AS
SELECT field_foo, field_bar FROM SampleHiveDataset;
```

The Claude path has not been exercised against the live API here — no Anthropic
key was available on this machine.

### Level 4: a real pull request

Run against [`LSUDOKO/ContextCI#1`](https://github.com/LSUDOKO/ContextCI/pull/1),
a PR whose only content is `ALTER TABLE SampleHiveDataset DROP COLUMN field_foo;`.

| Checked | Result |
| --- | --- |
| Diff read from the GitHub API | 1 change found in `migrations/999_drop_field_foo.sql` |
| Lineage resolved | 13 downstream assets |
| Verdict | `block` / critical, exit code **1** — merge is stopped |
| PR comment | posted once, then **edited in place** across five further runs (`created 11:12`, `updated 11:15`, one comment carrying the marker) |
| Auto-fix | with `CONTEXTCI_AUTOFIX=true`, `fix(contextci): …` committed a compatibility view to the PR branch |
| Graph write-back | `17/17` mutations; `Blast-Risk-High` from an earlier run **replaced** by `Blast-Risk-Critical`, not stacked |

### Through authenticated DataHub

The same PR run with `METADATA_SERVICE_AUTH_ENABLED=true` and a real
`DATAHUB_GMS_TOKEN`: anonymous GraphQL and OpenAPI both return **401**, the token
resolves as `urn:li:corpuser:datahub`, and the run reads 13 downstream assets and
applies all 17 mutations through the authenticated endpoint.

### In GitHub Actions

The workflow has run on PR #1 for real, not just locally
([runs](https://github.com/LSUDOKO/ContextCI/actions)):

```
phase 1: 1 schema change(s) detected              ← diff read from the GitHub API
phase 3: verdict from groq                        ← AI reasoning on the runner
phase 4a: comment …#issuecomment-5225846936       ← posted by github-actions[bot]
```

**DataHub is unreachable from a GitHub-hosted runner** if it only exists on your
laptop, and the run degrades to diff-only analysis with a banner saying so — which
is the designed behaviour, not a failure. For full lineage in CI you need DataHub
reachable from the runner: a hosted/cloud DataHub, a self-hosted runner on the
same network, or a tunnel. Set `DATAHUB_MCP_URL` to that endpoint.

Two CI-only bugs came out of this, both fixed: the workflow never forwarded
`GROQ_API_KEY` (so CI silently ran rule-based), and an unset Actions `vars.X`
interpolates to the *empty string*, which `os.getenv(name, default)` returns
verbatim — Groq was being called with an empty model name. Config reads now use
`os.getenv(x) or default` throughout.

Three more bugs surfaced from the live reruns, all fixed:

- Risk tags accumulated instead of replacing, so one asset carried
  `Blast-Risk-Critical`, `-Medium` and `-High` at once.
- A non-breaking verdict skipped write-back entirely, leaving markers from an
  earlier breaking run on the dataset forever.
- **The verdict was not reproducible.** The same input returned `high / block`
  twice and `medium / warn` on the third run. `temperature=0` does not fix this —
  MoE batching on hosted inference is nondeterministic by construction. The
  deterministic rules are now a floor: the model writes the summary, reasoning and
  migration and may escalate, but can never rule a change safer than the rules
  would have. Four consecutive runs then returned `block / critical` identically.

**Quickstart gotchas hit on the way**, in case you hit them too:

- The quickstart refuses to start below 13 GB of free Docker disk.
- If another process already holds host port `3306`, the quickstart's MySQL
  container can end up detached from `datahub_network`, and the system-update job
  then fails with `Timeout waiting for TCP mysql:3306`.
- Below ~5% free disk, OpenSearch sets a persistent `cluster.blocks.create_index`,
  and system-update fails at `BuildIndicesStep` with `403 index_create_block_exception`.
  Free disk, then clear it:
  `curl -XPUT localhost:9200/_cluster/settings -H 'Content-Type: application/json' -d '{"persistent":{"cluster.blocks.create_index":null}}'`

## Who this is for

| Role | What changes on Monday |
| --- | --- |
| **Data / analytics engineers** | Downstream breakage is caught before merge, not at 3am. The blast radius and a working migration arrive on the PR, so MTTR drops from hours of git archaeology to reading one comment. No more reviewing every backend PR by hand hoping to spot a schema change. |
| **Software engineers** | Feedback in the tool they already use. No DataHub login, no dbt knowledge, no waiting on a data team review — the PR tells them what they are about to break and hands them the fix. |
| **ML platform teams** | The failure mode ML actually has: a dropped feature does not raise, it feeds NULLs into training and the model silently degrades. ContextCI treats ML features and models as first-class downstream assets and escalates them above ordinary tables, because they fail quietly. |
| **Data leadership** | Fewer broken dashboards during business hours, less compute burned on pipelines that were doomed at merge time, and a catalog that records *why* a dataset is flagged instead of going stale. |

### Where it stops

Stated plainly, because a gate you cannot trust is worse than none:

- **Lineage quality is DataHub's, not ours.** On an uninstrumented warehouse you get
  table-level lineage only, and ContextCI says so on every asset (`⚠️ table-level only`)
  rather than implying certainty it does not have.
- **Table-name resolution assumes one platform per repository.** A repo spanning Postgres
  and Snowflake needs `DATAHUB_PLATFORM` per run.
- **The migration is a starting point, not a merge-ready commit.** It is generated from
  real schemas and real query history, and a human still reviews it — the same as any
  other suggested change.

## Roadmap — other surfaces

The GitHub Action is the right primary surface: no new UI, no new credentials, and it
lives where the decision is already being made. Two extensions are worth naming:

- **Hosted service / GitHub App** — "connect your repo, paste your DataHub URL" for teams
  without CI expertise. Deliberately *not* built here: it means holding other people's
  DataHub credentials, and the Action already installs in three lines of YAML.
- **VS Code extension** — show the blast radius inline while the migration is being
  written, before commit. The interesting version is a thin client over the same analyzer,
  not a second copy of it.

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
cp .env.example .env          # then fill in your keys
pip install -r requirements-dev.txt
pytest -q
```

74 tests cover diff parsing across four dialects, the risk-escalation rules, the
compliance gate, the rule floor, the DataHub write path (against a fake graph that
records emitted aspects), local diff splitting, and comment rendering. No network,
no DataHub, no LLM API.

### Repository layout

```
├── src/
│   ├── main.py                 # orchestrates the four phases; exits 1 on block
│   ├── models.py               # Pydantic contracts shared between phases
│   ├── diff_parser.py          # phase 1 — parse the PR diff
│   ├── datahub_mcp_client.py   # phase 2 reads + phase 4 writes
│   ├── blast_analyzer.py       # phase 3 verdict (LLM + rule floor + compliance gate)
│   ├── code_generator.py       # phase 3 migrations (prompt + templates)
│   └── github_reporter.py      # phase 4 PR comment and auto-fix commits
├── tests/                      # 74 tests, no network or external services
├── examples/
│   ├── demo_sample_hive.diff   # targets the DataHub sample data — use this for the demo
│   ├── breaking_change.diff    # SQL + Alembic + dbt YAML, shows parser breadth
│   ├── sample_dbt_fix.sql      # a generated migration
│   └── sample_pr_comment.md    # a real posted comment
├── scripts/
│   └── make_datahub_token.py   # mint a DataHub PAT into .env
├── .github/workflows/
│   └── contextci-gate.yml      # the GitHub Action
├── .devcontainer/
│   └── devcontainer.json       # Codespaces: Python 3.11 + docker-in-docker, 4-core
├── Makefile                    # make demo / test / gate / token
├── scripts/demo.sh             # the one-command demo
├── .env.example                # every setting, documented
├── TESTING.md                  # four-level manual test guide
├── Dockerfile
└── LICENSE                     # Apache 2.0
```

Agent and IDE tooling (`.agents/`, `.cursor/`, `.claude/`, `.windsurf/`,
`.clinerules/`, `.opencode/`, `AGENTS.md`) is local developer config and is
gitignored — the DataHub Skills those directories vendor are installed as a
plugin, not committed here.

---

## License

Apache 2.0 — see [LICENSE](LICENSE).
