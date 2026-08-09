# Hosting DataHub on GitHub Codespaces

A GitHub-hosted Actions runner cannot reach a DataHub on your laptop, so CI degrades to
diff-only analysis. Running DataHub inside a Codespace and exposing its port gives the
runner a real endpoint — full column-level lineage in CI, no tunnel, no cloud bill.

The Student Developer Pack includes Codespaces hours, and a 4-core machine is enough.

---

## Before you copy a guide written for another project

If you are adapting the ARGUS instructions, three details differ and each one costs an
hour to debug:

| Claim | Reality |
| --- | --- |
| "Port 8080 is the DataHub frontend" | **8080 is GMS, the API.** The frontend is **9002**. Verified on this instance: `9002` returns the DataHub login page, `8080/8081` returns `401` — an API, not a UI. Opening 8080 expecting a login screen wastes time. |
| "Set `DATAHUB_MCP_URL` to `<url>/mcp`" | The `/mcp` suffix belongs to the **DataHub MCP Server**, a separate process. ContextCI talks to **GMS** through the `acryl-datahub` SDK, so its `DATAHUB_MCP_URL` is the GMS root with **no suffix**. |
| "Make the port public so the backend can reach it" | Correct, and **that is exactly why you must turn authentication on first**. A public port on an unauthenticated GMS is an open, writable metadata store that anyone with the URL can read or mutate. |

Which port you expose depends on what you want:

- **CI needs GMS** (`8080`, or `8081` if you remapped it) — that is what ContextCI calls.
- **A demo audience needs the frontend** (`9002`) — the UI where the tags are visible.

---

## 1. Open the Codespace

**Code ▸ Codespaces ▸ … ▸ New with options**, then pick a **4-core / 16 GB** machine.

Do not accept the 2-core default. DataHub needs roughly 6 GB resident — GMS alone is
1.9 GB and OpenSearch 1.3 GB — and its CLI refuses to start below **13 GB free Docker
disk**. This repo's `.devcontainer/devcontainer.json` already requests 4 cores, so the
picker preselects it.

## 2. Start DataHub

```bash
make demo
```

That installs dependencies, boots the stack, ingests sample metadata with real
column-level lineage, and runs the gate. If you only want DataHub itself:

```bash
make datahub-up          # DATAHUB_MAPPED_GMS_PORT=8081 datahub docker quickstart
```

## 3. Turn on authentication *before* exposing anything

The quickstart ships with `METADATA_SERVICE_AUTH_ENABLED=false` — anonymous callers can
read and write. That is fine on localhost and unacceptable on a public URL.

Recreate GMS with the flag on, keeping its environment and the `datahub-gms` network
alias (the frontend resolves GMS by that name; without the alias, login fails with
`UnknownHostException: datahub-gms`). Then mint a token:

```bash
make token          # scripts/make_datahub_token.py → writes DATAHUB_GMS_TOKEN into .env
```

Verify the token is actually required — anonymous must fail:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8081/api/graphql \
  -H 'Content-Type: application/json' -d '{"query":"{ me { corpUser { username } } }"}'
# 401 expected. 200 means auth is still off — do not make the port public.
```

## 4. Make the port public

In the **Ports** panel: right-click the port → **Port Visibility → Public**, then
**Copy Local Address**. You get something like:

```
https://<codespace-name>-8081.app.github.dev
```

Expose **8081** (GMS) for CI. Expose **9002** as well only while you are demoing the UI.

> A private forwarded port is not reachable by GitHub Actions — the runner has no session
> cookie and gets an HTML sign-in page instead of JSON. "Public" is what makes it an API
> endpoint. That is also why step 3 is not optional.

## 5. Point the Action at it

Repository **Settings ▸ Secrets and variables ▸ Actions**:

| Secret | Value |
| --- | --- |
| `DATAHUB_MCP_URL` | the copied `https://…-8081.app.github.dev` address — **no `/mcp` suffix** |
| `DATAHUB_GMS_TOKEN` | the token from step 3 |
| `GROQ_API_KEY` | your Groq key (or `ANTHROPIC_API_KEY`) |

Open a pull request containing a schema change. The run now resolves real lineage instead
of printing the degraded banner, and writes its tags back to the Codespace DataHub.

Check it landed:

```bash
gh run view --log | grep "phase 2"
# phase 2: drop_column:… -> urn:li:dataset:(…) (13 downstream)
```

---

## The catch: hibernation

**A Codespace stops after 30 minutes idle** (default; configurable up to 4 hours in your
settings). When it stops, the URL dies and CI silently reverts to degraded runs — which is
correct behaviour, and confusing if you have forgotten why.

Worse, the forwarded URL **changes** if the Codespace is rebuilt, so the secret goes stale.

Practical rules:

- Start the Codespace before recording a demo or asking someone to review a PR.
- Raise the idle timeout in **Settings ▸ Codespaces ▸ Default idle timeout**.
- Treat this as demo infrastructure. A team running ContextCI for real points
  `DATAHUB_MCP_URL` at their existing DataHub — which is the normal case, since anyone who
  needs this tool already has a catalog.

## When it does not work

| Symptom | Cause |
| --- | --- |
| CI logs `DataHub unreachable` | Codespace hibernated, or the port is Private |
| Runner gets HTML instead of JSON | Port is Private — GitHub is serving its sign-in page |
| `401 Unauthorized` in CI | `DATAHUB_GMS_TOKEN` missing, expired, or from a different instance |
| `Table 'x' not found in DataHub catalog` | `DATAHUB_PLATFORM` mismatch — the sample data is `hive`, the default is `postgres` |
| Quickstart refuses to start | Under 13 GB free Docker disk — use a larger machine |
| `BuildIndicesStep` fails, `403 index_create_block_exception` | Disk hit OpenSearch's flood watermark. Free space, then clear the persistent block (see `CLAUDE.md`) |
