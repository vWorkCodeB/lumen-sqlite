"""
lumen-sqlite — An MCP server for structured data storage using SQLite.

This server exposes tools that let a language model create, query, and manage
SQLite databases through the Model Context Protocol.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Logging — stderr only (stdout is the MCP transport)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("lumen-sqlite")

# ---------------------------------------------------------------------------
# Server instructions — sent to every MCP client at connection time
# ---------------------------------------------------------------------------
SERVER_INSTRUCTIONS = """
# lumen-sqlite — Structured Data Workbench

You have access to a SQLite database server. SQLite is an embedded SQL database
engine — the world's most widely deployed database. Each database is a single
file. There is no server process to manage.

## When to use this tool

Use SQLite when your task involves **structured data that benefits from
querying, filtering, aggregating, joining, or sorting.** Specific signals:

- You're working with a collection of items that share a schema (rows of data).
- You need to answer questions like "how many?", "which ones match?",
  "what's the average?", "group by category", "find duplicates".
- You're comparing two sets of things (e.g., files expected vs. files found).
- You're tracking progress across many items (build errors, porting status).
- You need to cross-reference relationships between entities.
- You're accumulating data over multiple steps and will query it later.

## The _readme convention

When you create a database for your own use, always create a table named
`_readme` as the first thing you do. This table is self-documentation:
it describes the purpose of the database and its tables so that any future
session (or any other AI) can understand what the database is for.

