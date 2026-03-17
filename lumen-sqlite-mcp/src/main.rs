//! lumen-sqlite — An MCP server for structured data storage using SQLite.
//!
//! This server exposes tools that let a language model create, query, and manage
//! SQLite databases through the Model Context Protocol.

use std::collections::HashMap;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use csv as csv_crate;
use rmcp::handler::server::router::tool::ToolRouter;
use rmcp::handler::server::wrapper::Parameters;
use rmcp::model::{ServerCapabilities, ServerInfo};
use rmcp::schemars::JsonSchema;
use rmcp::{ServerHandler, ServiceExt, tool, tool_handler, tool_router};
use rusqlite::types::{ToSqlOutput, Value as SqlValue, ValueRef};
use rusqlite::{Connection, MAIN_DB};
use serde::Deserialize;
use tracing::info;

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

/// An open database connection with its resolved file path.
struct OpenDatabase {
    conn: Connection,
    path: PathBuf,
}

/// Server state shared across all tool invocations.
pub struct LumenSqlite {
    /// Tool router for MCP dispatch.
    tool_router: ToolRouter<Self>,
    /// Open database connections keyed by alias.
    connections: Mutex<HashMap<String, OpenDatabase>>,
    /// Base directory for resolving relative database paths.
    db_dir: PathBuf,
    /// Maximum rows returned by execute_sql.
    default_max_rows: usize,
    /// Maximum rows for export_query_csv.
    default_export_max_rows: usize,
    /// When true, all paths must resolve under db_dir.
    sandbox: bool,
}

/// Allowed file extensions for database files.
const ALLOWED_EXTENSIONS: &[&str] = &["db", "sqlite", "sqlite3"];

impl LumenSqlite {
    pub fn new() -> Self {
        let db_dir = std::env::var("LUMEN_SQLITE_DB_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|_| std::env::current_dir().unwrap_or_else(|_| PathBuf::from(".")));
        let db_dir = db_dir.canonicalize().unwrap_or_else(|_| db_dir.clone());

        let default_max_rows = std::env::var("LUMEN_SQLITE_MAX_ROWS")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(500);

        let default_export_max_rows = std::env::var("LUMEN_SQLITE_EXPORT_MAX_ROWS")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(10_000);

        let sandbox = std::env::var("LUMEN_SQLITE_SANDBOX")
            .map(|v| matches!(v.to_lowercase().as_str(), "1" | "true" | "yes"))
            .unwrap_or(false);

        Self {
            tool_router: Self::tool_router(),
            connections: Mutex::new(HashMap::new()),
            db_dir,
            default_max_rows,
            default_export_max_rows,
            sandbox,
        }
    }

    /// Resolve and validate a database file path.
    fn resolve_db_path(&self, path_str: &str) -> Result<PathBuf, String> {
        let p = Path::new(path_str);
        let p = if p.is_absolute() {
            p.to_path_buf()
        } else {
            self.db_dir.join(p)
        };

        // Canonicalize if the file already exists; otherwise canonicalize the parent.
        let resolved = if p.exists() {
            p.canonicalize().map_err(|e| format!("Cannot resolve path: {e}"))?
        } else {
            let parent = p
                .parent()
                .ok_or_else(|| "Invalid path: no parent directory".to_string())?;
            let parent = if parent.exists() {
                parent
                    .canonicalize()
                    .map_err(|e| format!("Cannot resolve parent: {e}"))?
            } else {
                return Err(format!(
                    "Parent directory '{}' does not exist",
                    parent.display()
                ));
            };
            parent.join(p.file_name().unwrap())
        };

        // Check extension
        let ext = resolved
            .extension()
            .and_then(|e| e.to_str())
            .unwrap_or("");
        if !ALLOWED_EXTENSIONS.contains(&ext) {
            return Err(format!(
                "File extension '.{ext}' is not allowed. Use one of: {}",
                ALLOWED_EXTENSIONS
                    .iter()
                    .map(|e| format!(".{e}"))
                    .collect::<Vec<_>>()
                    .join(", ")
            ));
        }

        // Sandbox check
        if self.sandbox && !resolved.starts_with(&self.db_dir) {
            return Err(format!(
                "Path '{}' is outside the allowed directory '{}'. \
                 Sandbox mode is enabled (LUMEN_SQLITE_SANDBOX=1).",
                resolved.display(),
                self.db_dir.display()
            ));
        }

        Ok(resolved)
    }
}

// ---------------------------------------------------------------------------
// SQL value conversion helpers
// ---------------------------------------------------------------------------

/// A JSON value that can be passed as a SQLite bound parameter.
struct JsonParam(serde_json::Value);

impl rusqlite::ToSql for JsonParam {
    fn to_sql(&self) -> rusqlite::Result<ToSqlOutput<'_>> {
        Ok(match &self.0 {
            serde_json::Value::Null => ToSqlOutput::Owned(SqlValue::Null),
            serde_json::Value::Bool(b) => ToSqlOutput::Owned(SqlValue::Integer(*b as i64)),
            serde_json::Value::Number(n) => {
                if let Some(i) = n.as_i64() {
                    ToSqlOutput::Owned(SqlValue::Integer(i))
                } else {
                    ToSqlOutput::Owned(SqlValue::Real(n.as_f64().unwrap_or(0.0)))
                }
            }
            serde_json::Value::String(s) => ToSqlOutput::Owned(SqlValue::Text(s.clone())),
            other => ToSqlOutput::Owned(SqlValue::Text(other.to_string())),
        })
    }
}

