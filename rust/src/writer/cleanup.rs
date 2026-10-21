use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};

use chrono::Utc;
use regex::Regex;
use rusqlite::{OptionalExtension, Transaction};
use serde_json::{json, Map, Value};

use super::actions::{error, RpcActionResult};
use super::feeds::recalculate_run_counts;
use super::protocol::RpcError;

pub(super) const CLEANUP_ACTIONS: &[&str] = &[
    "cleanup_missing_audio_paths",
    "clear_post_processing_data",
    "clear_post_processing_data_keep_transcript",
    "prepare_post_for_auto_retry",
    "cleanup_processed_post",
    "cleanup_processed_post_files_only",
];

// SQLAlchemy JSON defaults to none_as_null=False, so assigning Python None stores JSON text `null`.
const SQLALCHEMY_JSON_NULL: &str = "null";

pub fn is_cleanup_action(action: &str) -> bool {
    CLEANUP_ACTIONS.contains(&action)
}

pub fn execute(
    transaction: &Transaction<'_>,
    action: &str,
    params: &Map<String, Value>,
) -> RpcActionResult {
    match action {
        "cleanup_missing_audio_paths" => cleanup_missing_audio_paths(transaction),
        "clear_post_processing_data" => clear_post_processing_data(transaction, params),
        "clear_post_processing_data_keep_transcript" => {
            clear_post_processing_data_keep_transcript(transaction, params)
        }
        "prepare_post_for_auto_retry" => prepare_post_for_auto_retry(transaction, params),
        "cleanup_processed_post" => cleanup_processed_post(transaction, params),
        "cleanup_processed_post_files_only" => {
            cleanup_processed_post_files_only(transaction, params)
        }
        _ => Err(error(
            "unsupported_action",
            "writer action is not registered",
        )),
    }
}

#[derive(Debug)]
struct CleanupPost {
    id: i64,
    guid: String,
    title: String,
    feed_title: Option<String>,
    unprocessed: Option<String>,
    processed: Option<String>,
}

fn cleanup_missing_audio_paths(transaction: &Transaction<'_>) -> RpcActionResult {
    let posts = transaction
        .prepare(
            "SELECT post.id,post.guid,post.title,feed.title,
                    post.unprocessed_audio_path,post.processed_audio_path
             FROM post LEFT JOIN feed ON feed.id=post.feed_id
             WHERE post.whitelisted AND (
                post.unprocessed_audio_path IS NOT NULL OR post.processed_audio_path IS NOT NULL
             )",
        )
        .map_err(database_error)?
        .query_map([], |row| {
            Ok(CleanupPost {
                id: row.get(0)?,
                guid: row.get(1)?,
                title: row.get(2)?,
                feed_title: row.get(3)?,
                unprocessed: row.get(4)?,
                processed: row.get(5)?,
            })
        })
        .map_err(database_error)?
        .collect::<rusqlite::Result<Vec<_>>>()
        .map_err(database_error)?;
    let mut count = 0;
    for post in posts {
        let existing = find_existing_processed_audio(&post);
        let mut changed = false;
        match (post.processed.as_deref(), existing.as_ref()) {
            (current, Some(existing)) if current != Some(existing.to_string_lossy().as_ref()) => {
                transaction
                    .execute(
                        "UPDATE post SET processed_audio_path=?1 WHERE id=?2",
                        rusqlite::params![existing.to_string_lossy(), post.id],
                    )
                    .map_err(database_error)?;
                changed = true;
            }
            (Some(_), None) => {
                transaction
                    .execute(
                        "UPDATE post SET processed_audio_path=NULL WHERE id=?1",
                        [post.id],
                    )
                    .map_err(database_error)?;
                changed = true;
            }
            _ => {}
        }
        if post
            .unprocessed
            .as_ref()
            .is_some_and(|path| !Path::new(path).exists())
        {
            transaction
                .execute(
                    "UPDATE post SET unprocessed_audio_path=NULL WHERE id=?1",
                    [post.id],
                )
                .map_err(database_error)?;
            changed = true;
        }
        if changed {
            reset_latest_terminal_job(transaction, &post.guid)?;
            count += 1;
        }
    }
    Ok(json!(count))
}