Schema:
    CREATE TABLE _readme (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

Required rows:
    INSERT INTO _readme VALUES ('purpose', '<one-sentence description>');
    INSERT INTO _readme VALUES ('created', datetime('now'));

Recommended additional rows — one per table:
    INSERT INTO _readme VALUES ('table:<name>', '<what this table holds>');

The underscore prefix sorts _readme to the top of table listings. Read it
first when opening an unfamiliar database. Do NOT create _readme for
databases you received from external sources — only for databases you create.

A helper function `create_readme` is available as a tool; prefer using it.

## When NOT to use this tool

- For narrative text, documentation, or free-form notes — use files instead.
- For data you'll only read once and never query — just keep it in memory.
- For data that needs to survive beyond this session unless saved to a known
  path the user can find.

## Workflow pattern

1. **open_database** — Create or open a .db file. Pick a descriptive alias.
2. **Design your schema** — Think about what you'll query before creating
   tables. Use appropriate types (INTEGER, TEXT, REAL, BLOB) and constraints
   (NOT NULL, UNIQUE, CHECK, FOREIGN KEY, DEFAULT).
3. **execute_sql** — Create tables, insert data, run queries. One statement
   at a time. Use parameterized queries (? placeholders) for user-provided
   values to prevent SQL injection.
4. **execute_script** — For multi-statement DDL (creating several tables and
   indices at once), use execute_script instead of calling execute_sql
   repeatedly. Separate statements with semicolons.
5. **bulk_insert** — For loading many rows at once, use bulk_insert. It wraps
   all inserts in a single transaction for performance (see below).
6. **import_csv** — To load CSV text directly into a table (auto-creates the
   table from headers if it doesn't exist).
7. **Query iteratively** — Start with broad queries, then refine. Use
   GROUP BY, JOINs, subqueries, window functions as needed. SQLite supports
   most of SQL-92 and many modern features (CTEs, window functions, JSON).
8. **export_query_csv** — When you need to share results or feed them into
   another tool.
9. **close_database** — When you're done. Open databases consume resources.

## Discovering databases

- **list_databases** — Shows currently open databases.
- **list_database_files** — Shows .db files in the data directory, including
  ones from prior sessions that aren't currently open.
- **database_info** — One-call summary of an open database: _readme, all
  tables with row counts, and column schemas. Use this instead of calling
  list_tables + describe_table + SELECT COUNT(*) repeatedly.

## Transaction guidance — bulk loading

**Always wrap bulk inserts in an explicit transaction.** Without a transaction,
SQLite commits after every single INSERT, which means a separate disk flush
per row. With an explicit transaction, all rows are committed in one flush.

The performance difference is dramatic — typically 50–100× faster for
hundreds or thousands of rows.

Pattern using execute_script:
    BEGIN TRANSACTION;
    INSERT INTO t VALUES (...);
    INSERT INTO t VALUES (...);
    -- ... many more rows ...
    COMMIT;

Or better, use the **bulk_insert** tool, which handles the transaction
wrapping automatically and uses parameterized queries for safety.

Even when using execute_sql in a loop (one INSERT per call), you can
manually manage the transaction:
    execute_sql(alias, "BEGIN TRANSACTION")
    -- loop: execute_sql(alias, "INSERT INTO t VALUES (?, ?)", [...])
    execute_sql(alias, "COMMIT")

## Schema design tips

- Use INTEGER PRIMARY KEY AUTOINCREMENT for synthetic IDs.
- Use TEXT with CHECK constraints for enumerated values.
- Use DEFAULT (datetime('now')) for automatic timestamps.
- Create indices on columns you'll filter or join on frequently.
- Use foreign keys to express relationships; they're enforced by this server.
- Normalize when querying matters more than insert speed.
- Denormalize when you need simpler queries and data won't change much.

## SQLite-specific notes

- SQLite uses dynamic typing. The column type is a hint, not a constraint.
  INTEGER, TEXT, REAL, BLOB, and NULL are the five storage classes.
- AUTOINCREMENT guarantees monotonically increasing IDs but is slightly
  slower than plain INTEGER PRIMARY KEY.
- Use datetime('now') for UTC timestamps in ISO-8601 format.
- GROUP_CONCAT aggregates multiple values into a delimited string.
- The || operator concatenates strings.
- LIKE is case-insensitive for ASCII. Use GLOB for case-sensitive matching.
- CTEs (WITH ... AS) are excellent for readable complex queries.
- Window functions (ROW_NUMBER, RANK, LAG, LEAD, SUM OVER) are supported.
- Full-text search (FTS5) is available via CREATE VIRTUAL TABLE.

## Reference

- SQL syntax: https://www.sqlite.org/lang.html
- Built-in functions: https://www.sqlite.org/lang_corefunc.html
- Aggregate functions: https://www.sqlite.org/lang_aggfunc.html
- Date/time functions: https://www.sqlite.org/lang_datefunc.html
- Window functions: https://www.sqlite.org/windowfunctions.html
- JSON functions: https://www.sqlite.org/json1.html
- Full-text search: https://www.sqlite.org/fts5.html
- PRAGMA statements: https://www.sqlite.org/pragma.html
""".strip()

# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------
mcp = FastMCP("lumen-sqlite", instructions=SERVER_INSTRUCTIONS)

# ---------------------------------------------------------------------------
# State: open database connections keyed by a short alias
# ---------------------------------------------------------------------------
_connections: dict[str, sqlite3.Connection] = {}

# Allowed extensions for database files (safety measure)
_ALLOWED_EXTENSIONS = {".db", ".sqlite", ".sqlite3"}

# Base directory for resolving relative database paths.
# Can be overridden via the LUMEN_SQLITE_DB_DIR environment variable.
_DB_DIR: Path = Path(os.environ.get("LUMEN_SQLITE_DB_DIR", ".")).resolve()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_db_path(path_str: str) -> Path:
    """Resolve and validate a database file path."""
    p = Path(path_str)
    if not p.is_absolute():
        p = _DB_DIR / p
    p = p.resolve()

    if p.suffix.lower() not in _ALLOWED_EXTENSIONS:
        raise ValueError(
            f"File extension '{p.suffix}' is not allowed. "
            f"Use one of: {', '.join(sorted(_ALLOWED_EXTENSIONS))}"
        )
    return p


def _get_conn(alias: str) -> sqlite3.Connection:
    """Retrieve an open connection by alias, or raise a clear error."""
    if alias not in _connections:
        available = ", ".join(sorted(_connections)) if _connections else "(none)"
        raise ValueError(
            f"No open database with alias '{alias}'. "
            f"Open databases: {available}"
        )
    return _connections[alias]


def _format_rows(
    cursor: sqlite3.Cursor,
    max_rows: int = 500,
) -> dict[str, Any]:
    """Fetch rows from a cursor and return a structured result."""
    if cursor.description is None:
        rc = cursor.rowcount
        if rc < 0:
            return {
                "rowcount": 0,
                "message": "Statement executed successfully.",
            }
        return {
            "rowcount": rc,
            "message": f"{rc} row(s) affected.",
        }

    columns = [desc[0] for desc in cursor.description]
    rows = cursor.fetchmany(max_rows)
    truncated = len(rows) == max_rows and cursor.fetchone() is not None

    return {
        "columns": columns,
        "rows": [list(row) for row in rows],
        "row_count": len(rows),
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def open_database(path: str, alias: str | None = None) -> str:
    """Open (or create) a SQLite database file.

    Args:
        path: Path to the .db / .sqlite / .sqlite3 file.
              Relative paths are resolved against the server's base directory.
        alias: Short name to reference this database in subsequent calls.
               Defaults to the file stem (e.g. "mydata" for "mydata.db").
    """
    db_path = _resolve_db_path(path)
    if alias is None:
        alias = db_path.stem

    if alias in _connections:
        return f"Database '{alias}' is already open ({_connections[alias]}). Close it first or choose a different alias."

    creating = not db_path.exists()
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")   # better concurrency
    conn.execute("PRAGMA foreign_keys=ON")     # enforce FK constraints
    _connections[alias] = conn

    verb = "Created and opened" if creating else "Opened"
    log.info("%s database '%s' at %s", verb, alias, db_path)
    return f"{verb} database '{alias}' at {db_path}"


@mcp.tool()
def create_readme(alias: str, purpose: str, tables: dict[str, str] | None = None) -> str:
    """Create or replace a _readme table that documents the database.

    Call this immediately after creating a new database for your own use.
    Do NOT call this for databases received from external sources.

    The _readme table serves as self-documentation: any future session or
    any other AI that opens this database can read _readme to understand
    what the database is for and what each table contains.

    Args:
        alias: The alias of the database.
        purpose: A one-sentence description of the database's purpose.
        tables: Optional dict mapping table names to descriptions.
                e.g. {"tools": "SDK tools and their porting status",
                       "warnings": "Clang warning categories encountered"}
    """
    conn = _get_conn(alias)
    try:
        conn.execute("DROP TABLE IF EXISTS _readme")
        conn.execute("""
            CREATE TABLE _readme (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.execute(
            "INSERT INTO _readme VALUES ('purpose', ?)", (purpose,)
        )
        conn.execute(
            "INSERT INTO _readme VALUES ('created', datetime('now'))"
        )
        if tables:
            for tbl_name, tbl_desc in tables.items():
                conn.execute(
                    "INSERT INTO _readme VALUES (?, ?)",
                    (f"table:{tbl_name}", tbl_desc),
                )
        conn.commit()
        log.info("Created _readme in '%s'", alias)
        n = 2 + (len(tables) if tables else 0)
        return f"Created _readme table with {n} entries in '{alias}'."
    except sqlite3.Error as exc:
        conn.rollback()
        return f"Error creating _readme: {exc}"


@mcp.tool()
def close_database(alias: str) -> str:
    """Close an open database connection.

    Args:
        alias: The alias of the database to close.
    """
    conn = _get_conn(alias)
    conn.close()
    del _connections[alias]
    log.info("Closed database '%s'", alias)
    return f"Closed database '{alias}'."


@mcp.tool()
def list_databases() -> list[str]:
    """List all currently open database aliases."""
    return sorted(_connections.keys())


@mcp.tool()
def list_database_files() -> str:
    """List .db files in the data directory, including ones not currently open.

    Useful for discovering databases from prior sessions. Returns file names,
    sizes, and modification times. Does not open the databases.
    """
    files = []
    for ext in sorted(_ALLOWED_EXTENSIONS):
        files.extend(_DB_DIR.glob(f"*{ext}"))

    if not files:
        return f"No database files found in {_DB_DIR}"

    lines = [f"Database files in {_DB_DIR}:", ""]
    lines.append(f"{'File':<40} {'Size':>10} {'Modified'}")
    lines.append("-" * 70)
    for f in sorted(files):
        stat = f.stat()
        size_kb = stat.st_size / 1024
        if size_kb < 1024:
            size_str = f"{size_kb:.1f} KB"
        else:
            size_str = f"{size_kb/1024:.1f} MB"
        mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime))
        lines.append(f"{f.name:<40} {size_str:>10} {mtime}")

    return "\n".join(lines)


@mcp.tool()
def database_info(alias: str) -> str:
    """Get a comprehensive summary of an open database in one call.

    Returns:
    - The _readme table contents (if present)
    - All table names with row counts
    - Column schemas for each table

    Use this instead of calling list_tables + describe_table + SELECT COUNT(*)
    repeatedly. One call, full picture.

    Args:
        alias: The alias of the database.
    """
    conn = _get_conn(alias)
    lines: list[str] = [f"Database: {alias}", "=" * 60]

    # _readme contents
    try:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='_readme'"
        )
        if cursor.fetchone():
            lines.append("")
            lines.append("## _readme")
            readme_cursor = conn.execute("SELECT key, value FROM _readme ORDER BY key")
            for key, value in readme_cursor:
                lines.append(f"  {key}: {value}")
    except sqlite3.Error:
        pass

    # All tables with row counts
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = [row[0] for row in cursor.fetchall()]

    if not tables:
        lines.append("")
        lines.append("(no tables)")
        return "\n".join(lines)

    lines.append("")
    lines.append("## Tables")
    lines.append("")

    for table in tables:
        if table == "sqlite_sequence":
            continue  # internal bookkeeping table

        # Row count
        try:
            count_cursor = conn.execute(f'SELECT COUNT(*) FROM "{table}"')
            count = count_cursor.fetchone()[0]
        except sqlite3.Error:
            count = "?"

        lines.append(f"### {table} ({count} rows)")

        # Column info
        try:
            col_cursor = conn.execute(f'PRAGMA table_info("{table}")')
            cols = col_cursor.fetchall()
            for col in cols:
                cid, name, col_type, notnull, default_val, pk = col
                parts = [f"  {name}"]
                if col_type:
                    parts.append(col_type)
                if pk:
                    parts.append("PK")
                if notnull:
                    parts.append("NOT NULL")
                if default_val is not None:
                    parts.append(f"DEFAULT {default_val}")
                lines.append(" ".join(parts))
        except sqlite3.Error:
            lines.append("  (unable to read schema)")

        lines.append("")

    return "\n".join(lines)


