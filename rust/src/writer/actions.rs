use std::thread;
use std::time::Duration;

use rusqlite::types::Value as SqlValue;
use rusqlite::{OptionalExtension, Transaction};
use serde_json::{json, Value};

use super::protocol::{Operation, RpcError, TransactionCommand};

pub type RpcActionResult = Result<Value, RpcError>;

#[derive(Debug, Clone, Default)]
pub struct ActionRegistry {
    test_actions: bool,
}

impl ActionRegistry {
    pub fn with_test_actions() -> Self {
        Self { test_actions: true }
    }

    pub fn production_complete(&self) -> bool {
        // Check every compiled handler group's names against the envelope
        // validator. CI's live-registry gate separately compares these groups
        // with Python's current registry and executed differential cases.
        [
            super::users::USER_ACTIONS,
            super::feeds::FEED_ACTIONS,
            super::jobs::JOB_ACTIONS,
            super::processor::PROCESSOR_ACTIONS,
            super::cleanup::CLEANUP_ACTIONS,
            &[
                "ensure_active_run",
                "update_discord_settings",
                "update_combined_config",
            ],
        ]
        .into_iter()
        .flatten()
        .all(|name| {
            self.validate(&Operation::Action {
                action: (*name).to_owned(),
                params: serde_json::Map::new(),
            })
            .is_ok()
        })
    }

    pub fn validate(&self, operation: &Operation) -> Result<(), RpcError> {
        match operation {
            Operation::Action { action, .. }
                if self.test_actions
                    && matches!(
                        action.as_str(),
                        "__test_noop" | "__test_sleep" | "__test_insert"
                    ) =>
            {
                Ok(())
            }
            Operation::Action { action, .. }
                if matches!(
                    action.as_str(),
                    "ensure_active_run"
                        | "update_discord_settings"
                        | "update_combined_config"
                        | "create_user"
                        | "update_user_password"
                        | "delete_user"
                        | "set_user_role"
                        | "set_manual_feed_allowance"
                        | "upsert_discord_user"
                        | "set_user_billing_fields"
                        | "set_user_billing_by_customer_id"
                        | "update_user_last_active"
                        | "refresh_feed"
                        | "add_feed"
                        | "update_feed_settings"
                        | "increment_download_count"
                        | "whitelist_post"
                        | "ensure_user_feed_membership"
                        | "remove_user_feed_membership"
                        | "whitelist_latest_post_for_feed"
                        | "toggle_whitelist_all_for_feed"
                        | "create_dev_test_feed"
                        | "delete_feed_cascade"
                        | "create_feed_access_token"
                        | "touch_feed_access_token"
                        | "dequeue_job"
                        | "cleanup_stale_jobs"
                        | "clear_all_jobs"
                        | "clear_active_jobs"
                        | "create_job"
                        | "create_job_if_missing"
                        | "cancel_existing_jobs"
                        | "update_job_attribution"
                        | "update_job_status"
                        | "mark_cancelled"
                        | "mark_classification_parse_error"
                        | "record_ad_windows_count"
                        | "mark_auto_retry_attempted"
                        | "reassign_pending_jobs"
                        | "upsert_model_call"
                        | "delete_model_calls_for_post_by_model_name"
                        | "upsert_whisper_model_call"
                        | "replace_transcription"
                        | "start_transcription_replace"
                        | "insert_transcript_segments"
                        | "finish_transcription_replace"
                        | "finish_transcription_replace_from_artifact"
                        | "mark_model_call_failed"
                        | "insert_identifications"
                        | "replace_identifications"
                        | "replace_audio_segments"
                        | "cleanup_missing_audio_paths"
                        | "clear_post_processing_data"
                        | "clear_post_processing_data_keep_transcript"
                        | "prepare_post_for_auto_retry"
                        | "cleanup_processed_post"
                        | "cleanup_processed_post_files_only"
                ) =>
            {
                Ok(())
            }
            Operation::Transaction { commands } => {
                if commands.is_empty() {
                    return Err(error("malformed", "transaction must not be empty"));
                }
                for command in commands {
                    if matches!(command.operation, Operation::Transaction { .. }) {
                        return Err(error("malformed", "nested transactions are unsupported"));
                    }
                    if matches!(
                        &command.operation,
                        Operation::Action { action, .. } if action == "update_combined_config"
                    ) {
                        return Err(error(
                            "malformed",
                            "update_combined_config cannot be nested in a transaction",
                        ));
                    }
                    self.validate(&command.operation)?;
                }
                Ok(())
            }
            Operation::Update { model, .. }
                if matches!(model.as_str(), "Post" | "ModelCall" | "Feed") =>
            {
                Ok(())
            }
            Operation::Delete { model, .. }
                if matches!(model.as_str(), "Post" | "ModelCall" | "Feed") =>
            {
                Ok(())
            }
            Operation::Action { .. } => Err(error(
                "unsupported_action",
                "writer action is not registered",
            )),
            Operation::Unsupported { .. } => Err(error(
                "unsupported_operation",
                "writer operation is not supported",
            )),
            _ => Err(error(
                "unsupported_model",
                "writer model operation is not registered",
            )),
        }
    }

