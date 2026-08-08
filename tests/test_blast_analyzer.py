"""Tests for the rule-based analysis path (the LLM path is exercised end-to-end)."""

import pytest

from src.blast_analyzer import _analyze_with_rules, _render_context
from src.models import (
    AffectedAsset,
    ChangeType,
    DatasetGovernance,
    DatasetProfile,
    LineageContext,
    RecommendedAction,
    RiskLevel,
    SchemaChange,
    UsageStats,
)


def _change(change_type=ChangeType.DROP_COLUMN, column="customer_id"):
    return SchemaChange(
        table="analytics.orders",
        column=column,
        change_type=change_type,
        source_file="migrations/001.sql",
    )


def _context(change, downstream=None, resolved=True, terms=None):
    return LineageContext(
        change=change,
        dataset_urn="urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.orders,PROD)" if resolved else None,
        resolved=resolved,
        governance=DatasetGovernance(urn="urn:x", glossary_terms=terms or []) if resolved else None,
        downstream=downstream or [],
        errors=[] if resolved else ["Table 'analytics.orders' not found in DataHub catalog"],
    )


def _asset(name, type_="dataset", confirmed=False, terms=None):
    return AffectedAsset(
        urn=f"urn:li:{type_}:{name}",
        name=name,
        type=type_,
        column_level_confirmed=confirmed,
        glossary_terms=terms or [],
    )


def test_added_column_is_never_breaking():
    change = _change(ChangeType.ADD_COLUMN, "loyalty_tier")
    report = _analyze_with_rules(change, _context(change))
    assert report.is_breaking is False
    assert report.recommended_action is RecommendedAction.APPROVE


def test_no_downstream_approves():
    change = _change()
    report = _analyze_with_rules(change, _context(change))
    assert report.recommended_action is RecommendedAction.APPROVE
    assert report.risk_level is RiskLevel.LOW


def test_unresolved_table_warns_rather_than_approving():
    """Absent lineage is not evidence of safety."""
    change = _change()
    report = _analyze_with_rules(change, _context(change, resolved=False))
    assert report.recommended_action is RecommendedAction.WARN
    assert report.risk_level is RiskLevel.MEDIUM
    assert report.generated_fixes, "a fallback migration should still be offered"


def test_two_unconfirmed_datasets_warn():
    change = _change()
    ctx = _context(change, downstream=[_asset("dbt_a"), _asset("dbt_b")])
    report = _analyze_with_rules(change, ctx)
    assert report.is_breaking is True
    assert report.risk_level is RiskLevel.MEDIUM
    assert report.recommended_action is RecommendedAction.WARN


def test_column_level_confirmation_escalates_to_block():
    change = _change()
    ctx = _context(change, downstream=[_asset("dbt_a", confirmed=True)])
    report = _analyze_with_rules(change, ctx)
    assert report.risk_level is RiskLevel.HIGH
    assert report.recommended_action is RecommendedAction.BLOCK


def test_dashboard_escalates_risk():
    """Dashboards fail silently, so they weigh heavier than another table."""
    change = _change()
    ctx = _context(change, downstream=[_asset("exec_revenue", type_="dashboard")])
    report = _analyze_with_rules(change, ctx)
    assert report.risk_level is RiskLevel.HIGH


def test_pii_term_escalates_to_critical():
    change = _change()
    ctx = _context(change, downstream=[_asset("dbt_a", confirmed=True)], terms=["PII"])
    report = _analyze_with_rules(change, ctx)
    assert report.risk_level is RiskLevel.CRITICAL
    assert report.recommended_action is RecommendedAction.BLOCK


def test_drop_table_with_readers_is_always_critical():
    change = SchemaChange(
        table="analytics.orders",
        change_type=ChangeType.DROP_TABLE,
        source_file="migrations/002.sql",
    )
    ctx = _context(change, downstream=[_asset("dbt_a")])
    report = _analyze_with_rules(change, ctx)
    assert report.risk_level is RiskLevel.CRITICAL
    assert report.recommended_action is RecommendedAction.BLOCK


def test_confirmed_downstream_narrows_the_relevant_set():
    """When column-level lineage exists, unconfirmed neighbours don't inflate the count."""
    change = _change()
    downstream = [_asset("dbt_a", confirmed=True)] + [_asset(f"dbt_{i}") for i in range(6)]
    report = _analyze_with_rules(change, _context(change, downstream=downstream))
    # One confirmed consumer -> medium, escalated once for confirmation -> high.
    assert report.risk_level is RiskLevel.HIGH


