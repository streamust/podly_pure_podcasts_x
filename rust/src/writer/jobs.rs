use chrono::{Duration, NaiveDate, NaiveDateTime, Utc};
use rusqlite::types::Value as SqlValue;
use rusqlite::{params_from_iter, OptionalExtension, Transaction};
use serde_json::{json, Map, Value};
use uuid::Uuid;

use super::actions::{error, RpcActionResult};
use super::feeds::recalculate_run_counts;
use super::protocol::RpcError;

pub(super) const JOB_ACTIONS: &[&str] = &[
    "dequeue_job",
    "cleanup_stale_jobs",
    "clear_all_jobs",
    "clear_active_jobs",
    "create_job",
    "create_job_if_missing",
    "cancel_existing_jobs",
    "update_job_attribution",
    "update_job_status",
    "mark_cancelled",
    "mark_classification_parse_error",
    "record_ad_windows_count",
    "mark_auto_retry_attempted",
    "reassign_pending_jobs",
];

pub fn is_job_action(action: &str) -> bool {
    JOB_ACTIONS.contains(&action)
}

pub fn execute(
    transaction: &Transaction<'_>,
    action: &str,
    params: &Map<String, Value>,
) -> RpcActionResult {
    match action {
        "dequeue_job" => dequeue_job(transaction, params),
        "cleanup_stale_jobs" => cleanup_stale_jobs(transaction, params),
        "clear_all_jobs" => clear_all_jobs(transaction),
        "clear_active_jobs" => clear_active_jobs(transaction),
        "create_job" => create_job(transaction, params),
        "create_job_if_missing" => create_job_if_missing(transaction, params),
        "cancel_existing_jobs" => cancel_existing_jobs(transaction, params),
        "update_job_attribution" => update_job_attribution(transaction, params),
        "update_job_status" => update_job_status(transaction, params),
        "mark_cancelled" => mark_cancelled(transaction, params),
        "mark_classification_parse_error" => mark_classification_parse_error(transaction, params),
        "record_ad_windows_count" => record_ad_windows_count(transaction, params),
        "mark_auto_retry_attempted" => mark_auto_retry_attempted(transaction, params),
        "reassign_pending_jobs" => reassign_pending_jobs(transaction, params),
        _ => Err(error(
            "unsupported_action",
            "writer action is not registered",
        )),
    }
}

fn dequeue_job(transaction: &Transaction<'_>, params: &Map<String, Value>) -> RpcActionResult {
    let running = transaction
        .query_row(
            "SELECT 1 FROM processing_job WHERE status='running'
             ORDER BY started_at DESC NULLS LAST LIMIT 1",
            [],
            |_| Ok(()),
        )
        .optional()
        .map_err(database_error)?
        .is_some();
    if running {
        return Ok(Value::Null);
    }
    let job = transaction
        .query_row(
            "SELECT id,post_guid,jobs_manager_run_id FROM processing_job
             WHERE status='pending' ORDER BY created_at ASC LIMIT 1",
            [],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, Option<String>>(2)?,
                ))
            },
        )
        .optional()
        .map_err(database_error)?;
    let Some((job_id, post_guid, current_run)) = job else {
        return Ok(Value::Null);
    };
    let requested_run = params.get("run_id").filter(|value| truthy(value));
    let run = match requested_run {
        Some(value) => Some(sql_scalar(value)?),
        None => current_run.map(SqlValue::Text),
    };
    transaction
        .execute(
            "UPDATE processing_job SET status='running',started_at=?1,jobs_manager_run_id=?2
             WHERE id=?3",
            rusqlite::params![database_now(), run, job_id],
        )
        .map_err(database_error)?;
    Ok(json!({"job_id": job_id, "post_guid": post_guid}))
}

