#!/usr/bin/env bash
# One-command ContextCI demo: boot DataHub, load lineage, run the gate.
#
#     ./scripts/demo.sh          # or: make demo
#
# Safe to re-run. Every step is skipped if it is already done.
set -euo pipefail

GMS_PORT="${DATAHUB_MAPPED_GMS_PORT:-8081}"
export GMS_URL="http://localhost:${GMS_PORT}"   # exported: the inline python below reads it
UI_PORT=9002
DIFF="${1:-examples/demo_sample_hive.diff}"
SAMPLE_MCE_URL="https://raw.githubusercontent.com/datahub-project/datahub/master/metadata-ingestion/examples/mce_files/bootstrap_mce.json"

# A fresh quickstart is unauthenticated, but an instance with
# METADATA_SERVICE_AUTH_ENABLED=true needs a token for every call — including the
# sample-data ingest. Pick it up from .env when one is there.
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

bold() { printf "\n\033[1m%s\033[0m\n" "$*"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }

# Inside a Codespace, localhost URLs are useless to the reader — print the
# forwarded https URL instead.
public_url() {
  local port="$1"
  if [ -n "${CODESPACE_NAME:-}" ]; then
    echo "https://${CODESPACE_NAME}-${port}.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"
  else
    echo "http://localhost:${port}"
  fi
}

bold "1/4  Checking prerequisites"
command -v docker >/dev/null || { echo "docker not found"; exit 1; }
docker info >/dev/null 2>&1 || { echo "docker daemon not reachable"; exit 1; }
ok "docker ready"

FREE_GB=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
if [ "${FREE_GB:-0}" -lt 13 ]; then
  warn "only ${FREE_GB}GB free — DataHub's quickstart requires 13GB and will refuse to start."
  warn "Free space, or use a larger Codespace machine type."
fi

PY=python3
[ -x .venv/bin/python ] && PY=.venv/bin/python
$PY -c "import datahub" 2>/dev/null || {
  warn "installing dependencies (first run only)"
  $PY -m pip install -q -r requirements.txt
}
ok "python deps ready ($($PY --version))"

bold "2/4  Starting DataHub  (first run pulls ~10GB, allow 10-15 min)"
if curl -sf -m 5 "${GMS_URL}/config" >/dev/null 2>&1; then
  ok "already running at ${GMS_URL}"
else
  DATAHUB_MAPPED_GMS_PORT="${GMS_PORT}" $PY -m datahub docker quickstart
  printf "  waiting for GMS"
  until curl -sf -m 5 "${GMS_URL}/config" >/dev/null 2>&1; do printf "."; sleep 5; done
  echo
  ok "GMS healthy at ${GMS_URL}"
fi

bold "3/4  Loading sample metadata (7 datasets with real lineage)"
DATASET_URN="urn:li:dataset:(urn:li:dataPlatform:hive,SampleHiveDataset,PROD)"
if $PY - "$DATASET_URN" <<'PY' 2>/dev/null
import sys, os
from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
g = DataHubGraph(DatahubClientConfig(server=os.environ["GMS_URL"], token=os.getenv("DATAHUB_GMS_TOKEN") or None))
raise SystemExit(0 if g.exists(sys.argv[1]) else 1)
PY
then
  ok "sample metadata already ingested"
else
  curl -fsSL -o /tmp/bootstrap_mce.json "$SAMPLE_MCE_URL"
  cat > /tmp/contextci_recipe.yml <<YAML
source:
  type: file
  config: {path: /tmp/bootstrap_mce.json}
sink:
  type: datahub-rest
  config:
    server: ${GMS_URL}
$( [ -n "${DATAHUB_GMS_TOKEN:-}" ] && echo "    token: ${DATAHUB_GMS_TOKEN}" )
YAML
  $PY -m datahub ingest -c /tmp/contextci_recipe.yml >/dev/null
  ok "ingested sample lineage"
fi

bold "4/4  Running the gate against a breaking schema change"
echo "  diff: ${DIFF}"
grep -E '^\+.*ALTER TABLE' "$DIFF" | sed 's/^+/      /'
echo

set +e
DATAHUB_GMS_URL="${GMS_URL}" DATAHUB_PLATFORM=hive \
  DATAHUB_FRONTEND_URL="$(public_url $UI_PORT)" \
  TOOLS_IS_MUTATION_ENABLED=true \
  $PY -m src.main --diff "$DIFF"
VERDICT=$?
set -e

bold "Done"
echo "  Exit code ${VERDICT} — 1 means the gate would block the merge."
echo "  DataHub UI:  $(public_url $UI_PORT)   (login datahub / datahub)"
echo "  Search 'SampleHiveDataset' to see the tags ContextCI just wrote:"
echo "    · dataset  → Schema-Change-Pending, PR-Under-Review"
echo "    · column   → field_foo tagged in the Schema tab"
echo "    · upstream → Blast-Risk-* on each affected asset"
exit "$VERDICT"
