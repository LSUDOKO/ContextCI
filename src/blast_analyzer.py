"""Phase 3a — decide whether a schema change breaks anything, and how badly.

The LLM sees the change plus the DataHub lineage and governance context and
returns a structured verdict. When no API key is configured, or the model call
fails, a deterministic rule-based analysis takes over so the gate still produces
a usable answer instead of crashing the build.
"""

from __future__ import annotations

import logging
import os
from typing import List, Optional

from pydantic import BaseModel, Field

from .code_generator import FIX_GUIDANCE, template_fixes
from .models import (
    AffectedAsset,
    BlastReport,
    ChangeType,
    GeneratedFix,
    GovernanceGate,
    LineageContext,
    RecommendedAction,
    RiskLevel,
    SchemaChange,
)

logger = logging.getLogger(__name__)

MODEL = os.getenv("CONTEXTCI_MODEL", "claude-opus-5")
MAX_TOKENS = 16000

SYSTEM_PROMPT = """\
You are ContextCI, a data reliability engineer reviewing a schema change in a pull request.

You are given one schema change and the real lineage and governance context for
it from DataHub: which datasets, dashboards, ML models and jobs read from the
table, whether column-level lineage proves they read the specific column being
changed, who owns them, and what glossary terms and tags they carry.

Judge the change on that evidence:

- A change is breaking if any downstream asset reads the affected column or table.
- Column-level lineage that is confirmed is strong evidence. An unconfirmed
  downstream asset means DataHub could not prove the column is used, not that it
  is safe — weigh it lower, but do not ignore it.
- Glossary terms such as PII, GDPR-Sensitive or Revenue-Critical raise the risk
  level, as do dashboards and ML models, which fail silently rather than loudly.
- Adding a column is almost never breaking.

Risk levels: low (nothing downstream, or purely additive), medium (a small
number of unconfirmed downstream datasets), high (confirmed column-level
consumers, or a dashboard/ML model), critical (confirmed consumers carrying PII
or revenue-critical terms, or a dropped table with live readers).

Recommended action: block for high and critical, warn for medium, approve for low.

A separate compliance gate runs after you: if the column or anything downstream
carries a PII, GDPR, PHI or Tier-1 marker, the change is blocked for security
review regardless of your verdict. Judge the engineering blast radius; you do not
need to enforce that rule yourself, but do call out regulated data in your summary.

""" + FIX_GUIDANCE


class _LLMFix(BaseModel):
    file_path: str = Field(description="Repo-relative path where this migration should live")
    language: str = Field(description="sql, yaml, python or markdown")
    description: str = Field(description="One sentence on what this fix does and who it unblocks")
    code: str = Field(description="Complete, runnable code — not a sketch")


class _LLMVerdict(BaseModel):
    """Lean schema for the model. Asset details are merged in from DataHub."""

    is_breaking: bool
    risk_level: RiskLevel
    summary: str = Field(description="One or two sentences a reviewer can read at a glance")
    reasoning: str = Field(description="Why this risk level, citing the specific downstream assets")
    breaking_asset_urns: List[str] = Field(
        default_factory=list,
        description="URNs of the downstream assets this change actually breaks",
    )
    generated_fixes: List[_LLMFix] = Field(default_factory=list)
    recommended_action: RecommendedAction