def test_prompt_carries_profile_and_query_history():
    """The model must see real usage, not just the schema."""
    change = _change()
    ctx = _context(change, downstream=[_asset("dbt_a", confirmed=True)])
    ctx.profile = DatasetProfile(row_count=4_200_000, column_count=18, column_null_fraction=0.02)
    ctx.usage = UsageStats(
        total_queries=931,
        unique_users=17,
        column_query_count=604,
        top_queries=["SELECT customer_id, SUM(total)\n  FROM analytics.orders GROUP BY 1"],
    )
    prompt = _render_context(change, ctx)
    assert "rows: 4200000" in prompt
    assert "queries touching `customer_id`: 604" in prompt
    assert "SELECT customer_id, SUM(total) FROM analytics.orders GROUP BY 1" in prompt
    assert "null fraction: 0.020" in prompt


def test_prompt_omits_profile_sections_when_datahub_has_none():
    change = _change()
    prompt = _render_context(change, _context(change))
    assert "Dataset profile" not in prompt
    assert "Query history" not in prompt


@pytest.mark.parametrize(
    "change_type,column",
    [(ChangeType.DROP_COLUMN, "customer_id"), (ChangeType.RENAME_COLUMN, "customer_id")],
)
def test_breaking_changes_always_carry_a_fix(change_type, column):
    change = SchemaChange(
        table="analytics.orders",
        column=column,
        change_type=change_type,
        new_value="cust_id" if change_type is ChangeType.RENAME_COLUMN else None,
        source_file="migrations/003.sql",
    )
    ctx = _context(change, downstream=[_asset("dbt_a", confirmed=True)])
    report = _analyze_with_rules(change, ctx)
    assert report.generated_fixes
    assert report.generated_fixes[0].code.strip()


def test_model_may_escalate_above_the_rule_floor():
    from src.blast_analyzer import _apply_floor
    from src.models import BlastReport

    model = BlastReport(is_breaking=True, risk_level=RiskLevel.CRITICAL, summary="s",
                        recommended_action=RecommendedAction.BLOCK)
    rules = BlastReport(is_breaking=True, risk_level=RiskLevel.MEDIUM, summary="s",
                        recommended_action=RecommendedAction.WARN)
    out = _apply_floor(model, rules)
    assert out.risk_level is RiskLevel.CRITICAL
    assert out.recommended_action is RecommendedAction.BLOCK


def test_model_cannot_rule_a_change_safer_than_the_rules():
    """Hosted inference is not reproducible; the gate's decision must be."""
    from src.blast_analyzer import _apply_floor
    from src.models import BlastReport, GeneratedFix

    model = BlastReport(is_breaking=False, risk_level=RiskLevel.LOW, summary="looks fine",
                        recommended_action=RecommendedAction.APPROVE)
    rules = BlastReport(
        is_breaking=True, risk_level=RiskLevel.HIGH, summary="13 downstream",
        recommended_action=RecommendedAction.BLOCK,
        generated_fixes=[GeneratedFix(file_path="c.sql", code="SELECT 1;", description="d")],
    )
    out = _apply_floor(model, rules)
    assert out.risk_level is RiskLevel.HIGH
    assert out.recommended_action is RecommendedAction.BLOCK
    assert out.is_breaking is True
    assert out.summary == "looks fine", "the model still writes the narrative"
    assert out.generated_fixes, "a breaking verdict must carry a migration"


def test_flooring_also_restores_the_per_asset_risk():
    """A critical verdict must not leave the blast radius unflagged."""
    from src.blast_analyzer import _apply_floor
    from src.models import BlastReport

    hot = _asset("dbt_a", confirmed=True)
    hot.risk = RiskLevel.CRITICAL
    model = BlastReport(is_breaking=False, risk_level=RiskLevel.LOW, summary="fine",
                        recommended_action=RecommendedAction.APPROVE, affected_assets=[])
    rules = BlastReport(is_breaking=True, risk_level=RiskLevel.CRITICAL, summary="13 downstream",
                        recommended_action=RecommendedAction.BLOCK, affected_assets=[hot])
    out = _apply_floor(model, rules)
    assert [a.risk for a in out.affected_assets] == [RiskLevel.CRITICAL]
