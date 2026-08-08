"""Tests for the DataHub write-back path, using a fake graph.

No network: the fake records every aspect emitted so the assertions check what
would actually land on the graph.
"""

import pytest

from datahub.metadata.schema_classes import (
    EditableSchemaMetadataClass,
    GlobalTagsClass,
    InstitutionalMemoryClass,
    TagAssociationClass,
)

from src.datahub_mcp_client import DataHubMCPClient
from src.models import RiskLevel


class FakeGraph:
    def __init__(self):
        self.aspects = {}       # (urn, class) -> aspect
        self.emitted = []       # every MCP emitted, in order

    def get_aspect(self, entity_urn, aspect_type, version=0):
        return self.aspects.get((entity_urn, aspect_type))

    def exists(self, urn):
        return urn.startswith("urn:li:dataset")

    def emit_mcp(self, mcp, **kwargs):
        self.emitted.append(mcp)
        self.aspects[(mcp.entityUrn, type(mcp.aspect))] = mcp.aspect

    def close(self):
        pass


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("TOOLS_IS_MUTATION_ENABLED", raising=False)
    c = DataHubMCPClient.__new__(DataHubMCPClient)
    c.graph = FakeGraph()
    c.available = True
    c.last_error = None
    c._urn_cache = {}
    return c


URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.orders,PROD)"


def _tags(client, urn):
    aspect = client.graph.aspects.get((urn, GlobalTagsClass))
    return [t.tag for t in (aspect.tags if aspect else [])]


def test_apply_tag_is_idempotent(client):
    assert client.apply_tag(URN, "urn:li:tag:Schema-Change-Pending") is True
    first = len(client.graph.emitted)
    assert client.apply_tag(URN, "urn:li:tag:Schema-Change-Pending") is True
    assert len(client.graph.emitted) == first, "second call must not emit again"
    assert _tags(client, URN) == ["urn:li:tag:Schema-Change-Pending"]


def test_apply_tag_preserves_existing_tags(client):
    client.graph.aspects[(URN, GlobalTagsClass)] = GlobalTagsClass(
        tags=[TagAssociationClass(tag="urn:li:tag:Gold")]
    )
    client.apply_tag(URN, "urn:li:tag:PR-Under-Review")
    assert _tags(client, URN) == ["urn:li:tag:Gold", "urn:li:tag:PR-Under-Review"]


def test_field_tag_lands_on_the_changed_column(client):
    assert client.apply_field_tag(URN, "customer_id", "urn:li:tag:Schema-Change-Pending") is True
    aspect = client.graph.aspects[(URN, EditableSchemaMetadataClass)]
    field = aspect.editableSchemaFieldInfo[0]
    assert field.fieldPath == "customer_id"
    assert [t.tag for t in field.globalTags.tags] == ["urn:li:tag:Schema-Change-Pending"]


def test_field_tag_is_idempotent(client):
    client.apply_field_tag(URN, "customer_id", "urn:li:tag:Schema-Change-Pending")
    before = len(client.graph.emitted)
    client.apply_field_tag(URN, "customer_id", "urn:li:tag:Schema-Change-Pending")
    assert len(client.graph.emitted) == before


def test_note_is_keyed_by_pr_url_so_reruns_do_not_duplicate(client):
    url = "https://github.com/o/r/pull/7"
    client.add_dataset_note(URN, "Pending PR #7: drop customer_id", url=url)
    client.add_dataset_note(URN, "Pending PR #7: drop customer_id (updated)", url=url)
    aspect = client.graph.aspects[(URN, InstitutionalMemoryClass)]
    assert len(aspect.elements) == 1
    assert aspect.elements[0].description.endswith("(updated)")


def test_write_back_tags_source_column_and_downstream(client):
    downstream = "urn:li:dataset:(urn:li:dataPlatform:dbt,dim_customers,PROD)"
    outcome = client.write_back(
        source_urn=URN,
        downstream_urns=[downstream],
        risk_level=RiskLevel.CRITICAL,
        note="Pending PR #7",
        pr_url="https://github.com/o/r/pull/7",
        column_name="customer_id",
        requires_security_review=True,
    )
    assert all(outcome.values()), outcome
    assert set(_tags(client, URN)) == {
        "urn:li:tag:Schema-Change-Pending",
        "urn:li:tag:PR-Under-Review",
        "urn:li:tag:Security-Review-Required",
    }
    assert _tags(client, downstream) == ["urn:li:tag:Blast-Risk-Critical"]
    assert (URN, EditableSchemaMetadataClass) in client.graph.aspects


