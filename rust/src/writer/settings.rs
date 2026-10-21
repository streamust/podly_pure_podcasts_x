use rusqlite::types::Value as SqlValue;
use rusqlite::{Connection, OptionalExtension, Transaction};
use serde_json::{Map, Number, Value};

use super::actions::{error, RpcActionResult};

#[derive(Clone, Copy)]
enum Kind {
    Text,
    Integer,
    Number,
    Boolean,
}

#[derive(Clone, Copy)]
struct Field {
    api: &'static str,
    column: &'static str,
    kind: Kind,
}

const LLM: &[Field] = &[
    field("llm_api_key", Kind::Text),
    field("llm_model", Kind::Text),
    field("openai_base_url", Kind::Text),
    field("openai_timeout", Kind::Integer),
    field("openai_max_tokens", Kind::Integer),
    field("llm_max_concurrent_calls", Kind::Integer),
    field("llm_max_retry_attempts", Kind::Integer),
    field("llm_max_input_tokens_per_call", Kind::Integer),
    field("llm_enable_token_rate_limiting", Kind::Boolean),
    field("llm_max_input_tokens_per_minute", Kind::Integer),
    field("enable_boundary_refinement", Kind::Boolean),
    field("enable_word_level_boundary_refinder", Kind::Boolean),
    field("enable_llm_chapter_fallback_tagging", Kind::Boolean),
    field("chapter_full_block_text", Kind::Boolean),
    field("llm_service_tier", Kind::Text),
];
const PROCESSING: &[Field] = &[field("num_segments_to_input_to_prompt", Kind::Integer)];
const OUTPUT: &[Field] = &[
    field("fade_ms", Kind::Integer),
    field("bleep_padding_start_ms", Kind::Integer),
    field("bleep_padding_end_ms", Kind::Integer),
    field("min_ad_segement_separation_seconds", Kind::Integer),
    field("min_ad_segment_length_seconds", Kind::Integer),
    field("min_confidence", Kind::Number),
    field("auto_retry_zero_ads_on_parse_error", Kind::Boolean),
];
const APP: &[Field] = &[
    field("background_update_interval_minute", Kind::Integer),
    field("automatically_whitelist_new_episodes", Kind::Boolean),
    field("post_cleanup_retention_days", Kind::Integer),
    field(
        "number_of_episodes_to_whitelist_from_archive_of_new_feed",
        Kind::Integer,
    ),
    field("enable_public_landing_page", Kind::Boolean),
    field("user_limit_total", Kind::Integer),
    field("autoprocess_on_download", Kind::Boolean),
    field("cost_rate_per_hour", Kind::Number),
    field("whisper_cost_rate_per_hour", Kind::Number),
    field("ina_cost_rate_per_hour", Kind::Number),
];
const NOTIFICATIONS: &[Field] = &[
    field("enabled", Kind::Boolean),
    field("notify_on_failure", Kind::Boolean),
    field("notify_on_success", Kind::Boolean),
    field("notify_on_rust_fallback", Kind::Boolean),
    field("include_llm_explanation", Kind::Boolean),
];
const WHISPER_REMOTE: &[Field] = &[
    mapped("model", "remote_model", Kind::Text),
    mapped("api_key", "remote_api_key", Kind::Text),
    mapped("base_url", "remote_base_url", Kind::Text),
    mapped("language", "remote_language", Kind::Text),
    mapped("timeout_sec", "remote_timeout_sec", Kind::Integer),
    mapped("chunksize_mb", "remote_chunksize_mb", Kind::Integer),
    mapped("diarize", "remote_diarize", Kind::Boolean),
    mapped(
        "speaker_embeddings",
        "remote_speaker_embeddings",
        Kind::Boolean,
    ),
];
const WHISPER_GROQ: &[Field] = &[
    mapped("api_key", "groq_api_key", Kind::Text),
    mapped("model", "groq_model", Kind::Text),
    mapped("language", "groq_language", Kind::Text),
    mapped("max_retries", "groq_max_retries", Kind::Integer),
];

const fn field(name: &'static str, kind: Kind) -> Field {
    Field {
        api: name,
        column: name,
        kind,
    }
}

