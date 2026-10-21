use std::fs::File;
use std::path::{Path, PathBuf};

use chrono::Utc;
use rusqlite::types::Value as SqlValue;
use rusqlite::{OptionalExtension, Transaction};
use serde_json::{json, Map, Value};

use super::actions::{error, RpcActionResult};
use super::protocol::RpcError;

pub(super) const PROCESSOR_ACTIONS: &[&str] = &[
    "upsert_model_call",
    "delete_model_calls_for_post_by_model_name",
    "upsert_whisper_model_call",
    "replace_transcription",
    "start_transcription_replace",
    "insert_transcript_segments",
    "finish_transcription_replace",
    "finish_transcription_replace_from_artifact",
    "mark_model_call_failed",
    "insert_identifications",
    "replace_identifications",
    "replace_audio_segments",
];
const MAX_ARTIFACT_BYTES: u64 = 64 * 1024 * 1024;

pub fn is_processor_action(action: &str) -> bool {
    PROCESSOR_ACTIONS.contains(&action)
}

pub fn execute(
    transaction: &Transaction<'_>,
    action: &str,
    params: &Map<String, Value>,
) -> RpcActionResult {
    match action {
        "upsert_model_call" => upsert_model_call(transaction, params),
        "delete_model_calls_for_post_by_model_name" => {
            delete_model_calls_for_post_by_model_name(transaction, params)
        }
        "upsert_whisper_model_call" => upsert_whisper_model_call(transaction, params),
        "replace_transcription" => replace_transcription(transaction, params),
        "start_transcription_replace" => start_transcription_replace(transaction, params),
        "insert_transcript_segments" => insert_transcript_segments(transaction, params),
        "finish_transcription_replace" => finish_transcription_replace(transaction, params),
        "finish_transcription_replace_from_artifact" => {
            finish_transcription_replace_from_artifact(transaction, params)
        }
        "mark_model_call_failed" => mark_model_call_failed(transaction, params),
        "insert_identifications" => insert_identifications(transaction, params),
        "replace_identifications" => replace_identifications(transaction, params),
        "replace_audio_segments" => replace_audio_segments(transaction, params),
        _ => Err(error(
            "unsupported_action",
            "writer action is not registered",
        )),
    }
}

fn upsert_model_call(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let required = [
        "post_id",
        "model_name",
        "first_segment_sequence_num",
        "last_segment_sequence_num",
    ];
    if required
        .iter()
        .any(|field| params.get(*field).is_none_or(|value| value.is_null()))
    {
        return Err(error(
            "invalid_params",
            "post_id, model_name, first_segment_sequence_num, last_segment_sequence_num are required",
        ));
    }
    let prompt = params
        .get("prompt")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| error("invalid_params", "prompt is required"))?;
    let key = model_call_key(params, 0, -1)?;
    let id = match find_model_call(transaction, &key)? {
        Some(id) => id,
        None => insert_model_call(transaction, &key, prompt, "pending", 0, None, None)?,
    };
    let status: String = transaction
        .query_row("SELECT status FROM model_call WHERE id=?1", [id], |row| {
            row.get(0)
        })
        .map_err(database_error)?;
    if matches!(
        status.as_str(),
        "pending" | "retrying" | "failed_retries" | "failed_permanent" | "cancelled"
    ) {
        transaction
            .execute(
                "UPDATE model_call SET status='pending',prompt=?1,retry_attempts=0,
                    error_message=NULL,response=NULL WHERE id=?2",
                rusqlite::params![prompt, id],
            )
            .map_err(database_error)?;
    }
    Ok(json!({"model_call_id": id}))
}

fn delete_model_calls_for_post_by_model_name(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let post = params
        .get("post_id")
        .filter(|value| !value.is_null())
        .ok_or_else(|| error("invalid_params", "post_id and model_name are required"))?;
    let model = params
        .get("model_name")
        .filter(|value| truthy(value))
        .ok_or_else(|| error("invalid_params", "post_id and model_name are required"))?;
    let deleted = transaction
        .execute(
            "DELETE FROM model_call WHERE post_id=?1 AND model_name=?2",
            rusqlite::params![py_int(post)?, py_string(model)],
        )
        .map_err(database_error)?;
    Ok(json!({"deleted": deleted}))
}