@mcp.tool()
def execute_sql(alias: str, sql: str, params: list[Any] | None = None) -> str:
    """Execute a SQL statement against an open database.

    Returns query results for SELECT statements, or an affected-row count
    for INSERT / UPDATE / DELETE / DDL statements.

    This tool executes exactly ONE SQL statement. For multiple statements
    (e.g., creating several tables at once), use execute_script instead.

    Use parameterized queries with ? placeholders for safety:
        sql: "INSERT INTO t (name, age) VALUES (?, ?)"
        params: ["Alice", 30]

    Args:
        alias: The alias of the database to query.
        sql: The SQL statement to execute.
        params: Optional list of bind parameters (for parameterized queries).
    """
    conn = _get_conn(alias)
    params = params or []

    try:
        cursor = conn.execute(sql, params)
        result = _format_rows(cursor)
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        return f"SQL error: {exc}"

    # Return a readable text representation
    if "columns" in result:
        lines = ["\t".join(result["columns"])]
        for row in result["rows"]:
            lines.append("\t".join(str(v) for v in row))
        if result["truncated"]:
            lines.append(f"... (results truncated at {result['row_count']} rows)")
        return "\n".join(lines)
    else:
        return result["message"]


@mcp.tool()
def execute_script(alias: str, script: str) -> str:
    """Execute multiple SQL statements in a single call.

    Use this for schema setup (CREATE TABLE, CREATE INDEX) or batch operations
    where you need several statements executed together. Statements should be
    separated by semicolons.

    Note: This does NOT support parameter binding. Do not use this for
    statements that include user-provided values — use execute_sql with
    params for those.

    Args:
        alias: The alias of the database.
        script: SQL script containing one or more statements separated by semicolons.
    """
    conn = _get_conn(alias)
    try:
        conn.executescript(script)
        log.info("Executed script on '%s'", alias)
        return "Script executed successfully."
    except sqlite3.Error as exc:
        return f"SQL error: {exc}"


