use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::mpsc::{self, RecvTimeoutError, SyncSender, TrySendError};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use serde_json::Value;
use tokio::sync::oneshot;

use super::actions::{error, ActionRegistry};
use super::database;
use super::memory::{self, should_purge};
use super::protocol::{Command, Operation, RpcError};

#[derive(Debug)]
pub enum AdmissionError {
    NotReady,
    CapacityExhausted,
    PayloadTooLarge,
}

#[derive(Debug)]
pub struct ExecutionResult {
    pub command_id: String,
    pub result: Result<Value, RpcError>,
}

struct ByteBudget {
    used: Mutex<usize>,
    maximum: usize,
}

struct CapacityReservation {
    budget: Arc<ByteBudget>,
    bytes: usize,
    entries_used: Arc<AtomicUsize>,
}

impl Drop for CapacityReservation {
    fn drop(&mut self) {
        let mut used = self.budget.used.lock().expect("byte budget poisoned");
        *used = used.saturating_sub(self.bytes);
        self.entries_used.fetch_sub(1, Ordering::AcqRel);
    }
}

struct WorkItem {
    command: Command,
    _reservation: CapacityReservation,
    response: Option<oneshot::Sender<ExecutionResult>>,
    submitted_at: Instant,
}

struct MemoryActivity {
    active_handlers: AtomicUsize,
    activity_count: AtomicUsize,
    last_activity: Mutex<Instant>,
}

impl MemoryActivity {
    fn new() -> Self {
        Self {
            active_handlers: AtomicUsize::new(0),
            activity_count: AtomicUsize::new(0),
            last_activity: Mutex::new(Instant::now()),
        }
    }

    fn note_activity(&self) {
        *self.last_activity.lock().expect("memory activity poisoned") = Instant::now();
        self.activity_count.fetch_add(1, Ordering::AcqRel);
    }

    fn track_handler(&self) -> RequestActivity<'_> {
        self.active_handlers.fetch_add(1, Ordering::AcqRel);
        self.note_activity();
        RequestActivity { activity: self }
    }

    fn quiet_seconds(&self) -> u64 {
        self.last_activity
            .lock()
            .expect("memory activity poisoned")
            .elapsed()
            .as_secs()
    }
}

pub(super) struct RequestActivity<'a> {
    activity: &'a MemoryActivity,
}

impl Drop for RequestActivity<'_> {
    fn drop(&mut self) {
        self.activity.note_activity();
        self.activity.active_handlers.fetch_sub(1, Ordering::AcqRel);
    }
}

pub struct Admission {
    pub response: Option<oneshot::Receiver<ExecutionResult>>,
}

pub struct WriterExecutor {
    sender: Mutex<Option<SyncSender<WorkItem>>>,
    accepting: Arc<AtomicBool>,
    byte_budget: Arc<ByteBudget>,
    entries_used: Arc<AtomicUsize>,
    maximum_entries: usize,
    memory_activity: Arc<MemoryActivity>,
    thread: Mutex<Option<JoinHandle<()>>>,
}

impl WriterExecutor {
    pub fn start(
        db_path: &Path,
        queue_entries: usize,
        queue_bytes: usize,
        registry: ActionRegistry,
    ) -> anyhow::Result<Self> {
        Self::start_inner(db_path, queue_entries, queue_bytes, registry, false)
    }

    pub(super) fn start_with_test_foreign_keys(
        db_path: &Path,
        queue_entries: usize,
        queue_bytes: usize,
        registry: ActionRegistry,
    ) -> anyhow::Result<Self> {
        Self::start_inner(db_path, queue_entries, queue_bytes, registry, true)
    }