const fn mapped(api: &'static str, column: &'static str, kind: Kind) -> Field {
    Field { api, column, kind }
}

pub fn update_combined(connection: &Connection, params: &Map<String, Value>) -> RpcActionResult {
    let payload = params
        .get("payload")
        .and_then(Value::as_object)
        .ok_or_else(|| error("invalid_params", "payload must be a dictionary"))?;

    for (section, table, fields) in [
        ("llm", "llm_settings", LLM),
        ("processing", "processing_settings", PROCESSING),
        ("output", "output_settings", OUTPUT),
        ("app", "app_settings", APP),
    ] {
        if let Some(data) = section_data(payload, section)? {
            update_section(connection, table, data, fields, section == "llm")?;
        }
        if section == "llm" {
            if let Some(data) = section_data(payload, "whisper")? {
                update_whisper(connection, data)?;
            }
        }
    }
    if let Some(data) = section_data(payload, "notifications")? {
        update_notifications(connection, data)?;
    }
    read_combined(connection)
}

fn section_data<'a>(
    payload: &'a Map<String, Value>,
    section: &str,
) -> Result<Option<&'a Map<String, Value>>, super::protocol::RpcError> {
    match payload.get(section) {
        None => Ok(None),
        Some(Value::Null) => Ok(Some(empty_map())),
        Some(Value::Object(data)) => Ok(Some(data)),
        Some(_) => Err(error(
            "invalid_params",
            "config section must be an object or null",
        )),
    }
}

fn empty_map() -> &'static Map<String, Value> {
    static EMPTY: std::sync::OnceLock<Map<String, Value>> = std::sync::OnceLock::new();
    EMPTY.get_or_init(Map::new)
}

fn update_section(
    connection: &Connection,
    table: &str,
    data: &Map<String, Value>,
    fields: &[Field],
    skip_empty_api_key: bool,
) -> RpcActionResult {
    require_settings_row(connection, table)?;
    let transaction = connection.unchecked_transaction().map_err(|_| db_error())?;
    apply_fields(
        &transaction,
        table,
        data,
        fields,
        skip_empty_api_key.then_some("llm_api_key"),
    )?;
    transaction.commit().map_err(|_| db_error())?;
    Ok(Value::Null)
}

fn update_whisper(connection: &Connection, data: &Map<String, Value>) -> RpcActionResult {
    require_settings_row(connection, "whisper_settings")?;
    let transaction = connection.unchecked_transaction().map_err(|_| db_error())?;
    let mut whisper_type = read_text(&transaction, "whisper_settings", "whisper_type")?;
    if let Some(Value::String(value)) = data.get("whisper_type") {
        if matches!(value.as_str(), "remote" | "groq" | "test") {
            transaction
                .execute(
                    "UPDATE whisper_settings SET whisper_type=?1 WHERE id=1",
                    [value],
                )
                .map_err(|_| db_error())?;
            whisper_type.clone_from(value);
        }
    }
    let fields = match whisper_type.as_str() {
        "remote" => WHISPER_REMOTE,
        "groq" => WHISPER_GROQ,
        _ => &[],
    };
    apply_fields(
        &transaction,
        "whisper_settings",
        data,
        fields,
        Some("api_key"),
    )?;
    transaction.commit().map_err(|_| db_error())?;
    Ok(Value::Null)
}

fn update_notifications(connection: &Connection, data: &Map<String, Value>) -> RpcActionResult {
    require_settings_row(connection, "notification_settings")?;
    let transaction = connection.unchecked_transaction().map_err(|_| db_error())?;
    for field in NOTIFICATIONS {
        if let Some(value) = data.get(field.api) {
            let value = SqlValue::Integer(i64::from(python_truthy(value)));
            update_one(&transaction, "notification_settings", field.column, value)?;
        }
    }
    if let Some(value) = data.get("apprise_urls") {
        let urls = normalize_urls(value);
        let value = if urls.is_empty() {
            SqlValue::Null
        } else {
            SqlValue::Text(urls.join("\n"))
        };
        update_one(&transaction, "notification_settings", "apprise_urls", value)?;
    }
    transaction.commit().map_err(|_| db_error())?;
    Ok(Value::Null)
}

