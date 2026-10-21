use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use chrono::{DateTime, NaiveDate, NaiveDateTime, Utc};
use rusqlite::types::Value as SqlValue;
use rusqlite::{params_from_iter, OptionalExtension, Transaction};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use uuid::Uuid;

use super::actions::{error, RpcActionResult};
use super::protocol::RpcError;

pub(super) const FEED_ACTIONS: &[&str] = &[
    "refresh_feed",
    "add_feed",
    "update_feed_settings",
    "increment_download_count",
    "whitelist_post",
    "ensure_user_feed_membership",
    "remove_user_feed_membership",
    "whitelist_latest_post_for_feed",
    "toggle_whitelist_all_for_feed",
    "create_dev_test_feed",
    "delete_feed_cascade",
    "create_feed_access_token",
    "touch_feed_access_token",
];

pub fn is_feed_action(action: &str) -> bool {
    FEED_ACTIONS.contains(&action)
}

pub fn execute(
    transaction: &Transaction<'_>,
    action: &str,
    params: &Map<String, Value>,
) -> RpcActionResult {
    match action {
        "refresh_feed" => refresh_feed(transaction, params),
        "add_feed" => add_feed(transaction, params),
        "update_feed_settings" => update_feed_settings(transaction, params),
        "increment_download_count" => increment_download_count(transaction, params),
        "whitelist_post" => whitelist_post(transaction, params),
        "ensure_user_feed_membership" => ensure_user_feed_membership(transaction, params),
        "remove_user_feed_membership" => remove_user_feed_membership(transaction, params),
        "whitelist_latest_post_for_feed" => whitelist_latest_post(transaction, params),
        "toggle_whitelist_all_for_feed" => toggle_whitelist_all(transaction, params),
        "create_dev_test_feed" => create_dev_test_feed(transaction, params),
        "delete_feed_cascade" => delete_feed_cascade(transaction, params),
        "create_feed_access_token" => create_feed_access_token(transaction, params),
        "touch_feed_access_token" => touch_feed_access_token(transaction, params),
        _ => Err(error(
            "unsupported_action",
            "writer action is not registered",
        )),
    }
}

fn refresh_feed(transaction: &Transaction<'_>, params: &Map<String, Value>) -> RpcActionResult {
    let feed_id_value = params.get("feed_id").cloned().unwrap_or(Value::Null);
    let feed_id = lookup_id(transaction, "feed", &feed_id_value)?.ok_or_else(|| {
        error(
            "invalid_params",
            &format!("Feed {} not found", py_string(&feed_id_value)),
        )
    })?;
    let updates = object_or_default(params.get("updates"), "updates must be an object")?;
    let new_posts = array_or_default(params.get("new_posts"), "new_posts must be an array")?;
    let existing_updates = array_or_default(
        params.get("existing_post_updates"),
        "existing_post_updates must be an array",
    )?;

    for (field, value) in updates {
        update_feed_column(transaction, feed_id, field, value)?;
    }
    let mut created_guids = Vec::new();
    for post in new_posts {
        let post = post
            .as_object()
            .ok_or_else(|| error("invalid_params", "post data must be an object"))?;
        let (_, guid, whitelisted) = insert_post(transaction, post, Some(feed_id))?;
        if whitelisted {
            created_guids.push(guid);
        }
    }
    for guid in created_guids {
        insert_processing_job(transaction, &guid, "pending", 0, "Queued", 0.0)?;
    }

    let mut updated_posts_count = 0_i64;
    for update in existing_updates {
        let update = update
            .as_object()
            .ok_or_else(|| error("invalid_params", "post update must be an object"))?;
        let Some(post_id_value) = update.get("post_id").filter(|value| truthy(value)) else {
            continue;
        };
        let Ok(post_id) = py_int(post_id_value) else {
            return Err(error("invalid_params", "post_id must be an integer"));
        };
        let belongs = transaction
            .query_row(
                "SELECT 1 FROM post WHERE id=?1 AND feed_id=?2",
                rusqlite::params![post_id, feed_id],
                |_| Ok(()),
            )
            .optional()
            .map_err(database_error)?
            .is_some();
        if !belongs {
            continue;
        }
        let mut changed = false;
        for field in ["guid", "title", "description", "image_url", "duration"] {
            if let Some(value) = update.get(field) {
                update_post_column(transaction, post_id, field, value)?;
                changed = true;
            }
        }
        updated_posts_count += i64::from(changed);
    }
    if !updates.is_empty() || !new_posts.is_empty() || updated_posts_count > 0 {
        transaction
            .execute(
                "UPDATE feed SET last_changed_at=?1 WHERE id=?2",
                rusqlite::params![database_now(), feed_id],
            )
            .map_err(database_error)?;
    }
    recalculate_run_counts(transaction)?;
    Ok(json!({
        "feed_id": feed_id,
        "new_posts_count": new_posts.len(),
        "updated_posts_count": updated_posts_count,
    }))
}

fn add_feed(transaction: &Transaction<'_>, params: &Map<String, Value>) -> RpcActionResult {
    let feed = params
        .get("feed")
        .and_then(Value::as_object)
        .ok_or_else(|| error("invalid_params", "feed data must be a dictionary"))?;
    let posts = array_or_default(params.get("posts"), "posts must be an array")?;
    let feed_id = insert_feed(transaction, feed)?;
    let mut whitelisted_guids = Vec::new();
    for post in posts {
        let post = post
            .as_object()
            .ok_or_else(|| error("invalid_params", "post data must be an object"))?;
        let (_, guid, whitelisted) = insert_post(transaction, post, Some(feed_id))?;
        if whitelisted {
            whitelisted_guids.push(guid);
        }
    }
    for guid in whitelisted_guids {
        insert_processing_job(transaction, &guid, "pending", 0, "Queued", 0.0)?;
    }
    recalculate_run_counts(transaction)?;
    Ok(json!({"feed_id": feed_id}))
}

