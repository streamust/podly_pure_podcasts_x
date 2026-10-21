use std::sync::Arc;
use std::task::{Context as TaskContext, Poll};
use std::time::Duration;
use std::{future::Future, future::IntoFuture, pin::Pin};

use anyhow::Context;
use axum::body::{to_bytes, Body};
use axum::extract::State;
use axum::http::header::{AUTHORIZATION, WWW_AUTHENTICATE};
use axum::http::{HeaderValue, Request, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use serde_json::json;
use subtle::ConstantTimeEq;
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{OwnedSemaphorePermit, Semaphore};
use tower::limit::ConcurrencyLimitLayer;

use super::actions::ActionRegistry;
use super::config::WriterConfig;
use super::executor::{AdmissionError, WriterExecutor};
use super::lifecycle::{Lifecycle, LifecycleState};
use super::protocol::{decode_command, CommandResponse, RpcError, PROTOCOL_VERSION};

const BODY_LIMIT: usize = 16 * 1024 * 1024;
const HEADER_READ_TIMEOUT: Duration = Duration::from_secs(5);
const BODY_READ_TIMEOUT: Duration = Duration::from_secs(30);
const MAX_CONCURRENT_REQUESTS: usize = 64;

#[derive(Clone)]
struct AppState {
    auth_key: Arc<[u8]>,
    executor: Arc<WriterExecutor>,
    registry: ActionRegistry,
    request_deadline: Duration,
    lifecycle: Arc<LifecycleState>,
    registry_complete: bool,
}

pub async fn run(config: WriterConfig) -> anyhow::Result<()> {
    let registry = if config.enable_test_actions {
        ActionRegistry::with_test_actions()
    } else {
        ActionRegistry::default()
    };
    let executor = Arc::new(if config.enable_test_foreign_keys {
        WriterExecutor::start_with_test_foreign_keys(
            &config.db_path,
            config.queue_entries,
            config.queue_bytes,
            registry.clone(),
        )?
    } else {
        WriterExecutor::start(
            &config.db_path,
            config.queue_entries,
            config.queue_bytes,
            registry.clone(),
        )?
    });
    let lifecycle = Arc::new(LifecycleState::new());
    lifecycle.set(Lifecycle::Accepting);
    let registry_complete = config.enable_test_actions || registry.production_complete();
    let state = AppState {
        auth_key: Arc::from(config.auth_key),
        executor,
        registry,
        request_deadline: config.request_deadline,
        lifecycle,
        registry_complete,
    };
    let app = Router::new()
        .route("/v1/commands", post(commands))
        .route("/v1/ready", get(readiness))
        .layer(ConcurrencyLimitLayer::new(MAX_CONCURRENT_REQUESTS))
        .with_state(state.clone());
    let listener = TcpListener::bind(config.bind)
        .await
        .with_context(|| format!("failed to bind writer to {}", config.bind))?;
    eprintln!("podly_writer listening on {}", listener.local_addr()?);
    let (draining_tx, draining_rx) = tokio::sync::oneshot::channel();
    let shutdown_lifecycle = Arc::clone(&state.lifecycle);
    let shutdown_executor = Arc::clone(&state.executor);
    let server = axum::serve(LimitedListener::new(listener, MAX_CONCURRENT_REQUESTS), app)
        .with_graceful_shutdown(async move {
            wait_for_sigterm().await;
            shutdown_lifecycle.set(Lifecycle::Draining);
            shutdown_executor.stop_admission();
            let _ = draining_tx.send(());
        })
        .into_future();
    tokio::pin!(server);
    let signaled = tokio::select! {
        result = &mut server => {
            result.context("writer server failed")?;
            false
        },
        _ = draining_rx => {
            let drain_started = tokio::time::Instant::now();
            if tokio::time::timeout(config.shutdown_grace, &mut server).await.is_err() {
                eprintln!("writer shutdown grace exceeded; forcing process exit");
                std::process::exit(1);
            }
            let remaining = config
                .shutdown_grace
                .saturating_sub(drain_started.elapsed());
            let executor = Arc::clone(&state.executor);
            let joined = tokio::task::spawn_blocking(move || executor.shutdown());
            if !matches!(tokio::time::timeout(remaining, joined).await, Ok(Ok(Ok(())))) {
                eprintln!("writer shutdown grace exceeded; forcing process exit");
                std::process::exit(1);
            }
            true
        }
    };
    if !signaled {
        state
            .executor
            .shutdown()
            .map_err(|_| anyhow::anyhow!("writer executor thread panicked"))?;
    }
    Ok(())
}

#[cfg(unix)]
async fn wait_for_sigterm() {
    let mut signal = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        .expect("SIGTERM handler must install");
    signal.recv().await;
}

#[cfg(not(unix))]
async fn wait_for_sigterm() {
    let _ = tokio::signal::ctrl_c().await;
}

struct LimitedListener {
    inner: TcpListener,
    permits: Arc<Semaphore>,
}

impl LimitedListener {
    fn new(inner: TcpListener, maximum: usize) -> Self {
        Self {
            inner,
            permits: Arc::new(Semaphore::new(maximum)),
        }
    }
}

impl axum::serve::Listener for LimitedListener {
    type Io = LimitedStream;
    type Addr = std::net::SocketAddr;

    async fn accept(&mut self) -> (Self::Io, Self::Addr) {
        loop {
            let permit = Arc::clone(&self.permits)
                .acquire_owned()
                .await
                .expect("connection semaphore is never closed");
            match self.inner.accept().await {
                Ok((inner, address)) => {
                    return (
                        LimitedStream {
                            inner,
                            _permit: permit,
                            header_deadline: Box::pin(tokio::time::sleep(HEADER_READ_TIMEOUT)),
                            header_tail: Vec::with_capacity(4),
                            first_header_complete: false,
                        },
                        address,
                    );
                }
                Err(_) => tokio::time::sleep(Duration::from_secs(1)).await,
            }
        }
    }

    fn local_addr(&self) -> std::io::Result<Self::Addr> {
        self.inner.local_addr()
    }
}

struct LimitedStream {
    inner: TcpStream,
    _permit: OwnedSemaphorePermit,
    header_deadline: Pin<Box<tokio::time::Sleep>>,
    header_tail: Vec<u8>,
    first_header_complete: bool,
}

impl AsyncRead for LimitedStream {
    fn poll_read(
        mut self: Pin<&mut Self>,
        context: &mut TaskContext<'_>,
        buffer: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        let this = self.as_mut().get_mut();
        if !this.first_header_complete && this.header_deadline.as_mut().poll(context).is_ready() {
            return Poll::Ready(Err(std::io::Error::new(
                std::io::ErrorKind::TimedOut,
                "HTTP header read deadline exceeded",
            )));
        }
        let prior_length = buffer.filled().len();
        match Pin::new(&mut this.inner).poll_read(context, buffer) {
            Poll::Ready(Ok(())) => {
                if !this.first_header_complete {
                    for byte in &buffer.filled()[prior_length..] {
                        this.header_tail.push(*byte);
                        if this.header_tail.len() > 4 {
                            this.header_tail.remove(0);
                        }
                        if this.header_tail == b"\r\n\r\n" {
                            this.first_header_complete = true;
                            break;
                        }
                    }
                }
                Poll::Ready(Ok(()))
            }
            other => other,
        }
    }
}

impl AsyncWrite for LimitedStream {
    fn poll_write(
        mut self: Pin<&mut Self>,
        context: &mut TaskContext<'_>,
        buffer: &[u8],
    ) -> Poll<std::io::Result<usize>> {
        Pin::new(&mut self.inner).poll_write(context, buffer)
    }

    fn poll_flush(
        mut self: Pin<&mut Self>,
        context: &mut TaskContext<'_>,
    ) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.inner).poll_flush(context)
    }

    fn poll_shutdown(
        mut self: Pin<&mut Self>,
        context: &mut TaskContext<'_>,
    ) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.inner).poll_shutdown(context)
    }
}

