"""Tests for the lumen-sqlite MCP server.

Each tool is tested at the Python function level — we call the functions
directly rather than going through the MCP transport. This keeps tests
fast and focused on logic correctness.
"""

from __future__ import annotations

import csv
import io
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

# Set up a temp directory for databases BEFORE importing the module,
# so the module picks up our test DB_DIR.
_test_dir = tempfile.mkdtemp(prefix="lumen_sqlite_test_")
os.environ["LUMEN_SQLITE_DB_DIR"] = _test_dir
os.environ["LUMEN_SQLITE_MAX_ROWS"] = "100"

# Now import the module — it reads the env vars at import time
import lumen_sqlite as ls  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_connections():
    """Ensure all connections are closed after each test."""
    yield
    for alias in list(ls._connections):
        try:
            ls._connections[alias].close()
        except Exception:
            pass
    ls._connections.clear()


@pytest.fixture
def db(tmp_path):
    """Create a temp database and return its alias."""
    db_path = str(tmp_path / "test.db")
    result = ls.open_database(db_path, alias="test")
    assert "Opened" in result or "Created" in result
    return "test"


# ---------------------------------------------------------------------------
# Tests — Helpers
# ---------------------------------------------------------------------------

class TestValidateIdentifier:
    def test_valid_names(self):
        ls._validate_identifier("foo")
        ls._validate_identifier("_readme")
        ls._validate_identifier("Table123")
        ls._validate_identifier("a_b_c")

    def test_invalid_names(self):
        with pytest.raises(ValueError):
            ls._validate_identifier("123abc")  # starts with digit
        with pytest.raises(ValueError):
            ls._validate_identifier("my table")  # space
        with pytest.raises(ValueError):
            ls._validate_identifier("drop;--")  # injection attempt
        with pytest.raises(ValueError):
            ls._validate_identifier("")  # empty
        with pytest.raises(ValueError):
            ls._validate_identifier("col-name")  # hyphen


class TestResolvePath:
    def test_relative_path(self):
        p = ls._resolve_db_path("test.db")
        assert p.is_absolute()
        assert p.name == "test.db"

    def test_absolute_path(self, tmp_path):
        full = str(tmp_path / "abs.db")
        p = ls._resolve_db_path(full)
        assert str(p) == str(Path(full).resolve())

    def test_rejects_bad_extension(self):
        with pytest.raises(ValueError, match="not allowed"):
            ls._resolve_db_path("data.txt")


# ---------------------------------------------------------------------------
# Tests — Database lifecycle
# ---------------------------------------------------------------------------

class TestOpenClose:
    def test_open_creates_file(self, tmp_path):
        db_path = str(tmp_path / "new.db")
        result = ls.open_database(db_path, alias="new")
        assert "Created and opened" in result
        assert Path(db_path).exists()

    def test_open_existing(self, tmp_path):
        db_path = str(tmp_path / "existing.db")
        # Create it first
        sqlite3.connect(db_path).close()
        result = ls.open_database(db_path, alias="existing")
        assert "Opened" in result
        assert "Created" not in result

    def test_open_duplicate_alias(self, db):
        result = ls.open_database("dummy.db", alias="test")
        assert "already open" in result

    def test_close(self, db):
        result = ls.close_database("test")
        assert "Closed" in result
        assert "test" not in ls._connections

    def test_close_nonexistent(self):
        with pytest.raises(ValueError, match="No open database"):
            ls._get_conn("nope")


class TestListDatabases:
    def test_empty(self):
        result = ls.list_databases()
        assert result == []

    def test_with_databases(self, db, tmp_path):
        db2_path = str(tmp_path / "second.db")
        ls.open_database(db2_path, alias="second")
        result = ls.list_databases()
        assert result == ["second", "test"]


class TestListDatabaseFiles:
    def test_no_files(self):
        # Our test dir might be empty
        result = ls.list_database_files()
        # Either "No database files" or a table — just ensure it runs
        assert isinstance(result, str)

    def test_with_files(self, tmp_path):
        # Create a db file in the configured DB_DIR
        db_file = Path(_test_dir) / "discoverable.db"
        sqlite3.connect(str(db_file)).close()
        result = ls.list_database_files()
        assert "discoverable.db" in result