fn reset_latest_terminal_job(
    transaction: &Transaction<'_>,
    post_guid: &str,
) -> Result<(), RpcError> {
    let latest = transaction
        .query_row(
            "SELECT id,status FROM processing_job WHERE post_guid=?1
             ORDER BY created_at DESC NULLS LAST LIMIT 1",
            [post_guid],
            |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
        )
        .optional()
        .map_err(database_error)?;
    let Some((id, status)) = latest else {
        return Ok(());
    };
    if matches!(status.as_str(), "pending" | "running") {
        return Ok(());
    }
    let now = Utc::now().naive_utc();
    let history = serde_json::to_string(&json!([{
        "step":0,"step_name":"Not started",
        "started_at":now.format("%Y-%m-%dT%H:%M:%S%.6f").to_string()
    }]))
    .map_err(|_| error("internal_error", "stage history serialization failed"))?;
    transaction
        .execute(
            "UPDATE processing_job SET status='pending',current_step=0,
                progress_percentage=0.0,step_name='Not started',error_message=NULL,
                started_at=NULL,completed_at=NULL,stage_history=?1 WHERE id=?2",
            rusqlite::params![history, id],
        )
        .map_err(database_error)?;
    Ok(())
}

fn clear_post_processing_data(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let post_id = lookup_post_id(transaction, params, false)?;
    clear_post_processing_data_inner(transaction, post_id)?;
    Ok(json!({"post_id": post_id}))
}

fn clear_post_processing_data_inner(
    transaction: &Transaction<'_>,
    post_id: i64,
) -> Result<(), RpcError> {
    let guid = post_guid(transaction, post_id)?;
    delete_identifications_for_post(transaction, post_id)?;
    transaction
        .execute("DELETE FROM transcript_segment WHERE post_id=?1", [post_id])
        .map_err(database_error)?;
    transaction
        .execute("DELETE FROM audio_segment WHERE post_id=?1", [post_id])
        .map_err(database_error)?;
    transaction
        .execute("DELETE FROM model_call WHERE post_id=?1", [post_id])
        .map_err(database_error)?;
    transaction
        .execute("DELETE FROM processing_job WHERE post_guid=?1", [guid])
        .map_err(database_error)?;
    transaction
        .execute(
            "UPDATE post SET unprocessed_audio_path=NULL,processed_audio_path=NULL,
                duration=NULL,chapter_data=NULL,bleep_windows=?1,
                transcript_word_timestamps=?1,refined_ad_boundaries=?1,
                refined_ad_boundaries_updated_at=NULL WHERE id=?2",
            rusqlite::params![SQLALCHEMY_JSON_NULL, post_id],
        )
        .map_err(database_error)?;
    Ok(())
}

fn clear_post_processing_data_keep_transcript(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let post_id = lookup_post_id(transaction, params, false)?;
    let guid = post_guid(transaction, post_id)?;
    delete_identifications_for_post(transaction, post_id)?;
    transaction
        .execute("DELETE FROM audio_segment WHERE post_id=?1", [post_id])
        .map_err(database_error)?;
    delete_non_whisper_model_calls(transaction, post_id)?;
    transaction
        .execute("DELETE FROM processing_job WHERE post_guid=?1", [guid])
        .map_err(database_error)?;
    transaction
        .execute(
            "UPDATE post SET unprocessed_audio_path=NULL,processed_audio_path=NULL,
                duration=NULL,chapter_data=NULL,bleep_windows=?1,
                refined_ad_boundaries=?1,refined_ad_boundaries_updated_at=NULL WHERE id=?2",
            rusqlite::params![SQLALCHEMY_JSON_NULL, post_id],
        )
        .map_err(database_error)?;
    Ok(json!({"post_id": post_id}))
}