async fn readiness(State(state): State<AppState>) -> Response {
    let executor_ready = state.executor.is_accepting();
    let ready =
        state.registry_complete && executor_ready && state.lifecycle.get() == Lifecycle::Accepting;
    let status = if ready {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    };
    (
        status,
        Json(json!({
            "version": PROTOCOL_VERSION,
            "backend": "rust",
            "schema_revision": super::database::EXPECTED_SCHEMA_REVISION,
            "ready": ready,
            "executor_ready": executor_ready,
            "queue_capacity": {
                "entries_available": state.executor.available_entries(),
                "bytes_available": state.executor.available_bytes()
            },
            "lifecycle": state.lifecycle.get().as_str()
        })),
    )
        .into_response()
}

async fn commands(State(state): State<AppState>, request: Request<Body>) -> Response {
    let _request_activity = state.executor.track_request_activity();
    if let Err(error) = authenticate(request.headers().get(AUTHORIZATION), &state.auth_key) {
        return error.into_response();
    }
    if !state.registry_complete || state.lifecycle.get() != Lifecycle::Accepting {
        return rejected(
            StatusCode::SERVICE_UNAVAILABLE,
            None,
            "not_ready",
            "writer is not ready",
            true,
        );
    }
    let body =
        match tokio::time::timeout(BODY_READ_TIMEOUT, to_bytes(request.into_body(), BODY_LIMIT))
            .await
        {
            Ok(Ok(body)) => body,
            Ok(Err(_)) => {
                return rejected(
                    StatusCode::PAYLOAD_TOO_LARGE,
                    None,
                    "payload_too_large",
                    "request body exceeds the configured limit",
                    false,
                );
            }
            Err(_) => {
                return rejected(
                    StatusCode::BAD_REQUEST,
                    None,
                    "malformed",
                    "request body deadline exceeded",
                    true,
                );
            }
        };
    let command = match decode_command(&body) {
        Ok(command) => command,
        Err(_) => {
            return rejected(
                StatusCode::BAD_REQUEST,
                None,
                "malformed",
                "request body is not a valid command",
                false,
            );
        }
    };
    let command_id = command.command_id.clone();
    if command.version != PROTOCOL_VERSION {
        return rejected(
            StatusCode::BAD_REQUEST,
            Some(command_id),
            "unsupported_version",
            "protocol version is unsupported",
            false,
        );
    }
    if command.command_id.is_empty() || command.command_id.chars().count() > 128 {
        return rejected(
            StatusCode::BAD_REQUEST,
            None,
            "malformed",
            "command_id is invalid",
            false,
        );
    }
    if let Err(error) = state.registry.validate(&command.operation) {
        return rejected_error(StatusCode::UNPROCESSABLE_ENTITY, Some(command_id), error);
    }
    let wait = command.wait;
    let admission = match state.executor.submit(command, body.len()) {
        Ok(admission) => admission,
        Err(AdmissionError::CapacityExhausted) => {
            return rejected(
                StatusCode::TOO_MANY_REQUESTS,
                Some(command_id),
                "capacity_exhausted",
                "writer capacity exhausted",
                true,
            );
        }
        Err(AdmissionError::PayloadTooLarge) => {
            return rejected(
                StatusCode::PAYLOAD_TOO_LARGE,
                Some(command_id),
                "payload_too_large",
                "request exceeds writer byte capacity",
                false,
            );
        }
        Err(AdmissionError::NotReady) => {
            return rejected(
                StatusCode::SERVICE_UNAVAILABLE,
                Some(command_id),
                "not_ready",
                "writer is not accepting commands",
                true,
            );
        }
    };
    if !wait {
        return (
            StatusCode::ACCEPTED,
            Json(CommandResponse::Accepted {
                version: PROTOCOL_VERSION,
                command_id,
                admitted: true,
            }),
        )
            .into_response();
    }
    let Some(receiver) = admission.response else {
        return unknown(command_id);
    };
    match tokio::time::timeout(state.request_deadline, receiver).await {
        Ok(Ok(execution)) => match execution.result {
            Ok(result) => Json(CommandResponse::Completed {
                version: PROTOCOL_VERSION,
                command_id: execution.command_id,
                success: true,
                result: Some(result),
                error: None,
            })
            .into_response(),
            Err(error) => Json(CommandResponse::Completed {
                version: PROTOCOL_VERSION,
                command_id: execution.command_id,
                success: false,
                result: None,
                error: Some(error),
            })
            .into_response(),
        },
        Ok(Err(_)) | Err(_) => unknown(command_id),
    }
}

