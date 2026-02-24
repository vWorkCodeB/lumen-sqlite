# lumen-sqlite

An MCP server that gives AI language models the ability to create, query, and
manage structured data using [SQLite](https://www.sqlite.org/).

## Why would an AI want a database?

Language models are great at reasoning over text, but some problems are easier
when you can **store, query, and cross-reference structured data**:

- **Surveying many items** — scanning hundreds of files or components and
  recording properties for each one, then querying for patterns.
- **Triaging errors** — loading build errors or test failures into a table,
  grouping by error code, tracking fix progress.
- **Comparing sets** — two lists that should match (expected vs. actual),
  using JOINs and EXCEPT to find the differences instantly.
- **Dependency analysis** — mapping relationships between components and
  querying "what depends on X?" or "what does Y depend on?"
- **Progress tracking** — working through a list of N tasks, recording
  status, and querying what's done vs. what's next.

SQLite is ideal for this: it's serverless, zero-configuration, stores
everything in a single file, and Python ships with it built in.

## Features

**18 tools** for complete database lifecycle management:

| Tool | Description |
|------|-------------|
| `open_database` | Open or create a `.db` / `.sqlite` / `.sqlite3` file |
| `close_database` | Close an open database connection |
| `list_databases` | List currently open database aliases |
| `list_database_files` | Discover `.db` files in the data directory (including from prior sessions) |
| `database_info` | One-call summary: `_readme`, all tables with row counts and schemas |
| `create_readme` | Create a self-documenting `_readme` table |
| `execute_sql` | Execute a single SQL statement with optional parameter binding |
| `execute_script` | Execute multiple SQL statements (DDL batches, schema setup) |
| `bulk_insert` | Insert many rows in a single transaction-wrapped call |
| `import_csv` | Import CSV text into a table (auto-creates from headers) |
| `export_query_csv` | Run a SELECT and return results as CSV |
| `list_tables` | List all tables in a database |
| `describe_table` | Show column names, types, constraints, and DDL |
| `drop_table` | Drop a table with safety confirmation |
| `rename_table` | Rename a table |
| `explain_query` | Show the query execution plan (EXPLAIN QUERY PLAN) |
| `compact_database` | Reclaim space after deletes (VACUUM) |
| `backup_database` | Safely copy a database to a new file |

**3 prompt templates** for common workflows:
- `data_survey` — systematic cataloging of items
- `error_triage` — build/test error management
- `comparison` — comparing two sets of items

**Built-in guidance** — the server sends comprehensive instructions to the
AI at connection time, covering when to use SQLite, schema design tips,
transaction best practices, and the `_readme` self-documentation convention.

## Installation

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/sivadeilra/lumen-sqlite.git
cd lumen-sqlite
uv sync
```

## Running

```bash
# Run the server (stdio transport for MCP clients)
uv run python lumen_sqlite.py

# Test interactively with the MCP inspector
uv run mcp dev lumen_sqlite.py
```

## Client Configuration

### VS Code (Copilot / GitHub Copilot Chat)

Add to your `.vscode/mcp.json`:

```json
{
  "servers": {
    "lumen-sqlite": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--project", "/path/to/lumen-sqlite", "python", "lumen_sqlite.py"],
      "cwd": "/path/to/lumen-sqlite",
      "env": {
        "LUMEN_SQLITE_DB_DIR": "/path/to/your/data/directory"
      }
    }
  }
}
```

### Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "lumen-sqlite": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/lumen-sqlite", "python", "lumen_sqlite.py"],
      "env": {
        "LUMEN_SQLITE_DB_DIR": "/path/to/your/data/directory"
      }
    }
  }
}
```

### Other MCP clients

The server uses **stdio transport**. Point your MCP client at:
```
uv run --project /path/to/lumen-sqlite python lumen_sqlite.py
```

## Configuration

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `LUMEN_SQLITE_DB_DIR` | `.` (current directory) | Base directory for resolving relative database paths |

## Usage Examples

### Create a database and query it

```
→ open_database("survey.db")
← Created and opened database 'survey' at /data/survey.db

→ execute_script("survey", """
    CREATE TABLE items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        category TEXT,
        status TEXT DEFAULT 'pending'
    );
  """)
← Script executed successfully.

→ bulk_insert("survey", "items", ["name", "category"],
    [["Widget A", "hardware"], ["Widget B", "software"], ["Widget C", "hardware"]])
← Inserted 3 rows into 'items'.

→ execute_sql("survey", "SELECT category, COUNT(*) as n FROM items GROUP BY category")
← category	n
   hardware	2
   software	1
```

### Import CSV data

```
→ import_csv("survey", "name,score\nAlice,95\nBob,87\nCarol,92", table="students")
← Imported 3 rows into 'students' (2 columns: name, score).
```

### Discover databases from prior sessions

```
→ list_database_files()
← Database files in /data:
  survey.db          12.3 KB  2026-02-23 14:30
  errors.db           8.1 KB  2026-02-22 09:15
```

## The `_readme` Convention

When an AI creates a database for its own use, it should create a `_readme`
table as the first thing it does:

```sql
CREATE TABLE _readme (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO _readme VALUES ('purpose', 'Tracking build errors from Clang porting');
INSERT INTO _readme VALUES ('created', datetime('now'));
INSERT INTO _readme VALUES ('table:errors', 'Individual build errors with fix status');
```

The underscore prefix sorts `_readme` to the top of table listings. Any
future session that opens the database can read `_readme` first to understand
what it's looking at. The `create_readme` tool automates this.

## Safety

- Only files with `.db`, `.sqlite`, or `.sqlite3` extensions can be opened.
- Table and column names are validated against a safe identifier pattern.
- Foreign keys are enforced on all connections.
- WAL journal mode is enabled for better concurrency.
- Write operations are logged to stderr.

## Performance

Bulk operations use explicit transactions automatically. The difference is
dramatic:

| Rows | Without transaction | With transaction | Speedup |
|-----:|--------------------:|-----------------:|--------:|
| 100 | 53 ms | 1 ms | **39×** |
| 1,000 | 548 ms | 2 ms | **357×** |
| 10,000 | 4,395 ms | 13 ms | **337×** |

At scale with transactions: ~1 million rows/second.

## Development

```bash
# Run tests
uv run pytest tests/ -v

# Run with debug logging
LUMEN_SQLITE_LOG_LEVEL=DEBUG uv run python lumen_sqlite.py
```

## License

[MIT](LICENSE)