    pub fn execute(
        &self,
        transaction: &Transaction<'_>,
        operation: &Operation,
    ) -> Result<Value, RpcError> {
        match operation {
            Operation::Transaction { commands } => commands
                .iter()
                .map(|command| self.execute_transaction_command(transaction, command))
                .collect::<Result<Vec<_>, _>>()
                .map(|results| json!({ "results": results })),
            Operation::Update { model, id, data } => {
                execute_model_update(transaction, model, id, data)
            }
            Operation::Delete { model, id } => execute_model_delete(transaction, model, id),
            Operation::Action { action, params }
                if self.test_actions
                    && matches!(
                        action.as_str(),
                        "__test_noop" | "__test_sleep" | "__test_insert"
                    ) =>
            {
                match action.as_str() {
                    "__test_noop" => Ok(Value::Null),
                    "__test_sleep" => {
                        let milliseconds = params
                            .get("milliseconds")
                            .and_then(Value::as_u64)
                            .unwrap_or(0);
                        thread::sleep(Duration::from_millis(milliseconds));
                        Ok(json!({ "slept_ms": milliseconds }))
                    }
                    "__test_insert" => {
                        let value = params
                            .get("value")
                            .and_then(Value::as_str)
                            .ok_or_else(|| error("invalid_params", "value must be a string"))?;
                        transaction
                            .execute("INSERT INTO writer_test_events(value) VALUES (?1)", [value])
                            .map_err(database_error)?;
                        Ok(json!({ "inserted": value }))
                    }
                    _ => Err(error(
                        "unsupported_action",
                        "writer action is not registered",
                    )),
                }
            }
            Operation::Action { action, params } => {
                if super::users::is_user_action(action) {
                    super::users::execute(transaction, action, params)
                } else if super::feeds::is_feed_action(action) {
                    super::feeds::execute(transaction, action, params)
                } else if super::jobs::is_job_action(action) {
                    super::jobs::execute(transaction, action, params)
                } else if super::processor::is_processor_action(action) {
                    super::processor::execute(transaction, action, params)
                } else if super::cleanup::is_cleanup_action(action) {
                    super::cleanup::execute(transaction, action, params)
                } else {
                    super::system::execute(transaction, action, params)
                }
            }
            _ => self.validate(operation).and(Ok(Value::Null)),
        }
    }

    pub fn execute_non_atomic(
        &self,
        connection: &rusqlite::Connection,
        operation: &Operation,
    ) -> Option<RpcActionResult> {
        match operation {
            Operation::Action { action, params } if action == "update_combined_config" => {
                Some(super::settings::update_combined(connection, params))
            }
            _ => None,
        }
    }