fn apply_fields(
    transaction: &Transaction<'_>,
    table: &str,
    data: &Map<String, Value>,
    fields: &[Field],
    skip_empty: Option<&str>,
) -> RpcActionResult {
    for field in fields {
        let Some(value) = data.get(field.api) else {
            continue;
        };
        if skip_empty == Some(field.api) && (value.is_null() || value.as_str() == Some("")) {
            continue;
        }
        update_one(transaction, table, field.column, encode(value, field.kind)?)?;
    }
    Ok(Value::Null)
}

fn update_one(
    transaction: &Transaction<'_>,
    table: &str,
    column: &str,
    value: SqlValue,
) -> RpcActionResult {
    transaction
        .execute(
            &format!("UPDATE {table} SET {column}=?1 WHERE id=1"),
            [value],
        )
        .map_err(|_| db_error())?;
    Ok(Value::Null)
}

fn require_settings_row(connection: &Connection, table: &str) -> RpcActionResult {
    let exists = connection
        .query_row(&format!("SELECT 1 FROM {table} WHERE id=1"), [], |_| Ok(()))
        .optional()
        .map_err(|_| db_error())?
        .is_some();
    if exists {
        Ok(Value::Null)
    } else {
        Err(error(
            "settings_missing",
            "required settings row is missing",
        ))
    }
}

fn encode(value: &Value, kind: Kind) -> Result<SqlValue, super::protocol::RpcError> {
    if value.is_null() {
        return Ok(SqlValue::Null);
    }
    match kind {
        Kind::Text => value
            .as_str()
            .map(|value| SqlValue::Text(value.to_owned()))
            .ok_or_else(|| error("invalid_params", "text setting has invalid type")),
        Kind::Integer => value
            .as_i64()
            .map(SqlValue::Integer)
            .ok_or_else(|| error("invalid_params", "integer setting has invalid type")),
        Kind::Number => value
            .as_i64()
            .map(SqlValue::Integer)
            .or_else(|| value.as_f64().map(SqlValue::Real))
            .ok_or_else(|| error("invalid_params", "numeric setting has invalid type")),
        Kind::Boolean => value
            .as_bool()
            .map(|value| SqlValue::Integer(i64::from(value)))
            .ok_or_else(|| error("invalid_params", "boolean setting has invalid type")),
    }
}