    fn start_inner(
        db_path: &Path,
        queue_entries: usize,
        queue_bytes: usize,
        registry: ActionRegistry,
        enable_test_foreign_keys: bool,
    ) -> anyhow::Result<Self> {
        let (sender, receiver) = mpsc::sync_channel::<WorkItem>(queue_entries);
        let (started_tx, started_rx) = mpsc::sync_channel(1);
        let path = db_path.to_owned();
        let timing_enabled = writer_timing_enabled();
        let accepting = Arc::new(AtomicBool::new(false));
        let thread_accepting = Arc::clone(&accepting);
        let entries_used = Arc::new(AtomicUsize::new(0));
        let worker_entries_used = Arc::clone(&entries_used);
        let memory_activity = Arc::new(MemoryActivity::new());
        let worker_memory_activity = Arc::clone(&memory_activity);
        let trim_enabled =
            memory::memory_trim_enabled(std::env::var("PODLY_MEMORY_TRIM_ENABLED").ok().as_deref());
        let idle_trim_interval = if trim_enabled {
            memory::idle_trim_interval_seconds(
                std::env::var("PODLY_WRITER_IDLE_TRIM_INTERVAL_SEC")
                    .ok()
                    .as_deref(),
            )
            .map(Duration::from_secs)
        } else {
            None
        };
        let worker = thread::Builder::new()
            .name("podly-sqlite-writer".to_owned())
            .spawn(move || {
                let connection =
                    match database::open_ready_with_foreign_keys(&path, enable_test_foreign_keys) {
                        Ok(connection) => connection,
                        Err(error) => {
                            let _ = started_tx.send(Err(error.to_string()));
                            return;
                        }
                    };
                thread_accepting.store(true, Ordering::Release);
                let _ = started_tx.send(Ok(()));

                let mut last_purge_activity_count = 0;
                loop {
                    let work = match idle_trim_interval {
                        Some(interval) => match receiver.recv_timeout(interval) {
                            Ok(work) => work,
                            Err(RecvTimeoutError::Disconnected) => break,
                            Err(RecvTimeoutError::Timeout) => {
                                let activity_count = worker_memory_activity
                                    .activity_count
                                    .load(Ordering::Acquire);
                                let active_handlers = worker_memory_activity
                                    .active_handlers
                                    .load(Ordering::Acquire);
                                let pending_work = worker_entries_used.load(Ordering::Acquire);
                                if should_purge(
                                    active_handlers,
                                    pending_work,
                                    activity_count,
                                    last_purge_activity_count,
                                    worker_memory_activity.quiet_seconds(),
                                    interval.as_secs(),
                                ) {
                                    // This is a quiescence snapshot. No Tokio
                                    // task or transaction is blocked while the
                                    // allocator runs; a new request may begin
                                    // after this decision and overlap the purge.
                                    memory::purge_all_arenas();
                                    last_purge_activity_count = activity_count;
                                }
                                continue;
                            }
                        },
                        None => match receiver.recv() {
                            Ok(work) => work,
                            Err(_) => break,
                        },
                    };
                    let dequeued_at = Instant::now();
                    let queue_ms =
                        dequeued_at.duration_since(work.submitted_at).as_secs_f64() * 1000.0;
                    let mut work = work;
                    let purge_after = trim_enabled
                        && matches!(
                            &work.command.operation,
                            Operation::Action { action, .. } if memory::purge_after_action(action)
                        );
                    {
                        let result = execute_one(&connection, &registry, &work.command);
                        if timing_enabled {
                            let finished_at = Instant::now();
                            eprintln!(
                                "[WRITER_TIMING] {}",
                                writer_timing_json(
                                    &work.command,
                                    queue_ms,
                                    finished_at.duration_since(dequeued_at).as_secs_f64() * 1000.0,
                                    result.is_ok(),
                                )
                            );
                        }
                        let undelivered = match work.response.take() {
                            Some(response) => response
                                .send(ExecutionResult {
                                    command_id: work.command.command_id.clone(),
                                    result,
                                })
                                .err()
                                .map(|unsent| unsent.result),
                            None => Some(result),
                        };
                        // wait=false commands and disconnected callers have no
                        // one to report to; the log is their only failure signal.
                        if let Some(Err(error)) = undelivered {
                            eprintln!(
                                "[WRITER_UNOBSERVED_FAILURE] {}",
                                unobserved_failure_json(&work.command, &error)
                            );
                        }
                    }
                    drop(work);
                    if purge_after {
                        memory::purge_all_arenas();
                    }
                    worker_memory_activity.note_activity();
                }
                thread_accepting.store(false, Ordering::Release);
            })?;

        match started_rx.recv()? {
            Ok(()) => Ok(Self {
                sender: Mutex::new(Some(sender)),
                accepting,
                byte_budget: Arc::new(ByteBudget {
                    used: Mutex::new(0),
                    maximum: queue_bytes,
                }),
                entries_used,
                maximum_entries: queue_entries,
                memory_activity,
                thread: Mutex::new(Some(worker)),
            }),
            Err(message) => {
                let _ = worker.join();
                anyhow::bail!(message)
            }
        }
    }