class TestDatabaseInfo:
    def test_empty_database(self, db):
        result = ls.database_info("test")
        assert "no tables" in result.lower()

    def test_with_tables(self, db):
        conn = ls._get_conn("test")
        conn.execute(
            "CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)"
        )
        conn.execute("INSERT INTO items (name) VALUES ('alpha')")
        conn.commit()

        result = ls.database_info("test")
        assert "items" in result
        assert "1 rows" in result or "1 row" in result

    def test_with_readme(self, db):
        ls.create_readme("test", "Test database", {"items": "test items"})
        result = ls.database_info("test")
        assert "_readme" in result
        assert "Test database" in result


# ---------------------------------------------------------------------------
# Tests — Self-documentation
# ---------------------------------------------------------------------------

class TestCreateReadme:
    def test_basic(self, db):
        result = ls.create_readme("test", "My test DB")
        assert "_readme" in result
        assert "2 entries" in result

    def test_with_tables(self, db):
        result = ls.create_readme(
            "test",
            "My test DB",
            tables={"foo": "Foo data", "bar": "Bar data"},
        )
        assert "4 entries" in result

    def test_idempotent(self, db):
        ls.create_readme("test", "First")
        result = ls.create_readme("test", "Second")
        assert "2 entries" in result

        # Verify the purpose was updated
        conn = ls._get_conn("test")
        row = conn.execute(
            "SELECT value FROM _readme WHERE key='purpose'"
        ).fetchone()
        assert row[0] == "Second"


# ---------------------------------------------------------------------------
# Tests — SQL execution
# ---------------------------------------------------------------------------

class TestExecuteSql:
    def test_select(self, db):
        conn = ls._get_conn("test")
        conn.execute("CREATE TABLE nums (val INTEGER)")
        conn.executemany(
            "INSERT INTO nums VALUES (?)", [(i,) for i in range(5)]
        )
        conn.commit()

        result = ls.execute_sql("test", "SELECT val FROM nums ORDER BY val")
        assert "0" in result
        assert "4" in result

    def test_parameterized_insert(self, db):
        conn = ls._get_conn("test")
        conn.execute("CREATE TABLE kv (k TEXT, v TEXT)")
        conn.commit()

        result = ls.execute_sql(
            "test",
            "INSERT INTO kv VALUES (?, ?)",
            params=["key1", "val1"],
        )
        assert "1 row" in result

    def test_ddl(self, db):
        result = ls.execute_sql(
            "test", "CREATE TABLE t (id INTEGER PRIMARY KEY)"
        )
        assert "executed successfully" in result.lower() or "0 row" in result.lower()

    def test_sql_error(self, db):
        result = ls.execute_sql("test", "SELECT * FROM nonexistent")
        assert "SQL error" in result

    def test_max_rows(self, db):
        conn = ls._get_conn("test")
        conn.execute("CREATE TABLE big (n INTEGER)")
        conn.executemany(
            "INSERT INTO big VALUES (?)", [(i,) for i in range(200)]
        )
        conn.commit()

        result = ls.execute_sql("test", "SELECT * FROM big", max_rows=10)
        assert "truncated" in result

    def test_nonexistent_alias(self):
        with pytest.raises(ValueError):
            ls.execute_sql("nope", "SELECT 1")


class TestExecuteScript:
    def test_multi_statement(self, db):
        script = """
        CREATE TABLE a (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE b (id INTEGER PRIMARY KEY, ref INTEGER REFERENCES a(id));
        INSERT INTO a VALUES (1, 'one');
        INSERT INTO a VALUES (2, 'two');
        """
        result = ls.execute_script("test", script)
        assert "successfully" in result.lower()

        # Verify tables were created
        tables = ls.list_tables("test")
        assert "a" in tables
        assert "b" in tables

    def test_script_error(self, db):
        result = ls.execute_script("test", "INVALID SQL SYNTAX HERE;")
        assert "SQL error" in result


class TestExplainQuery:
    def test_basic_explain(self, db):
        conn = ls._get_conn("test")
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
        conn.commit()

        result = ls.explain_query(
            "test", "SELECT * FROM items WHERE id = ?", params=[1]
        )
        assert "plan" in result.lower() or "SEARCH" in result or "SCAN" in result

    def test_explain_error(self, db):
        result = ls.explain_query("test", "SELECT * FROM nonexistent")
        assert "SQL error" in result


# ---------------------------------------------------------------------------
# Tests — Bulk data operations
# ---------------------------------------------------------------------------