fn value_ref_to_string(vr: ValueRef<'_>) -> String {
    match vr {
        ValueRef::Null => String::new(),
        ValueRef::Integer(i) => i.to_string(),
        ValueRef::Real(f) => f.to_string(),
        ValueRef::Text(t) => String::from_utf8_lossy(t).into_owned(),
        ValueRef::Blob(b) => format!("<blob {} bytes>", b.len()),
    }
}

/// Validate that a name is a safe SQL identifier.
fn validate_identifier(name: &str, kind: &str) -> Result<(), String> {
    if name.is_empty() {
        return Err(format!("Invalid {kind}: empty string is not allowed."));
    }
    let mut chars = name.chars();
    let first = chars.next().unwrap();
    if !first.is_ascii_alphabetic() && first != '_' {
        return Err(format!(
            "Invalid {kind}: {name:?}. Must start with a letter or underscore."
        ));
    }
    if !name.chars().all(|c| c.is_ascii_alphanumeric() || c == '_') {
        return Err(format!(
            "Invalid {kind}: {name:?}. Must match [A-Za-z_][A-Za-z0-9_]*"
        ));
    }
    Ok(())
}

/// Escape a SQL identifier for use inside double-quotes.
fn escape_identifier(name: &str) -> String {
    name.replace('"', "\"\"")
}

// ---------------------------------------------------------------------------
// Tool parameter types
// ---------------------------------------------------------------------------

#[derive(Deserialize, JsonSchema)]
pub struct OpenDatabaseParams {
    /// Path to the .db / .sqlite / .sqlite3 file.
    /// Relative paths are resolved against the server's base directory.
    pub path: String,
    /// Short alias for this database (defaults to file stem).
    pub alias: Option<String>,
}

#[derive(Deserialize, JsonSchema)]
pub struct AliasParams {
    /// The alias of the database.
    pub alias: String,
}

#[derive(Deserialize, JsonSchema)]
pub struct BackupDatabaseParams {
    /// The alias of the source database.
    pub alias: String,
    /// Path for the backup file (.db / .sqlite / .sqlite3).
    pub destination: String,
}

#[derive(Deserialize, JsonSchema)]
pub struct CreateReadmeParams {
    /// The alias of the database.
    pub alias: String,
    /// A one-sentence description of the database's purpose.
    pub purpose: String,
    /// Optional map of table names to descriptions.
    pub tables: Option<HashMap<String, String>>,
}

#[derive(Deserialize, JsonSchema)]
pub struct ExecuteSqlParams {
    /// The alias of the database to query.
    pub alias: String,
    /// The SQL statement to execute.
    pub sql: String,
    /// Optional list of bind parameters (for parameterized queries using ? placeholders).
    pub params: Option<Vec<serde_json::Value>>,
    /// Maximum rows to return (default: from env or 500).
    pub max_rows: Option<usize>,
}

#[derive(Deserialize, JsonSchema)]
pub struct ExecuteScriptParams {
    /// The alias of the database.
    pub alias: String,
    /// SQL script containing one or more statements separated by semicolons.
    pub script: String,
}

#[derive(Deserialize, JsonSchema)]
pub struct ExplainQueryParams {
    /// The alias of the database.
    pub alias: String,
    /// The SQL query to explain.
    pub sql: String,
    /// Optional bind parameters.
    pub params: Option<Vec<serde_json::Value>>,
}

#[derive(Deserialize, JsonSchema)]
pub struct BulkInsertParams {
    /// The alias of the database.
    pub alias: String,
    /// The table to insert into.
    pub table: String,
    /// List of column names, e.g. ["name", "age", "city"].
    pub columns: Vec<String>,
    /// List of rows, each row being a list of values matching the columns.
    pub rows: Vec<Vec<serde_json::Value>>,
}

#[derive(Deserialize, JsonSchema)]
pub struct ImportCsvParams {
    /// The alias of the database.
    pub alias: String,
    /// CSV-formatted text with a header row followed by data rows.
    pub csv_text: String,
    /// Target table name. Defaults to "imported".
    pub table: Option<String>,
    /// What to do if the table already exists: "append" (default) or "replace".
    pub if_exists: Option<String>,
}

#[derive(Deserialize, JsonSchema)]
pub struct ExportQueryCsvParams {
    /// The alias of the database.
    pub alias: String,
    /// A SELECT statement.
    pub sql: String,
    /// Optional bind parameters.
    pub params: Option<Vec<serde_json::Value>>,
    /// Maximum rows to export (default: 10000). Set 0 for unlimited.
    pub max_rows: Option<usize>,
}

#[derive(Deserialize, JsonSchema)]
pub struct TableParams {
    /// The alias of the database.
    pub alias: String,
    /// The name of the table.
    pub table: String,
}

#[derive(Deserialize, JsonSchema)]
pub struct DropTableParams {
    /// The alias of the database.
    pub alias: String,
    /// The name of the table to drop.
    pub table: String,
    /// Set to true to actually drop the table. Default false (preview only).
    pub confirm: Option<bool>,
}

#[derive(Deserialize, JsonSchema)]
pub struct RenameTableParams {
    /// The alias of the database.
    pub alias: String,
    /// Current table name.
    pub old_name: String,
    /// New table name.
    pub new_name: String,
}