fn cleanup_stale_jobs(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let seconds = match params.get("older_than_seconds") {
        None => 3600.0,
        Some(Value::Number(value)) => value
            .as_f64()
            .ok_or_else(|| error("invalid_params", "older_than_seconds must be numeric"))?,
        _ => {
            return Err(error(
                "invalid_params",
                "older_than_seconds must be numeric",
            ))
        }
    };
    if !seconds.is_finite() {
        return Err(error("invalid_params", "older_than_seconds must be finite"));
    }
    let microseconds = (seconds * 1_000_000.0).round();
    if microseconds < i64::MIN as f64 || microseconds > i64::MAX as f64 {
        return Err(error(
            "invalid_params",
            "older_than_seconds is out of range",
        ));
    }
    let cutoff = Utc::now().naive_utc() - Duration::microseconds(microseconds as i64);
    let count = transaction
        .execute(
            "DELETE FROM processing_job WHERE created_at < ?1",
            [format_database_time(cutoff)],
        )
        .map_err(database_error)?;
    Ok(json!({"count": count}))
}

fn clear_all_jobs(transaction: &Transaction<'_>) -> RpcActionResult {
    transaction
        .execute("DELETE FROM processing_job", [])
        .map(|count| json!(count))
        .map_err(database_error)
}

fn clear_active_jobs(transaction: &Transaction<'_>) -> RpcActionResult {
    let count = transaction
        .execute(
            "DELETE FROM processing_job WHERE status IN ('pending','running')",
            [],
        )
        .map_err(database_error)?;
    if count > 0 {
        recalculate_run_counts(transaction)?;
    }
    Ok(json!(count))
}

fn create_job(transaction: &Transaction<'_>, params: &Map<String, Value>) -> RpcActionResult {
    let data = params
        .get("job_data")
        .and_then(Value::as_object)
        .ok_or_else(|| error("invalid_params", "job_data must be a dictionary"))?;
    let job_id = insert_job(transaction, data)?;
    let run_assigned = data.get("jobs_manager_run_id").is_some_and(truthy);
    if run_assigned {
        recalculate_run_counts(transaction)?;
    }
    Ok(json!({"job_id": job_id}))
}

fn create_job_if_missing(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let data = params
        .get("job_data")
        .and_then(Value::as_object)
        .ok_or_else(|| error("invalid_params", "job_data must be a dictionary"))?;
    let post_guid = data
        .get("post_guid")
        .filter(|value| truthy(value))
        .ok_or_else(|| error("invalid_params", "job_data must contain post_guid"))?;
    let exists = transaction
        .query_row(
            "SELECT 1 FROM processing_job WHERE post_guid=?1 LIMIT 1",
            [sql_scalar(post_guid)?],
            |_| Ok(()),
        )
        .optional()
        .map_err(database_error)?
        .is_some();
    if exists {
        return Ok(json!({"job_id": null, "skipped": true}));
    }
    create_job(transaction, params)
}

fn cancel_existing_jobs(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let post_guid = params.get("post_guid").cloned().unwrap_or(Value::Null);
    let current_id = params.get("current_job_id").cloned().unwrap_or(Value::Null);
    let count = transaction
        .execute(
            "DELETE FROM processing_job
             WHERE post_guid IS ?1 AND status IN ('pending','running') AND id IS NOT ?2",
            rusqlite::params![sql_scalar(&post_guid)?, sql_scalar(&current_id)?],
        )
        .map_err(database_error)?;
    if count > 0 {
        let post_id = transaction
            .query_row(
                "SELECT id FROM post WHERE guid IS ?1 LIMIT 1",
                [sql_scalar(&post_guid)?],
                |row| row.get::<_, i64>(0),
            )
            .optional()
            .map_err(database_error)?;
        if let Some(post_id) = post_id {
            transaction
                .execute(
                    "UPDATE model_call SET status='cancelled',
                        error_message='Superseded by a new processing job'
                     WHERE post_id=?1
                       AND status NOT IN ('success','failed_permanent','cancelled')",
                    [post_id],
                )
                .map_err(database_error)?;
        }
        recalculate_run_counts(transaction)?;
    }
    Ok(json!(count))
}

