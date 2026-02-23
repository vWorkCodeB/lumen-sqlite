# lumen-sqlite

An MCP server that gives language models the ability to store and manipulate
structured data using [SQLite](https://www.sqlite.org/).

## Why SQLite?

SQLite is an in-process, serverless, zero-configuration SQL database engine.
It stores an entire database — tables, indices, triggers, views — in a single
file. There's no server to install, no daemon to manage, no credentials to
configure. Python ships with the `sqlite3` module built in.

For an MCP server, this means the LLM can create, query, and manage relational
databases with nothing more than a file path.

## Capabilities

This server exposes the following MCP tools:

| Tool | Description |
|------|-------------|
| `open_database` | Open (or create) a SQLite database file |
| `close_database` | Close an open database connection |
| `list_databases` | List all currently open databases |
| `execute_sql` | Execute a SQL statement (DDL, DML, or query) |
| `describe_table` | Show column names, types, and constraints for a table |
| `list_tables` | List all tables in a database |
| `export_query_csv` | Run a SELECT and return results as CSV text |

## Quick Start

```bash
# Install dependencies
uv sync

# Run the server (stdio transport for MCP hosts)
uv run lumen_sqlite.py

# Or test interactively with the MCP inspector
uv run mcp dev lumen_sqlite.py
```

## Configuration

The server accepts an optional `--db-dir` argument to set a base directory for
database files. If not specified, database paths are resolved relative to the
current working directory.

## Safety

- Only files with a `.db`, `.sqlite`, or `.sqlite3` extension can be opened.
- The server will not follow symlinks outside the base directory.
- Write operations are logged to stderr.

## License

MIT
