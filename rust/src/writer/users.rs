use bcrypt::{hash, DEFAULT_COST};
use chrono::Utc;
use rusqlite::types::Value as SqlValue;
use rusqlite::{OptionalExtension, Transaction};
use serde_json::{json, Map, Value};

use super::actions::{error, RpcActionResult};
use super::protocol::RpcError;

pub(super) const USER_ACTIONS: &[&str] = &[
    "create_user",
    "update_user_password",
    "delete_user",
    "set_user_role",
    "set_manual_feed_allowance",
    "upsert_discord_user",
    "set_user_billing_fields",
    "set_user_billing_by_customer_id",
    "update_user_last_active",
];

pub fn is_user_action(action: &str) -> bool {
    USER_ACTIONS.contains(&action)
}

pub fn execute(
    transaction: &Transaction<'_>,
    action: &str,
    params: &Map<String, Value>,
) -> RpcActionResult {
    match action {
        "create_user" => create_user(transaction, params),
        "update_user_password" => update_user_password(transaction, params),
        "delete_user" => delete_user(transaction, params),
        "set_user_role" => set_user_role(transaction, params),
        "set_manual_feed_allowance" => set_manual_feed_allowance(transaction, params),
        "upsert_discord_user" => upsert_discord_user(transaction, params),
        "set_user_billing_fields" => set_user_billing_fields(transaction, params),
        "set_user_billing_by_customer_id" => set_user_billing_by_customer_id(transaction, params),
        "update_user_last_active" => update_user_last_active(transaction, params),
        _ => Err(error(
            "unsupported_action",
            "writer action is not registered",
        )),
    }
}

fn create_user(transaction: &Transaction<'_>, params: &Map<String, Value>) -> RpcActionResult {
    let username = params
        .get("username")
        .filter(|value| truthy(value))
        .and_then(Value::as_str)
        .ok_or_else(|| error("invalid_params", "username is required"))?
        .trim()
        .to_lowercase();
    if username.is_empty() {
        return Err(error("invalid_params", "username is required"));
    }
    let password = params
        .get("password")
        .and_then(Value::as_str)
        .filter(|password| !password.is_empty())
        .ok_or_else(|| error("invalid_params", "password is required"))?;
    let role = params
        .get("role")
        .filter(|value| truthy(value))
        .and_then(Value::as_str)
        .unwrap_or("user");
    validate_role(role)?;
    if find_user_id(transaction, "username", &username)?.is_some() {
        return Err(error(
            "invalid_params",
            "A user with that username already exists",
        ));
    }
    let password_hash = hash_password(password)?;
    let now = database_now();
    transaction
        .execute(
            "INSERT INTO users(
                username,password_hash,role,feed_allowance,feed_subscription_status,
                stripe_customer_id,stripe_subscription_id,created_at,updated_at,
                discord_id,discord_username,last_active,manual_feed_allowance
             ) VALUES (?1,?2,?3,0,'inactive',NULL,NULL,?4,?4,NULL,NULL,NULL,NULL)",
            rusqlite::params![username, password_hash, role, now],
        )
        .map_err(database_error)?;
    Ok(json!({"user_id": transaction.last_insert_rowid()}))
}

// Python's bcrypt rejects passwords over 72 bytes at login, so a truncated hash would lock the user out.
fn hash_password(password: &str) -> Result<String, RpcError> {
    if password.len() > 72 {
        return Err(error(
            "invalid_params",
            "password cannot be longer than 72 bytes",
        ));
    }
    hash(password, DEFAULT_COST).map_err(|_| error("internal_error", "password hashing failed"))
}

fn update_user_password(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let (user_id, original) = required_user_id(params)?;
    let password = params
        .get("new_password")
        .and_then(Value::as_str)
        .filter(|password| !password.is_empty())
        .ok_or_else(|| error("invalid_params", "new_password is required"))?;
    require_user(transaction, user_id, &original)?;
    let password_hash = hash_password(password)?;
    update_user_column(
        transaction,
        user_id,
        "password_hash",
        SqlValue::Text(password_hash),
    )?;
    Ok(json!({"user_id": user_id}))
}