fn update_job_attribution(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let job_value = params
        .get("job_id")
        .filter(|value| truthy(value))
        .ok_or_else(|| error("invalid_params", "job_id is required"))?;
    let job_id = py_string(job_value);
    let current = job_row(transaction, &job_id)?;
    let Some(current) = current else {
        return Err(error(
            "invalid_params",
            &format!("Job {} not found", py_string(job_value)),
        ));
    };
    let mut changed = false;
    if let Some(value) = params.get("run_id").filter(|value| truthy(value)) {
        let next = py_string(value);
        if current.run_id.as_deref() != Some(next.as_str()) {
            update_job_column(
                transaction,
                &job_id,
                "jobs_manager_run_id",
                SqlValue::Text(next),
            )?;
            changed = true;
        }
    }
    if current.requested_by_user_id.is_none() {
        if let Some(value) = params
            .get("requested_by_user_id")
            .filter(|value| truthy(value))
        {
            update_job_column(
                transaction,
                &job_id,
                "requested_by_user_id",
                sql_scalar(value)?,
            )?;
            changed = true;
        }
    }
    if let Some(value) = params
        .get("billing_user_id")
        .filter(|value| !value.is_null())
    {
        let next = sql_scalar(value)?;
        if !sql_value_matches_optional_i64(&next, current.billing_user_id) {
            update_job_column(transaction, &job_id, "billing_user_id", next)?;
            changed = true;
        }
    }
    Ok(json!({"job_id": job_id, "changed": changed}))
}

fn update_job_status(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let job_value = params.get("job_id").cloned().unwrap_or(Value::Null);
    let job_id = py_string(&job_value);
    let Some(current) = job_status_row(transaction, &job_id)? else {
        return Err(error(
            "invalid_params",
            &format!("Job {} not found", py_string(&job_value)),
        ));
    };
    let status = params.get("status").cloned().unwrap_or(Value::Null);
    if current.status == "cancelled" && status.as_str() != Some("cancelled") {
        return Ok(json!({"job_id": job_id, "status": "cancelled"}));
    }
    let step = params.get("step").cloned().unwrap_or(Value::Null);
    let step_name = params.get("step_name").cloned().unwrap_or(Value::Null);
    let now = Utc::now().naive_utc();
    update_job_column(transaction, &job_id, "status", sql_scalar(&status)?)?;
    update_job_column(transaction, &job_id, "current_step", sql_scalar(&step)?)?;
    update_job_column(transaction, &job_id, "step_name", sql_scalar(&step_name)?)?;
    if let Some(progress) = params.get("progress").filter(|value| !value.is_null()) {
        update_job_column(
            transaction,
            &job_id,
            "progress_percentage",
            sql_scalar(progress)?,
        )?;
    }
    if let Some(total) = params.get("total_steps").filter(|value| !value.is_null()) {
        let total = py_int(total).map_err(|message| error("invalid_params", &message))?;
        update_job_column(
            transaction,
            &job_id,
            "total_steps",
            SqlValue::Integer(total),
        )?;
    }
    if let Some(message) = params.get("error_message").filter(|value| truthy(value)) {
        update_job_column(transaction, &job_id, "error_message", sql_scalar(message)?)?;
    }
    let status_text = status.as_str();
    if status_text == Some("running") && current.started_at.is_none() {
        update_job_column(
            transaction,
            &job_id,
            "started_at",
            SqlValue::Text(format_database_time(now)),
        )?;
    } else if matches!(
        status_text,
        Some("completed" | "failed" | "cancelled" | "skipped")
    ) && current.completed_at.is_none()
    {
        update_job_column(
            transaction,
            &job_id,
            "completed_at",
            SqlValue::Text(format_database_time(now)),
        )?;
    }
    let mut history = current.history;
    let last_step = history.last().and_then(|entry| entry.get("step"));
    if history.is_empty() || last_step != Some(&step) {
        history.push(json!({
            "step": step,
            "step_name": step_name,
            "started_at": format_iso_time(now),
        }));
        update_job_column(
            transaction,
            &job_id,
            "stage_history",
            SqlValue::Text(
                serde_json::to_string(&history)
                    .map_err(|_| error("invalid_params", "invalid stage history"))?,
            ),
        )?;
    }
    if current.run_id.is_some() {
        recalculate_run_counts(transaction)?;
    }
    Ok(json!({"job_id": job_id, "status": status}))
}

