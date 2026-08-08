# Testing ContextCI by hand

Four levels, cheapest first. Each one works on its own — do level 1 and 2 with
nothing but this repo, and only reach for GitHub at level 4.

| Level | Proves | Needs |
| --- | --- | --- |
| 1. Unit tests | Every rule, parser and write path | Nothing |
| 2. Local diff, no DataHub | Diff parsing, LLM reasoning, migration generation | A Groq or Anthropic key |
| 3. Local diff + live DataHub | Column-level lineage, governance, tag write-back | A running DataHub |
| 4. Real pull request | The full gate, PR comment, auto-fix commit | A GitHub token and a repo |

---

## What you need

| Thing | Needed for | How to get it |
| --- | --- | --- |
| Python 3.11 | everything | 3.14 has no `pydantic-core` wheels — pin 3.11 |
| `GROQ_API_KEY` | levels 2–4 | console.groq.com → API Keys. Free tier is enough |
| A running DataHub | levels 3–4 | `datahub docker quickstart` (needs 13 GB free Docker disk) |
| `GITHUB_TOKEN` | level 4 only | A fine-grained PAT with **Contents: read & write** and **Pull requests: read & write** on the repo under test |
| `GITHUB_REPOSITORY`, `PR_NUMBER` | level 4 only | e.g. `LSUDOKO/ContextCI` and `1` |

Everything is read from `.env`, which is gitignored. Real environment variables
override it.

---

## Level 1 — unit tests

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -q
```

Expect `65 passed`. No network, no DataHub, no LLM.

**What this proves:** diff parsing across four dialects, the risk-escalation
table, the compliance gate, DataHub write idempotency, comment rendering.

---

## Level 2 — a diff on disk, no DataHub

Put your key in `.env`:

```
GROQ_API_KEY=gsk_...
```

Then:

```bash
.venv/bin/python -m src.main --diff examples/breaking_change.diff
```

**What you should see:** three changes found — a SQL `DROP COLUMN`, an Alembic
rename, and a dbt `schema.yml` removal — each with a generated migration, and a
"degraded run" banner because DataHub is unreachable. Exit code 0 or 1 depending
on the verdict.

**Try breaking it on purpose:**

```bash
printf '+++ b/m.sql\n@@ -0,0 +1 @@\n+ALTER TABLE orders ADD COLUMN nickname text;\n' > /tmp/safe.diff
.venv/bin/python -m src.main --diff /tmp/safe.diff        # → approve, exit 0

printf '+++ b/m.sql\n@@ -0,0 +1 @@\n+-- ALTER TABLE orders DROP COLUMN id;\n' > /tmp/comment.diff
.venv/bin/python -m src.main --diff /tmp/comment.diff     # → no changes detected
```

Commented-out DDL and additive changes must never raise an alarm. If they do,
that's a bug.

---

## Level 3 — against a live DataHub

### 3a. Start DataHub

```bash
.venv/bin/datahub docker quickstart          # or DATAHUB_MAPPED_GMS_PORT=8081 if 8080 is taken
curl -s localhost:8081/config                # GMS ready when this returns JSON
open http://localhost:9002                   # UI, login datahub / datahub
```

If the containers already exist, start them instead of re-running quickstart
(it aborts below 13 GB free disk) — see the "Bringing the local quickstart back
up" section in `CLAUDE.md`.

### 3b. Load metadata with lineage

```bash
curl -fsSL -o /tmp/bootstrap_mce.json \
  https://raw.githubusercontent.com/datahub-project/datahub/master/metadata-ingestion/examples/mce_files/bootstrap_mce.json

cat > /tmp/recipe.yml <<'YAML'
source:
  type: file
  config: {path: /tmp/bootstrap_mce.json}