fn delete_user(transaction: &Transaction<'_>, params: &Map<String, Value>) -> RpcActionResult {
    let (user_id, _) = required_user_id(params)?;
    if !user_exists(transaction, user_id)? {
        return Ok(json!({"deleted": false}));
    }
    transaction
        .execute("DELETE FROM feed_access_token WHERE user_id=?1", [user_id])
        .map_err(database_error)?;
    transaction
        .execute("DELETE FROM feed_supporter WHERE user_id=?1", [user_id])
        .map_err(database_error)?;
    transaction
        .execute(
            "UPDATE processing_job SET requested_by_user_id=NULL WHERE requested_by_user_id=?1",
            [user_id],
        )
        .map_err(database_error)?;
    transaction
        .execute(
            "UPDATE processing_job SET billing_user_id=NULL WHERE billing_user_id=?1",
            [user_id],
        )
        .map_err(database_error)?;
    transaction
        .execute("DELETE FROM users WHERE id=?1", [user_id])
        .map_err(database_error)?;
    Ok(json!({"deleted": true}))
}

fn set_user_role(transaction: &Transaction<'_>, params: &Map<String, Value>) -> RpcActionResult {
    let role_value = params.get("role");
    if !params.get("user_id").is_some_and(truthy) || !role_value.is_some_and(truthy) {
        return Err(error("invalid_params", "user_id and role are required"));
    }
    let (user_id, original) = required_user_id(params)?;
    let role = role_value.and_then(Value::as_str).unwrap_or("");
    validate_role(role)?;
    require_user(transaction, user_id, &original)?;
    update_user_column(
        transaction,
        user_id,
        "role",
        SqlValue::Text(role.to_owned()),
    )?;
    Ok(json!({"user_id": user_id}))
}

fn set_manual_feed_allowance(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let (user_id, original) = required_user_id(params)?;
    require_user(transaction, user_id, &original)?;
    let allowance = match params.get("allowance") {
        None | Some(Value::Null) => SqlValue::Null,
        Some(value) => SqlValue::Integer(
            py_int(value)
                .map_err(|_| error("invalid_params", "allowance must be an integer or None"))?,
        ),
    };
    update_user_column(transaction, user_id, "manual_feed_allowance", allowance)?;
    Ok(json!({"user_id": user_id}))
}

fn upsert_discord_user(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let discord_id = params.get("discord_id");
    let discord_username = params.get("discord_username");
    if !discord_id.is_some_and(truthy) || !discord_username.is_some_and(truthy) {
        return Err(error(
            "invalid_params",
            "discord_id and discord_username are required",
        ));
    }
    let discord_id = py_string(discord_id.unwrap());
    let discord_username = py_string(discord_username.unwrap());
    if let Some(user_id) = find_user_id(transaction, "discord_id", &discord_id)? {
        update_user_column(
            transaction,
            user_id,
            "discord_username",
            SqlValue::Text(discord_username),
        )?;
        return Ok(json!({"user_id": user_id, "created": false}));
    }
    if !params.get("allow_registration").is_none_or(truthy) {
        return Err(error(
            "invalid_params",
            "Self-registration via Discord is disabled",
        ));
    }
    let base_username: String = discord_username
        .to_lowercase()
        .replace(' ', "_")
        .chars()
        .take(50)
        .collect();
    let mut username = base_username.clone();
    let mut counter = 1;
    while find_user_id(transaction, "username", &username)?.is_some() {
        username = format!("{base_username}_{counter}");
        counter += 1;
    }
    let now = database_now();
    transaction
        .execute(
            "INSERT INTO users(
                username,password_hash,role,feed_allowance,feed_subscription_status,
                stripe_customer_id,stripe_subscription_id,created_at,updated_at,
                discord_id,discord_username,last_active,manual_feed_allowance
             ) VALUES (?1,'','user',0,'inactive',NULL,NULL,?2,?2,?3,?4,NULL,NULL)",
            rusqlite::params![username, now, discord_id, discord_username],
        )
        .map_err(database_error)?;
    Ok(json!({"user_id": transaction.last_insert_rowid(), "created": true}))
}

fn set_user_billing_fields(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let (user_id, original) = required_user_id(params)?;
    require_user(transaction, user_id, &original)?;
    apply_billing_fields(transaction, user_id, params, true)?;
    Ok(json!({"user_id": user_id}))
}

