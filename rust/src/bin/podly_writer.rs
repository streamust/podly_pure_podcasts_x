use std::io::{Read, Write};
use std::net::{Ipv4Addr, SocketAddr, TcpStream};
use std::time::Duration;

use anyhow::{bail, Context, Result};
use clap::Parser;
use podly_tools::writer::config::{WriterArgs, WriterConfig};

// The writer's SQLite work is handled by one dedicated executor thread. Keep
// Tokio's separate I/O runtime small instead of sizing it to every visible CPU.
#[tokio::main(worker_threads = 2)]
async fn main() -> Result<()> {
    let args = WriterArgs::parse();
    if args.probe {
        return probe(args.port);
    }
    let config = WriterConfig::from_args(args)?;
    podly_tools::writer::transport::run(config).await
}

fn probe(port: u16) -> Result<()> {
    let address = SocketAddr::from((Ipv4Addr::LOCALHOST, port));
    let mut socket = TcpStream::connect_timeout(&address, Duration::from_secs(2))
        .context("Rust writer readiness connection failed")?;
    socket.set_read_timeout(Some(Duration::from_secs(2)))?;
    socket.set_write_timeout(Some(Duration::from_secs(2)))?;
    socket.write_all(b"GET /v1/ready HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")?;
    let mut response = Vec::new();
    socket.take(8 * 1024).read_to_end(&mut response)?;
    let Some(split) = response.windows(4).position(|part| part == b"\r\n\r\n") else {
        bail!("Rust writer readiness response is malformed");
    };
    let (headers, body) = response.split_at(split + 4);
    let status = std::str::from_utf8(headers)?
        .lines()
        .next()
        .context("Rust writer readiness status is missing")?;
    if !status.contains(" 200 ") {
        bail!("Rust writer is not ready: {status}");
    }
    let ready: serde_json::Value = serde_json::from_slice(body)?;
    if ready["version"] != 1
        || ready["backend"] != "rust"
        || ready["ready"] != true
        || ready["executor_ready"] != true
        || ready["lifecycle"] != "accepting"
        || ready["schema_revision"] != podly_tools::writer::database::EXPECTED_SCHEMA_REVISION
    {
        bail!("Rust writer readiness identity or executor state mismatch");
    }
    Ok(())
}
