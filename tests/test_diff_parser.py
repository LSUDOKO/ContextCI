"""Tests for Phase 1 diff parsing."""

from src.diff_parser import is_schema_file, iter_diff_lines, parse_patch
from src.models import ChangeType


def _patch(*body: str) -> str:
    return "\n".join(("@@ -1,4 +1,5 @@",) + body)


def test_detects_drop_column():
    patch = _patch(" -- migration", "+ALTER TABLE analytics.orders DROP COLUMN customer_id;")
    (change,) = parse_patch("migrations/001_drop.sql", patch)
    assert change.change_type is ChangeType.DROP_COLUMN
    assert change.table == "analytics.orders"
    assert change.column == "customer_id"
    assert change.source_line == 2


def test_detects_rename_column_with_new_value():
    patch = _patch("+ALTER TABLE orders RENAME COLUMN customer_id TO cust_id;")
    (change,) = parse_patch("migrations/002_rename.sql", patch)
    assert change.change_type is ChangeType.RENAME_COLUMN
    assert change.column == "customer_id"
    assert change.new_value == "cust_id"


def test_detects_type_change():
    patch = _patch("+ALTER TABLE orders ALTER COLUMN total TYPE numeric(12,2);")
    (change,) = parse_patch("migrations/003_type.sql", patch)
    assert change.change_type is ChangeType.MODIFY_COLUMN
    assert change.column == "total"
    assert change.new_type.startswith("numeric")


def test_detects_mysql_modify_syntax():
    patch = _patch("+ALTER TABLE orders MODIFY COLUMN total DECIMAL(10,2);")
    (change,) = parse_patch("migrations/004_modify.sql", patch)
    assert change.change_type is ChangeType.MODIFY_COLUMN
    assert change.column == "total"


def test_detects_drop_table():
    patch = _patch("+DROP TABLE IF EXISTS analytics.legacy_orders;")
    (change,) = parse_patch("migrations/005_drop_table.sql", patch)
    assert change.change_type is ChangeType.DROP_TABLE
    assert change.table == "analytics.legacy_orders"
    assert change.column is None


def test_ignores_removed_lines_for_ddl():
    """A migration deleted from the branch is not a schema change being shipped."""
    patch = _patch("-ALTER TABLE orders DROP COLUMN customer_id;")
    assert parse_patch("migrations/006.sql", patch) == []


def test_ignores_commented_out_ddl():
    patch = _patch("+-- ALTER TABLE orders DROP COLUMN customer_id;")
    assert parse_patch("migrations/007.sql", patch) == []


def test_detects_alembic_drop_column():
    patch = _patch("+    op.drop_column('orders', 'customer_id')")
    (change,) = parse_patch("alembic/versions/abc_drop.py", patch)
    assert change.change_type is ChangeType.DROP_COLUMN
    assert (change.table, change.column) == ("orders", "customer_id")


def test_detects_alembic_rename_via_new_column_name():
    patch = _patch("+    op.alter_column('orders', 'customer_id', new_column_name='cust_id')")
    (change,) = parse_patch("alembic/versions/def_rename.py", patch)
    assert change.change_type is ChangeType.RENAME_COLUMN
    assert change.new_value == "cust_id"


def test_detects_dbt_yaml_column_removal():
    patch = _patch(
        " models:",
        "   - name: dim_customers",
        "     columns:",
        "       - name: customer_id",
        "-      - name: legacy_email",
    )
    (change,) = parse_patch("models/schema.yml", patch)
    assert change.change_type is ChangeType.DROP_COLUMN
    assert (change.table, change.column) == ("dim_customers", "legacy_email")


def test_dbt_yaml_one_for_one_swap_reads_as_rename():
    patch = _patch(
        " models:",
        "   - name: dim_customers",
        "     columns:",
        "-      - name: email",
        "+      - name: email_address",
    )
    (change,) = parse_patch("models/schema.yml", patch)
    assert change.change_type is ChangeType.RENAME_COLUMN
    assert change.new_value == "email_address"


def test_dbt_model_select_column_dropped():
    patch = _patch(
        " select",
        "     customer_id,",
        "-    legacy_email,",
        "     created_at",
    )
    (change,) = parse_patch("models/marts/dim_customers.sql", patch)
    assert change.change_type is ChangeType.DROP_COLUMN
    assert change.table == "dim_customers"
    assert change.column == "legacy_email"


def test_duplicate_detections_collapse():
    """The same statement appearing twice yields one change."""
    patch = _patch(
        "+ALTER TABLE orders DROP COLUMN customer_id;",
        "+ALTER TABLE orders DROP COLUMN customer_id;",
    )
    assert len(parse_patch("migrations/008.sql", patch)) == 1


def test_non_schema_files_are_skipped():
    assert not is_schema_file("README.md")
    assert parse_patch("README.md", _patch("+ALTER TABLE orders DROP COLUMN x;")) == []


def test_line_numbers_track_hunk_headers():
    patch = "@@ -10,3 +20,4 @@\n context\n+ALTER TABLE orders DROP COLUMN x;"
    (change,) = parse_patch("m.sql", patch)
    assert change.source_line == 21


def test_iter_diff_lines_marks_sides():
    kinds = [l.kind for l in iter_diff_lines("@@ -1,1 +1,2 @@\n ctx\n+new\n-old")]
    assert kinds == [" ", "+", "-"]