class TestBulkInsert:
    def test_basic(self, db):
        conn = ls._get_conn("test")
        conn.execute("CREATE TABLE people (name TEXT, age INTEGER)")
        conn.commit()

        result = ls.bulk_insert(
            "test",
            "people",
            ["name", "age"],
            [["Alice", 30], ["Bob", 25], ["Charlie", 35]],
        )
        assert "3 rows" in result

        count = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
        assert count == 3

    def test_empty_rows(self, db):
        result = ls.bulk_insert("test", "t", ["a"], [])
        assert "No rows" in result

    def test_invalid_table_name(self, db):
        with pytest.raises(ValueError, match="Invalid table name"):
            ls.bulk_insert("test", "drop;--", ["a"], [["x"]])

    def test_invalid_column_name(self, db):
        conn = ls._get_conn("test")
        conn.execute("CREATE TABLE t (a TEXT)")
        conn.commit()
        with pytest.raises(ValueError, match="Invalid column name"):
            ls.bulk_insert("test", "t", ["a; DROP TABLE t"], [["x"]])

    def test_rollback_on_error(self, db):
        conn = ls._get_conn("test")
        conn.execute(
            "CREATE TABLE strict (id INTEGER PRIMARY KEY, val TEXT NOT NULL)"
        )
        conn.commit()

        # Second row violates NOT NULL
        result = ls.bulk_insert(
            "test",
            "strict",
            ["id", "val"],
            [[1, "ok"], [2, None]],
        )
        assert "error" in result.lower()

        # Should have rolled back — no rows
        count = conn.execute("SELECT COUNT(*) FROM strict").fetchone()[0]
        assert count == 0


class TestImportCsv:
    def test_basic_import(self, db):
        csv_text = "name,age\nAlice,30\nBob,25\n"
        result = ls.import_csv("test", csv_text, table="people")
        assert "2 rows" in result

        conn = ls._get_conn("test")
        rows = conn.execute("SELECT * FROM people").fetchall()
        assert len(rows) == 2

    def test_auto_create_table(self, db):
        csv_text = "x,y,z\n1,2,3\n4,5,6\n"
        result = ls.import_csv("test", csv_text, table="coords")
        assert "2 rows" in result
        assert "3 columns" in result

    def test_replace_mode(self, db):
        csv1 = "val\n1\n2\n"
        ls.import_csv("test", csv1, table="data")
        csv2 = "val\n3\n4\n5\n"
        result = ls.import_csv("test", csv2, table="data", if_exists="replace")
        assert "3 rows" in result

        conn = ls._get_conn("test")
        count = conn.execute("SELECT COUNT(*) FROM data").fetchone()[0]
        assert count == 3

    def test_empty_csv(self, db):
        result = ls.import_csv("test", "")
        assert "empty" in result.lower()

    def test_header_only(self, db):
        result = ls.import_csv("test", "a,b,c\n")
        assert "no data" in result.lower()

    def test_invalid_table_name(self, db):
        with pytest.raises(ValueError, match="Invalid table name"):
            ls.import_csv("test", "a\n1\n", table="bad name!")


class TestExportCsv:
    def test_basic_export(self, db):
        conn = ls._get_conn("test")
        conn.execute("CREATE TABLE items (id INTEGER, name TEXT)")
        conn.execute("INSERT INTO items VALUES (1, 'one')")
        conn.execute("INSERT INTO items VALUES (2, 'two')")
        conn.commit()

        result = ls.export_query_csv("test", "SELECT * FROM items ORDER BY id")
        reader = csv.reader(io.StringIO(result))
        rows = list(reader)
        assert rows[0] == ["id", "name"]
        assert rows[1] == ["1", "one"]
        assert rows[2] == ["2", "two"]

    def test_non_select(self, db):
        conn = ls._get_conn("test")
        conn.execute("CREATE TABLE t (a TEXT)")
        conn.commit()
        result = ls.export_query_csv("test", "INSERT INTO t VALUES ('x')")
        assert "did not return results" in result.lower()


# ---------------------------------------------------------------------------
# Tests — Schema inspection and management
# ---------------------------------------------------------------------------