fn update_feed_settings(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let value = params
        .get("feed_id")
        .filter(|value| truthy(value))
        .ok_or_else(|| error("invalid_params", "feed_id is required"))?;
    let feed_id = py_int(value).map_err(|message| error("invalid_params", &message))?;
    if !row_exists(transaction, "feed", feed_id)? {
        return Err(error(
            "invalid_params",
            &format!("Feed {} not found", py_string(value)),
        ));
    }
    if let Some(value) = params.get("auto_whitelist_new_episodes_override") {
        update_feed_column(
            transaction,
            feed_id,
            "auto_whitelist_new_episodes_override",
            value,
        )?;
    }
    Ok(json!({"feed_id": feed_id}))
}

fn increment_download_count(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let value = params
        .get("post_id")
        .filter(|value| truthy(value))
        .ok_or_else(|| error("invalid_params", "post_id is required"))?;
    let updated = transaction
        .execute(
            "UPDATE post SET download_count=coalesce(download_count,0)+1 WHERE id=?1",
            [sql_scalar(value)?],
        )
        .map_err(database_error)?;
    Ok(json!({"post_id": value, "updated": updated}))
}

fn whitelist_post(transaction: &Transaction<'_>, params: &Map<String, Value>) -> RpcActionResult {
    let value = params
        .get("post_id")
        .filter(|value| truthy(value))
        .ok_or_else(|| error("invalid_params", "post_id is required"))?;
    let post_id = py_int(value).map_err(|message| error("invalid_params", &message))?;
    let updated = transaction
        .execute("UPDATE post SET whitelisted=1 WHERE id=?1", [post_id])
        .map_err(database_error)?;
    Ok(json!({"post_id": post_id, "updated": updated}))
}

fn ensure_user_feed_membership(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let (feed_id, user_id) = membership_ids(params)?;
    let previous_count = transaction
        .query_row(
            "SELECT COUNT(*) FROM feed_supporter WHERE feed_id=?1",
            [feed_id],
            |row| row.get::<_, i64>(0),
        )
        .map_err(database_error)?;
    let exists = transaction
        .query_row(
            "SELECT 1 FROM feed_supporter WHERE feed_id=?1 AND user_id=?2 LIMIT 1",
            rusqlite::params![feed_id, user_id],
            |_| Ok(()),
        )
        .optional()
        .map_err(database_error)?
        .is_some();
    if exists {
        return Ok(json!({"created": false, "previous_count": previous_count}));
    }
    transaction
        .execute(
            "INSERT INTO feed_supporter(feed_id,user_id,created_at) VALUES (?1,?2,?3)",
            rusqlite::params![feed_id, user_id, database_now()],
        )
        .map_err(database_error)?;
    Ok(json!({"created": true, "previous_count": previous_count}))
}

fn remove_user_feed_membership(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let (feed_id, user_id) = membership_ids(params)?;
    let removed = transaction
        .execute(
            "DELETE FROM feed_supporter WHERE feed_id=?1 AND user_id=?2",
            rusqlite::params![feed_id, user_id],
        )
        .map_err(database_error)?;
    Ok(json!({"removed": removed}))
}

fn whitelist_latest_post(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let value = params
        .get("feed_id")
        .filter(|value| truthy(value))
        .ok_or_else(|| error("invalid_params", "feed_id is required"))?;
    let feed_id = py_int(value).map_err(|message| error("invalid_params", &message))?;
    let latest = transaction
        .query_row(
            "SELECT id,guid,whitelisted FROM post WHERE feed_id=?1
             ORDER BY release_date DESC NULLS LAST,id DESC LIMIT 1",
            [feed_id],
            |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, bool>(2)?,
                ))
            },
        )
        .optional()
        .map_err(database_error)?;
    let Some((post_id, guid, whitelisted)) = latest else {
        return Ok(json!({"updated": false}));
    };
    if whitelisted {
        return Ok(json!({"updated": false, "post_guid": guid}));
    }
    transaction
        .execute("UPDATE post SET whitelisted=1 WHERE id=?1", [post_id])
        .map_err(database_error)?;
    Ok(json!({"updated": true, "post_guid": guid}))
}

fn toggle_whitelist_all(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let feed_value = params
        .get("feed_id")
        .filter(|value| !value.is_null())
        .ok_or_else(|| error("invalid_params", "feed_id and new_status are required"))?;
    let status_value = params
        .get("new_status")
        .filter(|value| !value.is_null())
        .ok_or_else(|| error("invalid_params", "feed_id and new_status are required"))?;
    let feed_id = py_int(feed_value).map_err(|message| error("invalid_params", &message))?;
    let updated = transaction
        .execute(
            "UPDATE post SET whitelisted=?1 WHERE feed_id=?2",
            rusqlite::params![truthy(status_value), feed_id],
        )
        .map_err(database_error)?;
    Ok(json!({"feed_id": feed_id, "updated_count": updated}))
}