fn prepare_post_for_auto_retry(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let post_id = lookup_post_id(transaction, params, false)?;
    let post = load_cleanup_post(transaction, post_id)?;
    for candidate in processed_audio_candidates(&post) {
        if !candidate.exists() || !candidate.is_file() {
            continue;
        }
        let _ = fs::remove_file(candidate);
    }
    delete_identifications_for_post(transaction, post_id)?;
    delete_non_whisper_model_calls(transaction, post_id)?;
    transaction
        .execute(
            "UPDATE post SET processed_audio_path=NULL,duration=NULL,chapter_data=NULL,
                bleep_windows=?1,refined_ad_boundaries=?1,
                refined_ad_boundaries_updated_at=NULL WHERE id=?2",
            rusqlite::params![SQLALCHEMY_JSON_NULL, post_id],
        )
        .map_err(database_error)?;
    Ok(json!({"post_id": post_id}))
}

fn cleanup_processed_post(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let post_id = lookup_post_id(transaction, params, true)?;
    clear_post_processing_data_inner(transaction, post_id)?;
    transaction
        .execute("UPDATE post SET whitelisted=0 WHERE id=?1", [post_id])
        .map_err(database_error)?;
    recalculate_run_counts(transaction)?;
    Ok(json!({"post_id": post_id}))
}

fn cleanup_processed_post_files_only(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let post_id = lookup_post_id(transaction, params, true)?;
    let paths = transaction
        .query_row(
            "SELECT unprocessed_audio_path,processed_audio_path FROM post WHERE id=?1",
            [post_id],
            |row| {
                Ok((
                    row.get::<_, Option<String>>(0)?,
                    row.get::<_, Option<String>>(1)?,
                ))
            },
        )
        .map_err(database_error)?;
    for path in [paths.0, paths.1].into_iter().flatten() {
        let path = Path::new(&path);
        if path.exists() {
            let _ = fs::remove_file(path);
        }
    }
    transaction
        .execute(
            "UPDATE post SET unprocessed_audio_path=NULL,processed_audio_path=NULL,
                whitelisted=0 WHERE id=?1",
            [post_id],
        )
        .map_err(database_error)?;
    Ok(json!({"post_id": post_id}))
}

fn delete_identifications_for_post(
    transaction: &Transaction<'_>,
    post_id: i64,
) -> Result<(), RpcError> {
    transaction
        .execute(
            "DELETE FROM identification WHERE transcript_segment_id IN (
                SELECT id FROM transcript_segment WHERE post_id=?1
             )",
            [post_id],
        )
        .map_err(database_error)?;
    Ok(())
}

fn delete_non_whisper_model_calls(
    transaction: &Transaction<'_>,
    post_id: i64,
) -> Result<(), RpcError> {
    transaction
        .execute(
            "DELETE FROM model_call WHERE post_id=?1 AND NOT (
                prompt='Whisper transcription job' OR model_name LIKE '%whisper%'
                OR model_name LIKE 'local_%'
             )",
            [post_id],
        )
        .map_err(database_error)?;
    Ok(())
}

fn lookup_post_id(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
    require_truthy: bool,
) -> Result<i64, RpcError> {
    let value = params.get("post_id").cloned().unwrap_or(Value::Null);
    if require_truthy && !truthy(&value) {
        return Err(error("invalid_params", "post_id is required"));
    }
    let database_value = json_scalar(&value)?;
    let row = transaction
        .query_row("SELECT id FROM post WHERE id=?1", [database_value], |row| {
            row.get::<_, i64>(0)
        })
        .optional()
        .map_err(database_error)?;
    row.ok_or_else(|| {
        error(
            "invalid_params",
            &format!("Post {} not found", py_string(&value)),
        )
    })
}

fn post_guid(transaction: &Transaction<'_>, post_id: i64) -> Result<String, RpcError> {
    transaction
        .query_row("SELECT guid FROM post WHERE id=?1", [post_id], |row| {
            row.get(0)
        })
        .map_err(database_error)
}

fn load_cleanup_post(transaction: &Transaction<'_>, post_id: i64) -> Result<CleanupPost, RpcError> {
    transaction
        .query_row(
            "SELECT post.id,post.guid,post.title,feed.title,
                    post.unprocessed_audio_path,post.processed_audio_path
             FROM post LEFT JOIN feed ON feed.id=post.feed_id WHERE post.id=?1",
            [post_id],
            |row| {
                Ok(CleanupPost {
                    id: row.get(0)?,
                    guid: row.get(1)?,
                    title: row.get(2)?,
                    feed_title: row.get(3)?,
                    unprocessed: row.get(4)?,
                    processed: row.get(5)?,
                })
            },
        )
        .map_err(database_error)
}