@mcp.tool()
def bulk_insert(
    alias: str,
    table: str,
    columns: list[str],
    rows: list[list[Any]],
) -> str:
    """Insert many rows into a table in a single, transaction-wrapped call.

    All rows are inserted within an explicit transaction using executemany(),
    which is dramatically faster than individual INSERT calls (typically
    50-100x for hundreds of rows). The transaction is committed on success
    or rolled back on any error.

    Args:
        alias: The alias of the database.
        table: The table to insert into.
        columns: List of column names, e.g. ["name", "age", "city"].
        rows: List of rows, each row being a list of values matching the
              columns. e.g. [["Alice", 30, "NYC"], ["Bob", 25, "LA"]].
    """
    conn = _get_conn(alias)

    if not rows:
        return "No rows to insert."

    col_clause = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    sql = f'INSERT INTO "{table}" ({col_clause}) VALUES ({placeholders})'

    try:
        conn.execute("BEGIN TRANSACTION")
        conn.executemany(sql, rows)
        conn.execute("COMMIT")
        log.info("Bulk-inserted %d rows into '%s.%s'", len(rows), alias, table)
        return f"Inserted {len(rows)} rows into '{table}'."
    except sqlite3.Error as exc:
        conn.execute("ROLLBACK")
        return f"Bulk insert error (rolled back): {exc}"