fn create_dev_test_feed(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let rss_url = params
        .get("rss_url")
        .filter(|value| truthy(value))
        .ok_or_else(|| error("invalid_params", "rss_url and title are required"))?;
    let title = params
        .get("title")
        .filter(|value| truthy(value))
        .ok_or_else(|| error("invalid_params", "rss_url and title are required"))?;
    if let Some(feed_id) = lookup_column_id(transaction, "feed", "rss_url", rss_url)? {
        return Ok(json!({"feed_id": feed_id, "created": false}));
    }
    let mut feed = Map::new();
    feed.insert("rss_url".to_owned(), rss_url.clone());
    feed.insert("title".to_owned(), title.clone());
    for field in ["image_url", "description", "author"] {
        feed.insert(
            field.to_owned(),
            params.get(field).cloned().unwrap_or(Value::Null),
        );
    }
    let feed_id = insert_feed(transaction, &feed)?;
    let post_count = params
        .get("post_count")
        .filter(|value| truthy(value))
        .map(py_int)
        .transpose()
        .map_err(|message| error("invalid_params", &message))?
        .unwrap_or(30);
    let guid_prefix = params
        .get("guid_prefix")
        .filter(|value| truthy(value))
        .map(py_string)
        .unwrap_or_else(|| "test-guid".to_owned());
    let download_prefix = params
        .get("download_url_prefix")
        .filter(|value| truthy(value))
        .map(py_string)
        .unwrap_or_else(|| "http://test-feed".to_owned());
    let dev_timestamp = Utc::now().naive_utc();
    let now = format_database_time(dev_timestamp);
    let iso_now = format_iso_time(dev_timestamp);
    if post_count > 0 {
        for index in 1..=post_count {
            let post = Map::from_iter([
                (
                    "guid".to_owned(),
                    json!(format!("{guid_prefix}-{feed_id}-{index}")),
                ),
                ("title".to_owned(), json!(format!("Test Episode {index}"))),
                (
                    "download_url".to_owned(),
                    json!(format!("{download_prefix}/{feed_id}/{index}.mp3")),
                ),
                ("release_date".to_owned(), json!(now)),
                ("duration".to_owned(), json!(3600)),
                (
                    "description".to_owned(),
                    json!(format!("Test episode description {index}")),
                ),
                ("whitelisted".to_owned(), json!(true)),
            ]);
            let (_, guid, _) = insert_post(transaction, &post, Some(feed_id))?;
            insert_processing_job_at(
                transaction,
                &guid,
                "completed",
                4,
                "completed",
                100.0,
                &now,
                &iso_now,
            )?;
        }
    }
    Ok(json!({"feed_id": feed_id, "created": true}))
}

fn delete_feed_cascade(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let value = params
        .get("feed_id")
        .filter(|value| truthy(value))
        .ok_or_else(|| error("invalid_params", "feed_id is required"))?;
    let feed_id = py_int(value).map_err(|message| error("invalid_params", &message))?;
    if !row_exists(transaction, "feed", feed_id)? {
        return Ok(json!({"deleted": false}));
    }
    transaction
        .execute(
            "DELETE FROM identification WHERE transcript_segment_id IN (
                SELECT transcript_segment.id FROM transcript_segment
                JOIN post ON post.id=transcript_segment.post_id WHERE post.feed_id=?1
             )",
            [feed_id],
        )
        .map_err(database_error)?;
    transaction
        .execute(
            "DELETE FROM transcript_segment WHERE post_id IN (SELECT id FROM post WHERE feed_id=?1)",
            [feed_id],
        )
        .map_err(database_error)?;
    transaction
        .execute(
            "DELETE FROM model_call WHERE post_id IN (SELECT id FROM post WHERE feed_id=?1)",
            [feed_id],
        )
        .map_err(database_error)?;
    transaction
        .execute(
            "DELETE FROM processing_job WHERE post_guid IN (SELECT guid FROM post WHERE feed_id=?1)",
            [feed_id],
        )
        .map_err(database_error)?;
    transaction
        .execute("DELETE FROM post WHERE feed_id=?1", [feed_id])
        .map_err(database_error)?;
    transaction
        .execute("DELETE FROM feed_access_token WHERE feed_id=?1", [feed_id])
        .map_err(database_error)?;
    transaction
        .execute("DELETE FROM feed_supporter WHERE feed_id=?1", [feed_id])
        .map_err(database_error)?;
    transaction
        .execute("DELETE FROM feed WHERE id=?1", [feed_id])
        .map_err(database_error)?;
    Ok(json!({"deleted": true, "feed_id": feed_id}))
}

fn create_feed_access_token(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let user_value = params
        .get("user_id")
        .filter(|value| truthy(value))
        .ok_or_else(|| error("invalid_params", "user_id is required"))?;
    let user_id = py_int(user_value).map_err(|message| error("invalid_params", &message))?;
    let feed_id = params
        .get("feed_id")
        .filter(|value| !value.is_null())
        .map(py_int)
        .transpose()
        .map_err(|message| error("invalid_params", &message))?;
    let existing = transaction
        .query_row(
            "SELECT id,token_id,token_secret FROM feed_access_token
             WHERE user_id=?1 AND revoked=0 AND feed_id IS ?2 LIMIT 1",
            rusqlite::params![user_id, feed_id],
            |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, Option<String>>(2)?,
                ))
            },
        )
        .optional()
        .map_err(database_error)?;
    if let Some((id, token_id, secret)) = existing {
        if let Some(secret) = secret.filter(|secret| !secret.is_empty()) {
            return Ok(json!({"token_id": token_id, "secret": secret}));
        }
        let secret = new_secret();
        let hash = hash_token(&secret);
        transaction
            .execute(
                "UPDATE feed_access_token SET token_hash=?1,token_secret=?2 WHERE id=?3",
                rusqlite::params![hash, secret, id],
            )
            .map_err(database_error)?;
        return Ok(json!({"token_id": token_id, "secret": secret}));
    }
    let token_id = Uuid::new_v4().simple().to_string();
    let secret = new_secret();
    transaction
        .execute(
            "INSERT INTO feed_access_token(
                token_id,token_hash,token_secret,feed_id,user_id,created_at,last_used_at,revoked
             ) VALUES (?1,?2,?3,?4,?5,?6,NULL,0)",
            rusqlite::params![
                token_id,
                hash_token(&secret),
                secret,
                feed_id,
                user_id,
                database_now()
            ],
        )
        .map_err(database_error)?;
    Ok(json!({"token_id": token_id, "secret": secret}))
}