#[derive(Debug, Clone, Copy)]
enum AuthFailure {
    Unauthorized,
    Forbidden,
}

impl IntoResponse for AuthFailure {
    fn into_response(self) -> Response {
        match self {
            Self::Unauthorized => unauthorized(),
            Self::Forbidden => rejected(
                StatusCode::FORBIDDEN,
                None,
                "forbidden",
                "authorization rejected",
                false,
            ),
        }
    }
}

fn authenticate(header: Option<&HeaderValue>, expected: &[u8]) -> Result<(), AuthFailure> {
    let Some(header) = header.and_then(|header| header.to_str().ok()) else {
        return Err(AuthFailure::Unauthorized);
    };
    let Some(token) = header.strip_prefix("PodlyWriter ") else {
        return Err(AuthFailure::Unauthorized);
    };
    let decoded = URL_SAFE_NO_PAD
        .decode(token)
        .map_err(|_| AuthFailure::Unauthorized)?;
    if !bool::from(decoded.as_slice().ct_eq(expected)) {
        return Err(AuthFailure::Forbidden);
    }
    Ok(())
}

fn unauthorized() -> Response {
    let mut response = rejected(
        StatusCode::UNAUTHORIZED,
        None,
        "unauthorized",
        "authorization required",
        false,
    );
    response
        .headers_mut()
        .insert(WWW_AUTHENTICATE, HeaderValue::from_static("PodlyWriter"));
    response
}

