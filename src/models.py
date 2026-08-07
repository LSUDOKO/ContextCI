"""Pydantic models shared across ContextCI phases.

These types are the contract between the four phases: the diff parser emits
``SchemaChange``, the DataHub client emits ``LineageContext``, and the LLM is
forced to return a ``BlastReport``.
"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class ChangeType(str, Enum):
    DROP_COLUMN = "drop_column"
    RENAME_COLUMN = "rename_column"
    MODIFY_COLUMN = "modify_column"
    ADD_COLUMN = "add_column"
    DROP_TABLE = "drop_table"
    RENAME_TABLE = "rename_table"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RecommendedAction(str, Enum):
    BLOCK = "block"
    WARN = "warn"
    APPROVE = "approve"


class SchemaChange(BaseModel):
    """A single schema mutation extracted from a pull request diff."""

    table: str = Field(description="Table name as written in the diff, e.g. 'analytics.orders'")
    column: Optional[str] = Field(default=None, description="Column being changed; None for table-level changes")
    change_type: ChangeType
    old_type: Optional[str] = None
    new_type: Optional[str] = None
    new_value: Optional[str] = Field(default=None, description="New column name for renames")
    source_file: str = Field(description="Repo-relative path of the file the change was found in")
    source_line: Optional[int] = None
    raw_statement: Optional[str] = Field(default=None, description="The matched SQL/YAML fragment")

    @property
    def identity(self) -> str:
        """Stable key used for de-duplication and idempotent tagging."""
        return f"{self.change_type.value}:{self.table}:{self.column or '*'}:{self.new_value or ''}"


class Owner(BaseModel):
    urn: str
    name: str
    type: str = Field(default="user", description="'user' or 'group'")
    email: Optional[str] = None

    @property
    def github_handle(self) -> Optional[str]:
        """Best-effort GitHub handle for @-mentions in PR comments."""
        if self.email and "@" in self.email:
            return self.email.split("@", 1)[0]
        return self.name or None


class AffectedAsset(BaseModel):
    urn: str
    name: str
    type: str = Field(description="dataset | dashboard | chart | mlModel | dataJob")
    platform: Optional[str] = None
    owners: List[Owner] = Field(default_factory=list)
    glossary_terms: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    degree: int = Field(default=1, description="Hops downstream from the changed dataset")
    column_level_confirmed: bool = Field(
        default=False,
        description="True when DataHub fine-grained lineage proves this asset consumes the changed column",
    )
    risk: RiskLevel = RiskLevel.MEDIUM


class DatasetGovernance(BaseModel):
    urn: str
    description: Optional[str] = None
    owners: List[Owner] = Field(default_factory=list)
    glossary_terms: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    columns: List[str] = Field(default_factory=list)


class LineageContext(BaseModel):
    """Everything DataHub knows about one schema change."""

    change: SchemaChange
    dataset_urn: Optional[str] = None
    resolved: bool = Field(default=False, description="False when the table could not be found in DataHub")
    governance: Optional[DatasetGovernance] = None
    downstream: List[AffectedAsset] = Field(default_factory=list)
    upstream: List[AffectedAsset] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)


class GeneratedFix(BaseModel):
    file_path: str
    language: str = Field(default="sql", description="sql | yaml | python | markdown")
    code: str
    description: str
    target_asset: Optional[str] = Field(default=None, description="URN of the asset this fix repairs")


class BlastReport(BaseModel):
    """Structured LLM verdict for one schema change."""

    is_breaking: bool
    risk_level: RiskLevel
    summary: str = Field(description="One or two sentences a reviewer can read at a glance")
    affected_assets: List[AffectedAsset] = Field(default_factory=list)
    generated_fixes: List[GeneratedFix] = Field(default_factory=list)
    recommended_action: RecommendedAction
    reasoning: Optional[str] = None


class ChangeVerdict(BaseModel):
    """Pairs a change with its report so the reporter can render both."""

    change: SchemaChange
    context: LineageContext
    report: BlastReport


class RunResult(BaseModel):
    """Aggregate outcome of one ContextCI run over a pull request."""

    verdicts: List[ChangeVerdict] = Field(default_factory=list)
    degraded: bool = Field(default=False, description="True when DataHub was unreachable and we ran blind")
    degraded_reason: Optional[str] = None

    @property
    def overall_action(self) -> RecommendedAction:
        actions = [v.report.recommended_action for v in self.verdicts]
        if RecommendedAction.BLOCK in actions:
            return RecommendedAction.BLOCK
        if RecommendedAction.WARN in actions:
            return RecommendedAction.WARN
        return RecommendedAction.APPROVE

    @property
    def overall_risk(self) -> RiskLevel:
        order = [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL]
        worst = RiskLevel.LOW
        for v in self.verdicts:
            if order.index(v.report.risk_level) > order.index(worst):
                worst = v.report.risk_level
        return worst