fn set_user_billing_by_customer_id(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let customer = params
        .get("stripe_customer_id")
        .filter(|value| truthy(value))
        .ok_or_else(|| error("invalid_params", "stripe_customer_id is required"))?;
    let Some(user_id) = find_user_id_value(transaction, "stripe_customer_id", customer)? else {
        return Ok(json!({"updated": false}));
    };
    apply_billing_fields(transaction, user_id, params, false)?;
    Ok(json!({"updated": true, "user_id": user_id}))
}

fn update_user_last_active(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let (user_id, original) = required_user_id(params)?;
    require_user(transaction, user_id, &original)?;
    let timestamp = Utc::now()
        .naive_utc()
        .format("%Y-%m-%dT%H:%M:%S%.6f")
        .to_string();
    update_user_column(
        transaction,
        user_id,
        "last_active",
        SqlValue::Text(timestamp.clone()),
    )?;
    Ok(json!({"user_id": user_id, "last_active": timestamp}))
}

fn apply_billing_fields(
    transaction: &Transaction<'_>,
    user_id: i64,
    params: &Map<String, Value>,
    include_customer: bool,
) -> Result<(), RpcError> {
    if include_customer {
        if let Some(value) = params.get("stripe_customer_id") {
            update_user_column(
                transaction,
                user_id,
                "stripe_customer_id",
                sql_scalar(value)?,
            )?;
        }
    }
    if let Some(value) = params.get("stripe_subscription_id") {
        update_user_column(
            transaction,
            user_id,
            "stripe_subscription_id",
            sql_scalar(value)?,
        )?;
    }
    if let Some(value) = params.get("feed_allowance") {
        let allowance = if truthy(value) {
            py_int(value).map_err(|message| error("invalid_params", &message))?
        } else {
            0
        };
        update_user_column(
            transaction,
            user_id,
            "feed_allowance",
            SqlValue::Integer(allowance),
        )?;
    }
    if let Some(value) = params.get("feed_subscription_status") {
        let status = if truthy(value) {
            sql_scalar(value)?
        } else {
            SqlValue::Text(String::new())
        };
        update_user_column(transaction, user_id, "feed_subscription_status", status)?;
    }
    Ok(())
}

fn required_user_id(params: &Map<String, Value>) -> Result<(i64, String), RpcError> {
    let value = params
        .get("user_id")
        .filter(|value| truthy(value))
        .ok_or_else(|| error("invalid_params", "user_id is required"))?;
    let original = py_string(value);
    let user_id = py_int(value).map_err(|message| error("invalid_params", &message))?;
    Ok((user_id, original))
}

fn require_user(
    transaction: &Transaction<'_>,
    user_id: i64,
    original: &str,
) -> Result<(), RpcError> {
    if user_exists(transaction, user_id)? {
        Ok(())
    } else {
        Err(error(
            "invalid_params",
            &format!("User {original} not found"),
        ))
    }
}

fn user_exists(transaction: &Transaction<'_>, user_id: i64) -> Result<bool, RpcError> {
    transaction
        .query_row("SELECT 1 FROM users WHERE id=?1", [user_id], |_| Ok(()))
        .optional()
        .map(|result| result.is_some())
        .map_err(database_error)
}

fn find_user_id(
    transaction: &Transaction<'_>,
    column: &str,
    value: &str,
) -> Result<Option<i64>, RpcError> {
    find_user_id_sql(transaction, column, SqlValue::Text(value.to_owned()))
}

fn find_user_id_value(
    transaction: &Transaction<'_>,
    column: &str,
    value: &Value,
) -> Result<Option<i64>, RpcError> {
    find_user_id_sql(transaction, column, sql_scalar(value)?)
}

fn find_user_id_sql(
    transaction: &Transaction<'_>,
    column: &str,
    value: SqlValue,
) -> Result<Option<i64>, RpcError> {
    if !matches!(column, "username" | "discord_id" | "stripe_customer_id") {
        return Err(error("internal_error", "invalid user lookup column"));
    }
    transaction
        .query_row(
            &format!("SELECT id FROM users WHERE {column}=?1 LIMIT 1"),
            [value],
            |row| row.get(0),
        )
        .optional()
        .map_err(database_error)
}