class TestListTables:
    def test_no_tables(self, db):
        result = ls.list_tables("test")
        assert "No tables" in result

    def test_with_tables(self, db):
        conn = ls._get_conn("test")
        conn.execute("CREATE TABLE alpha (id INTEGER)")
        conn.execute("CREATE TABLE beta (id INTEGER)")
        conn.commit()

        result = ls.list_tables("test")
        assert "alpha" in result
        assert "beta" in result


class TestDescribeTable:
    def test_basic(self, db):
        conn = ls._get_conn("test")
        conn.execute(
            "CREATE TABLE items ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  name TEXT NOT NULL,"
            "  value REAL DEFAULT 0.0"
            ")"
        )
        conn.commit()

        result = ls.describe_table("test", "items")
        assert "id" in result
        assert "name" in result
        assert "TEXT" in result
        assert "DDL" in result

    def test_nonexistent(self, db):
        result = ls.describe_table("test", "nonexistent")
        assert "not found" in result


class TestDropTable:
    def test_preview(self, db):
        conn = ls._get_conn("test")
        conn.execute("CREATE TABLE doomed (a TEXT, b INTEGER)")
        conn.execute("INSERT INTO doomed VALUES ('x', 1)")
        conn.commit()

        result = ls.drop_table("test", "doomed")
        assert "Would drop" in result
        assert "1 rows" in result or "1 row" in result
        assert "2 columns" in result

        # Table should still exist
        tables = ls.list_tables("test")
        assert "doomed" in tables

    def test_confirmed(self, db):
        conn = ls._get_conn("test")
        conn.execute("CREATE TABLE doomed (a TEXT)")
        conn.execute("INSERT INTO doomed VALUES ('x')")
        conn.commit()

        result = ls.drop_table("test", "doomed", confirm=True)
        assert "Dropped" in result

        tables = ls.list_tables("test")
        assert "doomed" not in tables

    def test_nonexistent(self, db):
        result = ls.drop_table("test", "nope")
        assert "does not exist" in result


class TestRenameTable:
    def test_basic(self, db):
        conn = ls._get_conn("test")
        conn.execute("CREATE TABLE old_name (a TEXT)")
        conn.commit()

        result = ls.rename_table("test", "old_name", "new_name")
        assert "Renamed" in result

        tables = ls.list_tables("test")
        assert "new_name" in tables
        assert "old_name" not in tables

    def test_nonexistent_source(self, db):
        result = ls.rename_table("test", "nope", "also_nope")
        assert "does not exist" in result

    def test_invalid_new_name(self, db):
        conn = ls._get_conn("test")
        conn.execute("CREATE TABLE valid (a TEXT)")
        conn.commit()

        with pytest.raises(ValueError, match="Invalid"):
            ls.rename_table("test", "valid", "bad name!")


# ---------------------------------------------------------------------------
# Tests — Backup and compact
# ---------------------------------------------------------------------------

class TestBackupDatabase:
    def test_basic_backup(self, db, tmp_path):
        conn = ls._get_conn("test")
        conn.execute("CREATE TABLE data (val TEXT)")
        conn.execute("INSERT INTO data VALUES ('hello')")
        conn.commit()

        dest = str(tmp_path / "backup.db")
        result = ls.backup_database("test", dest)
        assert "Backed up" in result
        assert Path(dest).exists()

        # Verify the backup has the data
        backup_conn = sqlite3.connect(dest)
        row = backup_conn.execute("SELECT val FROM data").fetchone()
        assert row[0] == "hello"
        backup_conn.close()

    def test_destination_exists(self, db, tmp_path):
        dest = str(tmp_path / "existing.db")
        Path(dest).touch()
        result = ls.backup_database("test", dest)
        assert "already exists" in result


class TestCompactDatabase:
    def test_basic_vacuum(self, db):
        conn = ls._get_conn("test")
        conn.execute("CREATE TABLE big (data TEXT)")
        # Insert some data then delete it — VACUUM should reclaim space
        for i in range(100):
            conn.execute("INSERT INTO big VALUES (?)", ("x" * 1000,))
        conn.commit()
        conn.execute("DELETE FROM big")
        conn.commit()

        result = ls.compact_database("test")
        assert "Compacted" in result


# ---------------------------------------------------------------------------
# Tests — MCP Prompts (they just return strings, so test they run)
# ---------------------------------------------------------------------------