fn upsert_whisper_model_call(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    if params.get("post_id").is_none_or(Value::is_null)
        || params.get("model_name").is_none_or(Value::is_null)
    {
        return Err(error(
            "invalid_params",
            "post_id and model_name are required",
        ));
    }
    let key = model_call_key(params, 0, -1)?;
    let prompt = params
        .get("prompt")
        .filter(|value| truthy(value))
        .map(py_string)
        .unwrap_or_else(|| "Whisper transcription job".to_owned());
    let default_reset = Map::from_iter([
        ("status".to_owned(), json!("pending")),
        ("prompt".to_owned(), json!("Whisper transcription job")),
        ("retry_attempts".to_owned(), json!(0)),
        ("error_message".to_owned(), Value::Null),
        ("response".to_owned(), Value::Null),
    ]);
    let reset = match params.get("reset_fields").filter(|value| truthy(value)) {
        None => &default_reset,
        Some(Value::Object(value)) => value,
        _ => return Err(error("invalid_params", "reset_fields must be an object")),
    };
    let initial_status = reset
        .get("status")
        .filter(|value| truthy(value))
        .map(py_string)
        .unwrap_or_else(|| "pending".to_owned());
    let retry_attempts = reset
        .get("retry_attempts")
        .filter(|value| truthy(value))
        .map(py_int)
        .transpose()?
        .unwrap_or(0);
    let error_message = reset.get("error_message").map(sql_scalar).transpose()?;
    let response = reset.get("response").map(sql_scalar).transpose()?;
    let id = match find_model_call(transaction, &key)? {
        Some(id) => id,
        None => insert_model_call(
            transaction,
            &key,
            &prompt,
            &initial_status,
            retry_attempts,
            error_message,
            response,
        )?,
    };
    for (field, value) in reset {
        if model_call_column(field).is_some() {
            update_model_call_column(transaction, id, field, value)?;
        }
    }
    Ok(json!({"model_call_id": id}))
}

fn replace_transcription(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let segments = params
        .get("segments")
        .and_then(Value::as_array)
        .ok_or_else(|| error("invalid_params", "segments must be a list"))?;
    let post_id = required_post_id(params)?;
    let model_call_id = params.get("model_call_id").cloned().unwrap_or(Value::Null);
    start_transcription_replace_inner(transaction, post_id, &model_call_id)?;
    let inserted = insert_transcript_segments_inner(transaction, post_id, segments)?;
    let normalized = normalize_word_timestamps(params.get("transcript_word_timestamps"))?;
    finish_transcription_replace_inner(
        transaction,
        post_id,
        &model_call_id,
        inserted as i64,
        normalized,
    )?;
    Ok(json!({"post_id": post_id, "segment_count": inserted}))
}

fn start_transcription_replace(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let post_id = required_post_id(params)?;
    let model_call_id = params.get("model_call_id").cloned().unwrap_or(Value::Null);
    let deleted = start_transcription_replace_inner(transaction, post_id, &model_call_id)?;
    Ok(json!({"post_id": post_id, "deleted_segments": deleted}))
}

fn start_transcription_replace_inner(
    transaction: &Transaction<'_>,
    post_id: i64,
    model_call_id: &Value,
) -> Result<usize, RpcError> {
    require_post(transaction, post_id)?;
    let ids = transaction
        .prepare("SELECT id FROM transcript_segment WHERE post_id=?1")
        .map_err(database_error)?
        .query_map([post_id], |row| row.get::<_, i64>(0))
        .map_err(database_error)?
        .collect::<rusqlite::Result<Vec<_>>>()
        .map_err(database_error)?;
    transaction
        .execute(
            "DELETE FROM identification WHERE transcript_segment_id IN (
                SELECT id FROM transcript_segment WHERE post_id=?1
             )",
            [post_id],
        )
        .map_err(database_error)?;
    transaction
        .execute("DELETE FROM transcript_segment WHERE post_id=?1", [post_id])
        .map_err(database_error)?;
    transaction
        .execute(
            "UPDATE post SET transcript_word_timestamps='null' WHERE id=?1",
            [post_id],
        )
        .map_err(database_error)?;
    if !model_call_id.is_null() {
        let id = py_int(model_call_id)?;
        transaction
            .execute(
                "UPDATE model_call SET first_segment_sequence_num=0,
                    last_segment_sequence_num=-1,response=NULL,status='pending',error_message=NULL
                 WHERE id=?1",
                [id],
            )
            .map_err(database_error)?;
    }
    Ok(ids.len())
}

fn insert_transcript_segments(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let post_id = required_post_id(params)?;
    let segments = params
        .get("segments")
        .and_then(Value::as_array)
        .ok_or_else(|| error("invalid_params", "segments must be a list"))?;
    require_post(transaction, post_id)?;
    let inserted = insert_transcript_segments_inner(transaction, post_id, segments)?;
    Ok(json!({"post_id": post_id, "inserted": inserted}))
}

fn insert_transcript_segments_inner(
    transaction: &Transaction<'_>,
    post_id: i64,
    segments: &[Value],
) -> Result<usize, RpcError> {
    let mut inserted = 0;
    for (index, segment) in segments.iter().enumerate() {
        let Some(segment) = segment.as_object() else {
            continue;
        };
        let sequence = segment
            .get("sequence_num")
            .map(py_int)
            .transpose()?
            .unwrap_or(index as i64);
        let start = py_float(
            segment
                .get("start_time")
                .ok_or_else(|| error("invalid_params", "start_time is required"))?,
        )?;
        let end = py_float(
            segment
                .get("end_time")
                .ok_or_else(|| error("invalid_params", "end_time is required"))?,
        )?;
        let text = py_string(
            segment
                .get("text")
                .ok_or_else(|| error("invalid_params", "text is required"))?,
        );
        let speaker = segment
            .get("speaker_label")
            .filter(|value| !value.is_null())
            .map(py_string);
        transaction
            .execute(
                "INSERT INTO transcript_segment(
                    post_id,sequence_num,start_time,end_time,text,speaker_label
                 ) VALUES (?1,?2,?3,?4,?5,?6)",
                rusqlite::params![post_id, sequence, start, end, text, speaker],
            )
            .map_err(database_error)?;
        inserted += 1;
    }
    Ok(inserted)
}

