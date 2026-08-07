"""Phase 1 — extract schema changes from a pull request diff.

Works directly on unified-diff patches (what the GitHub API returns), not on
whole files, because a PR only tells us what changed. Four dialects are
recognised:

* raw SQL DDL — ``ALTER TABLE ... DROP/RENAME/ALTER COLUMN``, ``DROP TABLE``
* Alembic / SQLAlchemy migrations — ``op.drop_column``, ``op.alter_column``
* dbt schema YAML — a ``- name:`` entry removed from a model's ``columns:``
* dbt model SQL — a select-list column that disappears from the model

Only *added* lines are scanned for DDL (a migration is introduced by adding
it), while dbt YAML and dbt SQL are judged by what the diff *removes*.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Dict, Iterable, List, Optional, Tuple

from .models import ChangeType, SchemaChange

logger = logging.getLogger(__name__)

SCHEMA_FILE_SUFFIXES = (".sql", ".py", ".yml", ".yaml", ".dbt")

_IDENT = r'[`"\[]?([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)*)[`"\]]?'

# --- raw SQL DDL ------------------------------------------------------------
_SQL_PATTERNS: List[Tuple[re.Pattern, ChangeType]] = [
    (
        re.compile(
            rf"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?{_IDENT}\s+DROP\s+(?:COLUMN\s+)?(?:IF\s+EXISTS\s+)?{_IDENT}",
            re.IGNORECASE,
        ),
        ChangeType.DROP_COLUMN,
    ),
    (
        re.compile(
            rf"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?{_IDENT}\s+RENAME\s+(?:COLUMN\s+)?{_IDENT}\s+TO\s+{_IDENT}",
            re.IGNORECASE,
        ),
        ChangeType.RENAME_COLUMN,
    ),
    (
        re.compile(
            rf"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?{_IDENT}\s+(?:ALTER|MODIFY)\s+(?:COLUMN\s+)?{_IDENT}"
            r"(?:\s+(?:SET\s+DATA\s+)?TYPE)?\s+([A-Za-z][\w()\s,]*)",
            re.IGNORECASE,
        ),
        ChangeType.MODIFY_COLUMN,
    ),
    (
        re.compile(
            rf"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?{_IDENT}\s+ADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?{_IDENT}"
            r"\s+([A-Za-z][\w()\s,]*)",
            re.IGNORECASE,
        ),
        ChangeType.ADD_COLUMN,
    ),
]

_DROP_TABLE = re.compile(
    rf"\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?{_IDENT}", re.IGNORECASE
)
_RENAME_TABLE = re.compile(
    rf"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?{_IDENT}\s+RENAME\s+TO\s+{_IDENT}", re.IGNORECASE
)

# --- Alembic / SQLAlchemy ---------------------------------------------------
_OP_DROP_COLUMN = re.compile(
    r"op\.drop_column\(\s*['\"]([\w.]+)['\"]\s*,\s*['\"]([\w$]+)['\"]", re.IGNORECASE
)
_OP_ALTER_COLUMN = re.compile(
    r"op\.alter_column\(\s*['\"]([\w.]+)['\"]\s*,\s*['\"]([\w$]+)['\"](?P<rest>[^\n]*)",
    re.IGNORECASE,
)
_OP_ADD_COLUMN = re.compile(
    r"op\.add_column\(\s*['\"]([\w.]+)['\"]\s*,\s*sa\.Column\(\s*['\"]([\w$]+)['\"]\s*,\s*([^,)]+)",
    re.IGNORECASE,
)
_NEW_COLUMN_NAME = re.compile(r"new_column_name\s*=\s*['\"]([\w$]+)['\"]")
_TYPE_KWARG = re.compile(r"type_\s*=\s*([\w.()]+)")

# --- dbt --------------------------------------------------------------------
_YAML_NAME = re.compile(r"^(\s*)-\s*name:\s*['\"]?([\w.$]+)['\"]?\s*$")
_YAML_COLUMNS_KEY = re.compile(r"^(\s*)columns:\s*$")
_SELECT_ALIAS = re.compile(
    r"^\s*(?:[\w.\"`]+\s+as\s+([\w$]+)|([\w\"`]+)\.([\w$]+)|([\w$]+))\s*,?\s*$", re.IGNORECASE
)
_SQL_KEYWORDS = {
    "select", "from", "where", "join", "on", "with", "as", "and", "or", "group",
    "order", "by", "having", "limit", "union", "all", "left", "right", "inner",
    "outer", "case", "when", "then", "else", "end", "distinct", "null",
}

_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


class DiffLine:
    """One line of a unified diff, tagged with its side and new-file position."""

    __slots__ = ("kind", "text", "new_lineno")

    def __init__(self, kind: str, text: str, new_lineno: Optional[int]):
        self.kind = kind  # "+", "-", or " "
        self.text = text
        self.new_lineno = new_lineno


def iter_diff_lines(patch: str) -> Iterable[DiffLine]:
    """Yield diff lines with new-file line numbers reconstructed from hunk headers."""
    new_lineno = 0
    for raw in (patch or "").splitlines():
        header = _HUNK_HEADER.match(raw)
        if header:
            new_lineno = int(header.group(1))
            continue
        if raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("+"):
            yield DiffLine("+", raw[1:], new_lineno)
            new_lineno += 1
        elif raw.startswith("-"):
            yield DiffLine("-", raw[1:], None)
        else:
            yield DiffLine(" ", raw[1:] if raw else "", new_lineno)
            new_lineno += 1


def is_schema_file(filename: str) -> bool:
    return filename.lower().endswith(SCHEMA_FILE_SUFFIXES)


def parse_patch(filename: str, patch: str) -> List[SchemaChange]:
    """Extract every schema change visible in one file's patch."""
    if not patch or not is_schema_file(filename):
        return []

    lines = list(iter_diff_lines(patch))
    changes: List[SchemaChange] = []

    changes.extend(_parse_sql_ddl(filename, lines))
    changes.extend(_parse_alembic(filename, lines))
    if filename.lower().endswith((".yml", ".yaml")):
        changes.extend(_parse_dbt_yaml(filename, lines))
    elif filename.lower().endswith(".sql"):
        changes.extend(_parse_dbt_model(filename, lines))

    return _dedupe(changes)