    pub fn submit(
        &self,
        command: Command,
        serialized_bytes: usize,
    ) -> Result<Admission, AdmissionError> {
        if !self.accepting.load(Ordering::Acquire) {
            return Err(AdmissionError::NotReady);
        }
        let reservation = self.reserve(serialized_bytes)?;
        let (response_tx, response_rx) = command.wait.then(oneshot::channel).unzip();
        let work = WorkItem {
            command,
            _reservation: reservation,
            response: response_tx,
            submitted_at: Instant::now(),
        };
        let sender = self.sender.lock().expect("writer sender poisoned");
        let Some(sender) = sender.as_ref() else {
            return Err(AdmissionError::NotReady);
        };
        match sender.try_send(work) {
            Ok(()) => Ok(Admission {
                response: response_rx,
            }),
            Err(TrySendError::Full(_)) => Err(AdmissionError::CapacityExhausted),
            Err(TrySendError::Disconnected(_)) => Err(AdmissionError::NotReady),
        }
    }

    pub fn stop_admission(&self) {
        self.accepting.store(false, Ordering::Release);
    }

    pub fn is_accepting(&self) -> bool {
        self.accepting.load(Ordering::Acquire)
    }

    pub(super) fn track_request_activity(&self) -> RequestActivity<'_> {
        self.memory_activity.track_handler()
    }

    #[cfg(test)]
    pub(super) fn active_handler_count(&self) -> usize {
        self.memory_activity.active_handlers.load(Ordering::Acquire)
    }

    pub fn available_entries(&self) -> usize {
        self.maximum_entries
            .saturating_sub(self.entries_used.load(Ordering::Acquire))
    }

    pub fn available_bytes(&self) -> usize {
        let used = self.byte_budget.used.lock().expect("byte budget poisoned");
        self.byte_budget.maximum.saturating_sub(*used)
    }

    pub fn shutdown(&self) -> thread::Result<()> {
        self.stop_admission();
        self.sender.lock().expect("writer sender poisoned").take();
        if let Some(worker) = self.thread.lock().expect("writer thread poisoned").take() {
            worker.join()
        } else {
            Ok(())
        }
    }

    fn reserve(&self, bytes: usize) -> Result<CapacityReservation, AdmissionError> {
        self.entries_used
            .try_update(Ordering::AcqRel, Ordering::Acquire, |used| {
                (used < self.maximum_entries).then_some(used + 1)
            })
            .map_err(|_| AdmissionError::CapacityExhausted)?;
        if bytes > self.byte_budget.maximum {
            self.entries_used.fetch_sub(1, Ordering::AcqRel);
            return Err(AdmissionError::PayloadTooLarge);
        }
        let mut used = self.byte_budget.used.lock().expect("byte budget poisoned");
        let Some(next) = used.checked_add(bytes) else {
            self.entries_used.fetch_sub(1, Ordering::AcqRel);
            return Err(AdmissionError::CapacityExhausted);
        };
        if next > self.byte_budget.maximum {
            self.entries_used.fetch_sub(1, Ordering::AcqRel);
            return Err(AdmissionError::CapacityExhausted);
        }
        *used = next;
        Ok(CapacityReservation {
            budget: Arc::clone(&self.byte_budget),
            bytes,
            entries_used: Arc::clone(&self.entries_used),
        })
    }
}

fn writer_timing_enabled() -> bool {
    writer_timing_enabled_value(std::env::var("PODLY_WRITER_TIMING_LOG").ok().as_deref())
}

fn writer_timing_enabled_value(value: Option<&str>) -> bool {
    value.is_some_and(|value| {
        matches!(
            value.trim().to_ascii_lowercase().as_str(),
            "1" | "true" | "yes" | "on"
        )
    })
}

fn operation_name(operation: &Operation) -> &'static str {
    match operation {
        Operation::Action { .. } => "action",
        Operation::Create { .. } => "create",
        Operation::Update { .. } => "update",
        Operation::Delete { .. } => "delete",
        Operation::Transaction { .. } => "transaction",
        Operation::Unsupported { .. } => "unsupported",
    }
}