fn touch_feed_access_token(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let token = params
        .get("token_id")
        .filter(|value| truthy(value))
        .ok_or_else(|| error("invalid_params", "token_id is required"))?;
    let token = sql_scalar(token)?;
    let existing = transaction
        .query_row(
            "SELECT id,token_secret FROM feed_access_token WHERE token_id=?1 AND revoked=0 LIMIT 1",
            [token],
            |row| Ok((row.get::<_, i64>(0)?, row.get::<_, Option<String>>(1)?)),
        )
        .optional()
        .map_err(database_error)?;
    let Some((id, current_secret)) = existing else {
        return Ok(json!({"updated": false}));
    };
    let replacement = if current_secret.is_none() {
        params
            .get("secret")
            .filter(|value| truthy(value))
            .map(py_string)
    } else {
        None
    };
    transaction
        .execute(
            "UPDATE feed_access_token SET last_used_at=?1,
                token_secret=COALESCE(token_secret,?2) WHERE id=?3",
            rusqlite::params![database_now(), replacement, id],
        )
        .map_err(database_error)?;
    Ok(json!({"updated": true}))
}

fn insert_feed(transaction: &Transaction<'_>, data: &Map<String, Value>) -> Result<i64, RpcError> {
    let now = database_now();
    let mut fields = vec![
        ("ad_detection_strategy", SqlValue::Text("llm".to_owned())),
        ("enable_profanity_bleeping", SqlValue::Integer(0)),
        ("confirm_whisperx_endpoint", SqlValue::Integer(0)),
        ("last_changed_at", SqlValue::Text(now)),
    ];
    for (field, value) in data {
        if !matches!(
            field.as_str(),
            "id" | "alt_id"
                | "title"
                | "description"
                | "author"
                | "rss_url"
                | "image_url"
                | "ad_detection_strategy"
                | "chapter_filter_strings"
                | "enable_llm_chapter_fallback_tagging"
                | "chapter_full_block_text"
                | "auto_whitelist_new_episodes_override"
                | "enable_profanity_bleeping"
                | "confirm_whisperx_endpoint"
                | "last_changed_at"
        ) {
            return Err(error("invalid_params", "invalid feed field"));
        }
        fields.retain(|(existing, _)| *existing != field);
        fields.push((field, sql_scalar(value)?));
    }
    insert_dynamic(transaction, "feed", &fields)?;
    Ok(data
        .get("id")
        .and_then(Value::as_i64)
        .unwrap_or_else(|| transaction.last_insert_rowid()))
}

fn insert_post(
    transaction: &Transaction<'_>,
    data: &Map<String, Value>,
    forced_feed_id: Option<i64>,
) -> Result<(i64, String, bool), RpcError> {
    let mut fields = vec![
        ("whitelisted", SqlValue::Integer(0)),
        ("download_count", SqlValue::Integer(0)),
    ];
    for (field, value) in data {
        if !matches!(
            field.as_str(),
            "feed_id"
                | "id"
                | "guid"
                | "download_url"
                | "title"
                | "unprocessed_audio_path"
                | "processed_audio_path"
                | "description"
                | "release_date"
                | "duration"
                | "whitelisted"
                | "image_url"
                | "download_count"
                | "chapter_data"
                | "bleep_windows"
                | "transcript_word_timestamps"
                | "refined_ad_boundaries"
                | "refined_ad_boundaries_updated_at"
        ) {
            return Err(error("invalid_params", "invalid post field"));
        }
        fields.retain(|(existing, _)| *existing != field);
        let value = if field == "release_date" {
            datetime_scalar(value)?
        } else if matches!(
            field.as_str(),
            "bleep_windows" | "transcript_word_timestamps" | "refined_ad_boundaries"
        ) {
            json_scalar(value)?
        } else {
            sql_scalar(value)?
        };
        fields.push((field, value));
    }
    if let Some(feed_id) = forced_feed_id {
        fields.retain(|(field, _)| *field != "feed_id");
        fields.push(("feed_id", SqlValue::Integer(feed_id)));
    }
    let guid = fields
        .iter()
        .find(|(field, _)| *field == "guid")
        .and_then(|(_, value)| match value {
            SqlValue::Text(value) => Some(value.clone()),
            _ => None,
        })
        .ok_or_else(|| error("database_error", "database operation failed"))?;
    let whitelisted = fields
        .iter()
        .find(|(field, _)| *field == "whitelisted")
        .is_some_and(|(_, value)| !matches!(value, SqlValue::Null | SqlValue::Integer(0)));
    insert_dynamic(transaction, "post", &fields)?;
    let id = data
        .get("id")
        .and_then(Value::as_i64)
        .unwrap_or_else(|| transaction.last_insert_rowid());
    Ok((id, guid, whitelisted))
}

fn insert_dynamic(
    transaction: &Transaction<'_>,
    table: &str,
    fields: &[(&str, SqlValue)],
) -> Result<(), RpcError> {
    let columns = fields
        .iter()
        .map(|(field, _)| *field)
        .collect::<Vec<_>>()
        .join(",");
    let placeholders = (1..=fields.len())
        .map(|index| format!("?{index}"))
        .collect::<Vec<_>>()
        .join(",");
    let values = fields
        .iter()
        .map(|(_, value)| value.clone())
        .collect::<Vec<_>>();
    transaction
        .execute(
            &format!("INSERT INTO {table}({columns}) VALUES ({placeholders})"),
            params_from_iter(values),
        )
        .map_err(database_error)?;
    Ok(())
}

fn insert_processing_job(
    transaction: &Transaction<'_>,
    guid: &str,
    status: &str,
    step: i64,
    step_name: &str,
    progress: f64,
) -> Result<(), RpcError> {
    let timestamp = Utc::now().naive_utc();
    let now = format_database_time(timestamp);
    let iso = format_iso_time(timestamp);
    insert_processing_job_at(
        transaction,
        guid,
        status,
        step,
        step_name,
        progress,
        &now,
        &iso,
    )
}