@mcp.tool()
def import_csv(
    alias: str,
    csv_text: str,
    table: str | None = None,
    if_exists: str = "append",
) -> str:
    """Import CSV text into a table.

    If the table does not exist, it will be created automatically with
    column names taken from the CSV header row (all columns TEXT).

    All rows are inserted within a single transaction for performance.

    Args:
        alias: The alias of the database.
        csv_text: CSV-formatted text with a header row followed by data rows.
        table: Target table name. Defaults to "imported".
        if_exists: What to do if the table already exists:
                   "append" (default) — add rows to existing table.
                   "replace" — drop and recreate the table first.
    """
    conn = _get_conn(alias)
    table = table or "imported"

    reader = csv.reader(io.StringIO(csv_text))
    try:
        headers = next(reader)
    except StopIteration:
        return "CSV text is empty (no header row)."

    headers = [h.strip() for h in headers]
    if not headers:
        return "CSV header row is empty."

    rows = [row for row in reader if row]  # skip blank lines
    if not rows:
        return "CSV has a header row but no data rows."

    try:
        if if_exists == "replace":
            conn.execute(f'DROP TABLE IF EXISTS "{table}"')

        # Create table if it doesn't exist
        col_defs = ", ".join(f'"{h}" TEXT' for h in headers)
        conn.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({col_defs})')

        # Insert all rows in a transaction
        placeholders = ", ".join("?" for _ in headers)
        sql = f'INSERT INTO "{table}" VALUES ({placeholders})'
        conn.execute("BEGIN TRANSACTION")
        conn.executemany(sql, rows)
        conn.execute("COMMIT")

        log.info("Imported %d rows into '%s.%s'", len(rows), alias, table)
        return f"Imported {len(rows)} rows into '{table}' ({len(headers)} columns: {', '.join(headers)})."

    except sqlite3.Error as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        return f"Import error: {exc}"


@mcp.tool()
def list_tables(alias: str) -> str:
    """List all tables in the database.

    Args:
        alias: The alias of the database.
    """
    conn = _get_conn(alias)
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = [row[0] for row in cursor.fetchall()]
    if not tables:
        return "No tables found."
    return "\n".join(tables)