fn find_existing_processed_audio(post: &CleanupPost) -> Option<PathBuf> {
    processed_audio_candidates(post).into_iter().find(|path| {
        path.metadata()
            .is_ok_and(|metadata| metadata.is_file() && metadata.len() > 0)
    })
}

fn processed_audio_candidates(post: &CleanupPost) -> Vec<PathBuf> {
    let mut candidates = Vec::new();
    if let Some(path) = &post.processed {
        candidates.push(PathBuf::from(path));
    }
    if let (Some(unprocessed), Some(feed)) = (&post.unprocessed, &post.feed_title) {
        if let Some(name) = Path::new(unprocessed).file_name() {
            candidates.push(
                podcast_data_root()
                    .join("srv")
                    .join(sanitize_modern(feed))
                    .join(name),
            );
        }
    }
    if let Some(feed) = &post.feed_title {
        let title = sanitize_legacy(&post.title);
        if !title.is_empty() {
            candidates.push(
                podcast_data_root()
                    .join("srv")
                    .join(sanitize_legacy(feed))
                    .join(format!("{title}.mp3")),
            );
            candidates.push(
                podcast_data_root()
                    .join("srv")
                    .join(sanitize_modern(feed))
                    .join(format!("{title}.mp3")),
            );
        }
    }
    let mut seen = HashSet::new();
    candidates
        .into_iter()
        .filter_map(|path| {
            let resolved = fs::canonicalize(&path).unwrap_or(path);
            seen.insert(resolved.clone()).then_some(resolved)
        })
        .collect()
}

fn sanitize_legacy(value: &str) -> String {
    Regex::new(r"[^a-zA-Z0-9\s]")
        .expect("static regex")
        .replace_all(value, "")
        .into_owned()
}

fn sanitize_modern(value: &str) -> String {
    let allowed = Regex::new(r"[^a-zA-Z0-9\s_.-]")
        .expect("static regex")
        .replace_all(value, "")
        .trim()
        .trim_end_matches('.')
        .to_owned();
    Regex::new(r"\s+")
        .expect("static regex")
        .replace_all(&allowed, "_")
        .into_owned()
}

fn podcast_data_root() -> PathBuf {
    std::env::var_os("PODLY_PODCAST_DATA_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            std::env::var_os("PODLY_INSTANCE_DIR")
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from("/app/src/instance"))
                .join("data")
        })
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

