"""Tests for PR comment rendering."""

from src.github_reporter import COMMENT_MARKER, render_comment
from src.models import (
    AffectedAsset,
    BlastReport,
    ChangeType,
    ChangeVerdict,
    GeneratedFix,
    LineageContext,
    Owner,
    RecommendedAction,
    RiskLevel,
    RunResult,
    SchemaChange,
)


def _verdict(risk=RiskLevel.HIGH, action=RecommendedAction.BLOCK):
    change = SchemaChange(
        table="analytics.orders",
        column="customer_id",
        change_type=ChangeType.DROP_COLUMN,
        source_file="migrations/001.sql",
        source_line=12,
    )
    asset = AffectedAsset(
        urn="urn:li:dataset:(urn:li:dataPlatform:dbt,dim_customers,PROD)",
        name="dim_customers",
        type="dataset",
        column_level_confirmed=True,
        owners=[Owner(urn="urn:li:corpuser:jdoe", name="jdoe", email="jdoe@example.com")],
        glossary_terms=["PII"],
        risk=risk,
    )
    report = BlastReport(
        is_breaking=True,
        risk_level=risk,
        summary="Dropping `customer_id` breaks 1 dbt model.",
        affected_assets=[asset],
        generated_fixes=[
            GeneratedFix(
                file_path="migrations/compat.sql",
                language="sql",
                code="CREATE OR REPLACE VIEW x AS SELECT 1;",
                description="Compatibility view",
            )
        ],
        recommended_action=action,
        reasoning="Column-level lineage confirms the consumer.",
    )
    return ChangeVerdict(change=change, context=LineageContext(change=change), report=report)


def test_comment_carries_the_idempotency_marker():
    body = render_comment(RunResult(verdicts=[_verdict()]))
    assert body.startswith(COMMENT_MARKER)


def test_blocking_verdict_is_stated_up_front():
    body = render_comment(RunResult(verdicts=[_verdict()]))
    assert "Blocked" in body.split("###")[0]
    assert "🟠 High" in body


def test_blast_radius_and_fixes_are_rendered():
    body = render_comment(RunResult(verdicts=[_verdict()]))
    assert "dim_customers" in body
    assert "✅ confirmed" in body
    assert "CREATE OR REPLACE VIEW" in body
    assert "```sql" in body


def test_owners_are_not_at_mentioned_by_default(monkeypatch):
    """DataHub owner names are not necessarily GitHub handles."""
    monkeypatch.delenv("CONTEXTCI_MENTION_OWNERS", raising=False)
    body = render_comment(RunResult(verdicts=[_verdict()]))
    assert "@jdoe" not in body
    assert "**jdoe**" in body


def test_owners_are_mentioned_when_opted_in(monkeypatch):
    monkeypatch.setenv("CONTEXTCI_MENTION_OWNERS", "true")
    body = render_comment(RunResult(verdicts=[_verdict()]))
    assert "@jdoe" in body


def test_low_risk_assets_are_not_attributed_to_owners(monkeypatch):
    monkeypatch.setenv("CONTEXTCI_MENTION_OWNERS", "true")
    body = render_comment(RunResult(verdicts=[_verdict(risk=RiskLevel.LOW, action=RecommendedAction.APPROVE)]))
    assert "Downstream owners" not in body


def test_degraded_run_is_flagged():
    result = RunResult(verdicts=[_verdict()], degraded=True, degraded_reason="ConnectionError: refused")
    body = render_comment(result)
    assert "Degraded run" in body
    assert "ConnectionError: refused" in body


def test_no_changes_renders_a_clean_message():
    body = render_comment(RunResult())
    assert "No schema changes detected" in body


def test_datahub_links_are_added_when_configured():
    body = render_comment(RunResult(verdicts=[_verdict()]), datahub_url="http://localhost:9002")
    assert "http://localhost:9002/dataset/urn:li:dataset:" in body