// ---------------------------------------------------------------------------
// MCP Tools
// ---------------------------------------------------------------------------

#[tool_router]
impl LumenSqlite {
    #[tool(description = "Open (or create) a SQLite database file.")]
    async fn open_database(&self, params: Parameters<OpenDatabaseParams>) -> String {
        let params = params.0;
        let db_path = match self.resolve_db_path(&params.path) {
            Ok(p) => p,
            Err(e) => return e,
        };

        let alias = params.alias.unwrap_or_else(|| {
            db_path
                .file_stem()
                .and_then(|s| s.to_str())
                .unwrap_or("db")
                .to_string()
        });

        let mut conns = self.connections.lock().unwrap();
        if conns.contains_key(&alias) {
            return format!(
                "Database '{alias}' is already open. \
                 Close it first or choose a different alias."
            );
        }

        let creating = !db_path.exists();
        let conn = match Connection::open(&db_path) {
            Ok(c) => c,
            Err(e) => return format!("Failed to open database: {e}"),
        };

        // Enable WAL mode and foreign keys
        let _ = conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;");

        let verb = if creating {
            "Created and opened"
        } else {
            "Opened"
        };
        info!("{verb} database '{alias}' at {}", db_path.display());
        conns.insert(alias.clone(), OpenDatabase { conn, path: db_path.clone() });
        format!("{verb} database '{alias}' at {}", db_path.display())
    }

    #[tool(description = "Close an open database connection.")]
    async fn close_database(&self, params: Parameters<AliasParams>) -> String {
        let alias = params.0.alias;
        let mut conns = self.connections.lock().unwrap();
        if conns.remove(&alias).is_some() {
            info!("Closed database '{alias}'");
            format!("Closed database '{alias}'.")
        } else {
            Self::no_such_alias(&alias, &conns)
        }
    }

    #[tool(description = "List all currently open database aliases.")]
    async fn list_databases(&self) -> String {
        let conns = self.connections.lock().unwrap();
        if conns.is_empty() {
            "(none)".to_string()
        } else {
            let mut aliases: Vec<&str> = conns.keys().map(|s| s.as_str()).collect();
            aliases.sort();
            aliases.join("\n")
        }
    }

    #[tool(description = "List .db files in the data directory, including ones not currently open. Useful for discovering databases from prior sessions.")]
    async fn list_database_files(&self) -> String {
        let mut files: Vec<PathBuf> = Vec::new();
        for ext in ALLOWED_EXTENSIONS {
            let pattern = self.db_dir.join(format!("*.{ext}"));
            if let Ok(entries) = glob::glob(pattern.to_string_lossy().as_ref()) {
                for entry in entries.flatten() {
                    files.push(entry);
                }
            }
        }
        files.sort();

        if files.is_empty() {
            return format!("No database files found in {}", self.db_dir.display());
        }

        let mut lines = vec![
            format!("Database files in {}:", self.db_dir.display()),
            String::new(),
            format!("{:<40} {:>10} {}", "File", "Size", "Modified"),
            "-".repeat(70),
        ];

        for f in &files {
            if let Ok(meta) = f.metadata() {
                let size = meta.len();
                let size_str = if size < 1024 * 1024 {
                    format!("{:.1} KB", size as f64 / 1024.0)
                } else {
                    format!("{:.1} MB", size as f64 / (1024.0 * 1024.0))
                };
                let mtime = meta
                    .modified()
                    .ok()
                    .and_then(|t| {
                        let secs = t
                            .duration_since(std::time::UNIX_EPOCH)
                            .ok()?
                            .as_secs();
                        // Format as YYYY-MM-DD HH:MM using chrono
                        use chrono::{DateTime, TimeZone, Utc};
                        let dt: DateTime<Utc> = Utc.timestamp_opt(secs as i64, 0).single()?;
                        Some(dt.format("%Y-%m-%d %H:%M").to_string())
                    })
                    .unwrap_or_else(|| "?".to_string());
                let name = f.file_name().and_then(|n| n.to_str()).unwrap_or("?");
                lines.push(format!("{:<40} {:>10} {}", name, size_str, mtime));
            }
        }
        lines.join("\n")
    }