def test_no_security_tag_when_the_gate_is_clear(client):
    client.write_back(URN, [], RiskLevel.MEDIUM, "note", column_name="customer_id")
    assert "urn:li:tag:Security-Review-Required" not in _tags(client, URN)


def test_risk_tag_replaces_the_previous_level(client):
    """A dataset must carry the current risk, not every risk it ever had."""
    down = "urn:li:dataset:(urn:li:dataPlatform:dbt,dim_customers,PROD)"
    client.write_back(URN, [down], RiskLevel.CRITICAL, "note")
    assert _tags(client, down) == ["urn:li:tag:Blast-Risk-Critical"]
    client.write_back(URN, [down], RiskLevel.MEDIUM, "note")
    assert _tags(client, down) == ["urn:li:tag:Blast-Risk-Medium"]


def test_replacing_a_risk_tag_leaves_unrelated_tags_alone(client):
    down = "urn:li:dataset:x"
    client.graph.aspects[(down, GlobalTagsClass)] = GlobalTagsClass(
        tags=[TagAssociationClass(tag="urn:li:tag:Gold"),
              TagAssociationClass(tag="urn:li:tag:Blast-Risk-Low")]
    )
    client.write_back(URN, [down], RiskLevel.HIGH, "note")
    assert _tags(client, down) == ["urn:li:tag:Gold", "urn:li:tag:Blast-Risk-High"]


def test_security_badge_is_cleared_when_the_gate_stops_firing(client):
    client.write_back(URN, [], RiskLevel.HIGH, "note", requires_security_review=True)
    assert "urn:li:tag:Security-Review-Required" in _tags(client, URN)
    client.write_back(URN, [], RiskLevel.HIGH, "note", requires_security_review=False)
    assert "urn:li:tag:Security-Review-Required" not in _tags(client, URN)
    assert "urn:li:tag:Schema-Change-Pending" in _tags(client, URN)


def test_clear_pending_removes_every_contextci_marker(client):
    down = "urn:li:dataset:(urn:li:dataPlatform:dbt,dim_customers,PROD)"
    client.write_back(URN, [down], RiskLevel.CRITICAL, "note",
                      column_name="customer_id", requires_security_review=True)
    client.clear_pending(URN, [down], "customer_id")
    assert _tags(client, URN) == []
    assert _tags(client, down) == []
    esm = client.graph.aspects[(URN, EditableSchemaMetadataClass)]
    assert esm.editableSchemaFieldInfo[0].globalTags.tags == []


def test_clear_pending_leaves_other_peoples_tags_alone(client):
    client.graph.aspects[(URN, GlobalTagsClass)] = GlobalTagsClass(
        tags=[TagAssociationClass(tag="urn:li:tag:Legacy")]
    )
    client.write_back(URN, [], RiskLevel.HIGH, "note")
    client.clear_pending(URN, [])
    assert _tags(client, URN) == ["urn:li:tag:Legacy"]


def test_mutations_can_be_disabled(client, monkeypatch):
    """TOOLS_IS_MUTATION_ENABLED=false makes every write a no-op dry run."""
    monkeypatch.setenv("TOOLS_IS_MUTATION_ENABLED", "false")
    outcome = client.write_back(URN, ["urn:li:dataset:x"], RiskLevel.HIGH, "note",
                                column_name="customer_id")
    assert not any(outcome.values())
    assert client.graph.emitted == []


def test_writes_are_noops_when_datahub_is_unreachable(client):
    client.available = False
    assert client.apply_tag(URN, "urn:li:tag:X") is False
    assert client.apply_field_tag(URN, "c", "urn:li:tag:X") is False
    assert client.add_dataset_note(URN, "n") is False