fn action_name(operation: &Operation) -> Option<&str> {
    match operation {
        Operation::Action { action, .. } => Some(action.as_str()),
        _ => None,
    }
}

// Error messages can echo caller values, so only the stable code is logged.
fn unobserved_failure_json(command: &Command, error: &RpcError) -> String {
    serde_json::json!({
        "command_id": command.command_id,
        "operation": operation_name(&command.operation),
        "action": action_name(&command.operation),
        "code": error.code,
        "outcome": error.outcome,
    })
    .to_string()
}

fn writer_timing_json(
    command: &Command,
    queue_ms: f64,
    execution_ms: f64,
    success: bool,
) -> String {
    let total_ms = queue_ms + execution_ms;
    serde_json::json!({
        "command_id": command.command_id,
        "operation": operation_name(&command.operation),
        "action": action_name(&command.operation),
        "queue_ms": queue_ms,
        "execution_ms": execution_ms,
        "total_ms": total_ms,
        "success": success,
    })
    .to_string()
}

impl Drop for WriterExecutor {
    fn drop(&mut self) {
        self.stop_admission();
        self.sender.lock().expect("writer sender poisoned").take();
        if let Some(worker) = self.thread.lock().expect("writer thread poisoned").take() {
            let _ = worker.join();
        }
    }
}