def _render_context(change: SchemaChange, context: LineageContext) -> str:
    lines = [
        "## Schema change",
        f"- type: {change.change_type.value}",
        f"- table: {change.table}",
        f"- column: {change.column or '(table-level change)'}",
    ]
    if change.new_value:
        lines.append(f"- new name: {change.new_value}")
    if change.new_type:
        lines.append(f"- new type: {change.new_type}")
    lines.append(f"- found in: {change.source_file}" + (f":{change.source_line}" if change.source_line else ""))
    if change.raw_statement:
        lines.append(f"- statement: {change.raw_statement}")

    lines.append("\n## DataHub context")
    if not context.resolved:
        lines.append("- table NOT found in the DataHub catalog; lineage is unknown")
        for err in context.errors:
            lines.append(f"- note: {err}")
        return "\n".join(lines)

    lines.append(f"- dataset urn: {context.dataset_urn}")
    gov = context.governance
    if gov:
        lines.append(f"- description: {gov.description or '(none)'}")
        lines.append(f"- owners: {', '.join(o.name for o in gov.owners) or '(none)'}")
        lines.append(f"- glossary terms: {', '.join(gov.glossary_terms) or '(none)'}")
        lines.append(f"- tags: {', '.join(gov.tags) or '(none)'}")
        if gov.columns:
            lines.append(f"- columns: {', '.join(gov.columns[:40])}")

    profile = context.profile
    if profile and (profile.row_count is not None or profile.column_count is not None):
        lines.append("\n## Dataset profile")
        lines.append(f"- rows: {profile.row_count if profile.row_count is not None else 'unknown'}")
        lines.append(f"- columns: {profile.column_count if profile.column_count is not None else 'unknown'}")
        if profile.size_in_bytes is not None:
            lines.append(f"- size: {profile.size_in_bytes} bytes")
        if profile.column_null_fraction is not None:
            lines.append(f"- `{change.column}` null fraction: {profile.column_null_fraction:.3f}")
        if profile.column_distinct_count is not None:
            lines.append(f"- `{change.column}` distinct values: {profile.column_distinct_count}")

    usage = context.usage
    if usage and (usage.total_queries or usage.top_queries):
        lines.append("\n## Query history (from DataHub usage stats)")
        lines.append(f"- total queries in window: {usage.total_queries}")
        lines.append(f"- unique users: {usage.unique_users}")
        if usage.column_query_count is not None:
            lines.append(f"- queries touching `{change.column}`: {usage.column_query_count}")
        if usage.top_queries:
            lines.append("- representative SQL people actually run against this table:")
            for query in usage.top_queries:
                collapsed = " ".join(query.split())
                lines.append(f"  ```sql\n  {collapsed[:800]}\n  ```")

    lines.append(f"\n## Downstream assets ({len(context.downstream)})")
    if not context.downstream:
        lines.append("- none recorded in DataHub")
    for asset in context.downstream:
        owners = ", ".join(o.name for o in asset.owners) or "unowned"
        terms = ", ".join(asset.glossary_terms) or "no terms"
        confirmed = "column-level CONFIRMED" if asset.column_level_confirmed else "table-level only"
        lines.append(
            f"- [{asset.type}] {asset.name} ({confirmed}, {asset.degree} hop(s)) "
            f"| urn: {asset.urn} | owners: {owners} | terms: {terms}"
        )

    if context.upstream:
        lines.append(f"\n## Upstream assets ({len(context.upstream)})")
        for asset in context.upstream[:10]:
            lines.append(f"- [{asset.type}] {asset.name}")

    return "\n".join(lines)


# Terms and tags that make a destructive change a compliance question, not just
# an engineering one. Matched case-insensitively as substrings so "GDPR-Sensitive"
# and "pii_email" both hit.
SENSITIVE_MARKERS = (
    "pii", "gdpr", "phi", "hipaa", "pci", "sensitive", "confidential", "personal-data",
)
TIER1_MARKERS = ("tier1", "tier-1", "tier_1", "critical", "regulated", "sox")

DESTRUCTIVE = (
    ChangeType.DROP_COLUMN,
    ChangeType.RENAME_COLUMN,
    ChangeType.MODIFY_COLUMN,
    ChangeType.DROP_TABLE,
    ChangeType.RENAME_TABLE,
)


def _matches(labels, markers) -> List[str]:
    hits = []
    for label in labels:
        low = label.lower().replace(" ", "-")
        if any(marker in low for marker in markers):
            hits.append(label)
    return hits