def _parse_sql_ddl(filename: str, lines: List[DiffLine]) -> List[SchemaChange]:
    """Match DDL statements on added lines."""
    out: List[SchemaChange] = []
    for line in lines:
        if line.kind != "+":
            continue
        text = _strip_sql_comment(line.text)
        if not text.strip():
            continue

        for pattern, change_type in _SQL_PATTERNS:
            match = pattern.search(text)
            if not match:
                continue
            groups = match.groups()
            table, column = groups[0], groups[1]
            new_value = groups[2].strip() if change_type is ChangeType.RENAME_COLUMN and len(groups) > 2 else None
            new_type = (
                groups[2].strip().rstrip(";,").strip()
                if change_type in (ChangeType.MODIFY_COLUMN, ChangeType.ADD_COLUMN) and len(groups) > 2
                else None
            )
            out.append(
                SchemaChange(
                    table=table,
                    column=column,
                    change_type=change_type,
                    new_value=new_value,
                    new_type=new_type,
                    source_file=filename,
                    source_line=line.new_lineno,
                    raw_statement=text.strip(),
                )
            )
            break
        else:
            rename_table = _RENAME_TABLE.search(text)
            if rename_table:
                out.append(
                    SchemaChange(
                        table=rename_table.group(1),
                        change_type=ChangeType.RENAME_TABLE,
                        new_value=rename_table.group(2),
                        source_file=filename,
                        source_line=line.new_lineno,
                        raw_statement=text.strip(),
                    )
                )
                continue
            drop_table = _DROP_TABLE.search(text)
            if drop_table:
                out.append(
                    SchemaChange(
                        table=drop_table.group(1),
                        change_type=ChangeType.DROP_TABLE,
                        source_file=filename,
                        source_line=line.new_lineno,
                        raw_statement=text.strip(),
                    )
                )
    return out


def _parse_alembic(filename: str, lines: List[DiffLine]) -> List[SchemaChange]:
    """Match Alembic migration operations on added lines."""
    if not filename.lower().endswith(".py"):
        return []
    out: List[SchemaChange] = []
    for line in lines:
        if line.kind != "+":
            continue
        text = line.text

        drop = _OP_DROP_COLUMN.search(text)
        if drop:
            out.append(
                SchemaChange(
                    table=drop.group(1),
                    column=drop.group(2),
                    change_type=ChangeType.DROP_COLUMN,
                    source_file=filename,
                    source_line=line.new_lineno,
                    raw_statement=text.strip(),
                )
            )
            continue

        alter = _OP_ALTER_COLUMN.search(text)
        if alter:
            rest = alter.group("rest") or ""
            renamed = _NEW_COLUMN_NAME.search(rest)
            new_type = _TYPE_KWARG.search(rest)
            out.append(
                SchemaChange(
                    table=alter.group(1),
                    column=alter.group(2),
                    change_type=ChangeType.RENAME_COLUMN if renamed else ChangeType.MODIFY_COLUMN,
                    new_value=renamed.group(1) if renamed else None,
                    new_type=new_type.group(1) if new_type else None,
                    source_file=filename,
                    source_line=line.new_lineno,
                    raw_statement=text.strip(),
                )
            )
            continue

        added = _OP_ADD_COLUMN.search(text)
        if added:
            out.append(
                SchemaChange(
                    table=added.group(1),
                    column=added.group(2),
                    change_type=ChangeType.ADD_COLUMN,
                    new_type=added.group(3).strip(),
                    source_file=filename,
                    source_line=line.new_lineno,
                    raw_statement=text.strip(),
                )
            )
    return out


