"""DataHub read/write client for ContextCI.

ContextCI treats DataHub as a two-way operating system: it *reads* column-level
lineage and governance context, then *writes* tags and notes back onto the graph
so the catalog records that a schema change is in flight.

Transport
---------
Reads and writes both go over the DataHub GMS API via the ``acryl-datahub`` SDK
(``DataHubGraph``), which is the same API the DataHub MCP Server exposes to
agents. Set ``DATAHUB_MCP_URL`` (or ``DATAHUB_GMS_URL``) to point at your
instance and ``DATAHUB_GMS_TOKEN`` for authenticated deployments.

Every method degrades gracefully: if DataHub is unreachable the client stays
constructed, ``available`` is False, reads return empty results and writes
return False. The agent then posts a warning instead of crashing the build.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Dict, List, Optional

from datahub.emitter.mce_builder import (
    make_dataset_urn,
    make_schema_field_urn,
    make_tag_urn,
)
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
from datahub.metadata.schema_classes import (
    AuditStampClass,
    GlobalTagsClass,
    InstitutionalMemoryClass,
    InstitutionalMemoryMetadataClass,
    SchemaMetadataClass,
    TagAssociationClass,
    TagPropertiesClass,
    UpstreamLineageClass,
)

from .models import (
    AffectedAsset,
    DatasetGovernance,
    LineageContext,
    Owner,
    RiskLevel,
    SchemaChange,
)

logger = logging.getLogger(__name__)

DEFAULT_MCP_URL = "http://localhost:3002"

_LINEAGE_QUERY = """
query contextciLineage($urn: String!, $direction: LineageDirection!, $count: Int!) {
  searchAcrossLineage(
    input: {urn: $urn, direction: $direction, query: "*", start: 0, count: $count}
  ) {
    total
    searchResults {
      degree
      entity {
        urn
        type
        ... on Dataset {
          name
          platform { name }
          properties { name }
          ownership { owners { owner {
            ... on CorpUser { urn username properties { displayName email } }
            ... on CorpGroup { urn name properties { displayName email } }
          } } }
          tags { tags { tag { urn properties { name } } } }
          glossaryTerms { terms { term { urn properties { name } } } }
        }
        ... on Dashboard {
          properties { name }
          ownership { owners { owner {
            ... on CorpUser { urn username properties { displayName email } }
            ... on CorpGroup { urn name properties { displayName email } }
          } } }
        }
        ... on Chart {
          properties { name }
          ownership { owners { owner {
            ... on CorpUser { urn username properties { displayName email } }
            ... on CorpGroup { urn name properties { displayName email } }
          } } }
        }
        ... on MLModel {
          name
          ownership { owners { owner {
            ... on CorpUser { urn username properties { displayName email } }
            ... on CorpGroup { urn name properties { displayName email } }
          } } }
        }
        ... on DataJob {
          properties { name }
          ownership { owners { owner {
            ... on CorpUser { urn username properties { displayName email } }
            ... on CorpGroup { urn name properties { displayName email } }
          } } }
        }
      }
    }
  }
}
"""

_GOVERNANCE_QUERY = """
query contextciGovernance($urn: String!) {
  dataset(urn: $urn) {
    urn
    properties { name description }
    editableProperties { description }
    ownership { owners { owner {
      ... on CorpUser { urn username properties { displayName email } }
      ... on CorpGroup { urn name properties { displayName email } }
    } } }
    tags { tags { tag { urn properties { name } } } }
    glossaryTerms { terms { term { urn properties { name } } } }
    schemaMetadata { fields { fieldPath } }
  }
}
"""


def _now_stamp() -> AuditStampClass:
    return AuditStampClass(time=int(time.time() * 1000), actor="urn:li:corpuser:contextci")


def _parse_owners(ownership: Optional[dict]) -> List[Owner]:
    owners: List[Owner] = []
    for entry in (ownership or {}).get("owners", []) or []:
        raw = entry.get("owner") or {}
        urn = raw.get("urn")
        if not urn:
            continue
        props = raw.get("properties") or {}
        is_group = urn.startswith("urn:li:corpGroup:")
        name = (
            props.get("displayName")
            or raw.get("username")
            or raw.get("name")
            or urn.rsplit(":", 1)[-1].strip("()")
        )
        owners.append(
            Owner(
                urn=urn,
                name=name,
                type="group" if is_group else "user",
                email=props.get("email"),
            )
        )
    return owners


def _parse_tags(tags: Optional[dict]) -> List[str]:
    out = []
    for entry in (tags or {}).get("tags", []) or []:
        tag = entry.get("tag") or {}
        props = tag.get("properties") or {}
        out.append(props.get("name") or (tag.get("urn", "").rsplit(":", 1)[-1]))
    return [t for t in out if t]


def _parse_terms(terms: Optional[dict]) -> List[str]:
    out = []
    for entry in (terms or {}).get("terms", []) or []:
        term = entry.get("term") or {}
        props = term.get("properties") or {}
        out.append(props.get("name") or (term.get("urn", "").rsplit(":", 1)[-1]))
    return [t for t in out if t]


class DataHubMCPClient:
    """Thin, failure-tolerant wrapper over the DataHub metadata graph."""

    def __init__(
        self,
        mcp_server_url: str = DEFAULT_MCP_URL,
        token: Optional[str] = None,
        timeout_sec: int = 30,
    ):
        self.server_url = (
            os.getenv("DATAHUB_MCP_URL") or os.getenv("DATAHUB_GMS_URL") or mcp_server_url
        ).rstrip("/")
        self.token = token or os.getenv("DATAHUB_GMS_TOKEN") or os.getenv("DATAHUB_TOKEN")
        self.available = False
        self.last_error: Optional[str] = None
        self.graph: Optional[DataHubGraph] = None
        self._urn_cache: Dict[str, Optional[str]] = {}

        try:
            self.graph = DataHubGraph(
                DatahubClientConfig(
                    server=self.server_url,
                    token=self.token or None,
                    timeout_sec=timeout_sec,
                )
            )
            self.graph.test_connection()
            self.available = True
            logger.info("Connected to DataHub at %s", self.server_url)
        except Exception as exc:  # noqa: BLE001 - degrade, never crash the CI job
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("DataHub unreachable at %s (%s)", self.server_url, self.last_error)

    # ------------------------------------------------------------------ reads

    def resolve_dataset_urn(
        self, table_name: str, platform: str = "postgres", env: str = "PROD"
    ) -> Optional[str]:
        """Map a table name from a diff to a DataHub dataset URN.

        Tries the deterministic URN first, then falls back to a catalog search so
        that partially-qualified names in SQL (``orders`` vs ``db.public.orders``)
        still resolve.
        """
        cache_key = f"{platform}|{env}|{table_name}"
        if cache_key in self._urn_cache:
            return self._urn_cache[cache_key]

        resolved: Optional[str] = None
        normalized = table_name.strip().strip('"`[]').lower()

        if self.available and self.graph:
            candidate = make_dataset_urn(platform=platform, name=normalized, env=env)
            try:
                if self.graph.exists(candidate):
                    resolved = candidate
            except Exception as exc:  # noqa: BLE001
                logger.debug("exists() failed for %s: %s", candidate, exc)

            if not resolved:
                resolved = self._search_dataset_urn(normalized, platform, env)

        self._urn_cache[cache_key] = resolved
        return resolved

    def _search_dataset_urn(self, table_name: str, platform: str, env: str) -> Optional[str]:
        """Find the best dataset whose name ends with the diff's table name."""
        leaf = table_name.rsplit(".", 1)[-1]
        try:
            candidates = list(
                self.graph.get_urns_by_filter(  # type: ignore[union-attr]
                    entity_types=["dataset"],
                    query=leaf,
                    batch_size=50,
                )
            )[:50]
        except Exception as exc:  # noqa: BLE001
            logger.debug("dataset search failed for %s: %s", table_name, exc)
            return None

        def score(urn: str) -> int:
            name = _dataset_name_from_urn(urn).lower()
            points = 0
            if name == table_name:
                points += 100
            if name.endswith("." + table_name) or name.endswith("." + leaf):
                points += 50
            if name.split(".")[-1] == leaf:
                points += 25
            if f"dataPlatform:{platform}," in urn:
                points += 10
            if urn.endswith(f",{env})"):
                points += 5
            return points

        ranked = sorted(candidates, key=score, reverse=True)
        if ranked and score(ranked[0]) > 0:
            return ranked[0]
        return None

    def get_column_lineage(self, dataset_urn: str, column_name: str) -> dict:
        """Return downstream assets that depend on ``column_name``.

        Downstreams are discovered with ``searchAcrossLineage``, then each
        downstream dataset's fine-grained (column-level) lineage is inspected to
        decide whether it genuinely consumes the changed column. Assets whose
        column-level lineage is unavailable are still returned, flagged with
        ``column_level_confirmed=False``, because absent lineage is not proof of
        safety.
        """
        empty = {"downstream_datasets": [], "dashboards": [], "ml_models": [], "data_jobs": [], "all": []}
        if not self.available or not self.graph:
            return empty

        results = self._search_lineage(dataset_urn, "DOWNSTREAM")
        field_urn = make_schema_field_urn(dataset_urn, column_name) if column_name else None

        assets: List[AffectedAsset] = []
        for asset in results:
            if field_urn and asset.type == "dataset":
                asset.column_level_confirmed = self._consumes_column(asset.urn, field_urn)
            assets.append(asset)

        buckets = {
            "downstream_datasets": [a for a in assets if a.type == "dataset"],
            "dashboards": [a for a in assets if a.type in ("dashboard", "chart")],
            "ml_models": [a for a in assets if a.type == "mlModel"],
            "data_jobs": [a for a in assets if a.type == "dataJob"],
            "all": assets,
        }
        return buckets

    def get_upstream_datasets(self, dataset_urn: str) -> List[AffectedAsset]:
        if not self.available or not self.graph:
            return []
        return self._search_lineage(dataset_urn, "UPSTREAM")

    def _search_lineage(self, dataset_urn: str, direction: str) -> List[AffectedAsset]:
        try:
            data = self.graph.execute_graphql(  # type: ignore[union-attr]
                _LINEAGE_QUERY,
                variables={"urn": dataset_urn, "direction": direction, "count": 100},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("lineage query failed for %s: %s", dataset_urn, exc)
            return self._scroll_lineage_fallback(dataset_urn, direction)

        out: List[AffectedAsset] = []
        for row in (data.get("searchAcrossLineage") or {}).get("searchResults", []) or []:
            entity = row.get("entity") or {}
            urn = entity.get("urn")
            if not urn or urn == dataset_urn:
                continue
            props = entity.get("properties") or {}
            name = props.get("name") or entity.get("name") or _dataset_name_from_urn(urn)
            out.append(
                AffectedAsset(
                    urn=urn,
                    name=name,
                    type=_normalize_entity_type(entity.get("type", "")),
                    platform=(entity.get("platform") or {}).get("name"),
                    owners=_parse_owners(entity.get("ownership")),
                    tags=_parse_tags(entity.get("tags")),
                    glossary_terms=_parse_terms(entity.get("glossaryTerms")),
                    degree=row.get("degree") or 1,
                )
            )
        return out

    def _scroll_lineage_fallback(self, dataset_urn: str, direction: str) -> List[AffectedAsset]:
        """URN-only lineage when the GraphQL schema differs from what we expect."""
        try:
            from datahub.ingestion.graph.openapi import LineageDirection

            result = self.graph.scroll_lineage(  # type: ignore[union-attr]
                urns=[dataset_urn],
                direction=LineageDirection[direction],
                count=100,
            )
            urns = {
                rel.urn
                for rel in getattr(result, "relationships", []) or []
                if getattr(rel, "urn", None) and rel.urn != dataset_urn
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("lineage fallback failed for %s: %s", dataset_urn, exc)
            return []

        return [
            AffectedAsset(
                urn=urn,
                name=_dataset_name_from_urn(urn),
                type=_type_from_urn(urn),
                degree=1,
            )
            for urn in sorted(urns)
        ]

    def _consumes_column(self, downstream_urn: str, upstream_field_urn: str) -> bool:
        """True when DataHub fine-grained lineage links the changed column here."""
        try:
            lineage = self.graph.get_aspect(downstream_urn, UpstreamLineageClass)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            logger.debug("fine-grained lineage unavailable for %s: %s", downstream_urn, exc)
            return False
        if not lineage or not lineage.fineGrainedLineages:
            return False
        for fine in lineage.fineGrainedLineages:
            if upstream_field_urn in (fine.upstreams or []):
                return True
        return False

    def get_dataset_governance(self, dataset_urn: str) -> dict:
        """Owners, glossary terms, tags, description and column list for a dataset."""
        blank = {
            "urn": dataset_urn,
            "owners": [],
            "glossary_terms": [],
            "tags": [],
            "description": None,
            "columns": [],
        }
        if not self.available or not self.graph:
            return blank

        try:
            data = self.graph.execute_graphql(_GOVERNANCE_QUERY, variables={"urn": dataset_urn})
        except Exception as exc:  # noqa: BLE001
            logger.warning("governance query failed for %s: %s", dataset_urn, exc)
            return self._governance_fallback(dataset_urn, blank)

        ds = data.get("dataset") or {}
        if not ds:
            return blank
        props = ds.get("properties") or {}
        editable = ds.get("editableProperties") or {}
        schema = ds.get("schemaMetadata") or {}
        return {
            "urn": dataset_urn,
            "owners": [o.model_dump() for o in _parse_owners(ds.get("ownership"))],
            "glossary_terms": _parse_terms(ds.get("glossaryTerms")),
            "tags": _parse_tags(ds.get("tags")),
            "description": editable.get("description") or props.get("description"),
            "columns": [f.get("fieldPath") for f in schema.get("fields", []) or [] if f.get("fieldPath")],
        }

    def _governance_fallback(self, dataset_urn: str, blank: dict) -> dict:
        """Aspect-level reads when GraphQL is unavailable."""
        out = dict(blank)
        try:
            ownership = self.graph.get_ownership(dataset_urn)  # type: ignore[union-attr]
            if ownership:
                out["owners"] = [
                    Owner(urn=o.owner, name=o.owner.rsplit(":", 1)[-1]).model_dump()
                    for o in ownership.owners or []
                ]
            tags = self.graph.get_tags(dataset_urn)  # type: ignore[union-attr]
            if tags:
                out["tags"] = [t.tag.rsplit(":", 1)[-1] for t in tags.tags or []]
            terms = self.graph.get_glossary_terms(dataset_urn)  # type: ignore[union-attr]
            if terms:
                out["glossary_terms"] = [t.urn.rsplit(":", 1)[-1] for t in terms.terms or []]
            schema = self.graph.get_aspect(dataset_urn, SchemaMetadataClass)  # type: ignore[union-attr]
            if schema:
                out["columns"] = [f.fieldPath for f in schema.fields or []]
        except Exception as exc:  # noqa: BLE001
            logger.debug("governance fallback failed for %s: %s", dataset_urn, exc)
        return out

    # ----------------------------------------------------------------- writes

    def apply_tag(self, dataset_urn: str, tag_urn: str) -> bool:
        """Add a tag to a dataset. Idempotent: re-running never duplicates tags."""
        if not self.available or not self.graph:
            return False
        try:
            current = self.graph.get_aspect(dataset_urn, GlobalTagsClass) or GlobalTagsClass(tags=[])
            if any(t.tag == tag_urn for t in current.tags or []):
                logger.info("tag %s already present on %s", tag_urn, dataset_urn)
                return True

            self._ensure_tag_exists(tag_urn)
            current.tags = list(current.tags or []) + [TagAssociationClass(tag=tag_urn)]
            self.graph.emit_mcp(
                MetadataChangeProposalWrapper(entityUrn=dataset_urn, aspect=current)
            )
            logger.info("tagged %s with %s", dataset_urn, tag_urn)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("apply_tag failed for %s: %s", dataset_urn, exc)
            return False

    def _ensure_tag_exists(self, tag_urn: str) -> None:
        """Give the tag a readable name so it renders properly in the DataHub UI."""
        try:
            if self.graph.exists(tag_urn):  # type: ignore[union-attr]
                return
            name = tag_urn.rsplit(":", 1)[-1]
            self.graph.emit_mcp(  # type: ignore[union-attr]
                MetadataChangeProposalWrapper(
                    entityUrn=tag_urn,
                    aspect=TagPropertiesClass(
                        name=name,
                        description="Applied by ContextCI during pull request schema validation.",
                    ),
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("could not create tag %s: %s", tag_urn, exc)

    def add_dataset_note(self, dataset_urn: str, note: str, url: Optional[str] = None) -> bool:
        """Attach a note about the pending change to the dataset.

        Stored as an institutional-memory link so the note is durable, visible in
        the DataHub UI, and keyed by the pull request URL — which makes repeated
        runs on the same PR idempotent.
        """
        if not self.available or not self.graph:
            return False
        link = url or "https://github.com/contextci/pending-schema-change"
        try:
            current = self.graph.get_aspect(
                dataset_urn, InstitutionalMemoryClass
            ) or InstitutionalMemoryClass(elements=[])
            elements = list(current.elements or [])
            for existing in elements:
                if existing.url == link:
                    if existing.description == note:
                        return True
                    existing.description = note
                    existing.updateStamp = _now_stamp()
                    break
            else:
                elements.append(
                    InstitutionalMemoryMetadataClass(
                        url=link, description=note, createStamp=_now_stamp()
                    )
                )
            current.elements = elements
            self.graph.emit_mcp(
                MetadataChangeProposalWrapper(entityUrn=dataset_urn, aspect=current)
            )
            logger.info("noted pending change on %s", dataset_urn)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("add_dataset_note failed for %s: %s", dataset_urn, exc)
            return False

    # ------------------------------------------------------------ orchestration

    def build_lineage_context(
        self, change: SchemaChange, platform: str = "postgres", env: str = "PROD"
    ) -> LineageContext:
        """Phase 2 entrypoint: gather everything DataHub knows about one change."""
        ctx = LineageContext(change=change)
        if not self.available:
            ctx.errors.append(f"DataHub unreachable: {self.last_error}")
            return ctx

        urn = self.resolve_dataset_urn(change.table, platform=platform, env=env)
        if not urn:
            ctx.errors.append(f"Table '{change.table}' not found in DataHub catalog")
            return ctx

        ctx.dataset_urn = urn
        ctx.resolved = True

        gov = self.get_dataset_governance(urn)
        ctx.governance = DatasetGovernance(
            urn=urn,
            description=gov.get("description"),
            owners=[Owner(**o) for o in gov.get("owners", [])],
            glossary_terms=gov.get("glossary_terms", []),
            tags=gov.get("tags", []),
            columns=gov.get("columns", []),
        )

        lineage = self.get_column_lineage(urn, change.column or "")
        ctx.downstream = lineage["all"]
        ctx.upstream = self.get_upstream_datasets(urn)
        return ctx

    def write_back(
        self,
        source_urn: str,
        downstream_urns: List[str],
        risk_level: RiskLevel,
        note: str,
        pr_url: Optional[str] = None,
    ) -> Dict[str, bool]:
        """Phase 4B: mutate the graph so DataHub records the in-flight change."""
        results: Dict[str, bool] = {}
        results[f"tag:{source_urn}"] = self.apply_tag(
            source_urn, make_tag_urn("Schema-Change-Pending")
        )
        results[f"note:{source_urn}"] = self.add_dataset_note(source_urn, note, url=pr_url)

        risk_tag = make_tag_urn(f"Blast-Risk-{risk_level.value.capitalize()}")
        for urn in downstream_urns:
            results[f"tag:{urn}"] = self.apply_tag(urn, risk_tag)
        return results

    def close(self) -> None:
        if self.graph:
            try:
                self.graph.close()
            except Exception:  # noqa: BLE001
                pass


def _dataset_name_from_urn(urn: str) -> str:
    """Pull the dataset name out of a dataset URN."""
    match = re.match(r"urn:li:dataset:\(urn:li:dataPlatform:[^,]+,(.+),[^,]+\)$", urn)
    return match.group(1) if match else urn.rsplit(":", 1)[-1].strip("()")


def _type_from_urn(urn: str) -> str:
    parts = urn.split(":")
    return parts[2] if len(parts) > 2 else "dataset"


def _normalize_entity_type(graphql_type: str) -> str:
    mapping = {
        "DATASET": "dataset",
        "DASHBOARD": "dashboard",
        "CHART": "chart",
        "MLMODEL": "mlModel",
        "DATA_JOB": "dataJob",
    }
    return mapping.get(graphql_type.upper(), graphql_type.lower() or "dataset")