class TestPrompts:
    def test_data_survey(self):
        result = ls.data_survey("test items", "name, category, status")
        assert "test items" in result
        assert "bulk_insert" in result

    def test_error_triage(self):
        result = ls.error_triage("compiler output")
        assert "compiler output" in result
        assert "bulk_insert" in result.lower() or "COMMIT" in result

    def test_comparison(self):
        result = ls.comparison("expected files", "actual files")
        assert "expected files" in result
        assert "actual files" in result


# ---------------------------------------------------------------------------
# Tests — Security
# ---------------------------------------------------------------------------

class TestEscapeIdentifier:
    def test_plain_name(self):
        assert ls._escape_identifier("foo") == "foo"

    def test_embedded_quote(self):
        assert ls._escape_identifier('foo"bar') == 'foo""bar'

    def test_empty(self):
        assert ls._escape_identifier("") == ""


class TestPathSandbox:
    """Test that LUMEN_SQLITE_SANDBOX restricts paths to _DB_DIR."""

    def test_sandbox_blocks_absolute_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ls, "_SANDBOX", True)
        outside = str(tmp_path / "escape.db")
        with pytest.raises(ValueError, match="outside the allowed directory"):
            ls._resolve_db_path(outside)

    def test_sandbox_blocks_traversal(self, monkeypatch):
        monkeypatch.setattr(ls, "_SANDBOX", True)
        with pytest.raises(ValueError, match="outside the allowed directory"):
            ls._resolve_db_path("../../escape.db")

    def test_sandbox_allows_relative_in_db_dir(self, monkeypatch):
        monkeypatch.setattr(ls, "_SANDBOX", True)
        # Should succeed — relative path resolves inside _DB_DIR
        p = ls._resolve_db_path("allowed.db")
        assert str(p).startswith(str(ls._DB_DIR))

    def test_no_sandbox_allows_absolute(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ls, "_SANDBOX", False)
        outside = str(tmp_path / "anywhere.db")
        # Should succeed — sandbox is off
        p = ls._resolve_db_path(outside)
        assert p == tmp_path / "anywhere.db"


class TestDescribeTableValidation:
    def test_rejects_bad_table_name(self, db):
        with pytest.raises(ValueError, match="Invalid table name"):
            ls.describe_table("test", "drop;--")

    def test_rejects_space_in_name(self, db):
        with pytest.raises(ValueError, match="Invalid table name"):
            ls.describe_table("test", "my table")


class TestDropTableValidation:
    def test_rejects_bad_table_name(self, db):
        with pytest.raises(ValueError, match="Invalid table name"):
            ls.drop_table("test", "Robert'); DROP TABLE students;--")

    def test_rejects_quotes_in_name(self, db):
        with pytest.raises(ValueError, match="Invalid table name"):
            ls.drop_table("test", 'foo"bar')


class TestExportCsvBounded:
    def test_truncation(self, db):
        conn = ls._get_conn("test")
        conn.execute("CREATE TABLE big (n INTEGER)")
        conn.executemany(
            "INSERT INTO big VALUES (?)", [(i,) for i in range(200)]
        )
        conn.commit()

        result = ls.export_query_csv(
            "test", "SELECT * FROM big", max_rows=10
        )
        # Should have header + 10 data rows + truncation notice
        lines = [l for l in result.strip().split("\n") if l]
        data_lines = [l for l in lines if not l.startswith("#")]
        assert len(data_lines) == 11  # 1 header + 10 rows
        assert "truncated" in result

    def test_no_truncation_when_under_limit(self, db):
        conn = ls._get_conn("test")
        conn.execute("CREATE TABLE small (n INTEGER)")
        conn.executemany(
            "INSERT INTO small VALUES (?)", [(i,) for i in range(5)]
        )
        conn.commit()

        result = ls.export_query_csv(
            "test", "SELECT * FROM small", max_rows=100
        )
        assert "truncated" not in result


class TestDatabaseInfoEscaping:
    """Verify database_info handles tables with unusual names safely."""

    def test_table_with_quote_in_name(self, db):
        conn = ls._get_conn("test")
        # Create a table with a double-quote in the name (unusual but valid)
        conn.execute('CREATE TABLE "weird""name" (id INTEGER)')
        conn.execute('INSERT INTO "weird""name" VALUES (1)')
        conn.commit()

        # database_info should handle this without crashing
        result = ls.database_info("test")
        assert isinstance(result, str)
        # The table should appear in the output
        assert 'weird"name' in result or "weird" in result