    fn execute_transaction_command(
        &self,
        transaction: &Transaction<'_>,
        command: &TransactionCommand,
    ) -> Result<Value, RpcError> {
        self.execute(transaction, &command.operation)
            .map(|data| {
                json!({
                    "command_id": command.command_id,
                    "success": true,
                    "data": data,
                    "error": Value::Null,
                })
            })
            .map_err(|failure| {
                error(
                    "transaction_failed",
                    &format!(
                        "transaction failed at {}: {}",
                        command.command_id, failure.message
                    ),
                )
            })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn transaction_update_result_matches_python_write_result_shape() {
        let connection = rusqlite::Connection::open_in_memory().unwrap();
        connection
            .execute(
                "CREATE TABLE post(id INTEGER PRIMARY KEY, duration REAL)",
                [],
            )
            .unwrap();
        connection
            .execute("INSERT INTO post(id, duration) VALUES (1, 10)", [])
            .unwrap();

        let transaction = connection.unchecked_transaction().unwrap();
        let registry = ActionRegistry::default();
        let operation = Operation::Transaction {
            commands: vec![TransactionCommand {
                command_id: "update-post".to_owned(),
                operation: Operation::Update {
                    model: "Post".to_owned(),
                    id: json!(1),
                    data: serde_json::Map::from_iter([("duration".to_owned(), json!(12.5))]),
                },
            }],
        };

        let result = registry.execute(&transaction, &operation).unwrap();
        assert_eq!(
            result,
            json!({
                "results": [{
                    "command_id": "update-post",
                    "success": true,
                    "data": null,
                    "error": null
                }]
            })
        );
        transaction.rollback().unwrap();
    }
}

pub fn error(code: &str, message: &str) -> RpcError {
    RpcError {
        code: code.to_owned(),
        message: message.to_owned(),
        retryable: false,
        outcome: "rolled_back",
    }
}

fn database_error(_error: rusqlite::Error) -> RpcError {
    error("database_error", "database operation failed")
}

#[derive(Clone, Copy)]
enum ColumnKind {
    Text,
    Integer,
    Numeric,
    Boolean,
    Json,
}

fn execute_model_update(
    transaction: &Transaction<'_>,
    model: &str,
    id: &Value,
    data: &serde_json::Map<String, Value>,
) -> Result<Value, RpcError> {
    let (table, fields) = model_spec(model).ok_or_else(|| {
        error(
            "unsupported_model",
            "writer model operation is not registered",
        )
    })?;
    let id = id
        .as_i64()
        .ok_or_else(|| error("invalid_id", "model id must be an integer"))?;
    if id == 0 {
        return Err(error("invalid_id", "model id must be nonzero"));
    }
    let exists = transaction
        .query_row(
            &format!("SELECT 1 FROM {table} WHERE id = ?1"),
            [id],
            |_| Ok(()),
        )
        .optional()
        .map_err(database_error)?
        .is_some();
    if !exists {
        return Err(error("not_found", "record not found"));
    }
    for (field, value) in data {
        let Some((column, kind)) = fields.iter().find(|(column, _)| *column == field) else {
            continue;
        };
        let value = sql_value(value, *kind)?;
        transaction
            .execute(
                &format!("UPDATE {table} SET {column} = ?1 WHERE id = ?2"),
                rusqlite::params![value, id],
            )
            .map_err(database_error)?;
    }
    Ok(Value::Null)
}

fn execute_model_delete(
    transaction: &Transaction<'_>,
    model: &str,
    id: &Value,
) -> Result<Value, RpcError> {
    let (table, _) = model_spec(model).ok_or_else(|| {
        error(
            "unsupported_model",
            "writer model operation is not registered",
        )
    })?;
    let id = id
        .as_i64()
        .ok_or_else(|| error("invalid_id", "model id must be an integer"))?;
    if id == 0 {
        return Err(error("invalid_id", "model id must be nonzero"));
    }
    // SQLAlchemy's legacy generic DELETE succeeds even if the primary key is
    // absent; intentionally ignore the affected-row count.
    transaction
        .execute(&format!("DELETE FROM {table} WHERE id = ?1"), [id])
        .map_err(database_error)?;
    Ok(Value::Null)
}

type ModelSpec = (&'static str, &'static [(&'static str, ColumnKind)]);

fn model_spec(model: &str) -> Option<ModelSpec> {
    const POST: &[(&str, ColumnKind)] = &[
        ("unprocessed_audio_path", ColumnKind::Text),
        ("processed_audio_path", ColumnKind::Text),
        ("duration", ColumnKind::Numeric),
        ("whitelisted", ColumnKind::Boolean),
        ("chapter_data", ColumnKind::Text),
        ("bleep_windows", ColumnKind::Json),
        ("transcript_word_timestamps", ColumnKind::Json),
        ("refined_ad_boundaries", ColumnKind::Json),
        ("refined_ad_boundaries_updated_at", ColumnKind::Text),
    ];
    const MODEL_CALL: &[(&str, ColumnKind)] = &[
        ("first_segment_sequence_num", ColumnKind::Integer),
        ("last_segment_sequence_num", ColumnKind::Integer),
        ("model_name", ColumnKind::Text),
        ("prompt", ColumnKind::Text),
        ("response", ColumnKind::Text),
        ("timestamp", ColumnKind::Text),
        ("status", ColumnKind::Text),
        ("error_message", ColumnKind::Text),
        ("retry_attempts", ColumnKind::Integer),
        ("next_retry_at", ColumnKind::Text),
        ("service_tier", ColumnKind::Text),
        ("prompt_tokens", ColumnKind::Integer),
        ("cached_prompt_tokens", ColumnKind::Integer),
        ("completion_tokens", ColumnKind::Integer),
        ("total_tokens", ColumnKind::Integer),
        ("estimated_cost_usd", ColumnKind::Numeric),
    ];
    const FEED: &[(&str, ColumnKind)] = &[
        ("ad_detection_strategy", ColumnKind::Text),
        ("chapter_filter_strings", ColumnKind::Text),
        ("enable_llm_chapter_fallback_tagging", ColumnKind::Boolean),
        ("chapter_full_block_text", ColumnKind::Boolean),
        ("auto_whitelist_new_episodes_override", ColumnKind::Boolean),
        ("enable_profanity_bleeping", ColumnKind::Boolean),
        ("confirm_whisperx_endpoint", ColumnKind::Boolean),
    ];
    match model {
        "Post" => Some(("post", POST)),
        "ModelCall" => Some(("model_call", MODEL_CALL)),
        "Feed" => Some(("feed", FEED)),
        _ => None,
    }
}

fn sql_value(value: &Value, kind: ColumnKind) -> Result<SqlValue, RpcError> {
    if value.is_null() && !matches!(kind, ColumnKind::Json) {
        return Ok(SqlValue::Null);
    }
    match kind {
        ColumnKind::Text => value
            .as_str()
            .map(|value| SqlValue::Text(value.to_owned()))
            .ok_or_else(|| error("invalid_field", "text field has invalid value")),
        ColumnKind::Integer => value
            .as_i64()
            .map(SqlValue::Integer)
            .ok_or_else(|| error("invalid_field", "integer field has invalid value")),
        ColumnKind::Numeric => value
            .as_i64()
            .map(SqlValue::Integer)
            .or_else(|| value.as_f64().map(SqlValue::Real))
            .ok_or_else(|| error("invalid_field", "numeric field has invalid value")),
        ColumnKind::Boolean => value
            .as_bool()
            .map(|value| SqlValue::Integer(i64::from(value)))
            .ok_or_else(|| error("invalid_field", "boolean field has invalid value")),
        ColumnKind::Json => serde_json::to_string(value)
            .map(SqlValue::Text)
            .map_err(|_| error("invalid_field", "JSON field has invalid value")),
    }
}
