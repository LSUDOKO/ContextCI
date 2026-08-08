"""Tests for the pre-merge compliance gate."""

from src.blast_analyzer import analyze, evaluate_governance_gate
from src.models import (
    AffectedAsset,
    ChangeType,
    DatasetGovernance,
    LineageContext,
    RecommendedAction,
    RiskLevel,
    SchemaChange,
)


def _change(change_type=ChangeType.DROP_COLUMN, column="email"):
    return SchemaChange(
        table="analytics.users",
        column=column,
        change_type=change_type,
        source_file="migrations/010.sql",
    )


def _context(change, source_terms=None, source_tags=None, downstream=None):
    return LineageContext(
        change=change,
        dataset_urn="urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.users,PROD)",
        resolved=True,
        governance=DatasetGovernance(
            urn="urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.users,PROD)",
            glossary_terms=source_terms or [],
            tags=source_tags or [],
        ),
        downstream=downstream or [],
    )


def _asset(name, terms=None, tags=None):
    return AffectedAsset(urn=f"urn:li:dataset:{name}", name=name, type="dataset",
                         glossary_terms=terms or [], tags=tags or [])


def test_pii_term_on_source_requires_security_review():
    change = _change()
    gate = evaluate_governance_gate(change, _context(change, source_terms=["PII"]))
    assert gate.requires_security_review is True
    assert gate.sensitive_terms == ["PII"]


def test_marker_matching_is_case_and_separator_insensitive():
    change = _change()
    gate = evaluate_governance_gate(change, _context(change, source_terms=["GDPR Sensitive"]))
    assert gate.requires_security_review is True


def test_downstream_pii_also_gates():
    """The column may be clean; what it feeds may not be."""
    change = _change()
    ctx = _context(change, downstream=[_asset("dim_users", terms=["PHI"])])
    gate = evaluate_governance_gate(change, ctx)
    assert gate.requires_security_review is True
    assert "PHI" in gate.sensitive_terms


def test_tier1_asset_is_listed_and_gates():
    change = _change()
    ctx = _context(change, downstream=[_asset("q3_revenue", tags=["Tier-1-Asset"])])
    gate = evaluate_governance_gate(change, ctx)
    assert gate.tier1_assets == ["q3_revenue"]
    assert gate.requires_security_review is True


def test_additive_change_never_gates():
    change = _change(ChangeType.ADD_COLUMN, "loyalty_tier")
    gate = evaluate_governance_gate(change, _context(change, source_terms=["PII"]))
    assert gate.requires_security_review is False
    assert gate.reasons == []


def test_clean_dataset_does_not_gate():
    change = _change()
    gate = evaluate_governance_gate(change, _context(change, source_terms=["Bronze"]))
    assert gate.requires_security_review is False


def test_gate_overrides_a_softer_verdict(monkeypatch):
    """No downstream lineage would normally approve. PII still blocks."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    change = _change()
    report = analyze(change, _context(change, source_terms=["PII"]))
    assert report.recommended_action is RecommendedAction.BLOCK
    assert report.risk_level is RiskLevel.HIGH
    assert report.governance_gate.requires_security_review is True
    assert "security sign-off" in (report.reasoning or "")


def test_gate_never_downgrades_a_critical_verdict(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    change = SchemaChange(
        table="analytics.users",
        change_type=ChangeType.DROP_TABLE,
        source_file="migrations/011.sql",
    )
    ctx = _context(change, source_terms=["PII"], downstream=[_asset("dim_users")])
    report = analyze(change, ctx)
    assert report.risk_level is RiskLevel.CRITICAL
    assert report.recommended_action is RecommendedAction.BLOCK