fn finish_transcription_replace(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let post_id = required_post_id(params)?;
    let model_call_id = params.get("model_call_id").cloned().unwrap_or(Value::Null);
    let count = params
        .get("segment_count")
        .filter(|value| truthy(value))
        .map(py_int)
        .transpose()?
        .unwrap_or(0);
    let normalized = normalize_word_timestamps(params.get("transcript_word_timestamps"))?;
    finish_transcription_replace_inner(transaction, post_id, &model_call_id, count, normalized)?;
    Ok(json!({"post_id": post_id, "segment_count": count}))
}

fn finish_transcription_replace_from_artifact(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let post_id = required_post_id(params)?;
    let artifact = params
        .get("artifact_path")
        .and_then(Value::as_str)
        .ok_or_else(|| error("invalid_params", "artifact_path is required"))?;
    let artifact = validate_artifact_path(Path::new(artifact))?;
    let artifact_size = artifact
        .metadata()
        .map_err(|_| {
            error(
                "invalid_params",
                "artifact_path must point to an existing file",
            )
        })?
        .len();
    if artifact_size > MAX_ARTIFACT_BYTES {
        return Err(error(
            "payload_too_large",
            "transcript artifact exceeds 64 MiB",
        ));
    }
    let file = File::open(&artifact).map_err(|_| {
        error(
            "invalid_params",
            "artifact_path must point to an existing file",
        )
    })?;
    let payload: Value = serde_json::from_reader(std::io::BufReader::new(file))
        .map_err(|_| error("invalid_params", "artifact is not valid JSON"))?;
    let normalized = normalize_word_timestamps(Some(&payload))?;
    let model_call_id = params.get("model_call_id").cloned().unwrap_or(Value::Null);
    let count = params
        .get("segment_count")
        .filter(|value| truthy(value))
        .map(py_int)
        .transpose()?
        .unwrap_or(0);
    finish_transcription_replace_inner(transaction, post_id, &model_call_id, count, normalized)?;
    Ok(json!({"post_id": post_id, "segment_count": count}))
}

fn finish_transcription_replace_inner(
    transaction: &Transaction<'_>,
    post_id: i64,
    model_call_id: &Value,
    count: i64,
    normalized: Option<Value>,
) -> Result<(), RpcError> {
    require_post(transaction, post_id)?;
    let value = serialize_word_timestamps(normalized)?;
    transaction
        .execute(
            "UPDATE post SET transcript_word_timestamps=?1 WHERE id=?2",
            rusqlite::params![value, post_id],
        )
        .map_err(database_error)?;
    if !model_call_id.is_null() {
        let id = py_int(model_call_id)?;
        transaction
            .execute(
                "UPDATE model_call SET first_segment_sequence_num=0,
                    last_segment_sequence_num=?1,response=?2,status='success',error_message=NULL
                 WHERE id=?3",
                rusqlite::params![count - 1, format!("{count} segments transcribed."), id],
            )
            .map_err(database_error)?;
    }
    Ok(())
}

fn serialize_word_timestamps(value: Option<Value>) -> Result<String, RpcError> {
    value
        .map(|value| serde_json::to_string(&value))
        .transpose()
        .map(|value| value.unwrap_or_else(|| "null".to_owned()))
        .map_err(|_| error("invalid_params", "invalid timestamp JSON"))
}

fn mark_model_call_failed(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let value = params
        .get("model_call_id")
        .filter(|value| !value.is_null())
        .ok_or_else(|| error("invalid_params", "model_call_id is required"))?;
    let id = py_int(value)?;
    let exists = transaction
        .query_row("SELECT 1 FROM model_call WHERE id=?1", [id], |_| Ok(()))
        .optional()
        .map_err(database_error)?
        .is_some();
    if !exists {
        return Ok(json!({"updated": false}));
    }
    let status = params
        .get("status")
        .map(py_string)
        .unwrap_or_else(|| "failed_permanent".to_owned());
    let message = params
        .get("error_message")
        .filter(|value| !value.is_null())
        .map(py_string);
    transaction
        .execute(
            "UPDATE model_call SET status=?1,error_message=?2 WHERE id=?3",
            rusqlite::params![status, message, id],
        )
        .map_err(database_error)?;
    Ok(json!({"updated": true, "model_call_id": id}))
}

fn insert_identifications(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let values = params
        .get("identifications")
        .and_then(Value::as_array)
        .ok_or_else(|| error("invalid_params", "identifications must be a list"))?;
    let inserted = insert_identifications_inner(transaction, values)?;
    Ok(json!({"inserted": inserted}))
}