fn mark_cancelled(transaction: &Transaction<'_>, params: &Map<String, Value>) -> RpcActionResult {
    let job_value = params.get("job_id").cloned().unwrap_or(Value::Null);
    let job_id = py_string(&job_value);
    let Some(current) = job_status_row(transaction, &job_id)? else {
        return Err(error(
            "invalid_params",
            &format!("Job {} not found", py_string(&job_value)),
        ));
    };
    let reason = params
        .get("reason")
        .filter(|value| truthy(value))
        .map(py_string)
        .unwrap_or_else(|| "Cancelled by user request".to_owned());
    transaction
        .execute(
            "UPDATE processing_job SET status='cancelled',step_name=?1,error_message=?1,
                completed_at=?2 WHERE id=?3",
            rusqlite::params![reason, database_now(), job_id],
        )
        .map_err(database_error)?;
    if current.run_id.is_some() {
        recalculate_run_counts(transaction)?;
    }
    Ok(json!({"job_id": job_id, "status": "cancelled"}))
}

fn mark_classification_parse_error(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    update_required_job_flag(transaction, params, "had_classification_parse_error")
}

fn record_ad_windows_count(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let (job_id, _) = require_job(transaction, params)?;
    if let Some(value) = params.get("count").filter(|value| !value.is_null()) {
        let count = py_int(value).map_err(|message| error("invalid_params", &message))?;
        update_job_column(
            transaction,
            &job_id,
            "ad_windows_count",
            SqlValue::Integer(count),
        )?;
    }
    Ok(json!({"job_id": job_id}))
}

fn mark_auto_retry_attempted(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    update_required_job_flag(transaction, params, "auto_retry_attempted")
}