fn execute_one(
    connection: &rusqlite::Connection,
    registry: &ActionRegistry,
    command: &Command,
) -> Result<Value, RpcError> {
    if let Some(result) = registry.execute_non_atomic(connection, &command.operation) {
        return result;
    }
    let transaction = connection
        .unchecked_transaction()
        .map_err(|_| error("database_error", "could not begin transaction"))?;
    let result = registry.execute(&transaction, &command.operation);
    match result {
        Ok(value) => transaction
            .commit()
            .map(|()| value)
            .map_err(|_| error("commit_failed", "database commit failed")),
        Err(error) => {
            let _ = transaction.rollback();
            Err(error)
        }
    }
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;
    use std::sync::{Arc, Barrier};

    use serde_json::{Map, Value};
    use tempfile::NamedTempFile;

    use super::*;
    use crate::writer::protocol::Operation;

    fn seed_revision(connection: &rusqlite::Connection) {
        connection
            .execute(
                "CREATE TABLE alembic_version(version_num TEXT NOT NULL)",
                [],
            )
            .unwrap();
        connection
            .execute(
                "INSERT INTO alembic_version(version_num) VALUES (?1)",
                [crate::writer::database::EXPECTED_SCHEMA_REVISION],
            )
            .unwrap();
    }

    #[test]
    fn timing_is_opt_in_and_payload_is_compatible_and_redacted() {
        assert!(!writer_timing_enabled_value(None));
        assert!(!writer_timing_enabled_value(Some("false")));
        assert!(writer_timing_enabled_value(Some(" YES ")));

        let command = Command {
            version: 1,
            command_id: "synthetic-command-id".to_owned(),
            wait: true,
            operation: Operation::Action {
                action: "update_user_last_active".to_owned(),
                params: Map::from_iter([(
                    "secret_value".to_owned(),
                    Value::String("must-not-be-logged".to_owned()),
                )]),
            },
        };
        let line = writer_timing_json(&command, 1.25, 2.5, true);
        let payload: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(payload["command_id"], "synthetic-command-id");
        assert_eq!(payload["operation"], "action");
        assert_eq!(payload["action"], "update_user_last_active");
        assert_eq!(payload["queue_ms"], 1.25);
        assert_eq!(payload["execution_ms"], 2.5);
        assert_eq!(payload["total_ms"], 3.75);
        assert_eq!(payload["success"], true);
        assert!(!line.contains("secret_value"));
        assert!(!line.contains("must-not-be-logged"));
    }

    #[test]
    fn unobserved_failure_log_is_redacted() {
        let command = Command {
            version: 1,
            command_id: "synthetic-async-id".to_owned(),
            wait: false,
            operation: Operation::Action {
                action: "touch_feed_access_token".to_owned(),
                params: Map::from_iter([(
                    "secret_value".to_owned(),
                    Value::String("must-not-be-logged".to_owned()),
                )]),
            },
        };
        let error = RpcError {
            code: "not_found".to_owned(),
            message: "token must-not-be-logged missing".to_owned(),
            retryable: false,
            outcome: "rolled_back",
        };
        let line = unobserved_failure_json(&command, &error);
        let payload: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(payload["command_id"], "synthetic-async-id");
        assert_eq!(payload["action"], "touch_feed_access_token");
        assert_eq!(payload["code"], "not_found");
        assert_eq!(payload["outcome"], "rolled_back");
        assert!(!line.contains("must-not-be-logged"));
        assert!(!line.contains("secret_value"));
    }

    fn command(action: &str, params: BTreeMap<&str, Value>, wait: bool) -> Command {
        Command {
            version: 1,
            command_id: action.to_owned(),
            wait,
            operation: Operation::Action {
                action: action.to_owned(),
                params: params
                    .into_iter()
                    .map(|(key, value)| (key.to_owned(), value))
                    .collect::<Map<_, _>>(),
            },
        }
    }

    #[test]
    fn executor_commits_on_one_owner_thread() {
        let file = NamedTempFile::new().unwrap();
        let connection = rusqlite::Connection::open(file.path()).unwrap();
        seed_revision(&connection);
        connection
            .execute("CREATE TABLE writer_test_events(value TEXT NOT NULL)", [])
            .unwrap();
        drop(connection);
        let executor =
            WriterExecutor::start(file.path(), 2, 1024, ActionRegistry::with_test_actions())
                .unwrap();

        let mut params = BTreeMap::new();
        params.insert("value", Value::String("committed".to_owned()));
        let reply = executor
            .submit(command("__test_insert", params, true), 100)
            .unwrap()
            .response
            .unwrap()
            .blocking_recv()
            .unwrap();
        assert!(reply.result.is_ok());
        executor.shutdown().unwrap();

        let connection = rusqlite::Connection::open(file.path()).unwrap();
        let count: i64 = connection
            .query_row("SELECT COUNT(*) FROM writer_test_events", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(count, 1);
    }

    #[test]
    fn aggregate_byte_budget_rejects_without_admission() {
        let file = NamedTempFile::new().unwrap();
        let connection = rusqlite::Connection::open(file.path()).unwrap();
        seed_revision(&connection);
        drop(connection);
        let executor =
            WriterExecutor::start(file.path(), 1, 5, ActionRegistry::with_test_actions()).unwrap();
        let result = executor.submit(command("__test_noop", BTreeMap::new(), false), 6);
        assert!(matches!(result, Err(AdmissionError::PayloadTooLarge)));
        executor.shutdown().unwrap();
    }

    #[test]
    fn transaction_failure_rolls_back_prior_subcommands() {
        let file = NamedTempFile::new().unwrap();
        let connection = rusqlite::Connection::open(file.path()).unwrap();
        seed_revision(&connection);
        connection
            .execute("CREATE TABLE writer_test_events(value TEXT NOT NULL)", [])
            .unwrap();
        drop(connection);
        let registry = ActionRegistry::with_test_actions();
        let executor = WriterExecutor::start(file.path(), 2, 1024, registry).unwrap();
        let insert = Operation::Action {
            action: "__test_insert".to_owned(),
            params: Map::from_iter([("value".to_owned(), Value::String("rolled-back".to_owned()))]),
        };
        let unsupported = Operation::Action {
            action: "not_registered".to_owned(),
            params: Map::new(),
        };
        let command = Command {
            version: 1,
            command_id: "transaction".to_owned(),
            wait: true,
            operation: Operation::Transaction {
                commands: vec![
                    crate::writer::protocol::TransactionCommand {
                        command_id: "first".to_owned(),
                        operation: insert,
                    },
                    crate::writer::protocol::TransactionCommand {
                        command_id: "second".to_owned(),
                        operation: unsupported,
                    },
                ],
            },
        };
        let reply = executor
            .submit(command, 100)
            .unwrap()
            .response
            .unwrap()
            .blocking_recv()
            .unwrap();
        assert!(reply.result.is_err());
        executor.shutdown().unwrap();

        let connection = rusqlite::Connection::open(file.path()).unwrap();
        let count: i64 = connection
            .query_row("SELECT COUNT(*) FROM writer_test_events", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(count, 0);
    }

    #[test]
    fn reachable_generic_update_preserves_numeric_shape_and_ignores_unknown_fields() {
        let file = NamedTempFile::new().unwrap();
        let connection = rusqlite::Connection::open(file.path()).unwrap();
        seed_revision(&connection);
        connection
            .execute(
                "CREATE TABLE post(
                    id INTEGER PRIMARY KEY,
                    duration INTEGER,
                    whitelisted BOOLEAN NOT NULL,
                    transcript_word_timestamps JSON,
                    processed_audio_path TEXT
                )",
                [],
            )
            .unwrap();
        connection
            .execute(
                "INSERT INTO post(id, duration, whitelisted) VALUES (1, 90, 0)",
                [],
            )
            .unwrap();
        drop(connection);
        let executor =
            WriterExecutor::start(file.path(), 4, 4096, ActionRegistry::default()).unwrap();
        let command = Command {
            version: 1,
            command_id: "generic-update".to_owned(),
            wait: true,
            operation: Operation::Update {
                model: "Post".to_owned(),
                id: Value::from(1),
                data: Map::from_iter([
                    ("duration".to_owned(), Value::from(90.5)),
                    ("whitelisted".to_owned(), Value::Bool(true)),
                    (
                        "transcript_word_timestamps".to_owned(),
                        serde_json::json!([{"word": "hello", "start": 0.0}]),
                    ),
                    ("unknown_legacy_field".to_owned(), Value::from("ignored")),
                ]),
            },
        };
        let result = executor
            .submit(command, 512)
            .unwrap()
            .response
            .unwrap()
            .blocking_recv()
            .unwrap();
        assert!(result.result.is_ok());
        let missing = Command {
            version: 1,
            command_id: "missing-update".to_owned(),
            wait: true,
            operation: Operation::Update {
                model: "Post".to_owned(),
                id: Value::from(999),
                data: Map::from_iter([("processed_audio_path".to_owned(), Value::Null)]),
            },
        };
        let missing = executor
            .submit(missing, 128)
            .unwrap()
            .response
            .unwrap()
            .blocking_recv()
            .unwrap();
        assert_eq!(missing.result.unwrap_err().code, "not_found");
        executor.shutdown().unwrap();

        let connection = rusqlite::Connection::open(file.path()).unwrap();
        let row = connection
            .query_row(
                "SELECT duration, typeof(duration), whitelisted, transcript_word_timestamps FROM post WHERE id=1",
                [],
                |row| {
                    Ok((
                        row.get::<_, f64>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, i64>(2)?,
                        row.get::<_, String>(3)?,
                    ))
                },
            )
            .unwrap();
        assert_eq!(row.0, 90.5);
        assert_eq!(row.1, "real");
        assert_eq!(row.2, 1);
        assert_eq!(
            serde_json::from_str::<Value>(&row.3).unwrap(),
            serde_json::json!([{"word": "hello", "start": 0.0}])
        );
    }

    #[test]
    fn concurrent_dequeue_requests_claim_exactly_one_job() {
        let file = NamedTempFile::new().unwrap();
        let connection = rusqlite::Connection::open(file.path()).unwrap();
        seed_revision(&connection);
        connection
            .execute(
                "CREATE TABLE processing_job(
                    id TEXT PRIMARY KEY,jobs_manager_run_id TEXT,post_guid TEXT NOT NULL,
                    status TEXT NOT NULL,started_at DATETIME,created_at DATETIME
                 )",
                [],
            )
            .unwrap();
        connection
            .execute(
                "INSERT INTO processing_job VALUES
                    ('job',NULL,'guid','pending',NULL,'2026-01-01')",
                [],
            )
            .unwrap();
        drop(connection);

        let executor = Arc::new(
            WriterExecutor::start(file.path(), 16, 16 * 1024, ActionRegistry::default()).unwrap(),
        );
        let barrier = Arc::new(Barrier::new(9));
        let workers = (0..8)
            .map(|index| {
                let executor = Arc::clone(&executor);
                let barrier = Arc::clone(&barrier);
                thread::spawn(move || {
                    barrier.wait();
                    let mut command = command("dequeue_job", BTreeMap::new(), true);
                    command.command_id = format!("dequeue-{index}");
                    executor
                        .submit(command, 128)
                        .unwrap()
                        .response
                        .unwrap()
                        .blocking_recv()
                        .unwrap()
                        .result
                        .unwrap()
                })
            })
            .collect::<Vec<_>>();
        barrier.wait();
        let results = workers
            .into_iter()
            .map(|worker| worker.join().unwrap())
            .collect::<Vec<_>>();
        assert_eq!(results.iter().filter(|result| !result.is_null()).count(), 1);
        assert_eq!(results.iter().filter(|result| result.is_null()).count(), 7);
        executor.shutdown().unwrap();

        let connection = rusqlite::Connection::open(file.path()).unwrap();
        let running: i64 = connection
            .query_row(
                "SELECT COUNT(*) FROM processing_job WHERE status='running'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(running, 1);
    }

    #[test]
    fn concurrent_cancel_and_completion_always_finish_cancelled() {
        let file = NamedTempFile::new().unwrap();
        let connection = rusqlite::Connection::open(file.path()).unwrap();
        seed_revision(&connection);
        connection
            .execute(
                "CREATE TABLE processing_job(
                    id TEXT PRIMARY KEY,jobs_manager_run_id TEXT,post_guid TEXT NOT NULL,
                    status TEXT NOT NULL,current_step INTEGER,step_name TEXT,total_steps INTEGER,
                    progress_percentage REAL,started_at DATETIME,completed_at DATETIME,
                    error_message TEXT,scheduler_job_id TEXT,created_at DATETIME,
                    requested_by_user_id INTEGER,billing_user_id INTEGER,stage_history JSON,
                    ad_windows_count INTEGER,had_classification_parse_error BOOLEAN NOT NULL,
                    auto_retry_attempted BOOLEAN NOT NULL
                 )",
                [],
            )
            .unwrap();
        connection
            .execute(
                "INSERT INTO processing_job VALUES (
                    'job',NULL,'guid','running',1,'Working',4,25.0,'2026-01-01',NULL,
                    NULL,NULL,'2026-01-01',NULL,NULL,'[]',NULL,0,0)",
                [],
            )
            .unwrap();
        drop(connection);

        let executor = Arc::new(
            WriterExecutor::start(file.path(), 4, 4096, ActionRegistry::default()).unwrap(),
        );
        let barrier = Arc::new(Barrier::new(3));
        let cancel = {
            let executor = Arc::clone(&executor);
            let barrier = Arc::clone(&barrier);
            thread::spawn(move || {
                barrier.wait();
                executor
                    .submit(
                        command(
                            "mark_cancelled",
                            BTreeMap::from([
                                ("job_id", Value::String("job".to_owned())),
                                ("reason", Value::String("Stopped".to_owned())),
                            ]),
                            true,
                        ),
                        256,
                    )
                    .unwrap()
                    .response
                    .unwrap()
                    .blocking_recv()
                    .unwrap()
            })
        };
        let complete = {
            let executor = Arc::clone(&executor);
            let barrier = Arc::clone(&barrier);
            thread::spawn(move || {
                barrier.wait();
                executor
                    .submit(
                        command(
                            "update_job_status",
                            BTreeMap::from([
                                ("job_id", Value::String("job".to_owned())),
                                ("status", Value::String("completed".to_owned())),
                                ("step", Value::from(4)),
                                ("step_name", Value::String("Completed".to_owned())),
                            ]),
                            true,
                        ),
                        256,
                    )
                    .unwrap()
                    .response
                    .unwrap()
                    .blocking_recv()
                    .unwrap()
            })
        };
        barrier.wait();
        assert!(cancel.join().unwrap().result.is_ok());
        assert!(complete.join().unwrap().result.is_ok());
        executor.shutdown().unwrap();

        let connection = rusqlite::Connection::open(file.path()).unwrap();
        let row: (String, String, String) = connection
            .query_row(
                "SELECT status,step_name,error_message FROM processing_job WHERE id='job'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        assert_eq!(
            row,
            ("cancelled".into(), "Stopped".into(), "Stopped".into())
        );
    }
}