#[allow(clippy::too_many_arguments)]
fn insert_processing_job_at(
    transaction: &Transaction<'_>,
    transaction_guid: &str,
    status: &str,
    step: i64,
    step_name: &str,
    progress: f64,
    now: &str,
    iso: &str,
) -> Result<(), RpcError> {
    let history = serde_json::to_string(&json!([{
        "step": step, "step_name": step_name, "started_at": iso
    }]))
    .map_err(|_| error("internal_error", "stage history serialization failed"))?;
    let completed = (status == "completed").then_some(now);
    let started = (status == "completed").then_some(now);
    transaction
        .execute(
            "INSERT INTO processing_job(
                id,jobs_manager_run_id,post_guid,status,current_step,step_name,total_steps,
                progress_percentage,started_at,completed_at,error_message,scheduler_job_id,
                created_at,requested_by_user_id,billing_user_id,stage_history,
                ad_windows_count,had_classification_parse_error,auto_retry_attempted
             ) VALUES (?1,NULL,?2,?3,?4,?5,4,?6,?7,?8,NULL,NULL,?9,NULL,NULL,?10,NULL,0,0)",
            rusqlite::params![
                Uuid::new_v4().to_string(),
                transaction_guid,
                status,
                step,
                (status == "completed").then_some(step_name),
                progress,
                started,
                completed,
                now,
                history
            ],
        )
        .map_err(database_error)?;
    Ok(())
}

pub(crate) fn recalculate_run_counts(transaction: &Transaction<'_>) -> Result<(), RpcError> {
    let run = transaction
        .query_row(
            "SELECT counters_reset_at FROM jobs_manager_run WHERE id='jobs-manager-singleton'",
            [],
            |row| row.get::<_, Option<String>>(0),
        )
        .optional()
        .map_err(database_error)?;
    let Some(cutoff) = run else {
        return Ok(());
    };
    let mut statement = transaction
        .prepare(
            "SELECT status,COUNT(*) FROM processing_job
             WHERE jobs_manager_run_id='jobs-manager-singleton'
               AND (?1 IS NULL OR created_at>=?1) GROUP BY status",
        )
        .map_err(database_error)?;
    let counts = statement
        .query_map([cutoff], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
        })
        .map_err(database_error)?
        .collect::<rusqlite::Result<std::collections::HashMap<_, _>>>()
        .map_err(database_error)?;
    let queued = counts.get("pending").unwrap_or(&0) + counts.get("queued").unwrap_or(&0);
    let running = *counts.get("running").unwrap_or(&0);
    let completed = *counts.get("completed").unwrap_or(&0);
    let failed = counts.get("failed").unwrap_or(&0) + counts.get("cancelled").unwrap_or(&0);
    let skipped = *counts.get("skipped").unwrap_or(&0);
    let total: i64 = counts.values().sum();
    let now = database_now();
    if queued + running > 0 {
        let status = if running > 0 { "running" } else { "pending" };
        transaction
            .execute(
                "UPDATE jobs_manager_run SET status=?1,total_jobs=?2,queued_jobs=?3,
                    running_jobs=?4,completed_jobs=?5,failed_jobs=?6,skipped_jobs=?7,
                    updated_at=?8,started_at=COALESCE(started_at,?8),
                    counters_reset_at=COALESCE(counters_reset_at,started_at,?8),completed_at=NULL
                 WHERE id='jobs-manager-singleton'",
                rusqlite::params![status, total, queued, running, completed, failed, skipped, now],
            )
            .map_err(database_error)?;
    } else {
        transaction
            .execute(
                "UPDATE jobs_manager_run SET status='pending',completed_at=?1,started_at=NULL,
                    total_jobs=0,queued_jobs=0,running_jobs=0,completed_jobs=0,
                    failed_jobs=0,skipped_jobs=0,updated_at=?1,counters_reset_at=?1
                 WHERE id='jobs-manager-singleton'",
                [now],
            )
            .map_err(database_error)?;
    }
    Ok(())
}

fn update_feed_column(
    transaction: &Transaction<'_>,
    feed_id: i64,
    field: &str,
    value: &Value,
) -> Result<(), RpcError> {
    if !matches!(
        field,
        "alt_id"
            | "title"
            | "description"
            | "author"
            | "rss_url"
            | "image_url"
            | "ad_detection_strategy"
            | "chapter_filter_strings"
            | "enable_llm_chapter_fallback_tagging"
            | "chapter_full_block_text"
            | "auto_whitelist_new_episodes_override"
            | "enable_profanity_bleeping"
            | "confirm_whisperx_endpoint"
            | "last_changed_at"
    ) {
        return Err(error("invalid_params", "invalid feed field"));
    }
    transaction
        .execute(
            &format!("UPDATE feed SET {field}=?1 WHERE id=?2"),
            rusqlite::params![sql_scalar(value)?, feed_id],
        )
        .map_err(database_error)?;
    Ok(())
}

fn update_post_column(
    transaction: &Transaction<'_>,
    post_id: i64,
    field: &str,
    value: &Value,
) -> Result<(), RpcError> {
    if !matches!(
        field,
        "guid" | "title" | "description" | "image_url" | "duration"
    ) {
        return Err(error("internal_error", "invalid existing-post field"));
    }
    transaction
        .execute(
            &format!("UPDATE post SET {field}=?1 WHERE id=?2"),
            rusqlite::params![sql_scalar(value)?, post_id],
        )
        .map_err(database_error)?;
    Ok(())
}

fn membership_ids(params: &Map<String, Value>) -> Result<(i64, i64), RpcError> {
    let feed = params.get("feed_id").filter(|value| truthy(value));
    let user = params.get("user_id").filter(|value| truthy(value));
    let (Some(feed), Some(user)) = (feed, user) else {
        return Err(error("invalid_params", "feed_id and user_id are required"));
    };
    Ok((
        py_int(feed).map_err(|message| error("invalid_params", &message))?,
        py_int(user).map_err(|message| error("invalid_params", &message))?,
    ))
}