@mcp.tool()
def describe_table(alias: str, table: str) -> str:
    """Show the schema of a table — column names, types, and constraints.

    Args:
        alias: The alias of the database.
        table: The name of the table to describe.
    """
    conn = _get_conn(alias)

    # Get column info via PRAGMA
    cursor = conn.execute(f"PRAGMA table_info({table})")
    columns = cursor.fetchall()
    if not columns:
        return f"Table '{table}' not found."

    lines = [f"Table: {table}", ""]
    lines.append(f"{'#':<4} {'Name':<30} {'Type':<15} {'Nullable':<10} {'Default':<15} {'PK'}")
    lines.append("-" * 80)
    for col in columns:
        cid, name, col_type, notnull, default_val, pk = col
        nullable = "NO" if notnull else "YES"
        default_str = str(default_val) if default_val is not None else ""
        pk_str = "YES" if pk else ""
        lines.append(f"{cid:<4} {name:<30} {col_type:<15} {nullable:<10} {default_str:<15} {pk_str}")

    # Also show the CREATE TABLE statement for full context
    create_cursor = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    create_row = create_cursor.fetchone()
    if create_row and create_row[0]:
        lines.append("")
        lines.append("DDL:")
        lines.append(create_row[0])

    return "\n".join(lines)


@mcp.tool()
def export_query_csv(alias: str, sql: str, params: list[Any] | None = None) -> str:
    """Run a SELECT query and return results as CSV text.

    Useful for exporting data or feeding into other tools.

    Args:
        alias: The alias of the database.
        sql: A SELECT statement.
        params: Optional bind parameters.
    """
    conn = _get_conn(alias)
    params = params or []

    try:
        cursor = conn.execute(sql, params)
    except sqlite3.Error as exc:
        return f"SQL error: {exc}"

    if cursor.description is None:
        return "Statement did not return results."

    columns = [desc[0] for desc in cursor.description]
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(columns)
    for row in cursor:
        writer.writerow(row)

    return output.getvalue()


# ---------------------------------------------------------------------------
# MCP Prompts — reusable prompt templates for common patterns
# ---------------------------------------------------------------------------

@mcp.prompt()
def data_survey(topic: str, fields: str) -> str:
    """Create a plan to survey and catalog items using a SQLite database.

    Args:
        topic: What you're surveying (e.g. "source files", "build errors",
               "API endpoints", "components").
        fields: Comma-separated list of properties to track for each item.
    """
    field_list = [f.strip() for f in fields.split(",")]
    columns = "\n".join(f"    {f} TEXT," for f in field_list)
    return f"""I need to do a systematic survey of {topic}.

Here's my plan:
1. Create a database to track results.
2. Define a table with these fields: {', '.join(field_list)}
3. Systematically examine each item and record findings.
4. Use SQL queries to analyze patterns, counts, and groupings.

Suggested schema (including _readme for self-documentation):

    CREATE TABLE _readme (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    INSERT INTO _readme VALUES ('purpose', 'Survey of {topic}');
    INSERT INTO _readme VALUES ('created', datetime('now'));
    INSERT INTO _readme VALUES ('table:survey', 'Items surveyed with properties');

    CREATE TABLE survey (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
    {columns}
        notes TEXT,
        surveyed_at TEXT DEFAULT (datetime('now'))
    );

**Bulk loading tip:** When inserting many rows, always wrap them in a
transaction for dramatically better performance:

    BEGIN TRANSACTION;
    INSERT INTO survey (...) VALUES (...);
    INSERT INTO survey (...) VALUES (...);
    -- ... many more rows ...
    COMMIT;

Or better yet, use the `bulk_insert` tool which handles transaction
wrapping automatically and accepts a list of rows.

After collecting data, useful analysis queries include:
- COUNT and GROUP BY to find distributions
- WHERE filters to find items matching criteria
- ORDER BY to rank items
- JOINs if relationships exist between entities

Let me open a database and begin the survey."""