fn rejected(
    status: StatusCode,
    command_id: Option<String>,
    code: &str,
    message: &str,
    retryable: bool,
) -> Response {
    rejected_error(
        status,
        command_id,
        RpcError {
            code: code.to_owned(),
            message: message.to_owned(),
            retryable,
            outcome: "not_admitted",
        },
    )
}

fn rejected_error(status: StatusCode, command_id: Option<String>, mut error: RpcError) -> Response {
    error.outcome = "not_admitted";
    (
        status,
        Json(CommandResponse::Rejected {
            version: PROTOCOL_VERSION,
            command_id,
            admitted: false,
            error,
        }),
    )
        .into_response()
}

fn unknown(command_id: String) -> Response {
    (
        StatusCode::GATEWAY_TIMEOUT,
        Json(CommandResponse::Unknown {
            version: PROTOCOL_VERSION,
            command_id,
            admitted: true,
            error: RpcError {
                code: "deadline_exceeded".to_owned(),
                message: "deadline exceeded after admission".to_owned(),
                retryable: false,
                outcome: "unknown",
            },
        }),
    )
        .into_response()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    use tempfile::NamedTempFile;

    #[test]
    fn auth_distinguishes_malformed_from_wrong_secret() {
        assert_eq!(
            authenticate(None, b"secret")
                .unwrap_err()
                .into_response()
                .status(),
            StatusCode::UNAUTHORIZED
        );
        let wrong = HeaderValue::from_static("PodlyWriter d3Jvbmc");
        assert_eq!(
            authenticate(Some(&wrong), b"secret")
                .unwrap_err()
                .into_response()
                .status(),
            StatusCode::FORBIDDEN
        );
        let correct = HeaderValue::from_static("PodlyWriter c2VjcmV0");
        assert!(authenticate(Some(&correct), b"secret").is_ok());
    }

    #[tokio::test]
    async fn request_activity_guard_covers_command_execution_and_response_conversion() {
        let file = NamedTempFile::new().unwrap();
        let connection = rusqlite::Connection::open(file.path()).unwrap();
        connection
            .execute(
                "CREATE TABLE alembic_version(version_num TEXT NOT NULL)",
                [],
            )
            .unwrap();
        connection
            .execute(
                "INSERT INTO alembic_version(version_num) VALUES (?1)",
                [super::super::database::EXPECTED_SCHEMA_REVISION],
            )
            .unwrap();
        drop(connection);

        let registry = ActionRegistry::with_test_actions();
        let executor =
            Arc::new(WriterExecutor::start(file.path(), 4, 4096, registry.clone()).unwrap());
        let lifecycle = Arc::new(LifecycleState::new());
        lifecycle.set(Lifecycle::Accepting);
        let state = AppState {
            auth_key: Arc::from(b"secret".to_vec()),
            executor: Arc::clone(&executor),
            registry,
            request_deadline: Duration::from_secs(5),
            lifecycle,
            registry_complete: true,
        };
        let payload = serde_json::json!({
            "version": 1,
            "command_id": "trim-guard-test",
            "operation": "action",
            "action": "__test_sleep",
            "params": {"milliseconds": 250},
            "wait": true
        });
        let authorization = format!("PodlyWriter {}", URL_SAFE_NO_PAD.encode(b"secret"));
        let request = Request::builder()
            .header(AUTHORIZATION, authorization)
            .body(Body::from(payload.to_string()))
            .unwrap();

        let response_task = tokio::spawn(async move { commands(State(state), request).await });
        tokio::time::timeout(Duration::from_secs(2), async {
            while executor.active_handler_count() == 0 {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("command handler should become active");
        assert_eq!(executor.active_handler_count(), 1);

        let response = response_task.await.unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(executor.active_handler_count(), 0);
        executor.shutdown().unwrap();
    }
}