fn lookup_id(
    transaction: &Transaction<'_>,
    table: &str,
    value: &Value,
) -> Result<Option<i64>, RpcError> {
    transaction
        .query_row(
            &format!("SELECT id FROM {table} WHERE id=?1"),
            [sql_scalar(value)?],
            |row| row.get(0),
        )
        .optional()
        .map_err(database_error)
}

fn lookup_column_id(
    transaction: &Transaction<'_>,
    table: &str,
    column: &str,
    value: &Value,
) -> Result<Option<i64>, RpcError> {
    if (table, column) != ("feed", "rss_url") {
        return Err(error("internal_error", "invalid lookup"));
    }
    transaction
        .query_row(
            "SELECT id FROM feed WHERE rss_url=?1 LIMIT 1",
            [sql_scalar(value)?],
            |row| row.get(0),
        )
        .optional()
        .map_err(database_error)
}

fn row_exists(transaction: &Transaction<'_>, table: &str, id: i64) -> Result<bool, RpcError> {
    transaction
        .query_row(&format!("SELECT 1 FROM {table} WHERE id=?1"), [id], |_| {
            Ok(())
        })
        .optional()
        .map(|row| row.is_some())
        .map_err(database_error)
}

fn object_or_default<'a>(
    value: Option<&'a Value>,
    message: &str,
) -> Result<&'a Map<String, Value>, RpcError> {
    static EMPTY: std::sync::LazyLock<Map<String, Value>> = std::sync::LazyLock::new(Map::new);
    match value {
        None => Ok(&EMPTY),
        Some(Value::Object(value)) => Ok(value),
        _ => Err(error("invalid_params", message)),
    }
}

fn array_or_default<'a>(value: Option<&'a Value>, message: &str) -> Result<&'a [Value], RpcError> {
    match value {
        None => Ok(&[]),
        Some(Value::Array(value)) => Ok(value),
        _ => Err(error("invalid_params", message)),
    }
}

fn datetime_scalar(value: &Value) -> Result<SqlValue, RpcError> {
    let Value::String(value) = value else {
        return sql_scalar(value);
    };
    let parsed = NaiveDateTime::parse_from_str(value, "%Y-%m-%dT%H:%M:%S%.f")
        .or_else(|_| NaiveDateTime::parse_from_str(value, "%Y-%m-%d %H:%M:%S%.f"))
        .or_else(|_| DateTime::parse_from_rfc3339(value).map(|date| date.naive_local()))
        .or_else(|_| {
            NaiveDate::parse_from_str(value, "%Y-%m-%d")
                .map(|date| date.and_hms_opt(0, 0, 0).expect("midnight is valid"))
        })
        .map_err(|_| error("invalid_params", "Invalid isoformat string"))?;
    Ok(SqlValue::Text(format_database_time(parsed)))
}