    #[tool(description = "Get a comprehensive summary of an open database: _readme contents, all tables with row counts and column schemas. Use this instead of calling list_tables + describe_table + SELECT COUNT(*) repeatedly.")]
    async fn database_info(&self, params: Parameters<AliasParams>) -> String {
        let alias = params.0.alias;
        let conns = self.connections.lock().unwrap();
        let db = match conns.get(&alias) {
            Some(d) => d,
            None => return Self::no_such_alias(&alias, &conns),
        };

        let mut lines = vec![format!("Database: {alias}"), "=".repeat(60)];

        // _readme contents
        let has_readme: bool = db
            .conn
            .query_row(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='_readme'",
                [],
                |_| Ok(true),
            )
            .unwrap_or(false);

        if has_readme {
            lines.push(String::new());
            lines.push("## _readme".to_string());
            if let Ok(mut stmt) = db.conn.prepare("SELECT key, value FROM _readme ORDER BY key") {
                if let Ok(rows) = stmt.query_map([], |row| {
                    Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
                }) {
                    for row in rows.flatten() {
                        lines.push(format!("  {}: {}", row.0, row.1));
                    }
                }
            }
        }

        // All tables
        let tables: Vec<String> = {
            let mut stmt = match db.conn.prepare(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
            ) {
                Ok(s) => s,
                Err(e) => return format!("Error reading tables: {e}"),
            };
            stmt.query_map([], |row| row.get::<_, String>(0))
                .map(|mapped| mapped.flatten().collect())
                .unwrap_or_default()
        };

        if tables.is_empty() {
            lines.push(String::new());
            lines.push("(no tables)".to_string());
            return lines.join("\n");
        }

        lines.push(String::new());
        lines.push("## Tables".to_string());

        for table in &tables {
            if table == "sqlite_sequence" {
                continue;
            }
            let safe = escape_identifier(table);

            let count: i64 = db
                .conn
                .query_row(&format!("SELECT COUNT(*) FROM \"{safe}\""), [], |r| r.get(0))
                .unwrap_or(-1);
            let count_str = if count < 0 {
                "?".to_string()
            } else {
                count.to_string()
            };

            lines.push(String::new());
            lines.push(format!("### {table} ({count_str} rows)"));

            if let Ok(mut stmt) = db.conn.prepare(&format!("PRAGMA table_info(\"{safe}\")")) {
                if let Ok(cols) = stmt.query_map([], |row| {
                    Ok((
                        row.get::<_, String>(1)?,  // name
                        row.get::<_, String>(2)?,  // type
                        row.get::<_, i32>(3)?,     // notnull
                        row.get::<_, Option<String>>(4)?, // default
                        row.get::<_, i32>(5)?,     // pk
                    ))
                }) {
                    for col in cols.flatten() {
                        let (name, col_type, notnull, default_val, pk) = col;
                        let mut parts = vec![format!("  {name}")];
                        if !col_type.is_empty() {
                            parts.push(col_type);
                        }
                        if pk != 0 {
                            parts.push("PK".to_string());
                        }
                        if notnull != 0 {
                            parts.push("NOT NULL".to_string());
                        }
                        if let Some(d) = default_val {
                            parts.push(format!("DEFAULT {d}"));
                        }
                        lines.push(parts.join(" "));
                    }
                }
            }
        }

        lines.push(String::new());
        lines.join("\n")
    }

    #[tool(description = "Safely copy an open database to a new file using SQLite's backup API. Safe to call while the database is in use.")]
    async fn backup_database(&self, params: Parameters<BackupDatabaseParams>) -> String {
        let BackupDatabaseParams { alias, destination } = params.0;
        let dest_path = match self.resolve_db_path(&destination) {
            Ok(p) => p,
            Err(e) => return e,
        };

        if dest_path.exists() {
            return format!(
                "Destination '{}' already exists. Choose a different path or remove it first.",
                dest_path.display()
            );
        }

        let conns = self.connections.lock().unwrap();
        let db = match conns.get(&alias) {
            Some(d) => d,
            None => return Self::no_such_alias(&alias, &conns),
        };

        match db.conn.backup(MAIN_DB, &dest_path, None) {
            Ok(()) => {
                let size_kb = dest_path
                    .metadata()
                    .map(|m| m.len() as f64 / 1024.0)
                    .unwrap_or(0.0);
                info!("Backed up '{alias}' to {}", dest_path.display());
                format!("Backed up '{alias}' to {} ({size_kb:.1} KB).", dest_path.display())
            }
            Err(e) => {
                let _ = std::fs::remove_file(&dest_path);
                format!("Backup error: {e}")
            }
        }
    }

    #[tool(description = "Rebuild the database file to reclaim free space (VACUUM). After many DELETE operations, the database file doesn't shrink until VACUUM is run.")]
    async fn compact_database(&self, params: Parameters<AliasParams>) -> String {
        let alias = params.0.alias;
        let conns = self.connections.lock().unwrap();
        let db = match conns.get(&alias) {
            Some(d) => d,
            None => return Self::no_such_alias(&alias, &conns),
        };

        let size_before = db.path.metadata().map(|m| m.len()).unwrap_or(0);
        match db.conn.execute_batch("VACUUM") {
            Err(e) => format!("Compact error: {e}"),
            Ok(()) => {
                let size_after = db.path.metadata().map(|m| m.len()).unwrap_or(0);
                let before_kb = size_before as f64 / 1024.0;
                let after_kb = size_after as f64 / 1024.0;
                if size_before > size_after {
                    let saved_kb = (size_before - size_after) as f64 / 1024.0;
                    format!(
                        "Compacted '{alias}': {before_kb:.1} KB → {after_kb:.1} KB (saved {saved_kb:.1} KB)."
                    )
                } else {
                    format!("Compacted '{alias}': no space to reclaim ({after_kb:.1} KB).")
                }
            }
        }
    }

    #[tool(description = "Create or replace a _readme table that self-documents the database. Call immediately after creating a new database. Do NOT call for databases from external sources.")]
    async fn create_readme(&self, params: Parameters<CreateReadmeParams>) -> String {
        let CreateReadmeParams { alias, purpose, tables } = params.0;
        let conns = self.connections.lock().unwrap();
        let db = match conns.get(&alias) {
            Some(d) => d,
            None => return Self::no_such_alias(&alias, &conns),
        };

        let result: rusqlite::Result<usize> = (|| {
            db.conn.execute_batch(
                "DROP TABLE IF EXISTS _readme;
                 CREATE TABLE _readme (key TEXT PRIMARY KEY, value TEXT NOT NULL);",
            )?;
            db.conn.execute(
                "INSERT INTO _readme VALUES ('purpose', ?1)",
                rusqlite::params![purpose],
            )?;
            db.conn.execute(
                "INSERT INTO _readme VALUES ('created', datetime('now'))",
                [],
            )?;
            let mut n = 2usize;
            if let Some(ref tbl_map) = tables {
                for (tbl_name, tbl_desc) in tbl_map {
                    db.conn.execute(
                        "INSERT INTO _readme VALUES (?1, ?2)",
                        rusqlite::params![format!("table:{tbl_name}"), tbl_desc],
                    )?;
                    n += 1;
                }
            }
            Ok(n)
        })();

        match result {
            Ok(n) => {
                info!("Created _readme in '{alias}'");
                format!("Created _readme table with {n} entries in '{alias}'.")
            }
            Err(e) => format!("Error creating _readme: {e}"),
        }
    }