def evaluate_governance_gate(change: SchemaChange, context: LineageContext) -> GovernanceGate:
    """Decide whether this change needs human sign-off before it can merge.

    Dropping a column that carries a PII glossary term, or one feeding a Tier-1
    asset, is a compliance decision. ContextCI does not let the risk heuristics
    wave that through — the gate forces a block regardless of downstream count.
    """
    gate = GovernanceGate()
    if change.change_type not in DESTRUCTIVE:
        return gate

    source_labels: List[str] = []
    if context.governance:
        source_labels = list(context.governance.glossary_terms) + list(context.governance.tags)

    sensitive = _matches(source_labels, SENSITIVE_MARKERS)
    tier1_assets: List[str] = []
    if _matches(source_labels, TIER1_MARKERS):
        tier1_assets.append(context.governance.urn if context.governance else change.table)

    for asset in context.downstream:
        labels = list(asset.glossary_terms) + list(asset.tags)
        asset_sensitive = _matches(labels, SENSITIVE_MARKERS)
        if asset_sensitive:
            sensitive.extend(asset_sensitive)
        if _matches(labels, TIER1_MARKERS):
            tier1_assets.append(asset.name)

    gate.sensitive_terms = sorted(set(sensitive))
    gate.tier1_assets = sorted(set(tier1_assets))

    verb = change.change_type.value.replace("_", " ")
    target = f"{change.table}.{change.column}" if change.column else change.table
    if gate.sensitive_terms:
        gate.reasons.append(
            f"`{target}` or an asset downstream of it carries "
            f"{', '.join(gate.sensitive_terms)}; a {verb} on regulated data needs security sign-off."
        )
    if gate.tier1_assets:
        gate.reasons.append(
            f"Tier-1 asset(s) affected: {', '.join(gate.tier1_assets)}. "
            "Tier-1 assets require an approved change record before a schema change merges."
        )
    gate.requires_security_review = bool(gate.reasons)
    return gate


def _apply_gate(report: BlastReport, gate: GovernanceGate) -> BlastReport:
    """A governance gate overrides a softer verdict — never the other way round."""
    report.governance_gate = gate
    if not gate.requires_security_review:
        return report
    report.recommended_action = RecommendedAction.BLOCK
    if _RISK_ORDER.index(report.risk_level) < _RISK_ORDER.index(RiskLevel.HIGH):
        report.risk_level = RiskLevel.HIGH
    report.reasoning = " ".join(filter(None, [report.reasoning, *gate.reasons]))
    return report


def analyze(change: SchemaChange, context: LineageContext) -> BlastReport:
    """Produce a blast report for one schema change."""
    gate = evaluate_governance_gate(change, context)
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if api_key:
        report = _analyze_with_llm(change, context)
        if report:
            return _apply_gate(report, gate)
        logger.warning("falling back to rule-based analysis for %s", change.identity)
    else:
        logger.info("ANTHROPIC_API_KEY not set; using rule-based analysis")
    return _apply_gate(_analyze_with_rules(change, context), gate)


def _analyze_with_llm(change: SchemaChange, context: LineageContext) -> Optional[BlastReport]:
    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic SDK not installed; using rule-based analysis")
        return None

    try:
        client = anthropic.Anthropic()
        response = client.messages.parse(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _render_context(change, context)}],
            output_format=_LLMVerdict,
        )
        if response.stop_reason == "refusal":
            logger.warning("model declined to analyze %s", change.identity)
            return None
        verdict = response.parsed_output
    except Exception as exc:  # noqa: BLE001 - never fail the build on the LLM
        logger.warning("LLM analysis failed for %s: %s", change.identity, exc)
        return None

    if verdict is None:
        return None

    breaking = set(verdict.breaking_asset_urns)
    affected = [
        _with_risk(asset, verdict.risk_level if asset.urn in breaking else RiskLevel.LOW)
        for asset in context.downstream
    ]
    fixes = [
        GeneratedFix(
            file_path=f.file_path,
            language=f.language,
            code=f.code,
            description=f.description,
            target_asset=context.dataset_urn,
        )
        for f in verdict.generated_fixes
    ]
    if verdict.is_breaking and not fixes:
        fixes = template_fixes(change, context)

    return BlastReport(
        is_breaking=verdict.is_breaking,
        risk_level=verdict.risk_level,
        summary=verdict.summary,
        affected_assets=affected,
        generated_fixes=fixes,
        recommended_action=verdict.recommended_action,
        reasoning=verdict.reasoning,
    )