fn json_scalar(value: &Value) -> Result<SqlValue, RpcError> {
    if value.is_null() {
        Ok(SqlValue::Null)
    } else {
        serde_json::to_string(value)
            .map(SqlValue::Text)
            .map_err(|_| error("invalid_params", "invalid JSON value"))
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
        _ => Err("invalid integer value".to_owned()),
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

fn new_secret() -> String {
    let first = Uuid::new_v4();
    let second = Uuid::new_v4();
    let mut bytes = [0_u8; 18];
    bytes[..16].copy_from_slice(first.as_bytes());
    bytes[16..].copy_from_slice(&second.as_bytes()[..2]);
    URL_SAFE_NO_PAD.encode(bytes)
}

fn hash_token(secret: &str) -> String {
    format!("{:x}", Sha256::digest(secret.as_bytes()))
}

fn database_now() -> String {
    format_database_time(Utc::now().naive_utc())
}

fn format_database_time(timestamp: NaiveDateTime) -> String {
    format!(
        "{}.{:06}",
        timestamp.format("%Y-%m-%d %H:%M:%S"),
        timestamp.and_utc().timestamp_subsec_micros()
    )
}

fn format_iso_time(timestamp: NaiveDateTime) -> String {
    timestamp.format("%Y-%m-%dT%H:%M:%S%.6f").to_string()
}

fn database_error(_error: rusqlite::Error) -> RpcError {
    error("database_error", "database operation failed")
}

#[cfg(test)]
mod tests {
    use rusqlite::Connection;

    use super::*;

    #[test]
    fn datetime_scalar_matches_sqlalchemy_datetime_storage() {
        for (input, expected) in [
            ("2026-01-03T04:05:06", "2026-01-03 04:05:06.000000"),
            ("2026-01-03T04:05:06.123456", "2026-01-03 04:05:06.123456"),
            ("2026-01-03", "2026-01-03 00:00:00.000000"),
        ] {
            assert_eq!(
                datetime_scalar(&json!(input)).unwrap(),
                SqlValue::Text(expected.to_owned())
            );
        }
    }

    fn schema(connection: &Connection) {
        connection
            .execute_batch(
                "CREATE TABLE feed(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,alt_id TEXT,title TEXT NOT NULL,
                    description TEXT,author TEXT,rss_url TEXT NOT NULL UNIQUE,image_url TEXT,
                    ad_detection_strategy TEXT NOT NULL,chapter_filter_strings TEXT,
                    enable_llm_chapter_fallback_tagging BOOLEAN,chapter_full_block_text BOOLEAN,
                    auto_whitelist_new_episodes_override BOOLEAN,
                    enable_profanity_bleeping BOOLEAN NOT NULL,
                    confirm_whisperx_endpoint BOOLEAN NOT NULL,last_changed_at DATETIME NOT NULL
                 );
                 CREATE TABLE post(
                    feed_id INTEGER NOT NULL,id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guid TEXT NOT NULL,download_url TEXT NOT NULL,title TEXT NOT NULL,
                    unprocessed_audio_path TEXT,processed_audio_path TEXT,description TEXT,
                    release_date DATETIME,duration INTEGER,whitelisted BOOLEAN NOT NULL,
                    image_url TEXT,download_count INTEGER,chapter_data TEXT,bleep_windows JSON,
                    transcript_word_timestamps JSON,refined_ad_boundaries JSON,
                    refined_ad_boundaries_updated_at DATETIME,
                    UNIQUE(feed_id,guid),UNIQUE(feed_id,download_url)
                 );
                 CREATE TABLE processing_job(
                    id TEXT PRIMARY KEY,jobs_manager_run_id TEXT,post_guid TEXT NOT NULL,
                    status TEXT NOT NULL,current_step INTEGER,step_name TEXT,total_steps INTEGER,
                    progress_percentage REAL,started_at DATETIME,completed_at DATETIME,
                    error_message TEXT,scheduler_job_id TEXT,created_at DATETIME,
                    requested_by_user_id INTEGER,billing_user_id INTEGER,stage_history JSON,
                    ad_windows_count INTEGER,had_classification_parse_error BOOLEAN NOT NULL,
                    auto_retry_attempted BOOLEAN NOT NULL
                 );
                 CREATE TABLE jobs_manager_run(
                    id TEXT PRIMARY KEY,status TEXT NOT NULL,trigger TEXT NOT NULL,
                    started_at DATETIME,completed_at DATETIME,total_jobs INTEGER NOT NULL,
                    queued_jobs INTEGER NOT NULL,running_jobs INTEGER NOT NULL,
                    completed_jobs INTEGER NOT NULL,failed_jobs INTEGER NOT NULL,
                    skipped_jobs INTEGER NOT NULL,context_json JSON,counters_reset_at DATETIME,
                    created_at DATETIME,updated_at DATETIME
                 );
                 CREATE TABLE feed_supporter(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,feed_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,created_at DATETIME NOT NULL,UNIQUE(feed_id,user_id)
                 );
                 CREATE TABLE feed_access_token(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,token_id TEXT NOT NULL UNIQUE,
                    token_hash TEXT NOT NULL,token_secret TEXT,feed_id INTEGER,user_id INTEGER NOT NULL,
                    created_at DATETIME NOT NULL,last_used_at DATETIME,revoked BOOLEAN NOT NULL
                 );
                 CREATE TABLE transcript_segment(id INTEGER PRIMARY KEY,post_id INTEGER NOT NULL);
                 CREATE TABLE identification(id INTEGER PRIMARY KEY,transcript_segment_id INTEGER NOT NULL);
                 CREATE TABLE model_call(id INTEGER PRIMARY KEY,post_id INTEGER NOT NULL);
                 CREATE TABLE audio_segment(id INTEGER PRIMARY KEY,post_id INTEGER NOT NULL,model_call_id INTEGER);
                ",
            )
            .unwrap();
    }

    fn add_sample_feed(connection: &mut Connection) -> i64 {
        let transaction = connection.transaction().unwrap();
        let result = add_feed(
            &transaction,
            &Map::from_iter([
                (
                    "feed".to_owned(),
                    json!({"title":"Feed","rss_url":"https://example.test/rss"}),
                ),
                (
                    "posts".to_owned(),
                    json!([
                        {"guid":"one","download_url":"https://example.test/1.mp3",
                         "title":"One","duration":12.5,"whitelisted":true,
                         "release_date":"2026-01-01T12:00:00"},
                        {"guid":"two","download_url":"https://example.test/2.mp3",
                         "title":"Two"}
                    ]),
                ),
            ]),
        )
        .unwrap();
        transaction.commit().unwrap();
        result["feed_id"].as_i64().unwrap()
    }

    #[test]
    fn add_and_refresh_preserve_defaults_jobs_numeric_shape_and_merge_rules() {
        let mut connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        let feed_id = add_sample_feed(&mut connection);
        let row: (String, i64, i64, String) = connection
            .query_row(
                "SELECT ad_detection_strategy,enable_profanity_bleeping,
                        confirm_whisperx_endpoint,typeof(last_changed_at) FROM feed WHERE id=?1",
                [feed_id],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .unwrap();
        assert_eq!(row, ("llm".into(), 0, 0, "text".into()));
        let numeric: (String, i64) = connection
            .query_row(
                "SELECT typeof(duration),whitelisted FROM post WHERE guid='one'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(numeric, ("real".into(), 1));
        assert_eq!(
            connection
                .query_row("SELECT COUNT(*) FROM processing_job", [], |row| row
                    .get::<_, i64>(0))
                .unwrap(),
            1
        );

        let post_id = connection
            .query_row("SELECT id FROM post WHERE guid='two'", [], |row| {
                row.get::<_, i64>(0)
            })
            .unwrap();
        let transaction = connection.transaction().unwrap();
        let result = refresh_feed(
            &transaction,
            &Map::from_iter([
                ("feed_id".to_owned(), json!(feed_id)),
                ("updates".to_owned(), json!({"description":"new"})),
                (
                    "new_posts".to_owned(),
                    json!([{"feed_id":feed_id,"guid":"three",
                            "download_url":"https://example.test/3.mp3","title":"Three"}]),
                ),
                (
                    "existing_post_updates".to_owned(),
                    json!([{"post_id":post_id,"duration":42,"description":"updated"},
                           {"post_id":999,"title":"ignored"}]),
                ),
            ]),
        )
        .unwrap();
        transaction.commit().unwrap();
        assert_eq!(result["new_posts_count"], 1);
        assert_eq!(result["updated_posts_count"], 1);
        assert_eq!(
            connection
                .query_row("SELECT duration FROM post WHERE id=?1", [post_id], |row| {
                    row.get::<_, i64>(0)
                })
                .unwrap(),
            42
        );
    }

    #[test]
    fn duplicate_refresh_rolls_back_prior_feed_mutation() {
        let mut connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        let feed_id = add_sample_feed(&mut connection);
        let transaction = connection.transaction().unwrap();
        let failure = refresh_feed(
            &transaction,
            &Map::from_iter([
                ("feed_id".to_owned(), json!(feed_id)),
                ("updates".to_owned(), json!({"title":"must roll back"})),
                (
                    "new_posts".to_owned(),
                    json!([{"feed_id":feed_id,"guid":"one",
                            "download_url":"https://other.test/duplicate.mp3","title":"Duplicate"}]),
                ),
            ]),
        );
        assert!(failure.is_err());
        transaction.rollback().unwrap();
        let title: String = connection
            .query_row("SELECT title FROM feed WHERE id=?1", [feed_id], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(title, "Feed");
    }

    #[test]
    fn membership_whitelist_and_download_actions_cover_noops_and_coercion() {
        let mut connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        let feed_id = add_sample_feed(&mut connection);
        let transaction = connection.transaction().unwrap();
        let ids = Map::from_iter([
            ("feed_id".to_owned(), json!(feed_id.to_string())),
            ("user_id".to_owned(), json!("7")),
        ]);
        assert_eq!(
            ensure_user_feed_membership(&transaction, &ids).unwrap(),
            json!({"created":true,"previous_count":0})
        );
        assert_eq!(
            ensure_user_feed_membership(&transaction, &ids).unwrap(),
            json!({"created":false,"previous_count":1})
        );
        let latest = whitelist_latest_post(
            &transaction,
            &Map::from_iter([("feed_id".to_owned(), json!(feed_id))]),
        )
        .unwrap();
        assert_eq!(latest, json!({"updated":false,"post_guid":"one"}));
        let post_id = transaction
            .query_row("SELECT id FROM post WHERE guid='two'", [], |row| {
                row.get::<_, i64>(0)
            })
            .unwrap();
        assert_eq!(
            increment_download_count(
                &transaction,
                &Map::from_iter([("post_id".to_owned(), json!(post_id))]),
            )
            .unwrap()["updated"],
            1
        );
        assert_eq!(
            remove_user_feed_membership(&transaction, &ids).unwrap()["removed"],
            1
        );
        transaction.commit().unwrap();
    }

    #[test]
    fn tokens_are_secure_hashed_idempotent_rotatable_and_touchable() {
        let mut connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        let feed_id = add_sample_feed(&mut connection);
        let transaction = connection.transaction().unwrap();
        let params = Map::from_iter([
            ("user_id".to_owned(), json!(9)),
            ("feed_id".to_owned(), json!(feed_id)),
        ]);
        let first = create_feed_access_token(&transaction, &params).unwrap();
        assert_eq!(first["token_id"].as_str().unwrap().len(), 32);
        assert_eq!(first["secret"].as_str().unwrap().len(), 24);
        assert_eq!(
            create_feed_access_token(&transaction, &params).unwrap(),
            first
        );
        let stored: (String, String) = transaction
            .query_row(
                "SELECT token_hash,token_secret FROM feed_access_token WHERE token_id=?1",
                [first["token_id"].as_str().unwrap()],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(stored.0, hash_token(&stored.1));
        transaction
            .execute(
                "UPDATE feed_access_token SET token_secret=NULL WHERE token_id=?1",
                [first["token_id"].as_str().unwrap()],
            )
            .unwrap();
        let rotated = create_feed_access_token(&transaction, &params).unwrap();
        assert_eq!(rotated["token_id"], first["token_id"]);
        assert_ne!(rotated["secret"], first["secret"]);
        assert_eq!(
            touch_feed_access_token(
                &transaction,
                &Map::from_iter([("token_id".to_owned(), rotated["token_id"].clone())]),
            )
            .unwrap(),
            json!({"updated":true})
        );
        transaction.commit().unwrap();
        let last_used: Option<String> = connection
            .query_row("SELECT last_used_at FROM feed_access_token", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert!(last_used.is_some());
    }

    #[test]
    fn delete_feed_removes_documented_graph_and_preserves_current_audio_orphan_behavior() {
        let mut connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        let feed_id = add_sample_feed(&mut connection);
        let post_id = connection
            .query_row("SELECT id FROM post LIMIT 1", [], |row| {
                row.get::<_, i64>(0)
            })
            .unwrap();
        let guid: String = connection
            .query_row("SELECT guid FROM post WHERE id=?1", [post_id], |row| {
                row.get(0)
            })
            .unwrap();
        connection
            .execute("INSERT INTO transcript_segment VALUES (1,?1)", [post_id])
            .unwrap();
        connection
            .execute("INSERT INTO identification VALUES (1,1)", [])
            .unwrap();
        connection
            .execute("INSERT INTO model_call VALUES (1,?1)", [post_id])
            .unwrap();
        connection
            .execute("INSERT INTO audio_segment VALUES (1,?1,1)", [post_id])
            .unwrap();
        connection
            .execute(
                "INSERT INTO processing_job(
                    id,post_guid,status,had_classification_parse_error,auto_retry_attempted
                 ) VALUES ('extra',?1,'pending',0,0)",
                [guid],
            )
            .unwrap();
        let transaction = connection.transaction().unwrap();
        let result = delete_feed_cascade(
            &transaction,
            &Map::from_iter([("feed_id".to_owned(), json!(feed_id))]),
        )
        .unwrap();
        transaction.commit().unwrap();
        assert_eq!(result, json!({"deleted":true,"feed_id":feed_id}));
        for table in [
            "feed",
            "post",
            "processing_job",
            "transcript_segment",
            "identification",
            "model_call",
        ] {
            let count: i64 = connection
                .query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |row| {
                    row.get(0)
                })
                .unwrap();
            assert_eq!(count, 0, "{table}");
        }
        let audio_count: i64 = connection
            .query_row("SELECT COUNT(*) FROM audio_segment", [], |row| row.get(0))
            .unwrap();
        assert_eq!(audio_count, 1);
    }
}