fn insert_identifications_inner(
    transaction: &Transaction<'_>,
    values: &[Value],
) -> Result<usize, RpcError> {
    let mut inserted = 0;
    for value in values {
        let Some(value) = value.as_object() else {
            continue;
        };
        let segment = py_int(
            value
                .get("transcript_segment_id")
                .ok_or_else(|| error("invalid_params", "transcript_segment_id is required"))?,
        )?;
        let call = py_int(
            value
                .get("model_call_id")
                .ok_or_else(|| error("invalid_params", "model_call_id is required"))?,
        )?;
        let label = value
            .get("label")
            .filter(|value| truthy(value))
            .map(py_string)
            .unwrap_or_else(|| "ad".to_owned());
        let confidence = value
            .get("confidence")
            .map(sql_scalar)
            .transpose()?
            .unwrap_or(SqlValue::Null);
        inserted += transaction
            .execute(
                "INSERT OR IGNORE INTO identification(
                    transcript_segment_id,model_call_id,label,confidence
                 ) VALUES (?1,?2,?3,?4)",
                rusqlite::params![segment, call, label, confidence],
            )
            .map_err(database_error)?;
    }
    Ok(inserted)
}

fn replace_identifications(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let empty = Vec::new();
    let delete_ids = match params.get("delete_ids").filter(|value| truthy(value)) {
        None => &empty,
        Some(Value::Array(value)) => value,
        _ => {
            return Err(error(
                "invalid_params",
                "delete_ids and new_identifications must be lists",
            ))
        }
    };
    let new = match params
        .get("new_identifications")
        .filter(|value| truthy(value))
    {
        None => &empty,
        Some(Value::Array(value)) => value,
        _ => {
            return Err(error(
                "invalid_params",
                "delete_ids and new_identifications must be lists",
            ))
        }
    };
    for id in delete_ids {
        transaction
            .execute("DELETE FROM identification WHERE id=?1", [py_int(id)?])
            .map_err(database_error)?;
    }
    let inserted = insert_identifications_inner(transaction, new)?;
    Ok(json!({"deleted": delete_ids.len(), "inserted": inserted}))
}

fn replace_audio_segments(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let post_id = required_post_id(params)?;
    let segments = params
        .get("segments")
        .and_then(Value::as_array)
        .ok_or_else(|| error("invalid_params", "segments must be a list"))?;
    require_post(transaction, post_id)?;
    transaction
        .execute("DELETE FROM audio_segment WHERE post_id=?1", [post_id])
        .map_err(database_error)?;
    let model_call_id = params
        .get("model_call_id")
        .filter(|value| !value.is_null())
        .map(py_int)
        .transpose()?;
    let mut inserted = 0;
    for segment in segments {
        let Some(segment) = segment.as_object() else {
            continue;
        };
        let start = segment
            .get("start_time")
            .ok_or_else(|| {
                error(
                    "invalid_params",
                    "audio segments require numeric start_time and end_time",
                )
            })
            .and_then(py_float)?;
        let end = segment
            .get("end_time")
            .ok_or_else(|| {
                error(
                    "invalid_params",
                    "audio segments require numeric start_time and end_time",
                )
            })
            .and_then(py_float)?;
        if end <= start {
            continue;
        }
        let label = segment
            .get("label")
            .ok_or_else(|| error("invalid_params", "audio segment label is required"))?;
        transaction
            .execute(
                "INSERT INTO audio_segment(post_id,model_call_id,label,start_time,end_time)
                 VALUES (?1,?2,?3,?4,?5)",
                rusqlite::params![post_id, model_call_id, py_string(label), start, end],
            )
            .map_err(database_error)?;
        inserted += 1;
    }
    Ok(json!({"post_id": post_id, "segment_count": inserted}))
}

struct ModelCallKey {
    post_id: i64,
    model_name: String,
    first: i64,
    last: i64,
}

fn model_call_key(
    params: &Map<String, Value>,
    default_first: i64,
    default_last: i64,
) -> Result<ModelCallKey, RpcError> {
    Ok(ModelCallKey {
        post_id: py_int(params.get("post_id").expect("validated post_id"))?,
        model_name: py_string(params.get("model_name").expect("validated model_name")),
        first: params
            .get("first_segment_sequence_num")
            .map(py_int)
            .transpose()?
            .unwrap_or(default_first),
        last: params
            .get("last_segment_sequence_num")
            .map(py_int)
            .transpose()?
            .unwrap_or(default_last),
    })
}

fn find_model_call(
    transaction: &Transaction<'_>,
    key: &ModelCallKey,
) -> Result<Option<i64>, RpcError> {
    transaction
        .query_row(
            "SELECT id FROM model_call WHERE post_id=?1 AND model_name=?2
                AND first_segment_sequence_num=?3 AND last_segment_sequence_num=?4
             ORDER BY timestamp DESC LIMIT 1",
            rusqlite::params![key.post_id, key.model_name, key.first, key.last],
            |row| row.get(0),
        )
        .optional()
        .map_err(database_error)
}

