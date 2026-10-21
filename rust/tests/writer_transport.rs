use std::io::{BufRead, BufReader, Read, Write};
use std::net::{Shutdown, TcpStream};
use std::process::{Child, ChildStderr, Command, Stdio};
use std::thread;
use std::time::Duration;

use rusqlite::Connection;
use serde_json::{json, Value};
use tempfile::NamedTempFile;

const AUTHORIZATION: &str = "PodlyWriter c2VjcmV0";

struct Server {
    child: Child,
    _stderr: BufReader<ChildStderr>,
    port: u16,
}

impl Server {
    fn start(db: &NamedTempFile, queue_entries: usize, deadline_ms: u64) -> Self {
        let queue_entries = queue_entries.to_string();
        let deadline_ms = deadline_ms.to_string();
        let mut child = Command::new(env!("CARGO_BIN_EXE_podly_writer"))
            .args([
                "--db",
                db.path().to_str().unwrap(),
                "--port",
                "0",
                "--enable-test-actions",
                "--test-queue-entries",
                &queue_entries,
                "--test-request-deadline-ms",
                &deadline_ms,
            ])
            .env("PODLY_IPC_AUTHKEY", "secret")
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let mut stderr = BufReader::new(child.stderr.take().unwrap());
        let mut ready_line = String::new();
        stderr.read_line(&mut ready_line).unwrap();
        assert!(ready_line.starts_with("podly_writer listening on 127.0.0.1:"));
        let port = ready_line
            .trim()
            .rsplit_once(':')
            .unwrap()
            .1
            .parse()
            .unwrap();
        Self {
            child,
            _stderr: stderr,
            port,
        }
    }