    #[tool(description = "Execute a single SQL statement. Returns results for SELECT, or affected row count for INSERT/UPDATE/DELETE/DDL. Use ? placeholders with params for safety.")]
    async fn execute_sql(&self, params: Parameters<ExecuteSqlParams>) -> String {
        let ExecuteSqlParams { alias, sql, params: bind_params, max_rows } = params.0;
        let max_rows = max_rows.unwrap_or(self.default_max_rows);
        let bind_params = bind_params.unwrap_or_default();
        let json_params: Vec<JsonParam> = bind_params.into_iter().map(JsonParam).collect();

        let conns = self.connections.lock().unwrap();
        let db = match conns.get(&alias) {
            Some(d) => d,
            None => return Self::no_such_alias(&alias, &conns),
        };

        let mut stmt = match db.conn.prepare(&sql) {
            Ok(s) => s,
            Err(e) => return format!("SQL error: {e}"),
        };

        let col_count = stmt.column_count();

        if col_count > 0 {
            // SELECT-like: returns rows
            let col_names: Vec<String> =
                stmt.column_names().iter().map(|&s| s.to_string()).collect();

            let rows_result: rusqlite::Result<Vec<Vec<String>>> =
                stmt.query_map(rusqlite::params_from_iter(json_params.iter()), |row| {
                    let vals: Vec<String> = (0..col_count)
                        .map(|i| {
                            row.get_ref(i)
                                .map(value_ref_to_string)
                                .unwrap_or_default()
                        })
                        .collect();
                    Ok(vals)
                })
                .and_then(|mapped| {
                    let mut collected = Vec::new();
                    for r in mapped {
                        collected.push(r?);
                        if collected.len() >= max_rows + 1 {
                            break;
                        }
                    }
                    Ok(collected)
                });

            match rows_result {
                Err(e) => format!("SQL error: {e}"),
                Ok(mut rows) => {
                    let truncated = rows.len() > max_rows;
                    if truncated {
                        rows.pop();
                    }
                    let mut lines = vec![col_names.join("\t")];
                    for row in &rows {
                        lines.push(row.join("\t"));
                    }
                    if truncated {
                        lines.push(format!("... (results truncated at {} rows)", rows.len()));
                    }
                    lines.join("\n")
                }
            }
        } else {
            // DML / DDL: returns affected row count
            match stmt.execute(rusqlite::params_from_iter(json_params.iter())) {
                Ok(n) => format!("{n} row(s) affected."),
                Err(e) => format!("SQL error: {e}"),
            }
        }
    }

    #[tool(description = "Execute multiple SQL statements in a single call. Use for schema setup (CREATE TABLE, CREATE INDEX) or batch operations. Statements are separated by semicolons. Does not support parameter binding.")]
    async fn execute_script(&self, params: Parameters<ExecuteScriptParams>) -> String {
        let ExecuteScriptParams { alias, script } = params.0;
        let conns = self.connections.lock().unwrap();
        let db = match conns.get(&alias) {
            Some(d) => d,
            None => return Self::no_such_alias(&alias, &conns),
        };
        match db.conn.execute_batch(&script) {
            Ok(()) => "Script executed successfully.".to_string(),
            Err(e) => format!("SQL error: {e}"),
        }
    }

    #[tool(description = "Show how SQLite will execute a query (EXPLAIN QUERY PLAN). Use to understand whether indices are used, whether full table scans occur, and the join order.")]
    async fn explain_query(&self, params: Parameters<ExplainQueryParams>) -> String {
        let ExplainQueryParams { alias, sql, params: bind_params } = params.0;
        let bind_params = bind_params.unwrap_or_default();
        let json_params: Vec<JsonParam> = bind_params.into_iter().map(JsonParam).collect();

        let conns = self.connections.lock().unwrap();
        let db = match conns.get(&alias) {
            Some(d) => d,
            None => return Self::no_such_alias(&alias, &conns),
        };

        let explain_sql = format!("EXPLAIN QUERY PLAN {sql}");
        let mut stmt = match db.conn.prepare(&explain_sql) {
            Ok(s) => s,
            Err(e) => return format!("SQL error: {e}"),
        };

        let rows_result: rusqlite::Result<Vec<(i32, String)>> = stmt
            .query_map(rusqlite::params_from_iter(json_params.iter()), |row| {
                // EXPLAIN QUERY PLAN: (id, parent, notused, detail)
                let id: i32 = row.get(0)?;
                let detail: String = row.get(3)?;
                Ok((id, detail))
            })
            .and_then(|mapped| mapped.collect());

        match rows_result {
            Err(e) => format!("SQL error: {e}"),
            Ok(rows) if rows.is_empty() => "No query plan available.".to_string(),
            Ok(rows) => {
                let mut lines = vec!["Query plan:".to_string(), String::new()];
                for (id, detail) in &rows {
                    let indent = "  ".repeat(*id as usize);
                    lines.push(format!("{indent}{detail}"));
                }
                lines.join("\n")
            }
        }
    }