_RISK_ORDER = [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL]
# Escalation markers are broader than the compliance gate: revenue-critical raises
# risk but is an engineering call, not a security review.
_ESCALATING_MARKERS = SENSITIVE_MARKERS + TIER1_MARKERS + ("revenue-critical", "revenue")
_LOUD_FAILURE_TYPES = {"dashboard", "chart", "mlmodel"}


def _escalate(level: RiskLevel, steps: int = 1) -> RiskLevel:
    return _RISK_ORDER[min(_RISK_ORDER.index(level) + steps, len(_RISK_ORDER) - 1)]


def _with_risk(asset: AffectedAsset, level: RiskLevel) -> AffectedAsset:
    updated = asset.model_copy()
    updated.risk = level
    return updated


def _analyze_with_rules(change: SchemaChange, context: LineageContext) -> BlastReport:
    """Deterministic analysis, used when the LLM is unavailable.

    Deliberately errs toward caution: an unresolved table or an unconfirmed
    downstream asset is treated as a real risk, because absent lineage is not
    evidence of safety.
    """
    if change.change_type is ChangeType.ADD_COLUMN:
        return BlastReport(
            is_breaking=False,
            risk_level=RiskLevel.LOW,
            summary=f"Adding `{change.column}` to `{change.table}` is additive and does not break readers.",
            recommended_action=RecommendedAction.APPROVE,
            reasoning="Additive schema changes leave existing queries valid.",
        )

    if not context.resolved:
        return BlastReport(
            is_breaking=False,
            risk_level=RiskLevel.MEDIUM,
            summary=(
                f"`{change.table}` is not in the DataHub catalog, so the blast radius of this "
                f"{change.change_type.value.replace('_', ' ')} could not be verified."
            ),
            generated_fixes=template_fixes(change, context),
            recommended_action=RecommendedAction.WARN,
            reasoning="; ".join(context.errors) or "Table could not be resolved to a dataset URN.",
        )

    downstream = context.downstream
    confirmed = [a for a in downstream if a.column_level_confirmed]
    relevant = confirmed or downstream

    if not downstream:
        return BlastReport(
            is_breaking=False,
            risk_level=RiskLevel.LOW,
            summary=f"No downstream assets read `{change.table}` according to DataHub.",
            recommended_action=RecommendedAction.APPROVE,
            reasoning="DataHub records no downstream lineage for this dataset.",
        )

    count = len(relevant)
    if count <= 2:
        risk = RiskLevel.MEDIUM
    elif count <= 5:
        risk = RiskLevel.HIGH
    else:
        risk = RiskLevel.CRITICAL

    if confirmed:
        risk = _escalate(risk)
    if any(a.type in _LOUD_FAILURE_TYPES for a in relevant):
        risk = _escalate(risk)

    labels = [t for a in relevant for t in a.glossary_terms + a.tags]
    if context.governance:
        labels += context.governance.glossary_terms + context.governance.tags
    if _matches(labels, _ESCALATING_MARKERS):
        risk = _escalate(risk)

    if change.change_type is ChangeType.DROP_TABLE:
        risk = RiskLevel.CRITICAL

    action = (
        RecommendedAction.BLOCK
        if risk in (RiskLevel.HIGH, RiskLevel.CRITICAL)
        else RecommendedAction.WARN
    )
    target = f"`{change.table}.{change.column}`" if change.column else f"`{change.table}`"
    verb = change.change_type.value.replace("_", " ").capitalize()

    return BlastReport(
        is_breaking=True,
        risk_level=risk,
        summary=(
            f"{verb} on {target} affects {count} downstream asset(s), "
            f"{len(confirmed)} of which DataHub confirms read this column."
        ),
        affected_assets=[_with_risk(a, risk if a in relevant else RiskLevel.LOW) for a in downstream],
        generated_fixes=template_fixes(change, context),
        recommended_action=action,
        reasoning=(
            "Rule-based analysis (no LLM available): risk scaled by downstream count, "
            "column-level confirmation, asset type and governance terms."
        ),
    )