#[allow(clippy::too_many_arguments)]
fn insert_model_call(
    transaction: &Transaction<'_>,
    key: &ModelCallKey,
    prompt: &str,
    status: &str,
    retries: i64,
    error_message: Option<SqlValue>,
    response: Option<SqlValue>,
) -> Result<i64, RpcError> {
    let inserted = transaction
        .execute(
            "INSERT OR IGNORE INTO model_call(
                post_id,first_segment_sequence_num,last_segment_sequence_num,model_name,
                prompt,response,timestamp,status,error_message,retry_attempts
             ) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10)",
            rusqlite::params![
                key.post_id,
                key.first,
                key.last,
                key.model_name,
                prompt,
                response.unwrap_or(SqlValue::Null),
                database_now(),
                status,
                error_message.unwrap_or(SqlValue::Null),
                retries
            ],
        )
        .map_err(database_error)?;
    if inserted > 0 {
        Ok(transaction.last_insert_rowid())
    } else {
        find_model_call(transaction, key)?
            .ok_or_else(|| error("database_error", "database operation failed"))
    }
}

fn model_call_column(field: &str) -> Option<()> {
    matches!(
        field,
        "post_id"
            | "first_segment_sequence_num"
            | "last_segment_sequence_num"
            | "model_name"
            | "prompt"
            | "response"
            | "timestamp"
            | "status"
            | "error_message"
            | "retry_attempts"
            | "next_retry_at"
            | "service_tier"
            | "prompt_tokens"
            | "cached_prompt_tokens"
            | "completion_tokens"
            | "total_tokens"
            | "estimated_cost_usd"
    )
    .then_some(())
}

fn update_model_call_column(
    transaction: &Transaction<'_>,
    id: i64,
    field: &str,
    value: &Value,
) -> Result<(), RpcError> {
    model_call_column(field).ok_or_else(|| error("internal_error", "invalid model-call field"))?;
    let value = sql_scalar(value)?;
    transaction
        .execute(
            &format!("UPDATE model_call SET {field}=?1 WHERE id=?2"),
            rusqlite::params![value, id],
        )
        .map_err(database_error)?;
    Ok(())
}

fn normalize_word_timestamps(payload: Option<&Value>) -> Result<Option<Value>, RpcError> {
    let Some(segments) = payload.and_then(Value::as_array) else {
        return Ok(None);
    };
    let mut normalized_segments = Vec::new();
    for segment in segments {
        let Some(segment) = segment.as_object() else {
            continue;
        };
        let Some(sequence) = segment.get("sequence_num") else {
            continue;
        };
        let Some(words) = segment.get("words").and_then(Value::as_array) else {
            continue;
        };
        let Ok(sequence) = py_int(sequence) else {
            continue;
        };
        let mut normalized_words = Vec::new();
        for word in words {
            let Some(word) = word.as_object() else {
                continue;
            };
            let (Some(raw_word), Some(start), Some(end)) =
                (word.get("word"), word.get("start"), word.get("end"))
            else {
                continue;
            };
            let (Ok(start), Ok(end)) = (py_float(start), py_float(end)) else {
                continue;
            };
            if end < start {
                continue;
            }
            let score = match word.get("score") {
                None | Some(Value::Null) => Value::Null,
                Some(value) => json!(py_float(value)?),
            };
            normalized_words.push(json!({
                "word": py_string(raw_word), "start": start, "end": end, "score": score
            }));
        }
        if !normalized_words.is_empty() {
            normalized_segments.push(json!({"sequence_num":sequence,"words":normalized_words}));
        }
    }
    Ok((!normalized_segments.is_empty()).then_some(Value::Array(normalized_segments)))
}

fn validate_artifact_path(path: &Path) -> Result<PathBuf, RpcError> {
    let expanded = if path.starts_with("~") {
        let home = std::env::var_os("HOME")
            .map(PathBuf::from)
            .ok_or_else(|| error("invalid_params", "artifact_path cannot expand home"))?;
        path.strip_prefix("~").map_or_else(
            |_| path.to_owned(),
            |suffix| home.join(suffix.strip_prefix("/").unwrap_or(suffix)),
        )
    } else {
        path.to_owned()
    };
    let resolved = expanded.canonicalize().map_err(|_| {
        error(
            "invalid_params",
            "artifact_path must point to an existing file",
        )
    })?;
    let instance = std::env::var_os("PODLY_INSTANCE_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/app/src/instance"));
    let data = std::env::var_os("PODLY_PODCAST_DATA_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| instance.join("data"));
    validate_artifact_path_with_roots(resolved, &[instance, data])
}

fn validate_artifact_path_with_roots(
    resolved: PathBuf,
    roots: &[PathBuf],
) -> Result<PathBuf, RpcError> {
    let allowed = roots
        .iter()
        .filter_map(|root| root.canonicalize().ok())
        .any(|root| resolved == root || resolved.starts_with(&root));
    if !allowed {
        return Err(error(
            "invalid_params",
            "artifact_path must be under the Podly instance data root",
        ));
    }
    if !resolved.is_file() {
        return Err(error(
            "invalid_params",
            "artifact_path must point to an existing file",
        ));
    }
    Ok(resolved)
}

