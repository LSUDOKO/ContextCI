"""Phase 3b — backward-compatible migration code.

Two sources of fixes:

* **LLM-generated** — the prompt fragments here are folded into the blast
  analysis call so the model sees the real downstream SQL surface and writes
  migrations against it.
* **Template fallback** — deterministic shadow-column / compatibility-view
  patterns used when no API key is configured or the model call fails. They are
  intentionally conservative: preserve the old name, keep old readers working,
  and let the drop happen in a later release.
"""

from __future__ import annotations

from typing import List

from .models import ChangeType, GeneratedFix, LineageContext, SchemaChange

FIX_GUIDANCE = """\
When the change breaks downstream assets, generate migration code that keeps
old readers working through a deprecation window:

- DROP COLUMN: do not drop in this PR. Emit a compatibility view (or dbt model)
  that still exposes the column, and stage the physical drop for a later release
  once downstream owners have migrated.
- RENAME COLUMN: add the new name while keeping the old one readable, e.g. a
  view exposing `new_name AS old_name`, or a dbt model selecting
  `COALESCE(new_name, old_name) AS old_name`.
- MODIFY COLUMN: widening types (int -> bigint, varchar(n) -> text) is usually
  safe; narrowing is not. For narrowing, emit a backfill + validation query.
- Every fix must be runnable SQL or a complete dbt model file, not a sketch.
- Reference the real downstream asset names from the lineage context.
"""


def _view_name(table: str) -> str:
    return f"{table}_compat"


def template_fixes(change: SchemaChange, context: LineageContext) -> List[GeneratedFix]:
    """Deterministic fallback migrations used when the LLM is unavailable."""
    table = change.table
    column = change.column
    downstream = ", ".join(a.name for a in context.downstream[:5]) or "unknown downstream assets"

    if change.change_type is ChangeType.DROP_COLUMN and column:
        return [
            GeneratedFix(
                file_path=f"migrations/compat_{table.replace('.', '_')}_{column}.sql",
                language="sql",
                description=(
                    f"Compatibility view keeping `{column}` readable for {downstream} "
                    "while they migrate. Drop the column in a follow-up release."
                ),
                code=(
                    f"-- ContextCI: staged removal of {table}.{column}\n"
                    f"-- Step 1 (this PR): expose a compatibility view so downstream readers keep working.\n"
                    f"CREATE OR REPLACE VIEW {_view_name(table)} AS\n"
                    f"SELECT\n"
                    f"    *,\n"
                    f"    NULL AS {column}  -- deprecated, removal scheduled\n"
                    f"FROM {table};\n\n"
                    f"-- Step 2 (follow-up release, after downstream owners migrate):\n"
                    f"-- ALTER TABLE {table} DROP COLUMN {column};\n"
                    f"-- DROP VIEW {_view_name(table)};\n"
                ),
                target_asset=context.dataset_urn,
            )
        ]

    if change.change_type is ChangeType.RENAME_COLUMN and column and change.new_value:
        new = change.new_value
        return [
            GeneratedFix(
                file_path=f"migrations/compat_{table.replace('.', '_')}_{column}.sql",
                language="sql",
                description=(
                    f"Expose both `{column}` and `{new}` so {downstream} can migrate "
                    "without a coordinated deploy."
                ),
                code=(
                    f"-- ContextCI: backward-compatible rename of {table}.{column} -> {new}\n"
                    f"CREATE OR REPLACE VIEW {_view_name(table)} AS\n"
                    f"SELECT\n"
                    f"    *,\n"
                    f"    {new} AS {column}  -- old name, deprecated\n"
                    f"FROM {table};\n\n"
                    f"-- dbt equivalent for downstream models still selecting the old name:\n"
                    f"-- SELECT COALESCE({new}, {column}) AS {column} FROM {{{{ ref('{table.split('.')[-1]}') }}}}\n"
                ),
                target_asset=context.dataset_urn,
            )
        ]

    if change.change_type is ChangeType.MODIFY_COLUMN and column:
        return [
            GeneratedFix(
                file_path=f"migrations/validate_{table.replace('.', '_')}_{column}.sql",
                language="sql",
                description=(
                    f"Validation query to run before changing the type of `{column}`. "
                    f"A non-zero count means the change would truncate data used by {downstream}."
                ),
                code=(
                    f"-- ContextCI: verify {table}.{column} survives the type change"
                    f"{' to ' + change.new_type if change.new_type else ''}\n"
                    f"SELECT COUNT(*) AS rows_that_would_not_fit\n"
                    f"FROM {table}\n"
                    f"WHERE {column} IS NOT NULL\n"
                    f"  AND {column}::text <> {column}::{change.new_type or 'text'}::text;\n"
                ),
                target_asset=context.dataset_urn,
            )
        ]

    if change.change_type is ChangeType.DROP_TABLE:
        return [
            GeneratedFix(
                file_path=f"migrations/compat_{table.replace('.', '_')}.sql",
                language="sql",
                description=f"Keep `{table}` readable for {downstream} during deprecation.",
                code=(
                    f"-- ContextCI: {table} is scheduled for removal.\n"
                    f"-- Rename instead of dropping so downstream reads fail loudly in staging first:\n"
                    f"ALTER TABLE {table} RENAME TO {table}_deprecated;\n"
                    f"CREATE OR REPLACE VIEW {table} AS SELECT * FROM {table}_deprecated;\n"
                ),
                target_asset=context.dataset_urn,
            )
        ]

    return []