    #[tool(description = "Insert many rows into a table in a single transaction-wrapped call. Typically 50-100x faster than individual INSERT calls for large batches.")]
    async fn bulk_insert(&self, params: Parameters<BulkInsertParams>) -> String {
        let BulkInsertParams { alias, table, columns, rows } = params.0;

        if rows.is_empty() {
            return "No rows to insert.".to_string();
        }
        if let Err(e) = validate_identifier(&table, "table name") {
            return e;
        }
        for col in &columns {
            if let Err(e) = validate_identifier(col, "column name") {
                return e;
            }
        }

        let col_clause: String = columns
            .iter()
            .map(|c| format!("\"{}\"", escape_identifier(c)))
            .collect::<Vec<_>>()
            .join(", ");
        let placeholders: String = (0..columns.len())
            .map(|_| "?")
            .collect::<Vec<_>>()
            .join(", ");
        let sql = format!("INSERT INTO \"{}\" ({col_clause}) VALUES ({placeholders})", escape_identifier(&table));

        let conns = self.connections.lock().unwrap();
        let db = match conns.get(&alias) {
            Some(d) => d,
            None => return Self::no_such_alias(&alias, &conns),
        };

        let result: rusqlite::Result<()> = (|| {
            db.conn.execute_batch("BEGIN TRANSACTION")?;
            let mut stmt = db.conn.prepare(&sql)?;
            for row in &rows {
                let json_params: Vec<JsonParam> =
                    row.iter().cloned().map(JsonParam).collect();
                stmt.execute(rusqlite::params_from_iter(json_params.iter()))?;
            }
            drop(stmt);
            db.conn.execute_batch("COMMIT")?;
            Ok(())
        })();

        match result {
            Ok(()) => {
                info!("Bulk-inserted {} rows into '{alias}.{table}'", rows.len());
                format!("Inserted {} rows into '{table}'.", rows.len())
            }
            Err(e) => {
                let _ = db.conn.execute_batch("ROLLBACK");
                format!("Bulk insert error (rolled back): {e}")
            }
        }
    }

    #[tool(description = "Import CSV text into a table. If the table does not exist, it is created automatically from the header row (all columns TEXT). All rows inserted in a single transaction.")]
    async fn import_csv(&self, params: Parameters<ImportCsvParams>) -> String {
        let ImportCsvParams { alias, csv_text, table, if_exists } = params.0;
        let table = table.unwrap_or_else(|| "imported".to_string());
        let if_exists = if_exists.unwrap_or_else(|| "append".to_string());

        if let Err(e) = validate_identifier(&table, "table name") {
            return e;
        }

        let mut rdr = csv_crate::Reader::from_reader(io::Cursor::new(csv_text.as_bytes()));
        let headers: Vec<String> = match rdr.headers() {
            Ok(h) => h.iter().map(|s| s.trim().to_string()).collect(),
            Err(e) => return format!("CSV parse error: {e}"),
        };

        if headers.is_empty() {
            return "CSV header row is empty.".to_string();
        }
        for h in &headers {
            if let Err(e) = validate_identifier(h, "column name (from CSV header)") {
                return e;
            }
        }

        let rows: Vec<Vec<String>> = rdr
            .records()
            .filter_map(|r| r.ok())
            .filter(|r| !r.is_empty())
            .map(|r| r.iter().map(|s| s.to_string()).collect())
            .collect();

        if rows.is_empty() {
            return "CSV has a header row but no data rows.".to_string();
        }

        let conns = self.connections.lock().unwrap();
        let db = match conns.get(&alias) {
            Some(d) => d,
            None => return Self::no_such_alias(&alias, &conns),
        };

        let escaped_table = escape_identifier(&table);
        let col_defs: String = headers
            .iter()
            .map(|h| format!("\"{}\" TEXT", escape_identifier(h)))
            .collect::<Vec<_>>()
            .join(", ");
        let placeholders: String = (0..headers.len())
            .map(|_| "?")
            .collect::<Vec<_>>()
            .join(", ");
        let insert_sql = format!("INSERT INTO \"{escaped_table}\" VALUES ({placeholders})");

        let result: rusqlite::Result<()> = (|| {
            if if_exists == "replace" {
                db.conn.execute_batch(&format!("DROP TABLE IF EXISTS \"{escaped_table}\""))?;
            }
            db.conn.execute_batch(&format!(
                "CREATE TABLE IF NOT EXISTS \"{escaped_table}\" ({col_defs})"
            ))?;
            db.conn.execute_batch("BEGIN TRANSACTION")?;
            let mut stmt = db.conn.prepare(&insert_sql)?;
            for row in &rows {
                let row_refs: Vec<&dyn rusqlite::ToSql> = row
                    .iter()
                    .map(|s| s as &dyn rusqlite::ToSql)
                    .collect();
                stmt.execute(row_refs.as_slice())?;
            }
            drop(stmt);
            db.conn.execute_batch("COMMIT")?;
            Ok(())
        })();

        match result {
            Ok(()) => {
                info!("Imported {} rows into '{alias}.{table}'", rows.len());
                format!(
                    "Imported {} rows into '{table}' ({} columns: {}).",
                    rows.len(),
                    headers.len(),
                    headers.join(", ")
                )
            }
            Err(e) => {
                let _ = db.conn.execute_batch("ROLLBACK");
                format!("Import error: {e}")
            }
        }
    }

