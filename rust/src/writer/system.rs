use chrono::Utc;
use rusqlite::{OptionalExtension, Transaction};
use serde_json::{json, Map, Value};

use super::actions::{error, RpcActionResult};

const SINGLETON_RUN_ID: &str = "jobs-manager-singleton";

pub fn execute(
    transaction: &Transaction<'_>,
    action: &str,
    params: &Map<String, Value>,
) -> RpcActionResult {
    match action {
        "ensure_active_run" => ensure_active_run(transaction, params),
        "update_discord_settings" => update_discord_settings(transaction, params),
        _ => Err(error(
            "unsupported_action",
            "writer action is not registered",
        )),
    }
}

fn ensure_active_run(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let trigger = match params.get("trigger") {
        None => "system",
        Some(Value::String(value)) => value,
        _ => return Err(error("invalid_params", "trigger must be a string")),
    };
    let mut context = match params.get("context") {
        None | Some(Value::Null) => Map::new(),
        Some(Value::Object(value)) => value.clone(),
        _ => return Err(error("invalid_params", "context must be an object or null")),
    };
    let now = Utc::now().naive_utc();
    let database_now = now.format("%Y-%m-%d %H:%M:%S%.6f").to_string();
    context.insert("last_trigger".to_owned(), Value::String(trigger.to_owned()));
    context.insert(
        "last_trigger_at".to_owned(),
        Value::String(now.format("%Y-%m-%dT%H:%M:%S%.6f").to_string()),
    );
    let context = serde_json::to_string(&context)
        .map_err(|_| error("invalid_params", "context is not valid JSON"))?;
    let exists = transaction
        .query_row(
            "SELECT 1 FROM jobs_manager_run WHERE id=?1",
            [SINGLETON_RUN_ID],
            |_| Ok(()),
        )
        .optional()
        .map_err(|_| error("database_error", "database operation failed"))?
        .is_some();
    if exists {
        transaction
            .execute(
                "UPDATE jobs_manager_run
                 SET trigger=?1, context_json=?2, updated_at=?3,
                     started_at=COALESCE(started_at, ?3),
                     counters_reset_at=COALESCE(counters_reset_at, started_at, ?3)
                 WHERE id=?4",
                rusqlite::params![trigger, context, database_now, SINGLETON_RUN_ID],
            )
            .map_err(|_| error("database_error", "database operation failed"))?;
    } else {
        transaction
            .execute(
                "INSERT INTO jobs_manager_run(
                    id,status,trigger,started_at,completed_at,total_jobs,
                    queued_jobs,running_jobs,completed_jobs,failed_jobs,skipped_jobs,
                    context_json,counters_reset_at,created_at,updated_at
                 ) VALUES (?1,'running',?2,?3,NULL,0,0,0,0,0,0,?4,?3,?3,?3)",
                rusqlite::params![SINGLETON_RUN_ID, trigger, database_now, context],
            )
            .map_err(|_| error("database_error", "database operation failed"))?;
    }
    Ok(json!({"run_id": SINGLETON_RUN_ID}))
}

fn update_discord_settings(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let now = Utc::now()
        .naive_utc()
        .format("%Y-%m-%d %H:%M:%S%.6f")
        .to_string();
    transaction
        .execute(
            "INSERT OR IGNORE INTO discord_settings(
                id,client_id,client_secret,redirect_uri,guild_ids,
                allow_registration,created_at,updated_at
             ) VALUES (1,NULL,NULL,NULL,NULL,1,?1,?1)",
            [&now],
        )
        .map_err(|_| error("database_error", "database operation failed"))?;
    for (field, kind) in [
        ("client_id", "text"),
        ("client_secret", "text"),
        ("redirect_uri", "text"),
        ("guild_ids", "text"),
        ("allow_registration", "bool"),
    ] {
        let Some(value) = params.get(field) else {
            continue;
        };
        let value = match (kind, value) {
            (_, Value::Null) => rusqlite::types::Value::Null,
            ("text", Value::String(value)) => rusqlite::types::Value::Text(value.clone()),
            ("bool", Value::Bool(value)) => rusqlite::types::Value::Integer(i64::from(*value)),
            _ => return Err(error("invalid_params", "discord setting has invalid type")),
        };
        transaction
            .execute(
                &format!("UPDATE discord_settings SET {field}=?1 WHERE id=1"),
                [value],
            )
            .map_err(|_| error("database_error", "database operation failed"))?;
    }
    transaction
        .execute(
            "UPDATE discord_settings SET updated_at=?1 WHERE id=1",
            [&now],
        )
        .map_err(|_| error("database_error", "database operation failed"))?;
    Ok(json!({"updated": true}))
}

#[cfg(test)]
mod tests {
    use rusqlite::Connection;

    use super::*;

    fn schema(connection: &Connection) {
        connection
            .execute_batch(
                "CREATE TABLE jobs_manager_run(
                    id TEXT PRIMARY KEY,status TEXT NOT NULL,trigger TEXT NOT NULL,
                    started_at TEXT,completed_at TEXT,total_jobs INTEGER NOT NULL,
                    queued_jobs INTEGER NOT NULL,running_jobs INTEGER NOT NULL,
                    completed_jobs INTEGER NOT NULL,failed_jobs INTEGER NOT NULL,
                    skipped_jobs INTEGER NOT NULL,context_json JSON,
                    counters_reset_at TEXT,created_at TEXT,updated_at TEXT
                 );
                 CREATE TABLE discord_settings(
                    id INTEGER PRIMARY KEY,client_id TEXT,client_secret TEXT,
                    redirect_uri TEXT,guild_ids TEXT,allow_registration BOOLEAN NOT NULL,
                    created_at TEXT NOT NULL,updated_at TEXT NOT NULL
                 );",
            )
            .unwrap();
    }

    #[test]
    fn ensure_active_run_creates_then_preserves_status_and_counters() {
        let mut connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        let transaction = connection.transaction().unwrap();
        let result = ensure_active_run(
            &transaction,
            &Map::from_iter([
                ("trigger".to_owned(), json!("startup")),
                (
                    "context".to_owned(),
                    json!({"source": "test", "last_trigger": "overwritten"}),
                ),
            ]),
        )
        .unwrap();
        assert_eq!(result["run_id"], SINGLETON_RUN_ID);
        transaction.commit().unwrap();
        connection
            .execute(
                "UPDATE jobs_manager_run SET status='completed', total_jobs=7 WHERE id=?1",
                [SINGLETON_RUN_ID],
            )
            .unwrap();
        let transaction = connection.transaction().unwrap();
        ensure_active_run(&transaction, &Map::new()).unwrap();
        transaction.commit().unwrap();
        let (status, total, trigger, context): (String, i64, String, String) = connection
            .query_row(
                "SELECT status,total_jobs,trigger,context_json FROM jobs_manager_run",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .unwrap();
        assert_eq!(status, "completed");
        assert_eq!(total, 7);
        assert_eq!(trigger, "system");
        assert_eq!(
            serde_json::from_str::<Value>(&context).unwrap()["last_trigger"],
            "system"
        );
    }

    #[test]
    fn discord_settings_create_defaults_and_apply_partial_updates() {
        let mut connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        let transaction = connection.transaction().unwrap();
        update_discord_settings(
            &transaction,
            &Map::from_iter([("client_id".to_owned(), json!("client"))]),
        )
        .unwrap();
        transaction.commit().unwrap();
        let (client_id, allow): (String, i64) = connection
            .query_row(
                "SELECT client_id,allow_registration FROM discord_settings WHERE id=1",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(client_id, "client");
        assert_eq!(allow, 1);
    }
}