fn update_user_column(
    transaction: &Transaction<'_>,
    user_id: i64,
    column: &str,
    value: SqlValue,
) -> Result<(), RpcError> {
    if !matches!(
        column,
        "password_hash"
            | "role"
            | "manual_feed_allowance"
            | "discord_username"
            | "stripe_customer_id"
            | "stripe_subscription_id"
            | "feed_allowance"
            | "feed_subscription_status"
            | "last_active"
    ) {
        return Err(error("internal_error", "invalid user update column"));
    }
    transaction
        .execute(
            &format!("UPDATE users SET {column}=?1, updated_at=?2 WHERE id=?3"),
            rusqlite::params![value, database_now(), user_id],
        )
        .map_err(database_error)?;
    Ok(())
}

fn validate_role(role: &str) -> Result<(), RpcError> {
    if matches!(role, "admin" | "user") {
        Ok(())
    } else {
        Err(error("invalid_params", "role must be 'admin' or 'user'"))
    }
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64().is_some_and(|value| value != 0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

fn py_int(value: &Value) -> Result<i64, String> {
    match value {
        Value::Bool(value) => Ok(i64::from(*value)),
        Value::Number(value) => value
            .as_i64()
            .or_else(|| value.as_u64().and_then(|value| i64::try_from(value).ok()))
            .or_else(|| value.as_f64().map(|value| value.trunc() as i64))
            .ok_or_else(|| "invalid integer value".to_owned()),
        Value::String(value) => value
            .trim()
            .parse::<i64>()
            .map_err(|_| format!("invalid literal for int() with base 10: '{value}'")),
        _ => Err(format!(
            "int() argument must be a string, a bytes-like object or a real number, not '{}'",
            match value {
                Value::Null => "NoneType",
                Value::Array(_) => "list",
                Value::Object(_) => "dict",
                _ => "unknown",
            }
        )),
    }
}

fn py_string(value: &Value) -> String {
    match value {
        Value::Null => "None".to_owned(),
        Value::Bool(true) => "True".to_owned(),
        Value::Bool(false) => "False".to_owned(),
        Value::String(value) => value.clone(),
        _ => value.to_string(),
    }
}

fn sql_scalar(value: &Value) -> Result<SqlValue, RpcError> {
    match value {
        Value::Null => Ok(SqlValue::Null),
        Value::Bool(value) => Ok(SqlValue::Integer(i64::from(*value))),
        Value::Number(value) if value.is_i64() => Ok(SqlValue::Integer(value.as_i64().unwrap())),
        Value::Number(value) if value.is_u64() => value
            .as_u64()
            .and_then(|value| i64::try_from(value).ok())
            .map(SqlValue::Integer)
            .ok_or_else(|| error("invalid_params", "integer is out of range")),
        Value::Number(value) => value
            .as_f64()
            .map(SqlValue::Real)
            .ok_or_else(|| error("invalid_params", "invalid numeric value")),
        Value::String(value) => Ok(SqlValue::Text(value.clone())),
        _ => Err(error("invalid_params", "value must be a scalar")),
    }
}

fn database_now() -> String {
    Utc::now()
        .naive_utc()
        .format("%Y-%m-%d %H:%M:%S%.6f")
        .to_string()
}

fn database_error(_error: rusqlite::Error) -> RpcError {
    error("database_error", "database operation failed")
}

#[cfg(test)]
mod tests {
    use bcrypt::verify;
    use rusqlite::Connection;

    use super::*;

    fn schema(connection: &Connection) {
        connection
            .execute_batch(
                "CREATE TABLE users(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username VARCHAR(255) NOT NULL UNIQUE,
                    password_hash VARCHAR(255) NOT NULL,
                    role VARCHAR(50) NOT NULL,
                    feed_allowance INTEGER NOT NULL,
                    feed_subscription_status VARCHAR(32) NOT NULL,
                    stripe_customer_id VARCHAR(64),
                    stripe_subscription_id VARCHAR(64),
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    discord_id VARCHAR(32) UNIQUE,
                    discord_username VARCHAR(100),
                    last_active DATETIME,
                    manual_feed_allowance INTEGER
                 );
                 CREATE TABLE feed_access_token(
                    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL
                 );
                 CREATE TABLE feed_supporter(
                    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL
                 );
                 CREATE TABLE processing_job(
                    id TEXT PRIMARY KEY,
                    requested_by_user_id INTEGER,
                    billing_user_id INTEGER
                 );",
            )
            .unwrap();
    }

    fn seed_user(connection: &Connection, username: &str) -> i64 {
        connection
            .execute(
                "INSERT INTO users(
                    username,password_hash,role,feed_allowance,feed_subscription_status,
                    created_at,updated_at
                 ) VALUES (?1,'old','user',0,'inactive','2020-01-01','2020-01-01')",
                [username],
            )
            .unwrap();
        connection.last_insert_rowid()
    }

    #[test]
    fn create_user_normalizes_defaults_and_writes_python_compatible_bcrypt() {
        let mut connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        let transaction = connection.transaction().unwrap();
        let result = create_user(
            &transaction,
            &Map::from_iter([
                ("username".to_owned(), json!("  Alice ")),
                ("password".to_owned(), json!("secret")),
            ]),
        )
        .unwrap();
        let user_id = result["user_id"].as_i64().unwrap();
        transaction.commit().unwrap();
        let row: (String, String, String, i64, String) = connection
            .query_row(
                "SELECT username,password_hash,role,feed_allowance,
                        feed_subscription_status FROM users WHERE id=?1",
                [user_id],
                |row| {
                    Ok((
                        row.get(0)?,
                        row.get(1)?,
                        row.get(2)?,
                        row.get(3)?,
                        row.get(4)?,
                    ))
                },
            )
            .unwrap();
        assert_eq!(row.0, "alice");
        assert_eq!(row.2, "user");
        assert_eq!(row.3, 0);
        assert_eq!(row.4, "inactive");
        assert!(row.1.starts_with("$2"));
        assert_eq!(row.1.split('$').nth(2), Some("12"));
        assert!(verify("secret", &row.1).unwrap());
    }

    #[test]
    fn passwords_over_72_bytes_are_rejected_without_changing_the_hash() {
        let mut connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        let user_id = seed_user(&connection, "alice");
        let before: String = connection
            .query_row(
                "SELECT password_hash FROM users WHERE id=?1",
                [user_id],
                |row| row.get(0),
            )
            .unwrap();
        let transaction = connection.transaction().unwrap();
        for password in ["a".repeat(73), "é".repeat(37)] {
            let err = update_user_password(
                &transaction,
                &Map::from_iter([
                    ("user_id".to_owned(), json!(user_id)),
                    ("new_password".to_owned(), json!(password)),
                ]),
            )
            .unwrap_err();
            assert_eq!(err.code, "invalid_params");
        }
        update_user_password(
            &transaction,
            &Map::from_iter([
                ("user_id".to_owned(), json!(user_id)),
                ("new_password".to_owned(), json!("a".repeat(72))),
            ]),
        )
        .unwrap();
        transaction.rollback().unwrap();
        // Hash of "a" * 72 from Python's bcrypt: both sides agree at the limit.
        assert!(verify(
            "a".repeat(72),
            "$2b$04$xrhEYmKyZiOU0kdJeFKSlutWNvBKt7jHpMyPOEXvgE..wnVLzBefq"
        )
        .unwrap());
        let after: String = connection
            .query_row(
                "SELECT password_hash FROM users WHERE id=?1",
                [user_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(before, after);
    }

    #[test]
    fn password_role_allowance_and_activity_updates_match_action_contract() {
        let mut connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        let user_id = seed_user(&connection, "alice");
        let transaction = connection.transaction().unwrap();
        update_user_password(
            &transaction,
            &Map::from_iter([
                ("user_id".to_owned(), json!(user_id.to_string())),
                ("new_password".to_owned(), json!("new secret")),
            ]),
        )
        .unwrap();
        set_user_role(
            &transaction,
            &Map::from_iter([
                ("user_id".to_owned(), json!(user_id)),
                ("role".to_owned(), json!("admin")),
            ]),
        )
        .unwrap();
        set_manual_feed_allowance(
            &transaction,
            &Map::from_iter([
                ("user_id".to_owned(), json!(user_id)),
                ("allowance".to_owned(), json!("7")),
            ]),
        )
        .unwrap();
        let active = update_user_last_active(
            &transaction,
            &Map::from_iter([("user_id".to_owned(), json!(user_id))]),
        )
        .unwrap();
        transaction.commit().unwrap();
        let row: (String, String, i64, String) = connection
            .query_row(
                "SELECT password_hash,role,manual_feed_allowance,last_active
                 FROM users WHERE id=?1",
                [user_id],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .unwrap();
        assert!(verify("new secret", &row.0).unwrap());
        assert_eq!(row.1, "admin");
        assert_eq!(row.2, 7);
        assert_eq!(active["last_active"], row.3);
    }

    #[test]
    fn discord_and_billing_updates_preserve_defaults_and_partial_fields() {
        let mut connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        let transaction = connection.transaction().unwrap();
        let created = upsert_discord_user(
            &transaction,
            &Map::from_iter([
                ("discord_id".to_owned(), json!(1234)),
                ("discord_username".to_owned(), json!("Test User")),
            ]),
        )
        .unwrap();
        let user_id = created["user_id"].as_i64().unwrap();
        set_user_billing_fields(
            &transaction,
            &Map::from_iter([
                ("user_id".to_owned(), json!(user_id)),
                ("stripe_customer_id".to_owned(), json!("cus_1")),
                ("feed_allowance".to_owned(), Value::Null),
                ("feed_subscription_status".to_owned(), json!("active")),
            ]),
        )
        .unwrap();
        let updated = set_user_billing_by_customer_id(
            &transaction,
            &Map::from_iter([
                ("stripe_customer_id".to_owned(), json!("cus_1")),
                ("stripe_subscription_id".to_owned(), json!("sub_1")),
            ]),
        )
        .unwrap();
        transaction.commit().unwrap();
        assert_eq!(updated, json!({"updated": true, "user_id": user_id}));
        let row: (String, String, String, i64, String, String) = connection
            .query_row(
                "SELECT username,password_hash,discord_id,feed_allowance,
                        feed_subscription_status,stripe_subscription_id
                 FROM users WHERE id=?1",
                [user_id],
                |row| {
                    Ok((
                        row.get(0)?,
                        row.get(1)?,
                        row.get(2)?,
                        row.get(3)?,
                        row.get(4)?,
                        row.get(5)?,
                    ))
                },
            )
            .unwrap();
        assert_eq!(
            row,
            (
                "test_user".into(),
                "".into(),
                "1234".into(),
                0,
                "active".into(),
                "sub_1".into()
            )
        );
    }

    #[test]
    fn delete_user_reproduces_orm_relationship_side_effects() {
        let mut connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        let user_id = seed_user(&connection, "alice");
        connection
            .execute("INSERT INTO feed_access_token VALUES (1,?1)", [user_id])
            .unwrap();
        connection
            .execute("INSERT INTO feed_supporter VALUES (1,?1)", [user_id])
            .unwrap();
        connection
            .execute("INSERT INTO processing_job VALUES ('job',?1,?1)", [user_id])
            .unwrap();
        let transaction = connection.transaction().unwrap();
        assert_eq!(
            delete_user(
                &transaction,
                &Map::from_iter([("user_id".to_owned(), json!(user_id))]),
            )
            .unwrap(),
            json!({"deleted": true})
        );
        transaction.commit().unwrap();
        assert_eq!(
            connection
                .query_row("SELECT COUNT(*) FROM users", [], |row| row.get::<_, i64>(0))
                .unwrap(),
            0
        );
        assert_eq!(
            connection
                .query_row(
                    "SELECT requested_by_user_id IS NULL AND billing_user_id IS NULL
                     FROM processing_job",
                    [],
                    |row| row.get::<_, i64>(0),
                )
                .unwrap(),
            1
        );
    }

    #[test]
    fn validation_errors_leave_existing_values_unchanged() {
        let mut connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        let user_id = seed_user(&connection, "alice");
        let transaction = connection.transaction().unwrap();
        let failure = set_manual_feed_allowance(
            &transaction,
            &Map::from_iter([
                ("user_id".to_owned(), json!(user_id)),
                ("allowance".to_owned(), json!("invalid")),
            ]),
        )
        .unwrap_err();
        assert_eq!(failure.message, "allowance must be an integer or None");
        transaction.rollback().unwrap();
        let allowance: Option<i64> = connection
            .query_row(
                "SELECT manual_feed_allowance FROM users WHERE id=?1",
                [user_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(allowance, None);
    }
}