    #[tool(description = "Run a SELECT query and return results as CSV text. Useful for exporting data or feeding into other tools.")]
    async fn export_query_csv(&self, params: Parameters<ExportQueryCsvParams>) -> String {
        let ExportQueryCsvParams { alias, sql, params: bind_params, max_rows } = params.0;
        let max_rows = max_rows.unwrap_or(self.default_export_max_rows);
        let bind_params = bind_params.unwrap_or_default();
        let json_params: Vec<JsonParam> = bind_params.into_iter().map(JsonParam).collect();

        let conns = self.connections.lock().unwrap();
        let db = match conns.get(&alias) {
            Some(d) => d,
            None => return Self::no_such_alias(&alias, &conns),
        };

        let mut stmt = match db.conn.prepare(&sql) {
            Ok(s) => s,
            Err(e) => return format!("SQL error: {e}"),
        };

        if stmt.column_count() == 0 {
            return "Statement did not return results.".to_string();
        }

        let col_names: Vec<String> =
            stmt.column_names().iter().map(|&s| s.to_string()).collect();
        let col_count = stmt.column_count();

        let mut output = Vec::new();
        let mut wtr = csv_crate::Writer::from_writer(&mut output);
        if let Err(e) = wtr.write_record(&col_names) {
            return format!("CSV error: {e}");
        }

        let rows_result: rusqlite::Result<(usize, bool)> =
            stmt.query_map(rusqlite::params_from_iter(json_params.iter()), |row| {
                let vals: Vec<String> = (0..col_count)
                    .map(|i| {
                        row.get_ref(i)
                            .map(value_ref_to_string)
                            .unwrap_or_default()
                    })
                    .collect();
                Ok(vals)
            })
            .and_then(|mapped| {
                let mut count = 0usize;
                let mut truncated = false;
                for row in mapped {
                    let r = row?;
                    if max_rows > 0 && count >= max_rows {
                        truncated = true;
                        break;
                    }
                    wtr.write_record(&r)
                        .map_err(|e| rusqlite::Error::ToSqlConversionFailure(Box::new(e)))?;
                    count += 1;
                }
                Ok((count, truncated))
            });

        if let Err(e) = wtr.flush() {
            return format!("CSV write error: {e}");
        }
        drop(wtr);

        match rows_result {
            Err(e) => format!("SQL error: {e}"),
            Ok((_count, truncated)) => {
                let mut csv_text = String::from_utf8_lossy(&output).into_owned();
                if truncated {
                    csv_text.push_str(&format!("\n# ... truncated at {max_rows} rows\n"));
                }
                csv_text
            }
        }
    }

    #[tool(description = "List all tables in the database.")]
    async fn list_tables(&self, params: Parameters<AliasParams>) -> String {
        let alias = params.0.alias;
        let conns = self.connections.lock().unwrap();
        let db = match conns.get(&alias) {
            Some(d) => d,
            None => return Self::no_such_alias(&alias, &conns),
        };

        let result: rusqlite::Result<Vec<String>> = db
            .conn
            .prepare("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            .and_then(|mut stmt| {
                stmt.query_map([], |row| row.get(0))
                    .and_then(|mapped| mapped.collect())
            });

