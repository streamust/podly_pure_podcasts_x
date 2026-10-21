use std::path::Path;
use std::time::Duration;

use anyhow::{bail, Context, Result};
use rusqlite::{Connection, OpenFlags};

pub const EXPECTED_SCHEMA_REVISION: &str = "080b5181e23a";

pub fn open_existing(path: &Path) -> Result<Connection> {
    let metadata = path
        .metadata()
        .with_context(|| format!("writer database does not exist: {}", path.display()))?;
    if !metadata.is_file() {
        bail!("writer database is not a regular file: {}", path.display());
    }
    Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_WRITE)
        .with_context(|| format!("failed to open writer database: {}", path.display()))
}

pub fn open_ready(path: &Path) -> Result<Connection> {
    open_ready_with_foreign_keys(path, false)
}

pub(super) fn open_ready_with_foreign_keys(
    path: &Path,
    enable_foreign_keys: bool,
) -> Result<Connection> {
    let connection = open_existing(path)?;
    connection.busy_timeout(Duration::from_millis(90_000))?;
    let foreign_keys = if enable_foreign_keys { "ON" } else { "OFF" };
    connection.execute_batch(&format!(
        "PRAGMA journal_mode=WAL;
         PRAGMA synchronous=NORMAL;
         PRAGMA wal_autocheckpoint=1000;
         PRAGMA journal_size_limit=67108864;
         PRAGMA foreign_keys={foreign_keys};"
    ))?;
    let revisions = {
        let mut statement = connection
            .prepare("SELECT version_num FROM alembic_version ORDER BY version_num")
            .context("database has no readable Alembic revision")?;
        let revisions = statement
            .query_map([], |row| row.get::<_, String>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        revisions
    };
    if revisions != [EXPECTED_SCHEMA_REVISION] {
        bail!("database schema revision is incompatible");
    }
    Ok(connection)
}

#[cfg(test)]
mod tests {
    use tempfile::{NamedTempFile, TempDir};

    use super::*;

    fn seed_revision(connection: &Connection, revision: &str) {
        connection
            .execute(
                "CREATE TABLE alembic_version(version_num TEXT NOT NULL)",
                [],
            )
            .unwrap();
        connection
            .execute(
                "INSERT INTO alembic_version(version_num) VALUES (?1)",
                [revision],
            )
            .unwrap();
    }

    #[test]
    fn wrong_path_is_not_created() {
        let directory = TempDir::new().unwrap();
        let path = directory.path().join("missing.db");
        assert!(open_ready(&path).is_err());
        assert!(!path.exists());
    }

    #[test]
    fn incompatible_revision_is_rejected() {
        let file = NamedTempFile::new().unwrap();
        let connection = Connection::open(file.path()).unwrap();
        seed_revision(&connection, "wrong");
        drop(connection);
        assert!(open_ready(file.path()).is_err());
    }

    #[test]
    fn compatible_database_gets_expected_pragmas() {
        let file = NamedTempFile::new().unwrap();
        let connection = Connection::open(file.path()).unwrap();
        seed_revision(&connection, EXPECTED_SCHEMA_REVISION);
        drop(connection);
        let connection = open_ready(file.path()).unwrap();
        let journal: String = connection
            .query_row("PRAGMA journal_mode", [], |row| row.get(0))
            .unwrap();
        let synchronous: i64 = connection
            .query_row("PRAGMA synchronous", [], |row| row.get(0))
            .unwrap();
        let foreign_keys: i64 = connection
            .query_row("PRAGMA foreign_keys", [], |row| row.get(0))
            .unwrap();
        assert_eq!(journal, "wal");
        assert_eq!(synchronous, 1);
        assert_eq!(foreign_keys, 0);
    }
}