@mcp.prompt()
def error_triage(error_source: str) -> str:
    """Set up a database for triaging and tracking errors or warnings.

    Args:
        error_source: Where the errors come from (e.g. "build output",
                      "test results", "log file", "lint report").
    """
    return f"""I need to triage errors from {error_source}.

Plan:
1. Create a triage database with a table for each error.
2. Record: file, line, error code/type, message, severity, fix status.
3. Use GROUP BY to find the most common error types.
4. Track which errors I've fixed vs. which remain.

Suggested schema (including _readme):

    CREATE TABLE _readme (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    INSERT INTO _readme VALUES ('purpose', 'Error triage from {error_source}');
    INSERT INTO _readme VALUES ('created', datetime('now'));
    INSERT INTO _readme VALUES ('table:errors', 'Individual errors with status tracking');

    CREATE TABLE errors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file TEXT NOT NULL,
        line INTEGER,
        error_code TEXT,
        message TEXT NOT NULL,
        severity TEXT DEFAULT 'error',
        status TEXT DEFAULT 'open' CHECK(status IN ('open','fixed','wontfix','deferred')),
        fix_description TEXT,
        recorded_at TEXT DEFAULT (datetime('now'))
    );
    CREATE INDEX idx_errors_code ON errors(error_code);
    CREATE INDEX idx_errors_status ON errors(status);

**Bulk loading tip:** If you have many errors to record, wrap them in a
single transaction or use the `bulk_insert` tool:

    BEGIN TRANSACTION;
    INSERT INTO errors (file, line, error_code, message) VALUES (...);
    INSERT INTO errors (file, line, error_code, message) VALUES (...);
    COMMIT;

This is 50-100x faster than individual auto-committed inserts.

After recording all errors, I'll analyze with:
    SELECT error_code, COUNT(*) as n FROM errors GROUP BY error_code ORDER BY n DESC;
    SELECT file, COUNT(*) as n FROM errors GROUP BY file ORDER BY n DESC;
    SELECT status, COUNT(*) FROM errors GROUP BY status;

Let me create the database and start recording errors."""


@mcp.prompt()
def comparison(left_name: str, right_name: str) -> str:
    """Set up a database for comparing two sets of items.

    Args:
        left_name: Description of the first set (e.g. "expected files").
        right_name: Description of the second set (e.g. "actual files").
    """
    return f"""I need to compare two sets: "{left_name}" vs. "{right_name}".

Plan:
1. Create tables for each set with a shared key column.
2. Load both sets.
3. Use LEFT JOIN / RIGHT JOIN / EXCEPT to find:
   - Items only in "{left_name}" (missing from right)
   - Items only in "{right_name}" (unexpected)
   - Items in both (matched)
   - Items in both but with different properties (mismatched)

Suggested schema (including _readme):

    CREATE TABLE _readme (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    INSERT INTO _readme VALUES ('purpose', 'Comparison: {left_name} vs {right_name}');
    INSERT INTO _readme VALUES ('created', datetime('now'));
    INSERT INTO _readme VALUES ('table:left_set', '{left_name}');
    INSERT INTO _readme VALUES ('table:right_set', '{right_name}');

    CREATE TABLE left_set (
        key TEXT PRIMARY KEY,
        -- add property columns as needed
    );
    CREATE TABLE right_set (
        key TEXT PRIMARY KEY,
        -- matching property columns
    );

**Bulk loading tip:** When loading the sets, use `bulk_insert` or wrap
inserts in a transaction (BEGIN TRANSACTION / INSERT ... / COMMIT) for
dramatically better performance — especially when comparing large sets.

Analysis queries:
    -- Only in left
    SELECT key FROM left_set WHERE key NOT IN (SELECT key FROM right_set);
    -- Only in right
    SELECT key FROM right_set WHERE key NOT IN (SELECT key FROM left_set);
    -- In both
    SELECT l.key FROM left_set l JOIN right_set r ON l.key = r.key;

Let me set this up."""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    log.info("lumen-sqlite MCP server starting (db_dir=%s)", _DB_DIR)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()