        match result {
            Err(e) => format!("SQL error: {e}"),
            Ok(tables) if tables.is_empty() => "No tables found.".to_string(),
            Ok(tables) => tables.join("\n"),
        }
    }

    #[tool(description = "Show the schema of a table: column names, types, constraints, and the CREATE TABLE DDL.")]
    async fn describe_table(&self, params: Parameters<TableParams>) -> String {
        let TableParams { alias, table } = params.0;
        let conns = self.connections.lock().unwrap();
        let db = match conns.get(&alias) {
            Some(d) => d,
            None => return Self::no_such_alias(&alias, &conns),
        };

        let safe = escape_identifier(&table);
        let cols_result: rusqlite::Result<Vec<(i32, String, String, i32, Option<String>, i32)>> =
            db.conn
                .prepare(&format!("PRAGMA table_info(\"{safe}\")"))
                .and_then(|mut stmt| {
                    stmt.query_map([], |row| {
                        Ok((
                            row.get::<_, i32>(0)?,
                            row.get::<_, String>(1)?,
                            row.get::<_, String>(2)?,
                            row.get::<_, i32>(3)?,
                            row.get::<_, Option<String>>(4)?,
                            row.get::<_, i32>(5)?,
                        ))
                    })
                    .and_then(|mapped| mapped.collect())
                });

        let cols = match cols_result {
            Err(e) => return format!("SQL error: {e}"),
            Ok(c) if c.is_empty() => return format!("Table '{table}' not found."),
            Ok(c) => c,
        };

        let mut lines = vec![
            format!("Table: {table}"),
            String::new(),
            format!(
                "{:<4} {:<30} {:<15} {:<10} {:<15} {}",
                "#", "Name", "Type", "Nullable", "Default", "PK"
            ),
            "-".repeat(80),
        ];

        for (cid, name, col_type, notnull, default_val, pk) in &cols {
            let nullable = if *notnull != 0 { "NO" } else { "YES" };
            let default_str = default_val.as_deref().unwrap_or("");
            let pk_str = if *pk != 0 { "YES" } else { "" };
            lines.push(format!(
                "{:<4} {:<30} {:<15} {:<10} {:<15} {}",
                cid, name, col_type, nullable, default_str, pk_str
            ));
        }

        // DDL
        let ddl: Option<String> = db
            .conn
            .query_row(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?1",
                rusqlite::params![table],
                |row| row.get(0),
            )
            .ok()
            .flatten();

        if let Some(ddl) = ddl {
            lines.push(String::new());
            lines.push("DDL:".to_string());
            lines.push(ddl);
        }

        lines.join("\n")
    }

    #[tool(description = "Drop a table from the database. For safety, call first without confirm to see a preview (row count, columns), then again with confirm=true to actually drop the table.")]
    async fn drop_table(&self, params: Parameters<DropTableParams>) -> String {
        let DropTableParams { alias, table, confirm } = params.0;
        let confirm = confirm.unwrap_or(false);

        let conns = self.connections.lock().unwrap();
        let db = match conns.get(&alias) {
            Some(d) => d,
            None => return Self::no_such_alias(&alias, &conns),
        };

        // Check it exists
        let exists: bool = db
            .conn
            .query_row(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?1",
                rusqlite::params![table],
                |_| Ok(true),
            )
            .unwrap_or(false);
        if !exists {
            return format!("Table '{table}' does not exist.");
        }

        let safe = escape_identifier(&table);

        let count: i64 = db
            .conn
            .query_row(&format!("SELECT COUNT(*) FROM \"{safe}\""), [], |r| r.get(0))
            .unwrap_or(-1);

        let col_names: Vec<String> = db
            .conn
            .prepare(&format!("PRAGMA table_info(\"{safe}\")"))
            .and_then(|mut stmt| {
                stmt.query_map([], |row| row.get::<_, String>(1))
                    .and_then(|m| m.collect())
            })
            .unwrap_or_default();

        if !confirm {
            return format!(
                "Would drop table '{table}' with {count} rows and {} column(s) ({}). \
                 Call again with confirm=true to proceed.",
                col_names.len(),
                col_names.join(", ")
            );
        }

        match db.conn.execute_batch(&format!("DROP TABLE \"{safe}\"")) {
            Ok(()) => {
                info!("Dropped table '{table}' from '{alias}'");
                format!("Dropped table '{table}' ({count} rows deleted).")
            }
            Err(e) => format!("Error dropping table: {e}"),
        }
    }

    #[tool(description = "Rename a table via ALTER TABLE ... RENAME TO.")]
    async fn rename_table(&self, params: Parameters<RenameTableParams>) -> String {
        let RenameTableParams { alias, old_name, new_name } = params.0;

        if let Err(e) = validate_identifier(&new_name, "new table name") {
            return e;
        }

        let conns = self.connections.lock().unwrap();
        let db = match conns.get(&alias) {
            Some(d) => d,
            None => return Self::no_such_alias(&alias, &conns),
        };

        let exists: bool = db
            .conn
            .query_row(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?1",
                rusqlite::params![old_name],
                |_| Ok(true),
            )
            .unwrap_or(false);
        if !exists {
            return format!("Table '{old_name}' does not exist.");
        }

        let old_safe = escape_identifier(&old_name);
        let new_safe = escape_identifier(&new_name);
        match db.conn.execute_batch(&format!("ALTER TABLE \"{old_safe}\" RENAME TO \"{new_safe}\""))
        {
            Ok(()) => {
                info!("Renamed table '{old_name}' to '{new_name}' in '{alias}'");
                format!("Renamed table '{old_name}' to '{new_name}'.")
            }
            Err(e) => format!("Error renaming table: {e}"),
        }
    }
}

impl LumenSqlite {
    fn no_such_alias(alias: &str, conns: &HashMap<String, OpenDatabase>) -> String {
        let available = if conns.is_empty() {
            "(none)".to_string()
        } else {
            let mut aliases: Vec<&str> = conns.keys().map(|s| s.as_str()).collect();
            aliases.sort();
            aliases.join(", ")
        };
        format!("No open database with alias '{alias}'. Open databases: {available}")
    }
}

// ---------------------------------------------------------------------------
// MCP handler wiring
// ---------------------------------------------------------------------------

#[tool_handler]
impl ServerHandler for LumenSqlite {
    fn get_info(&self) -> ServerInfo {
        ServerInfo::new(ServerCapabilities::builder().enable_tools().build())
            .with_instructions(SERVER_INSTRUCTIONS)
            .with_server_info(
                rmcp::model::Implementation::new("lumen-sqlite", env!("CARGO_PKG_VERSION"))
            )
    }
}

// ---------------------------------------------------------------------------
// Server instructions
// ---------------------------------------------------------------------------

const SERVER_INSTRUCTIONS: &str = include_str!("../instructions.md");

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // Log to stderr (stdout is the MCP transport)
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info".into()),
        )
        .with_writer(std::io::stderr)
        .init();

    info!(
        "lumen-sqlite MCP server starting (db_dir={})",
        std::env::var("LUMEN_SQLITE_DB_DIR").unwrap_or_else(|_| ".".into())
    );

    let server = LumenSqlite::new();
    let transport = rmcp::transport::io::stdio();
    let ct = server.serve(transport).await?;
    ct.waiting().await?;
    Ok(())
}