fn reassign_pending_jobs(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> RpcActionResult {
    let Some(run_id) = params.get("run_id").filter(|value| truthy(value)) else {
        return Ok(json!(0));
    };
    let run_id = sql_scalar(run_id)?;
    let count = transaction
        .execute(
            "UPDATE processing_job SET jobs_manager_run_id=?1
             WHERE status='pending' AND jobs_manager_run_id IS NOT ?1",
            [run_id],
        )
        .map_err(database_error)?;
    if count > 0 {
        recalculate_run_counts(transaction)?;
    }
    Ok(json!(count))
}

fn insert_job(
    transaction: &Transaction<'_>,
    data: &Map<String, Value>,
) -> Result<String, RpcError> {
    let now = Utc::now().naive_utc();
    let job_id = data
        .get("id")
        .filter(|value| !value.is_null())
        .map(py_string)
        .unwrap_or_else(|| Uuid::new_v4().to_string());
    let created_at = match data.get("created_at").filter(|value| !value.is_null()) {
        Some(value) => datetime_scalar(value)?,
        None => SqlValue::Text(format_database_time(now)),
    };
    let created_iso = match &created_at {
        SqlValue::Text(value) => database_to_iso(value),
        _ => format_iso_time(now),
    };
    let current_step = data
        .get("current_step")
        .filter(|value| !value.is_null())
        .cloned()
        .unwrap_or_else(|| json!(0));
    let step_name = data.get("step_name").cloned().unwrap_or(Value::Null);
    let history = data
        .get("stage_history")
        .filter(|value| truthy(value))
        .cloned()
        .unwrap_or_else(|| {
            let seed_step = if truthy(&current_step) {
                current_step.clone()
            } else {
                json!(0)
            };
            let seed_name = if truthy(&step_name) {
                step_name.clone()
            } else {
                json!("Queued")
            };
            json!([{"step":seed_step,"step_name":seed_name,"started_at":created_iso}])
        });
    let mut fields = vec![
        ("id", SqlValue::Text(job_id.clone())),
        ("current_step", sql_scalar(&current_step)?),
        ("total_steps", SqlValue::Integer(4)),
        ("progress_percentage", SqlValue::Real(0.0)),
        ("created_at", created_at),
        ("stage_history", json_scalar(&history)?),
        ("had_classification_parse_error", SqlValue::Integer(0)),
        ("auto_retry_attempted", SqlValue::Integer(0)),
    ];
    for (field, value) in data {
        if !matches!(
            field.as_str(),
            "id" | "jobs_manager_run_id"
                | "post_guid"
                | "status"
                | "current_step"
                | "step_name"
                | "total_steps"
                | "progress_percentage"
                | "started_at"
                | "completed_at"
                | "error_message"
                | "scheduler_job_id"
                | "created_at"
                | "requested_by_user_id"
                | "billing_user_id"
                | "stage_history"
                | "ad_windows_count"
                | "had_classification_parse_error"
                | "auto_retry_attempted"
        ) {
            return Err(error("invalid_params", "invalid job field"));
        }
        if matches!(field.as_str(), "id" | "created_at" | "stage_history") {
            continue;
        }
        // SQLAlchemy applies a column default for an explicit None, like an omitted key.
        if value.is_null() && fields.iter().any(|(existing, _)| *existing == field) {
            continue;
        }
        fields.retain(|(existing, _)| *existing != field);
        fields.push((field, sql_scalar(value)?));
    }
    insert_dynamic(transaction, "processing_job", &fields)?;
    Ok(job_id)
}

fn update_required_job_flag(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
    column: &str,
) -> RpcActionResult {
    let (job_id, _) = require_job(transaction, params)?;
    update_job_column(transaction, &job_id, column, SqlValue::Integer(1))?;
    Ok(json!({"job_id": job_id}))
}

fn require_job(
    transaction: &Transaction<'_>,
    params: &Map<String, Value>,
) -> Result<(String, JobRow), RpcError> {
    let value = params.get("job_id").cloned().unwrap_or(Value::Null);
    let job_id = py_string(&value);
    let Some(row) = job_row(transaction, &job_id)? else {
        return Err(error(
            "invalid_params",
            &format!("Job {} not found", py_string(&value)),
        ));
    };
    Ok((job_id, row))
}

struct JobRow {
    run_id: Option<String>,
    requested_by_user_id: Option<i64>,
    billing_user_id: Option<i64>,
}

fn job_row(transaction: &Transaction<'_>, job_id: &str) -> Result<Option<JobRow>, RpcError> {
    transaction
        .query_row(
            "SELECT jobs_manager_run_id,requested_by_user_id,billing_user_id
             FROM processing_job WHERE id=?1",
            [job_id],
            |row| {
                Ok(JobRow {
                    run_id: row.get(0)?,
                    requested_by_user_id: row.get(1)?,
                    billing_user_id: row.get(2)?,
                })
            },
        )
        .optional()
        .map_err(database_error)
}

struct JobStatusRow {
    status: String,
    run_id: Option<String>,
    started_at: Option<String>,
    completed_at: Option<String>,
    history: Vec<Value>,
}

fn job_status_row(
    transaction: &Transaction<'_>,
    job_id: &str,
) -> Result<Option<JobStatusRow>, RpcError> {
    transaction
        .query_row(
            "SELECT status,jobs_manager_run_id,started_at,completed_at,stage_history
             FROM processing_job WHERE id=?1",
            [job_id],
            |row| {
                let history: Option<String> = row.get(4)?;
                let history = history
                    .and_then(|value| serde_json::from_str::<Vec<Value>>(&value).ok())
                    .unwrap_or_default();
                Ok(JobStatusRow {
                    status: row.get(0)?,
                    run_id: row.get(1)?,
                    started_at: row.get(2)?,
                    completed_at: row.get(3)?,
                    history,
                })
            },
        )
        .optional()
        .map_err(database_error)
}

fn update_job_column(
    transaction: &Transaction<'_>,
    job_id: &str,
    column: &str,
    value: SqlValue,
) -> Result<(), RpcError> {
    if !matches!(
        column,
        "jobs_manager_run_id"
            | "requested_by_user_id"
            | "billing_user_id"
            | "status"
            | "current_step"
            | "step_name"
            | "progress_percentage"
            | "total_steps"
            | "error_message"
            | "started_at"
            | "completed_at"
            | "stage_history"
            | "had_classification_parse_error"
            | "ad_windows_count"
            | "auto_retry_attempted"
    ) {
        return Err(error("internal_error", "invalid job update column"));
    }
    transaction
        .execute(
            &format!("UPDATE processing_job SET {column}=?1 WHERE id=?2"),
            rusqlite::params![value, job_id],
        )
        .map_err(database_error)?;
    Ok(())
}

fn insert_dynamic(
    transaction: &Transaction<'_>,
    table: &str,
    fields: &[(&str, SqlValue)],
) -> Result<(), RpcError> {
    if table != "processing_job" {
        return Err(error("internal_error", "invalid insert table"));
    }
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

fn sql_value_matches_optional_i64(value: &SqlValue, current: Option<i64>) -> bool {
    match (value, current) {
        (SqlValue::Null, None) => true,
        (SqlValue::Integer(next), Some(current)) => *next == current,
        _ => false,
    }
}

fn datetime_scalar(value: &Value) -> Result<SqlValue, RpcError> {
    let Value::String(value) = value else {
        return sql_scalar(value);
    };
    let parsed = NaiveDateTime::parse_from_str(value, "%Y-%m-%dT%H:%M:%S%.f")
        .or_else(|_| NaiveDateTime::parse_from_str(value, "%Y-%m-%d %H:%M:%S%.f"))
        .or_else(|_| {
            NaiveDate::parse_from_str(value, "%Y-%m-%d")
                .map(|date| date.and_hms_opt(0, 0, 0).expect("midnight is valid"))
        })
        .or_else(|_| chrono::DateTime::parse_from_rfc3339(value).map(|date| date.naive_local()))
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

fn database_to_iso(value: &str) -> String {
    value.replace(' ', "T")
}

fn database_now() -> String {
    format_database_time(Utc::now().naive_utc())
}

fn format_database_time(value: NaiveDateTime) -> String {
    value.format("%Y-%m-%d %H:%M:%S%.6f").to_string()
}

fn format_iso_time(value: NaiveDateTime) -> String {
    value.format("%Y-%m-%dT%H:%M:%S%.6f").to_string()
}

fn database_error(_error: rusqlite::Error) -> RpcError {
    error("database_error", "database operation failed")
}

#[cfg(test)]
mod tests {
    use rusqlite::Connection;

    use super::*;

    fn schema(connection: &Connection) {
        connection
            .execute_batch(
                "CREATE TABLE processing_job(
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
                 CREATE TABLE post(id INTEGER PRIMARY KEY,guid TEXT NOT NULL);
                 CREATE TABLE model_call(
                    id INTEGER PRIMARY KEY,post_id INTEGER NOT NULL,status TEXT NOT NULL,
                    error_message TEXT
                 );",
            )
            .unwrap();
    }

    fn seed_run(connection: &Connection) {
        connection
            .execute(
                "INSERT INTO jobs_manager_run VALUES (
                    'jobs-manager-singleton','running','test','2020-01-01',NULL,
                    0,0,0,0,0,0,NULL,'2020-01-01','2020-01-01','2020-01-01')",
                [],
            )
            .unwrap();
    }

    fn create(transaction: &Transaction<'_>, id: &str, guid: &str, status: &str, created_at: &str) {
        create_job(
            transaction,
            &Map::from_iter([(
                "job_data".to_owned(),
                json!({
                    "id":id,"post_guid":guid,"status":status,"created_at":created_at
                }),
            )]),
        )
        .unwrap();
    }

    #[test]
    fn create_job_supplies_defaults_and_create_if_missing_is_serially_idempotent() {
        let mut connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        let transaction = connection.transaction().unwrap();
        let result = create_job(
            &transaction,
            &Map::from_iter([(
                "job_data".to_owned(),
                json!({"post_guid":"guid","status":"pending","step_name":"Queued"}),
            )]),
        )
        .unwrap();
        let job_id = result["job_id"].as_str().unwrap();
        assert_eq!(Uuid::parse_str(job_id).unwrap().get_version_num(), 4);
        let row: (i64, i64, f64, String, i64, i64) = transaction
            .query_row(
                "SELECT current_step,total_steps,progress_percentage,stage_history,
                        had_classification_parse_error,auto_retry_attempted
                 FROM processing_job WHERE id=?1",
                [job_id],
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
        assert_eq!((row.0, row.1, row.2, row.4, row.5), (0, 4, 0.0, 0, 0));
        let history: Value = serde_json::from_str(&row.3).unwrap();
        assert_eq!(history[0]["step"], 0);
        assert_eq!(history[0]["step_name"], "Queued");
        assert_eq!(
            create_job_if_missing(
                &transaction,
                &Map::from_iter([(
                    "job_data".to_owned(),
                    json!({"post_guid":"guid","status":"pending"}),
                )]),
            )
            .unwrap(),
            json!({"job_id":null,"skipped":true})
        );
        transaction.commit().unwrap();
    }

    #[test]
    fn dequeue_is_fifo_and_a_running_job_blocks_every_later_claim() {
        let mut connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        let transaction = connection.transaction().unwrap();
        create(&transaction, "later", "later-guid", "pending", "2026-01-02");
        create(&transaction, "first", "first-guid", "pending", "2026-01-01");
        let claimed = dequeue_job(
            &transaction,
            &Map::from_iter([("run_id".to_owned(), json!("run-1"))]),
        )
        .unwrap();
        assert_eq!(claimed, json!({"job_id":"first","post_guid":"first-guid"}));
        assert_eq!(dequeue_job(&transaction, &Map::new()).unwrap(), Value::Null);
        let row: (String, String) = transaction
            .query_row(
                "SELECT status,jobs_manager_run_id FROM processing_job WHERE id='first'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(row, ("running".into(), "run-1".into()));
        transaction.commit().unwrap();
    }

    #[test]
    fn cancellation_owns_terminal_state_against_late_worker_updates() {
        let mut connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        let transaction = connection.transaction().unwrap();
        create(&transaction, "job", "guid", "running", "2026-01-01");
        mark_cancelled(
            &transaction,
            &Map::from_iter([
                ("job_id".to_owned(), json!("job")),
                ("reason".to_owned(), json!("Stopped")),
            ]),
        )
        .unwrap();
        let before: (String, String, String, String) = transaction
            .query_row(
                "SELECT status,step_name,error_message,completed_at FROM processing_job WHERE id='job'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .unwrap();
        let result = update_job_status(
            &transaction,
            &Map::from_iter([
                ("job_id".to_owned(), json!("job")),
                ("status".to_owned(), json!("completed")),
                ("step".to_owned(), json!(4)),
                ("step_name".to_owned(), json!("Completed")),
                ("progress".to_owned(), json!(100)),
            ]),
        )
        .unwrap();
        let after: (String, String, String, String) = transaction
            .query_row(
                "SELECT status,step_name,error_message,completed_at FROM processing_job WHERE id='job'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .unwrap();
        assert_eq!(result, json!({"job_id":"job","status":"cancelled"}));
        assert_eq!(after, before);
        transaction.commit().unwrap();
    }

    #[test]
    fn status_updates_append_only_new_steps_and_record_flags() {
        let mut connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        let transaction = connection.transaction().unwrap();
        create(&transaction, "job", "guid", "pending", "2026-01-01");
        for (step, name) in [(1, "Download"), (1, "Still downloading"), (2, "Transcribe")] {
            update_job_status(
                &transaction,
                &Map::from_iter([
                    ("job_id".to_owned(), json!("job")),
                    ("status".to_owned(), json!("running")),
                    ("step".to_owned(), json!(step)),
                    ("step_name".to_owned(), json!(name)),
                ]),
            )
            .unwrap();
        }
        mark_classification_parse_error(
            &transaction,
            &Map::from_iter([("job_id".to_owned(), json!("job"))]),
        )
        .unwrap();
        record_ad_windows_count(
            &transaction,
            &Map::from_iter([
                ("job_id".to_owned(), json!("job")),
                ("count".to_owned(), json!("3")),
            ]),
        )
        .unwrap();
        mark_auto_retry_attempted(
            &transaction,
            &Map::from_iter([("job_id".to_owned(), json!("job"))]),
        )
        .unwrap();
        let row: (String, i64, i64, i64) = transaction
            .query_row(
                "SELECT stage_history,had_classification_parse_error,
                        ad_windows_count,auto_retry_attempted FROM processing_job WHERE id='job'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .unwrap();
        let history: Vec<Value> = serde_json::from_str(&row.0).unwrap();
        assert_eq!(history.len(), 3);
        assert_eq!(
            history
                .iter()
                .map(|entry| entry["step"].as_i64())
                .collect::<Vec<_>>(),
            vec![Some(0), Some(1), Some(2)]
        );
        assert_eq!((row.1, row.2, row.3), (1, 3, 1));
        transaction.commit().unwrap();
    }

    #[test]
    fn cancel_existing_jobs_preserves_current_and_terminal_calls() {
        let mut connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        seed_run(&connection);
        connection
            .execute("INSERT INTO post VALUES (1,'guid')", [])
            .unwrap();
        for (id, status) in [(1, "pending"), (2, "success"), (3, "retrying")] {
            connection
                .execute(
                    "INSERT INTO model_call VALUES (?1,1,?2,NULL)",
                    rusqlite::params![id, status],
                )
                .unwrap();
        }
        let transaction = connection.transaction().unwrap();
        create(&transaction, "old", "guid", "pending", "2026-01-01");
        create(&transaction, "current", "guid", "running", "2026-01-02");
        assert_eq!(
            cancel_existing_jobs(
                &transaction,
                &Map::from_iter([
                    ("post_guid".to_owned(), json!("guid")),
                    ("current_job_id".to_owned(), json!("current")),
                ]),
            )
            .unwrap(),
            json!(1)
        );
        assert_eq!(
            transaction
                .query_row(
                    "SELECT COUNT(*) FROM processing_job WHERE id='current'",
                    [],
                    |row| row.get::<_, i64>(0)
                )
                .unwrap(),
            1
        );
        let statuses = transaction
            .prepare("SELECT status FROM model_call ORDER BY id")
            .unwrap()
            .query_map([], |row| row.get::<_, String>(0))
            .unwrap()
            .collect::<rusqlite::Result<Vec<_>>>()
            .unwrap();
        assert_eq!(statuses, ["cancelled", "success", "cancelled"]);
        transaction.commit().unwrap();
    }

    #[test]
    fn attribution_reassignment_and_clear_actions_preserve_terminal_jobs() {
        let mut connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        seed_run(&connection);
        let transaction = connection.transaction().unwrap();
        create(&transaction, "pending", "one", "pending", "2026-01-01");
        create(&transaction, "complete", "two", "completed", "2026-01-02");
        assert_eq!(
            update_job_attribution(
                &transaction,
                &Map::from_iter([
                    ("job_id".to_owned(), json!("pending")),
                    ("requested_by_user_id".to_owned(), json!(8)),
                    ("billing_user_id".to_owned(), json!(9)),
                ]),
            )
            .unwrap()["changed"],
            true
        );
        assert_eq!(
            reassign_pending_jobs(
                &transaction,
                &Map::from_iter([("run_id".to_owned(), json!("jobs-manager-singleton"))]),
            )
            .unwrap(),
            json!(1)
        );
        assert_eq!(clear_active_jobs(&transaction).unwrap(), json!(1));
        assert_eq!(
            transaction
                .query_row("SELECT status FROM processing_job", [], |row| row
                    .get::<_, String>(0))
                .unwrap(),
            "completed"
        );
        transaction.commit().unwrap();
    }

    #[test]
    fn explicit_nulls_keep_defaults_and_attribution() {
        let mut connection = Connection::open_in_memory().unwrap();
        schema(&connection);
        let transaction = connection.transaction().unwrap();
        create_job(
            &transaction,
            &Map::from_iter([(
                "job_data".to_owned(),
                json!({
                    "id":"job","post_guid":"guid","status":"pending",
                    "current_step":null,"total_steps":null,"progress_percentage":null,
                    "created_at":null,"had_classification_parse_error":null,
                    "auto_retry_attempted":null,"billing_user_id":101
                }),
            )]),
        )
        .unwrap();
        assert_eq!(
            update_job_attribution(
                &transaction,
                &Map::from_iter([
                    ("job_id".to_owned(), json!("job")),
                    ("billing_user_id".to_owned(), Value::Null),
                ]),
            )
            .unwrap()["changed"],
            false
        );
        let row: (i64, i64, f64, Option<String>, i64, i64, i64) = transaction
            .query_row(
                "SELECT current_step,total_steps,progress_percentage,created_at,
                        had_classification_parse_error,auto_retry_attempted,billing_user_id
                 FROM processing_job WHERE id='job'",
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
        assert_eq!(
            (row.0, row.1, row.2, row.4, row.5, row.6),
            (0, 4, 0.0, 0, 0, 101)
        );
        assert!(row.3.is_some());
    }
}