sink:
  type: datahub-rest
  config: {server: http://localhost:8081}
YAML

.venv/bin/datahub ingest -c /tmp/recipe.yml
```

Expect `produced 105 events`.

### 3c. Point ContextCI at a dataset that really has lineage

`.env` should already contain:

```
DATAHUB_GMS_URL=http://localhost:8081
DATAHUB_FRONTEND_URL=http://localhost:9002
DATAHUB_PLATFORM=hive
TOOLS_IS_MUTATION_ENABLED=true
```

```bash
cat > /tmp/live_demo.diff <<'EOF'
+++ b/migrations/012_drop_field_foo.sql
@@ -0,0 +1,2 @@
+-- Legacy column cleanup
+ALTER TABLE SampleHiveDataset DROP COLUMN field_foo;
EOF

.venv/bin/python -m src.main --diff /tmp/live_demo.diff
```

**What you should see:**

```
phase 1: 1 schema change(s)
phase 3: verdict from groq
phase 4b: 17/17 DataHub mutations applied
```

plus the full Markdown comment listing 13 downstream assets — datasets, Airflow
data jobs, ML features and ML primary keys — with their owners.

### 3d. Check the write-back in the DataHub UI

Open <http://localhost:9002> and search `SampleHiveDataset`. You should see:

- Dataset tags: **Schema-Change-Pending**, **PR-Under-Review** (and its original `Legacy` tag, still there)
- The **field_foo** row in the Schema tab tagged **Schema-Change-Pending**
- The Documentation tab carrying a link to the diff with the blast-radius note
- `fct_users_created` tagged **Blast-Risk-Critical**

### 3e. Prove it is idempotent

Run 3c again. The mutation count stays `17/17`, no tag is duplicated, and the
note is updated in place rather than appended. This is the property that lets the
agent run on every push to a PR.

### 3f. Prove the dry run is really dry

```bash
TOOLS_IS_MUTATION_ENABLED=false .venv/bin/python -m src.main --diff /tmp/live_demo.diff
```

The log says what it *would* have written and the graph is untouched.

### 3g. Trigger the compliance gate

Tag a dataset as PII in the DataHub UI (Tags → Add → `PII`), then re-run 3c. The
verdict must become **block** with a "Security review required" banner, whatever
the blast radius says.

---

## Level 4 — a real pull request

### 4a. Give the Action its secrets

In the repo under test: **Settings → Secrets and variables → Actions**.

Secrets:

| Name | Value |
| --- | --- |
| `GROQ_API_KEY` | your Groq key |
| `DATAHUB_MCP_URL` | your GMS endpoint — must be reachable *from the runner*, so `localhost` only works on a self-hosted runner |
| `DATAHUB_GMS_TOKEN` | only if your DataHub requires auth |

Variables (optional): `DATAHUB_PLATFORM`, `DATAHUB_ENV`, `CONTEXTCI_AUTOFIX`,
`CONTEXTCI_MENTION_OWNERS`.

`GITHUB_TOKEN` is injected by Actions automatically — you do not create it.

### 4b. Open a breaking PR

```bash
git checkout -b test/drop-column
mkdir -p migrations
echo "ALTER TABLE SampleHiveDataset DROP COLUMN field_foo;" > migrations/999_drop.sql
git add migrations/999_drop.sql
git commit -m "test: drop a column"
git push -u origin test/drop-column
gh pr create --fill
```

### 4c. What to check

1. The **ContextCI Schema Gate** check appears on the PR.
2. A comment is posted with the blast radius, the risk badge and the migration.
3. Push another commit to the branch — the **same comment is edited**, not duplicated.
4. On a block verdict the check is **red** and merge is prevented (turn on branch protection requiring the check).
5. The tags appear in DataHub as in 3d.
6. With `CONTEXTCI_AUTOFIX=true` and a medium-or-worse verdict, a `fix(contextci): …` commit lands on your branch.

### 4d. Running the same thing locally against a real PR

You do not need the Action to test level 4 logic:

```bash
GITHUB_TOKEN=ghp_... GITHUB_REPOSITORY=LSUDOKO/ContextCI PR_NUMBER=1 \
  .venv/bin/python -m src.main
```

This reads the real diff, posts the real comment, and writes the real tags.

---

## Reading the exit code

| Code | Meaning |
| --- | --- |
| `0` | approve or warn — merge may proceed |
| `1` | **block** — the gate stops the merge |
| `2` | misconfiguration (no `GITHUB_REPOSITORY` / `PR_NUMBER` and no `--diff`) |

Only a real block verdict returns 1. DataHub being down, a missing API key, or a
GitHub failure all degrade to a warning — the gate is never the reason your CI
breaks.

---

## When something looks wrong

| Symptom | Cause |
| --- | --- |
| `Table 'x' not found in DataHub catalog` | `DATAHUB_PLATFORM` doesn't match the dataset's platform. The sample data is `hive`, not `postgres` |
| Verdict is always rule-based | No key found. Check `.env` is in the working directory and the key name is exact |
| Groq `413 Request too large` | Free tier is 8000 TPM. Lower `CONTEXTCI_GROQ_MAX_TOKENS` |
| `phase 4b: skipped` | DataHub unreachable, or `TOOLS_IS_MUTATION_ENABLED=false` |
| Comment posted twice | The marker was stripped. ContextCI finds its comment by the `<!-- contextci-blast-report -->` HTML comment |
| `403` from GitHub | Token lacks **Pull requests: write**, or the workflow lacks `permissions: pull-requests: write` |