fn python_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64().is_some_and(|value| value != 0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

fn normalize_urls(value: &Value) -> Vec<String> {
    match value {
        Value::String(value) => value
            .lines()
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(str::to_owned)
            .collect(),
        Value::Array(values) => values
            .iter()
            .map(|value| match value {
                Value::String(value) => value.clone(),
                other => other.to_string(),
            })
            .map(|value| value.trim().to_owned())
            .filter(|value| !value.is_empty())
            .collect(),
        _ => Vec::new(),
    }
}

fn read_combined(connection: &Connection) -> RpcActionResult {
    let llm = read_fields(connection, "llm_settings", LLM)?;
    let processing = read_fields(connection, "processing_settings", PROCESSING)?;
    let output = read_fields(connection, "output_settings", OUTPUT)?;
    let app = read_fields(connection, "app_settings", APP)?;
    let mut notifications = read_fields(connection, "notification_settings", NOTIFICATIONS)?;
    let urls = read_text_optional(connection, "notification_settings", "apprise_urls")?
        .map(|urls| {
            urls.lines()
                .map(str::trim)
                .filter(|url| !url.is_empty())
                .map(|url| Value::String(url.to_owned()))
                .collect()
        })
        .unwrap_or_default();
    notifications.insert("apprise_urls".to_owned(), Value::Array(urls));

    let whisper_type = read_text(connection, "whisper_settings", "whisper_type")?;
    if whisper_type == "local" {
        return Err(error(
            "invalid_config",
            "stored whisper type is unsupported",
        ));
    }
    let mut whisper = Map::from_iter([(
        "whisper_type".to_owned(),
        Value::String(whisper_type.clone()),
    )]);
    let fields = match whisper_type.as_str() {
        "remote" => WHISPER_REMOTE,
        "groq" => WHISPER_GROQ,
        _ => &[],
    };
    whisper.extend(read_fields(connection, "whisper_settings", fields)?);
    Ok(Value::Object(Map::from_iter([
        ("llm".to_owned(), Value::Object(llm)),
        ("whisper".to_owned(), Value::Object(whisper)),
        ("processing".to_owned(), Value::Object(processing)),
        ("output".to_owned(), Value::Object(output)),
        ("app".to_owned(), Value::Object(app)),
        ("notifications".to_owned(), Value::Object(notifications)),
    ])))
}

fn read_fields(
    connection: &Connection,
    table: &str,
    fields: &[Field],
) -> Result<Map<String, Value>, super::protocol::RpcError> {
    fields
        .iter()
        .map(|field| {
            read_value(connection, table, field.column, field.kind)
                .map(|value| (field.api.to_owned(), value))
        })
        .collect()
}

fn read_value(
    connection: &Connection,
    table: &str,
    column: &str,
    kind: Kind,
) -> Result<Value, super::protocol::RpcError> {
    let value: SqlValue = connection
        .query_row(
            &format!("SELECT {column} FROM {table} WHERE id=1"),
            [],
            |row| row.get(0),
        )
        .map_err(|_| db_error())?;
    Ok(match value {
        SqlValue::Null => Value::Null,
        SqlValue::Integer(value) if matches!(kind, Kind::Boolean) => Value::Bool(value != 0),
        SqlValue::Integer(value) => Value::Number(value.into()),
        SqlValue::Real(value) => Number::from_f64(value)
            .map(Value::Number)
            .ok_or_else(|| error("database_error", "database contains a non-finite number"))?,
        SqlValue::Text(value) => Value::String(value),
        SqlValue::Blob(_) => return Err(db_error()),
    })
}

fn read_text(
    connection: &Connection,
    table: &str,
    column: &str,
) -> Result<String, super::protocol::RpcError> {
    read_text_optional(connection, table, column)?
        .ok_or_else(|| error("database_error", "required setting is null"))
}

fn read_text_optional(
    connection: &Connection,
    table: &str,
    column: &str,
) -> Result<Option<String>, super::protocol::RpcError> {
    connection
        .query_row(
            &format!("SELECT {column} FROM {table} WHERE id=1"),
            [],
            |row| row.get(0),
        )
        .map_err(|_| db_error())
}

fn db_error() -> super::protocol::RpcError {
    error("database_error", "database operation failed")
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    fn fixture() -> Connection {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch(
            "CREATE TABLE llm_settings(
                id INTEGER PRIMARY KEY,llm_api_key TEXT,llm_model TEXT NOT NULL,
                openai_base_url TEXT,openai_timeout INTEGER NOT NULL,
                openai_max_tokens INTEGER NOT NULL,llm_max_concurrent_calls INTEGER NOT NULL,
                llm_max_retry_attempts INTEGER NOT NULL,llm_max_input_tokens_per_call INTEGER,
                llm_enable_token_rate_limiting BOOLEAN NOT NULL,
                llm_max_input_tokens_per_minute INTEGER,enable_boundary_refinement BOOLEAN NOT NULL,
                enable_word_level_boundary_refinder BOOLEAN NOT NULL,
                enable_llm_chapter_fallback_tagging BOOLEAN NOT NULL,
                chapter_full_block_text BOOLEAN NOT NULL,llm_service_tier TEXT NOT NULL);
             INSERT INTO llm_settings VALUES(1,NULL,'old',NULL,300,4096,3,5,NULL,0,NULL,1,0,0,0,'default');
             CREATE TABLE whisper_settings(
                id INTEGER PRIMARY KEY,whisper_type TEXT NOT NULL,remote_model TEXT NOT NULL,
                remote_api_key TEXT,remote_base_url TEXT NOT NULL,remote_language TEXT NOT NULL,
                remote_timeout_sec INTEGER NOT NULL,remote_chunksize_mb INTEGER NOT NULL,
                remote_diarize BOOLEAN NOT NULL,remote_speaker_embeddings BOOLEAN NOT NULL,
                groq_api_key TEXT,groq_model TEXT NOT NULL,groq_language TEXT NOT NULL,
                groq_max_retries INTEGER NOT NULL);
             INSERT INTO whisper_settings VALUES(1,'groq','whisper-1',NULL,'https://api.openai.com/v1','en',600,24,0,0,NULL,'whisper-large-v3-turbo','en',0);
             CREATE TABLE processing_settings(id INTEGER PRIMARY KEY,num_segments_to_input_to_prompt INTEGER NOT NULL);
             INSERT INTO processing_settings VALUES(1,60);
             CREATE TABLE output_settings(
                id INTEGER PRIMARY KEY,fade_ms INTEGER NOT NULL,bleep_padding_start_ms INTEGER NOT NULL,
                bleep_padding_end_ms INTEGER NOT NULL,min_ad_segement_separation_seconds INTEGER NOT NULL,
                min_ad_segment_length_seconds INTEGER NOT NULL,min_confidence REAL NOT NULL,
                auto_retry_zero_ads_on_parse_error BOOLEAN NOT NULL);
             INSERT INTO output_settings VALUES(1,3000,150,150,60,14,0.8,0);
             CREATE TABLE app_settings(
                id INTEGER PRIMARY KEY,background_update_interval_minute INTEGER,
                automatically_whitelist_new_episodes BOOLEAN NOT NULL,post_cleanup_retention_days INTEGER,
                number_of_episodes_to_whitelist_from_archive_of_new_feed INTEGER NOT NULL,
                enable_public_landing_page BOOLEAN NOT NULL,user_limit_total INTEGER,
                autoprocess_on_download BOOLEAN NOT NULL,cost_rate_per_hour REAL NOT NULL,
                whisper_cost_rate_per_hour REAL NOT NULL,ina_cost_rate_per_hour REAL NOT NULL);
             INSERT INTO app_settings VALUES(1,30,1,5,1,0,NULL,0,0.04,0.04,0.0);
             CREATE TABLE notification_settings(
                id INTEGER PRIMARY KEY,enabled BOOLEAN NOT NULL,apprise_urls TEXT,
                notify_on_failure BOOLEAN NOT NULL,notify_on_success BOOLEAN NOT NULL,
                notify_on_rust_fallback BOOLEAN NOT NULL,include_llm_explanation BOOLEAN NOT NULL);
             INSERT INTO notification_settings VALUES(1,0,NULL,1,0,0,1);",
        ).unwrap();
        connection
    }

    #[test]
    fn combined_update_returns_full_shape_and_normalizes_sections() {
        let connection = fixture();
        let params = Map::from_iter([(
            "payload".to_owned(),
            json!({
                "llm": {"llm_model": "new", "llm_api_key": ""},
                "whisper": {"whisper_type": "remote", "model": "remote-new", "api_key": "secret"},
                "notifications": {"enabled": "false", "apprise_urls": [" one ", "", 2]}
            }),
        )]);
        let result = update_combined(&connection, &params).unwrap();
        assert_eq!(result["llm"]["llm_model"], "new");
        assert_eq!(result["llm"]["llm_api_key"], Value::Null);
        assert_eq!(result["whisper"]["whisper_type"], "remote");
        assert_eq!(result["whisper"]["model"], "remote-new");
        assert_eq!(result["notifications"]["enabled"], true);
        assert_eq!(result["notifications"]["apprise_urls"], json!(["one", "2"]));
    }

    #[test]
    fn later_section_failure_preserves_earlier_section_commit() {
        let connection = fixture();
        let params = Map::from_iter([(
            "payload".to_owned(),
            json!({"llm": {"llm_model": "committed"}, "output": {"min_confidence": {"bad": true}}}),
        )]);
        assert!(update_combined(&connection, &params).is_err());
        let model: String = connection
            .query_row("SELECT llm_model FROM llm_settings WHERE id=1", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(model, "committed");
    }
}