fn required_post_id(params: &Map<String, Value>) -> Result<i64, RpcError> {
    params
        .get("post_id")
        .filter(|value| !value.is_null())
        .ok_or_else(|| error("invalid_params", "post_id is required"))
        .and_then(py_int)
}

fn require_post(transaction: &Transaction<'_>, post_id: i64) -> Result<(), RpcError> {
    let exists = transaction
        .query_row("SELECT 1 FROM post WHERE id=?1", [post_id], |_| Ok(()))
        .optional()
        .map_err(database_error)?
        .is_some();
    if exists {
        Ok(())
    } else {
        Err(error(
            "invalid_params",
            &format!("Post {post_id} not found"),
        ))
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

fn py_int(value: &Value) -> Result<i64, RpcError> {
    match value {
        Value::Bool(value) => Ok(i64::from(*value)),
        Value::Number(value) => value
            .as_i64()
            .or_else(|| value.as_u64().and_then(|value| i64::try_from(value).ok()))
            .or_else(|| value.as_f64().map(|value| value.trunc() as i64))
            .ok_or_else(|| error("invalid_params", "invalid integer value")),
        Value::String(value) => value
            .trim()
            .parse::<i64>()
            .map_err(|_| error("invalid_params", "invalid integer value")),
        _ => Err(error("invalid_params", "invalid integer value")),
    }
}

fn py_float(value: &Value) -> Result<f64, RpcError> {
    match value {
        Value::Bool(value) => Ok(if *value { 1.0 } else { 0.0 }),
        Value::Number(value) => value
            .as_f64()
            .ok_or_else(|| error("invalid_params", "invalid numeric value")),
        Value::String(value) => value
            .trim()
            .parse::<f64>()
            .map_err(|_| error("invalid_params", "invalid numeric value")),
        _ => Err(error("invalid_params", "invalid numeric value")),
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
    use std::fs;

    use rusqlite::Connection;
    use tempfile::TempDir;

    use super::*;

    fn schema(connection: &Connection) {
        connection
            .execute_batch(
                "CREATE TABLE post(
                    id INTEGER PRIMARY KEY,transcript_word_timestamps JSON
                 );
                 CREATE TABLE model_call(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,post_id INTEGER NOT NULL,
                    first_segment_sequence_num INTEGER NOT NULL,
                    last_segment_sequence_num INTEGER NOT NULL,model_name TEXT NOT NULL,
                    prompt TEXT NOT NULL,response TEXT,timestamp DATETIME NOT NULL,
                    status TEXT NOT NULL,error_message TEXT,retry_attempts INTEGER NOT NULL,
                    next_retry_at DATETIME,service_tier TEXT,prompt_tokens INTEGER,
                    cached_prompt_tokens INTEGER,completion_tokens INTEGER,total_tokens INTEGER,
                    estimated_cost_usd REAL,
                    UNIQUE(post_id,first_segment_sequence_num,last_segment_sequence_num,model_name)
                 );
                 CREATE TABLE transcript_segment(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,post_id INTEGER NOT NULL,
                    sequence_num INTEGER NOT NULL,start_time REAL NOT NULL,end_time REAL NOT NULL,
                    text TEXT NOT NULL,speaker_label TEXT,UNIQUE(post_id,sequence_num)
                 );
                 CREATE TABLE identification(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,transcript_segment_id INTEGER NOT NULL,
                    model_call_id INTEGER NOT NULL,confidence REAL,label TEXT NOT NULL,
                    UNIQUE(transcript_segment_id,model_call_id,label)
                 );
                 CREATE TABLE audio_segment(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,post_id INTEGER NOT NULL,
                    model_call_id INTEGER,label TEXT NOT NULL,start_time REAL NOT NULL,
                    end_time REAL NOT NULL
                 );
                 INSERT INTO post VALUES (1,NULL);",
            )
            .unwrap();
    }

    fn upsert_params(prompt: &str) -> Map<String, Value> {
        Map::from_iter([
            ("post_id".to_owned(), json!(1)),
            ("model_name".to_owned(), json!("model")),
            ("first_segment_sequence_num".to_owned(), json!(0)),
            ("last_segment_sequence_num".to_owned(), json!(4)),
            ("prompt".to_owned(), json!(prompt)),
        ])
    }

    #[test]
    fn transcription_actions_persist_python_json_null_representation() {
        let mut connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        let transaction = connection.transaction().unwrap();
        let post_params = Map::from_iter([("post_id".to_owned(), json!(1))]);

        start_transcription_replace(&transaction, &post_params).unwrap();
        let cleared: String = transaction
            .query_row(
                "SELECT transcript_word_timestamps FROM post WHERE id=1",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(cleared, "null");

        finish_transcription_replace(&transaction, &post_params).unwrap();
        let finished: String = transaction
            .query_row(
                "SELECT transcript_word_timestamps FROM post WHERE id=1",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(finished, "null");
        transaction.commit().unwrap();
    }

    #[test]
    fn model_call_upserts_reset_retryable_rows_but_reuse_success() {
        let mut connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        let transaction = connection.transaction().unwrap();
        let first = upsert_model_call(&transaction, &upsert_params("first")).unwrap();
        let id = first["model_call_id"].as_i64().unwrap();
        transaction
            .execute(
                "UPDATE model_call SET status='failed_permanent',response='old',
                    error_message='bad',retry_attempts=8 WHERE id=?1",
                [id],
            )
            .unwrap();
        assert_eq!(
            upsert_model_call(&transaction, &upsert_params("retry")).unwrap(),
            first
        );
        let reset: (String, String, Option<String>, Option<String>, i64) = transaction
            .query_row(
                "SELECT status,prompt,response,error_message,retry_attempts
                 FROM model_call WHERE id=?1",
                [id],
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
        assert_eq!(reset, ("pending".into(), "retry".into(), None, None, 0));
        transaction
            .execute(
                "UPDATE model_call SET status='success',response='done' WHERE id=?1",
                [id],
            )
            .unwrap();
        upsert_model_call(&transaction, &upsert_params("must-not-replace")).unwrap();
        let reusable: (String, String) = transaction
            .query_row(
                "SELECT prompt,response FROM model_call WHERE id=?1",
                [id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(reusable, ("retry".into(), "done".into()));
        transaction.commit().unwrap();
    }

    #[test]
    fn whisper_upsert_applies_custom_reset_fields_to_existing_row() {
        let mut connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        let transaction = connection.transaction().unwrap();
        let params = Map::from_iter([
            ("post_id".to_owned(), json!(1)),
            ("model_name".to_owned(), json!("whisper")),
            (
                "reset_fields".to_owned(),
                json!({"status":"retrying","prompt":"again","retry_attempts":3,
                       "service_tier":"batch","response":null}),
            ),
        ]);
        let id = upsert_whisper_model_call(&transaction, &params).unwrap()["model_call_id"]
            .as_i64()
            .unwrap();
        let row: (String, String, i64, String) = transaction
            .query_row(
                "SELECT status,prompt,retry_attempts,service_tier FROM model_call WHERE id=?1",
                [id],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .unwrap();
        assert_eq!(row, ("retrying".into(), "again".into(), 3, "batch".into()));
        transaction.commit().unwrap();
    }

    #[test]
    fn transcription_replace_preserves_order_precision_and_normalizes_words() {
        let mut connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        let transaction = connection.transaction().unwrap();
        let model_id = upsert_model_call(&transaction, &upsert_params("transcribe")).unwrap()
            ["model_call_id"]
            .as_i64()
            .unwrap();
        transaction
            .execute(
                "INSERT INTO transcript_segment(post_id,sequence_num,start_time,end_time,text)
                 VALUES (1,0,0,1,'old')",
                [],
            )
            .unwrap();
        let old_segment = transaction.last_insert_rowid();
        transaction
            .execute(
                "INSERT INTO identification(transcript_segment_id,model_call_id,label)
                 VALUES (?1,?2,'ad')",
                rusqlite::params![old_segment, model_id],
            )
            .unwrap();
        let result = replace_transcription(
            &transaction,
            &Map::from_iter([
                ("post_id".to_owned(), json!(1)),
                ("model_call_id".to_owned(), json!(model_id)),
                (
                    "segments".to_owned(),
                    json!([null,{"sequence_num":"7","start_time":0.123456789012345,
                                      "end_time":1.987654321098765,"text":"héllo",
                                      "speaker_label":42}]),
                ),
                (
                    "transcript_word_timestamps".to_owned(),
                    json!([
                        {"sequence_num":"7","words":[
                            {"word":"same","start":1.0,"end":1.0,"score":0.999999999},
                            {"word":"bad","start":2.0,"end":1.0}
                        ]},
                        {"sequence_num":null,"words":[]},
                        "skip"
                    ]),
                ),
            ]),
        )
        .unwrap();
        assert_eq!(result, json!({"post_id":1,"segment_count":1}));
        let segment: (i64, f64, f64, String, String) = transaction
            .query_row(
                "SELECT sequence_num,start_time,end_time,text,speaker_label
                 FROM transcript_segment",
                [],
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
        assert_eq!(segment.0, 7);
        assert!((segment.1 - 0.123456789012345).abs() < f64::EPSILON);
        assert!((segment.2 - 1.987654321098765).abs() < f64::EPSILON);
        assert_eq!((segment.3, segment.4), ("héllo".into(), "42".into()));
        let words: String = transaction
            .query_row(
                "SELECT transcript_word_timestamps FROM post WHERE id=1",
                [],
                |row| row.get(0),
            )
            .unwrap();
        let words: Value = serde_json::from_str(&words).unwrap();
        assert_eq!(words.as_array().unwrap().len(), 1);
        assert_eq!(words[0]["words"].as_array().unwrap().len(), 1);
        assert_eq!(
            transaction
                .query_row("SELECT COUNT(*) FROM identification", [], |row| row
                    .get::<_, i64>(0))
                .unwrap(),
            0
        );
        transaction.commit().unwrap();
    }

    #[test]
    fn malformed_audio_replacement_rolls_back_the_initial_delete() {
        let mut connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        connection
            .execute(
                "INSERT INTO audio_segment(post_id,label,start_time,end_time)
                 VALUES (1,'old',0,1)",
                [],
            )
            .unwrap();
        let transaction = connection.transaction().unwrap();
        let failure = replace_audio_segments(
            &transaction,
            &Map::from_iter([
                ("post_id".to_owned(), json!(1)),
                (
                    "segments".to_owned(),
                    json!([{"label":"new","start_time":0,"end_time":2},
                           {"label":"bad","start_time":"oops","end_time":3}]),
                ),
            ]),
        );
        assert!(failure.is_err());
        transaction.rollback().unwrap();
        let row: (String, f64, f64) = connection
            .query_row(
                "SELECT label,start_time,end_time FROM audio_segment",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        assert_eq!(row, ("old".into(), 0.0, 1.0));
    }

    #[test]
    fn identification_retries_ignore_duplicates_and_report_requested_deletes() {
        let mut connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        let transaction = connection.transaction().unwrap();
        transaction
            .execute(
                "INSERT INTO transcript_segment(post_id,sequence_num,start_time,end_time,text)
                 VALUES (1,0,0,1,'text')",
                [],
            )
            .unwrap();
        let segment = transaction.last_insert_rowid();
        let call = upsert_model_call(&transaction, &upsert_params("identify")).unwrap()
            ["model_call_id"]
            .as_i64()
            .unwrap();
        let rows = json!([{"transcript_segment_id":segment,"model_call_id":call,
                           "label":null,"confidence":0.876543210987654}]);
        let params = Map::from_iter([("identifications".to_owned(), rows.clone())]);
        assert_eq!(
            insert_identifications(&transaction, &params).unwrap()["inserted"],
            1
        );
        assert_eq!(
            insert_identifications(&transaction, &params).unwrap()["inserted"],
            0
        );
        let replaced = replace_identifications(
            &transaction,
            &Map::from_iter([
                ("delete_ids".to_owned(), json!([999, 999])),
                ("new_identifications".to_owned(), rows),
            ]),
        )
        .unwrap();
        assert_eq!(replaced, json!({"deleted":2,"inserted":0}));
        let confidence: f64 = transaction
            .query_row("SELECT confidence FROM identification", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert!((confidence - 0.876543210987654).abs() < f64::EPSILON);
        transaction.commit().unwrap();
    }

    #[test]
    fn large_transcript_batch_is_complete_and_ordered() {
        let mut connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        let segments = (0..10_000)
            .map(|index| {
                json!({"sequence_num":index,"start_time":index as f64 / 10.0,
                       "end_time":index as f64 / 10.0 + 0.05,"text":format!("word {index}")})
            })
            .collect::<Vec<_>>();
        let transaction = connection.transaction().unwrap();
        let result = insert_transcript_segments(
            &transaction,
            &Map::from_iter([
                ("post_id".to_owned(), json!(1)),
                ("segments".to_owned(), Value::Array(segments)),
            ]),
        )
        .unwrap();
        assert_eq!(result["inserted"], 10_000);
        let row: (i64, i64, String) = transaction
            .query_row(
                "SELECT COUNT(*),MAX(sequence_num),
                        (SELECT text FROM transcript_segment WHERE sequence_num=9999)
                 FROM transcript_segment",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        assert_eq!(row, (10_000, 9_999, "word 9999".into()));
        transaction.commit().unwrap();
    }

    #[test]
    fn artifact_validation_rejects_escape_and_stream_normalizer_preserves_original() {
        let root = TempDir::new().unwrap();
        let outside = TempDir::new().unwrap();
        let artifact = root.path().join("words.json");
        let payload = json!([{"sequence_num":0,"words":[
            {"word":"ok","start":0.0,"end":0.5,"score":0.9}
        ]}]);
        fs::write(&artifact, serde_json::to_vec(&payload).unwrap()).unwrap();
        let resolved = artifact.canonicalize().unwrap();
        assert_eq!(
            validate_artifact_path_with_roots(resolved.clone(), &[root.path().to_owned()]).unwrap(),
            resolved
        );
        assert!(
            validate_artifact_path_with_roots(resolved.clone(), &[outside.path().to_owned()])
                .is_err()
        );
        let parsed: Value =
            serde_json::from_reader(std::io::BufReader::new(File::open(&resolved).unwrap()))
                .unwrap();
        assert_eq!(
            normalize_word_timestamps(Some(&parsed)).unwrap(),
            Some(payload.clone())
        );
        assert_eq!(
            serde_json::from_slice::<Value>(&fs::read(&artifact).unwrap()).unwrap(),
            payload
        );
    }
}