    fn request(&self, path: &str, authorization: Option<&str>, body: &[u8]) -> (u16, Value) {
        request(self.port, path, authorization, body)
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn fixture_db() -> NamedTempFile {
    let file = NamedTempFile::new().unwrap();
    let connection = Connection::open(file.path()).unwrap();
    connection
        .execute(
            "CREATE TABLE alembic_version(version_num TEXT NOT NULL)",
            [],
        )
        .unwrap();
    connection
        .execute(
            "INSERT INTO alembic_version(version_num) VALUES (?1)",
            [podly_tools::writer::database::EXPECTED_SCHEMA_REVISION],
        )
        .unwrap();
    connection
        .execute("CREATE TABLE writer_test_events(value TEXT NOT NULL)", [])
        .unwrap();
    drop(connection);
    file
}

#[test]
fn readiness_probe_requires_the_actual_rust_executor() {
    let db = fixture_db();
    let mut server = Server::start(&db, 4, 1_000);
    let port = server.port.to_string();
    let probe = || {
        Command::new(env!("CARGO_BIN_EXE_podly_writer"))
            .args(["--probe", "--port", &port])
            .output()
            .unwrap()
    };
    assert!(probe().status.success());
    server.child.kill().unwrap();
    server.child.wait().unwrap();
    assert!(!probe().status.success());
}

fn command(command_id: &str, action: &str, wait: bool, params: Value) -> Vec<u8> {
    serde_json::to_vec(&json!({
        "version": 1,
        "command_id": command_id,
        "operation": "action",
        "action": action,
        "params": params,
        "wait": wait
    }))
    .unwrap()
}

fn insert_then_sleep(command_id: &str, value: &str, milliseconds: u64) -> Vec<u8> {
    serde_json::to_vec(&json!({
        "version": 1,
        "command_id": command_id,
        "operation": "transaction",
        "commands": [
            {
                "command_id": format!("{command_id}-insert"),
                "operation": "action",
                "action": "__test_insert",
                "params": {"value": value}
            },
            {
                "command_id": format!("{command_id}-sleep"),
                "operation": "action",
                "action": "__test_sleep",
                "params": {"milliseconds": milliseconds}
            }
        ],
        "wait": false
    }))
    .unwrap()
}

fn request(port: u16, path: &str, authorization: Option<&str>, body: &[u8]) -> (u16, Value) {
    let mut stream = TcpStream::connect(("127.0.0.1", port)).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let authorization = authorization
        .map(|value| format!("Authorization: {value}\r\n"))
        .unwrap_or_default();
    write!(
        stream,
        "POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n{authorization}Content-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    )
    .unwrap();
    let _ = stream.write_all(body);
    let mut response = Vec::new();
    stream.read_to_end(&mut response).unwrap();
    let split = response
        .windows(4)
        .position(|part| part == b"\r\n\r\n")
        .unwrap();
    let (headers, body) = response.split_at(split + 4);
    let status = std::str::from_utf8(headers)
        .unwrap()
        .lines()
        .next()
        .unwrap()
        .split_whitespace()
        .nth(1)
        .unwrap()
        .parse()
        .unwrap();
    (status, serde_json::from_slice(body).unwrap())
}

fn readiness(port: u16) -> (u16, Value) {
    let mut stream = TcpStream::connect(("127.0.0.1", port)).unwrap();
    stream
        .write_all(b"GET /v1/ready HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        .unwrap();
    let mut response = Vec::new();
    stream.read_to_end(&mut response).unwrap();
    let split = response
        .windows(4)
        .position(|part| part == b"\r\n\r\n")
        .unwrap();
    let (headers, body) = response.split_at(split + 4);
    let status = std::str::from_utf8(headers)
        .unwrap()
        .lines()
        .next()
        .unwrap()
        .split_whitespace()
        .nth(1)
        .unwrap()
        .parse()
        .unwrap();
    (status, serde_json::from_slice(body).unwrap())
}

#[test]
fn isolated_rust_transport_covers_admission_deadlines_and_reconnect() {
    let db = fixture_db();
    let server = Server::start(&db, 1, 50);

    let (status, body) = readiness(server.port);
    assert_eq!(status, 200);
    assert_eq!(body["backend"], "rust");
    assert_eq!(body["schema_revision"], "080b5181e23a");
    assert_eq!(body["ready"], true);

    // Two I/O workers, the main thread and the single SQLite owner. The
    // concurrent requests below must still succeed without a CPU-sized pool.
    #[cfg(target_os = "linux")]
    {
        let threads = std::fs::read_dir(format!("/proc/{}/task", server.child.id()))
            .unwrap()
            .count();
        assert_eq!(
            threads, 4,
            "writer runtime must retain its fixed thread bound"
        );
    }

    let (status, body) = server.request("/v1/commands", None, b"{}");
    assert_eq!(status, 401);
    assert_eq!(body["error"]["code"], "unauthorized");
    let (status, body) = server.request("/v1/commands", Some("PodlyWriter d3Jvbmc"), b"{}");
    assert_eq!(status, 403);
    assert_eq!(body["error"]["code"], "forbidden");

    let (status, body) = server.request("/v1/commands", Some(AUTHORIZATION), b"{");
    assert_eq!(status, 400);
    assert!(body.get("command_id").is_none());
    let duplicate = br#"{"version":1,"version":1,"command_id":"duplicate","operation":"action","action":"__test_noop","params":{},"wait":true}"#;
    assert_eq!(
        server
            .request("/v1/commands", Some(AUTHORIZATION), duplicate)
            .0,
        400
    );

    let mut wrong_version = command("wrong-version", "__test_noop", true, json!({}));
    let position = wrong_version.iter().position(|byte| *byte == b'1').unwrap();
    wrong_version[position] = b'2';
    assert_eq!(
        server
            .request("/v1/commands", Some(AUTHORIZATION), &wrong_version)
            .0,
        400
    );

    let unsupported = command("unsupported", "not_registered", true, json!({}));
    assert_eq!(
        server
            .request("/v1/commands", Some(AUTHORIZATION), &unsupported)
            .0,
        422
    );
    let connection = Connection::open(db.path()).unwrap();
    let count: i64 = connection
        .query_row("SELECT COUNT(*) FROM writer_test_events", [], |row| {
            row.get(0)
        })
        .unwrap();
    assert_eq!(count, 0);
    drop(connection);

    let oversized = vec![b' '; 16 * 1024 * 1024 + 1];
    assert_eq!(
        server
            .request("/v1/commands", Some(AUTHORIZATION), &oversized)
            .0,
        413
    );

    let slow = command(
        "slow-async",
        "__test_sleep",
        false,
        json!({"milliseconds": 300}),
    );
    assert_eq!(
        server.request("/v1/commands", Some(AUTHORIZATION), &slow).0,
        202
    );
    let saturated = command("saturated", "__test_noop", false, json!({}));
    assert_eq!(
        server
            .request("/v1/commands", Some(AUTHORIZATION), &saturated)
            .0,
        429
    );
    thread::sleep(Duration::from_millis(350));

    let deadline = command(
        "deadline",
        "__test_sleep",
        true,
        json!({"milliseconds": 150}),
    );
    let (status, body) = server.request("/v1/commands", Some(AUTHORIZATION), &deadline);
    assert_eq!(status, 504);
    assert_eq!(body["state"], "unknown");
    thread::sleep(Duration::from_millis(175));

    let dropped = command("dropped", "__test_sleep", true, json!({"milliseconds": 50}));
    let mut stream = TcpStream::connect(("127.0.0.1", server.port)).unwrap();
    write!(
        stream,
        "POST /v1/commands HTTP/1.1\r\nHost: localhost\r\nAuthorization: {AUTHORIZATION}\r\nContent-Length: {}\r\n\r\n",
        dropped.len()
    )
    .unwrap();
    stream.write_all(&dropped).unwrap();
    stream.shutdown(Shutdown::Both).unwrap();
    thread::sleep(Duration::from_millis(75));
    let healthy = command("after-drop", "__test_noop", true, json!({}));
    assert_eq!(
        server
            .request("/v1/commands", Some(AUTHORIZATION), &healthy)
            .0,
        200
    );

    drop(server);
    let restarted = Server::start(&db, 8, 500);
    let port = restarted.port;
    let slow_waiter = thread::spawn(move || {
        let payload = command(
            "slow-waiter",
            "__test_sleep",
            true,
            json!({"milliseconds": 150}),
        );
        request(port, "/v1/commands", Some(AUTHORIZATION), &payload)
    });
    thread::sleep(Duration::from_millis(20));
    let accepted_later = command("accepted-later", "__test_noop", false, json!({}));
    let (status, body) = restarted.request("/v1/commands", Some(AUTHORIZATION), &accepted_later);
    assert_eq!(status, 202);
    assert_eq!(body["command_id"], "accepted-later");
    let (status, body) = slow_waiter.join().unwrap();
    assert_eq!(status, 200);
    assert_eq!(body["command_id"], "slow-waiter");

    let handles = (0..8)
        .map(|index| {
            let port = restarted.port;
            thread::spawn(move || {
                let id = format!("correlation-{index}");
                let payload = command(&id, "__test_noop", true, json!({}));
                let (status, body) = request(port, "/v1/commands", Some(AUTHORIZATION), &payload);
                assert_eq!(status, 200);
                assert_eq!(body["command_id"], id);
            })
        })
        .collect::<Vec<_>>();
    for handle in handles {
        handle.join().unwrap();
    }
}

#[test]
fn sigterm_drains_but_sigkill_rolls_back_in_flight_work() {
    let graceful_db = fixture_db();
    let mut graceful = Server::start(&graceful_db, 8, 1_000);
    let payload = insert_then_sleep("graceful", "committed", 150);
    assert_eq!(
        graceful
            .request("/v1/commands", Some(AUTHORIZATION), &payload)
            .0,
        202
    );
    // SAFETY: the PID belongs to the child process owned by this test.
    let signal_result = unsafe { libc::kill(graceful.child.id() as i32, libc::SIGTERM) };
    assert_eq!(signal_result, 0);
    assert!(graceful.child.wait().unwrap().success());
    let connection = Connection::open(graceful_db.path()).unwrap();
    let count: i64 = connection
        .query_row("SELECT COUNT(*) FROM writer_test_events", [], |row| {
            row.get(0)
        })
        .unwrap();
    assert_eq!(count, 1);
    drop(connection);

    let abrupt_db = fixture_db();
    let mut abrupt = Server::start(&abrupt_db, 8, 2_000);
    let payload = insert_then_sleep("abrupt", "rolled-back", 1_000);
    assert_eq!(
        abrupt
            .request("/v1/commands", Some(AUTHORIZATION), &payload)
            .0,
        202
    );
    thread::sleep(Duration::from_millis(50));
    abrupt.child.kill().unwrap();
    abrupt.child.wait().unwrap();
    let connection = Connection::open(abrupt_db.path()).unwrap();
    let count: i64 = connection
        .query_row("SELECT COUNT(*) FROM writer_test_events", [], |row| {
            row.get(0)
        })
        .unwrap();
    assert_eq!(count, 0);
}