def _parse_dbt_yaml(filename: str, lines: List[DiffLine]) -> List[SchemaChange]:
    """Detect columns removed from a dbt schema.yml.

    Walks the diff in order, tracking the current model (a ``- name:`` at the
    model indent level) and whether we are inside that model's ``columns:``
    block, so a removed ``- name: foo`` is attributed to the right table.
    """
    out: List[SchemaChange] = []
    current_model: Optional[str] = None
    columns_indent: Optional[int] = None
    removed_names: Dict[str, List[str]] = {}
    added_names: Dict[str, List[str]] = {}

    for line in lines:
        text = line.text
        cols_key = _YAML_COLUMNS_KEY.match(text)
        if cols_key:
            columns_indent = len(cols_key.group(1))
            continue

        name_match = _YAML_NAME.match(text)
        if not name_match:
            if text.strip() and not text.startswith(" ") and columns_indent is not None:
                columns_indent = None
            continue

        indent, name = len(name_match.group(1)), name_match.group(2)
        inside_columns = columns_indent is not None and indent > columns_indent

        if not inside_columns:
            # A model-level (or source-level) name resets the column scope.
            if line.kind != "-":
                current_model = name
            columns_indent = None
            continue

        if not current_model:
            continue
        if line.kind == "-":
            removed_names.setdefault(current_model, []).append(name)
        elif line.kind == "+":
            added_names.setdefault(current_model, []).append(name)

    for model, removed in removed_names.items():
        added = added_names.get(model, [])
        for column in removed:
            if column in added:
                continue  # moved or reformatted, not removed
            change_type = ChangeType.DROP_COLUMN
            new_value = None
            # A 1:1 swap inside one model reads as a rename.
            leftover_added = [a for a in added if a not in removed]
            if len(removed) == 1 and len(leftover_added) == 1:
                change_type = ChangeType.RENAME_COLUMN
                new_value = leftover_added[0]
            out.append(
                SchemaChange(
                    table=model,
                    column=column,
                    change_type=change_type,
                    new_value=new_value,
                    source_file=filename,
                    raw_statement=f"dbt schema: column '{column}' removed from model '{model}'",
                )
            )
    return out


def _parse_dbt_model(filename: str, lines: List[DiffLine]) -> List[SchemaChange]:
    """Detect select-list columns that vanish from a dbt model.

    ponytail: line-level heuristic, not a SQL parse — it compares the set of
    identifiers on removed lines against added ones. Upgrade to sqlglot column
    resolution if false positives on complex CTEs become a problem.
    """
    if "/models/" not in filename and not filename.startswith("models/"):
        return []

    removed = _select_identifiers(l.text for l in lines if l.kind == "-")
    added = _select_identifiers(l.text for l in lines if l.kind == "+")
    dropped = removed - added
    if not dropped:
        return []

    model = os.path.basename(filename).rsplit(".", 1)[0]
    gained = added - removed
    return [
        SchemaChange(
            table=model,
            column=column,
            change_type=ChangeType.RENAME_COLUMN if len(dropped) == 1 and len(gained) == 1 else ChangeType.DROP_COLUMN,
            new_value=next(iter(gained)) if len(dropped) == 1 and len(gained) == 1 else None,
            source_file=filename,
            raw_statement=f"dbt model '{model}': column '{column}' no longer selected",
        )
        for column in sorted(dropped)
    ]


def _select_identifiers(texts: Iterable[str]) -> set:
    """Column names appearing alone (or aliased) on a line of a select list."""
    found = set()
    for text in texts:
        stripped = _strip_sql_comment(text).strip()
        if not stripped or stripped.lower().startswith(("select", "from", "where", "{{", "{%")):
            continue
        match = _SELECT_ALIAS.match(stripped)
        if not match:
            continue
        name = next((g for g in match.groups() if g), None)
        if name and name.lower() not in _SQL_KEYWORDS:
            found.add(name.strip('"`'))
    return found


def _strip_sql_comment(text: str) -> str:
    return text.split("--", 1)[0]


def _dedupe(changes: List[SchemaChange]) -> List[SchemaChange]:
    """Collapse duplicate detections so a change is reported once."""
    seen = {}
    for change in changes:
        seen.setdefault(change.identity, change)
    return list(seen.values())


def parse_pull_request(
    repo_full_name: str, pr_number: int, token: Optional[str] = None
) -> List[SchemaChange]:
    """Fetch a PR's diff from GitHub and extract every schema change in it."""
    from github import Auth, Github

    token = token or os.getenv("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is required to read the pull request diff")

    client = Github(auth=Auth.Token(token))
    try:
        pull = client.get_repo(repo_full_name).get_pull(pr_number)
        changes: List[SchemaChange] = []
        for file in pull.get_files():
            if file.status == "removed" or not is_schema_file(file.filename):
                continue
            changes.extend(parse_patch(file.filename, file.patch or ""))
        logger.info("found %d schema change(s) in %s#%d", len(changes), repo_full_name, pr_number)
        return _dedupe(changes)
    finally:
        client.close()