fn json_scalar(value: &Value) -> Result<rusqlite::types::Value, RpcError> {
    match value {
        Value::Null => Ok(rusqlite::types::Value::Null),
        Value::Bool(value) => Ok(rusqlite::types::Value::Integer(i64::from(*value))),
        Value::Number(value) if value.is_i64() => {
            Ok(rusqlite::types::Value::Integer(value.as_i64().unwrap()))
        }
        Value::Number(value) => value
            .as_f64()
            .map(rusqlite::types::Value::Real)
            .ok_or_else(|| error("invalid_params", "invalid post_id")),
        Value::String(value) => Ok(rusqlite::types::Value::Text(value.clone())),
        _ => Err(error("invalid_params", "invalid post_id")),
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
                "CREATE TABLE feed(id INTEGER PRIMARY KEY,title TEXT);
                 CREATE TABLE post(
                    id INTEGER PRIMARY KEY,feed_id INTEGER,guid TEXT NOT NULL,title TEXT NOT NULL,
                    whitelisted BOOLEAN NOT NULL,unprocessed_audio_path TEXT,
                    processed_audio_path TEXT,duration INTEGER,chapter_data TEXT,
                    bleep_windows JSON,transcript_word_timestamps JSON,
                    refined_ad_boundaries JSON,refined_ad_boundaries_updated_at DATETIME
                 );
                 CREATE TABLE transcript_segment(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,post_id INTEGER NOT NULL
                 );
                 CREATE TABLE identification(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,transcript_segment_id INTEGER NOT NULL
                 );
                 CREATE TABLE audio_segment(id INTEGER PRIMARY KEY AUTOINCREMENT,post_id INTEGER NOT NULL);
                 CREATE TABLE model_call(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,post_id INTEGER NOT NULL,
                    model_name TEXT NOT NULL,prompt TEXT NOT NULL
                 );
                 CREATE TABLE processing_job(
                    id TEXT PRIMARY KEY,jobs_manager_run_id TEXT,post_guid TEXT NOT NULL,
                    status TEXT NOT NULL,current_step INTEGER,step_name TEXT,
                    progress_percentage REAL,started_at DATETIME,completed_at DATETIME,
                    error_message TEXT,created_at DATETIME,stage_history JSON
                 );
                 CREATE TABLE jobs_manager_run(
                    id TEXT PRIMARY KEY,status TEXT NOT NULL,trigger TEXT NOT NULL,
                    started_at DATETIME,completed_at DATETIME,total_jobs INTEGER NOT NULL,
                    queued_jobs INTEGER NOT NULL,running_jobs INTEGER NOT NULL,
                    completed_jobs INTEGER NOT NULL,failed_jobs INTEGER NOT NULL,
                    skipped_jobs INTEGER NOT NULL,context_json JSON,counters_reset_at DATETIME,
                    created_at DATETIME,updated_at DATETIME
                 );
                 INSERT INTO feed VALUES (1,'A Feed');
                 INSERT INTO post VALUES (
                    1,1,'guid','A Post',1,'/missing/input.mp3','/missing/output.mp3',
                    42,'chapters','[]','[]','[]','2026-01-01'
                 );",
            )
            .unwrap();
    }

    fn seed_graph(connection: &Connection, segments: usize) {
        for index in 0..segments {
            connection
                .execute("INSERT INTO transcript_segment(post_id) VALUES (1)", [])
                .unwrap();
            connection
                .execute(
                    "INSERT INTO identification(transcript_segment_id) VALUES (?1)",
                    [connection.last_insert_rowid()],
                )
                .unwrap();
            if index == 0 {
                connection
                    .execute("INSERT INTO audio_segment(post_id) VALUES (1)", [])
                    .unwrap();
            }
        }
        for (model, prompt) in [
            ("gpt", "classify"),
            ("openai-whisper", "other"),
            ("local_large", "other"),
            ("other", "Whisper transcription job"),
        ] {
            connection
                .execute(
                    "INSERT INTO model_call(post_id,model_name,prompt) VALUES (1,?1,?2)",
                    rusqlite::params![model, prompt],
                )
                .unwrap();
        }
        connection
            .execute(
                "INSERT INTO processing_job VALUES (
                    'job',NULL,'guid','completed',4,'Done',100,NULL,'2026-01-01',NULL,
                    '2026-01-01','[]')",
                [],
            )
            .unwrap();
    }

    fn assert_json_null_columns(connection: &Connection, columns: &[&str]) {
        for column in columns {
            let (storage_type, value): (String, String) = connection
                .query_row(
                    &format!("SELECT typeof(\"{column}\"),\"{column}\" FROM post WHERE id=1"),
                    [],
                    |row| Ok((row.get(0)?, row.get(1)?)),
                )
                .unwrap();
            assert_eq!(
                (storage_type.as_str(), value.as_str()),
                ("text", "null"),
                "{column}"
            );
        }
    }

    #[test]
    fn missing_path_scan_clears_paths_and_requeues_latest_terminal_job() {
        let temp = TempDir::new().unwrap();
        let existing = temp.path().join("existing.mp3");
        fs::write(&existing, b"audio").unwrap();
        let mut connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        connection
            .execute(
                "UPDATE post SET processed_audio_path=?1 WHERE id=1",
                [existing.to_string_lossy().into_owned()],
            )
            .unwrap();
        seed_graph(&connection, 1);
        let transaction = connection.transaction().unwrap();
        assert_eq!(cleanup_missing_audio_paths(&transaction).unwrap(), json!(1));
        transaction.commit().unwrap();
        let post: (Option<String>, String) = connection
            .query_row(
                "SELECT unprocessed_audio_path,processed_audio_path FROM post WHERE id=1",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(post.0, None);
        assert_eq!(post.1, existing.canonicalize().unwrap().to_string_lossy());
        let job: (
            String,
            i64,
            f64,
            String,
            Option<String>,
            Option<String>,
            String,
        ) = connection
            .query_row(
                "SELECT status,current_step,progress_percentage,step_name,
                        started_at,completed_at,stage_history FROM processing_job",
                [],
                |row| {
                    Ok((
                        row.get(0)?,
                        row.get(1)?,
                        row.get(2)?,
                        row.get(3)?,
                        row.get(4)?,
                        row.get(5)?,
                        row.get(6)?,
                    ))
                },
            )
            .unwrap();
        assert_eq!(job.0, "pending");
        assert_eq!(job.1, 0);
        assert_eq!(job.2, 0.0);
        assert_eq!(job.3, "Not started");
        assert_eq!(job.4, None);
        assert_eq!(job.5, None);
        let history: Value = serde_json::from_str(&job.6).unwrap();
        assert_eq!(history[0]["step_name"], "Not started");
    }

    #[test]
    fn clear_all_handles_more_than_one_batch_and_removes_entire_graph() {
        let mut connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        seed_graph(&connection, 501);
        let transaction = connection.transaction().unwrap();
        assert_eq!(
            clear_post_processing_data(
                &transaction,
                &Map::from_iter([("post_id".to_owned(), json!(1))]),
            )
            .unwrap(),
            json!({"post_id":1})
        );
        transaction.commit().unwrap();
        for table in [
            "transcript_segment",
            "identification",
            "audio_segment",
            "model_call",
            "processing_job",
        ] {
            let count: i64 = connection
                .query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |row| {
                    row.get(0)
                })
                .unwrap();
            assert_eq!(count, 0, "{table}");
        }
        let row: (Option<String>, Option<i64>, Option<String>) = connection
            .query_row(
                "SELECT processed_audio_path,duration,transcript_word_timestamps FROM post",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        assert_eq!(row, (None, None, Some("null".to_owned())));
        assert_json_null_columns(
            &connection,
            &[
                "bleep_windows",
                "transcript_word_timestamps",
                "refined_ad_boundaries",
            ],
        );
        let chapter_data_is_sql_null: bool = connection
            .query_row("SELECT chapter_data IS NULL FROM post", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert!(chapter_data_is_sql_null);
    }

    #[test]
    fn keep_transcript_preserves_segments_words_and_all_whisper_variants() {
        let mut connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        seed_graph(&connection, 3);
        let transaction = connection.transaction().unwrap();
        clear_post_processing_data_keep_transcript(
            &transaction,
            &Map::from_iter([("post_id".to_owned(), json!(1))]),
        )
        .unwrap();
        transaction.commit().unwrap();
        assert_eq!(
            connection
                .query_row("SELECT COUNT(*) FROM transcript_segment", [], |row| row
                    .get::<_, i64>(0))
                .unwrap(),
            3
        );
        assert_eq!(
            connection
                .query_row("SELECT COUNT(*) FROM identification", [], |row| row
                    .get::<_, i64>(0))
                .unwrap(),
            0
        );
        let models = connection
            .prepare("SELECT model_name FROM model_call ORDER BY id")
            .unwrap()
            .query_map([], |row| row.get::<_, String>(0))
            .unwrap()
            .collect::<rusqlite::Result<Vec<_>>>()
            .unwrap();
        assert_eq!(models, ["openai-whisper", "local_large", "other"]);
        let words: String = connection
            .query_row("SELECT transcript_word_timestamps FROM post", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(words, "[]");
        assert_json_null_columns(&connection, &["bleep_windows", "refined_ad_boundaries"]);
    }

    #[test]
    fn auto_retry_file_delete_survives_database_rollback_but_db_state_does_not() {
        let temp = TempDir::new().unwrap();
        let processed = temp.path().join("processed.mp3");
        let unprocessed = temp.path().join("input.mp3");
        fs::write(&processed, b"processed").unwrap();
        fs::write(&unprocessed, b"input").unwrap();
        let mut connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        connection
            .execute(
                "UPDATE post SET processed_audio_path=?1,unprocessed_audio_path=?2 WHERE id=1",
                rusqlite::params![
                    processed.to_string_lossy().into_owned(),
                    unprocessed.to_string_lossy().into_owned()
                ],
            )
            .unwrap();
        seed_graph(&connection, 2);
        let transaction = connection.transaction().unwrap();
        prepare_post_for_auto_retry(
            &transaction,
            &Map::from_iter([("post_id".to_owned(), json!(1))]),
        )
        .unwrap();
        transaction.rollback().unwrap();
        assert!(!processed.exists());
        assert!(unprocessed.exists());
        let stored: String = connection
            .query_row("SELECT processed_audio_path FROM post", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(stored, processed.to_string_lossy());
        assert_eq!(
            connection
                .query_row("SELECT COUNT(*) FROM identification", [], |row| row
                    .get::<_, i64>(0))
                .unwrap(),
            2
        );
    }

    #[test]
    fn auto_retry_clears_json_columns_to_json_null() {
        let mut connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        seed_graph(&connection, 1);
        let transaction = connection.transaction().unwrap();
        prepare_post_for_auto_retry(
            &transaction,
            &Map::from_iter([("post_id".to_owned(), json!(1))]),
        )
        .unwrap();
        transaction.commit().unwrap();
        assert_json_null_columns(&connection, &["bleep_windows", "refined_ad_boundaries"]);
        let words_are_preserved: bool = connection
            .query_row(
                "SELECT transcript_word_timestamps='[]' FROM post",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert!(words_are_preserved);
    }

    #[test]
    fn files_only_unlinks_paths_and_preserves_metadata_graph() {
        let temp = TempDir::new().unwrap();
        let first = temp.path().join("first.mp3");
        let second = temp.path().join("second.mp3");
        fs::write(&first, b"one").unwrap();
        fs::write(&second, b"two").unwrap();
        let mut connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        connection
            .execute(
                "UPDATE post SET unprocessed_audio_path=?1,processed_audio_path=?2 WHERE id=1",
                rusqlite::params![
                    first.to_string_lossy().into_owned(),
                    second.to_string_lossy().into_owned()
                ],
            )
            .unwrap();
        seed_graph(&connection, 1);
        let transaction = connection.transaction().unwrap();
        cleanup_processed_post_files_only(
            &transaction,
            &Map::from_iter([("post_id".to_owned(), json!("1"))]),
        )
        .unwrap();
        transaction.commit().unwrap();
        assert!(!first.exists());
        assert!(!second.exists());
        let row: (Option<String>, Option<String>, i64, i64) = connection
            .query_row(
                "SELECT unprocessed_audio_path,processed_audio_path,whitelisted,duration FROM post",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .unwrap();
        assert_eq!(row, (None, None, 0, 42));
        assert_eq!(
            connection
                .query_row("SELECT COUNT(*) FROM processing_job", [], |row| row
                    .get::<_, i64>(0))
                .unwrap(),
            1
        );
    }

    #[test]
    fn cleanup_processed_post_clears_graph_unwhitelists_and_recounts() {
        let mut connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        seed_graph(&connection, 1);
        connection
            .execute(
                "INSERT INTO jobs_manager_run VALUES (
                    'jobs-manager-singleton','running','test','2026-01-01',NULL,
                    1,0,0,1,0,0,NULL,'2026-01-01','2026-01-01','2026-01-01')",
                [],
            )
            .unwrap();
        let transaction = connection.transaction().unwrap();
        assert_eq!(
            cleanup_processed_post(
                &transaction,
                &Map::from_iter([("post_id".to_owned(), json!(1))]),
            )
            .unwrap(),
            json!({"post_id":1})
        );
        transaction.commit().unwrap();
        assert_eq!(
            connection
                .query_row("SELECT whitelisted FROM post", [], |row| row
                    .get::<_, i64>(0))
                .unwrap(),
            0
        );
        assert_json_null_columns(
            &connection,
            &[
                "bleep_windows",
                "transcript_word_timestamps",
                "refined_ad_boundaries",
            ],
        );
        let run: (String, i64) = connection
            .query_row(
                "SELECT status,total_jobs FROM jobs_manager_run",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(run, ("pending".into(), 0));
    }
}
