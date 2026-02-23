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
# Server instance
# ---------------------------------------------------------------------------
mcp = FastMCP("lumen-sqlite")

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
        return {
            "rowcount": cursor.rowcount,
            "message": f"{cursor.rowcount} row(s) affected.",
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
def execute_sql(alias: str, sql: str, params: list[Any] | None = None) -> str:
    """Execute a SQL statement against an open database.

    Returns query results for SELECT statements, or an affected-row count
    for INSERT / UPDATE / DELETE / DDL statements.

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
# Entry point
# ---------------------------------------------------------------------------

def main():
    log.info("lumen-sqlite MCP server starting (db_dir=%s)", _DB_DIR)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
