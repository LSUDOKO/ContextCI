"""Tests for the local `--diff` entrypoint's multi-file diff splitting."""

from pathlib import Path

from src.main import parse_diff_file
from src.models import ChangeType

FIXTURE = Path(__file__).resolve().parents[1] / "examples" / "breaking_change.diff"


def test_example_diff_yields_one_change_per_dialect():
    changes = parse_diff_file(FIXTURE.read_text())
    by_type = {c.change_type for c in changes}
    assert ChangeType.DROP_COLUMN in by_type
    assert ChangeType.RENAME_COLUMN in by_type
    assert len(changes) == 3


def test_changes_are_attributed_to_the_right_file():
    changes = {c.column: c for c in parse_diff_file(FIXTURE.read_text())}
    assert changes["customer_id"].source_file == "migrations/007_drop_customer_id.sql"
    assert changes["email"].source_file == "migrations/008_rename_email.py"
    assert changes["legacy_signup_source"].source_file == "models/marts/dim_customers.yml"


def test_alembic_rename_carries_the_new_name():
    changes = {c.column: c for c in parse_diff_file(FIXTURE.read_text())}
    assert changes["email"].new_value == "email_address"


def test_git_headers_are_not_parsed_as_content():
    """`--- a/file` and `diff --git` lines must never look like removed SQL."""
    patch = "diff --git a/m.sql b/m.sql\n--- a/m.sql\n+++ b/m.sql\n@@ -1 +1,2 @@\n+ALTER TABLE t DROP COLUMN c;"
    changes = parse_diff_file(patch)
    assert len(changes) == 1
    assert changes[0].source_file == "m.sql"


def test_diff_without_file_headers_falls_back_to_the_given_name():
    patch = "@@ -1 +1,2 @@\n+ALTER TABLE t DROP COLUMN c;"
    (change,) = parse_diff_file(patch, fallback_name="local.sql")
    assert change.source_file == "local.sql"


def test_empty_diff_is_not_an_error():
    assert parse_diff_file("") == []
