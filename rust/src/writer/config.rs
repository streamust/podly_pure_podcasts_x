use std::net::{IpAddr, Ipv4Addr, SocketAddr};
use std::path::PathBuf;
use std::time::Duration;

use anyhow::{bail, Context, Result};
use clap::Parser;

pub const DEFAULT_PORT: u16 = 50_001;

#[derive(Debug, Parser)]
#[command(name = "podly_writer", about = "Podly single-owner SQLite writer")]
pub struct WriterArgs {
    /// Check a running Rust writer's authenticated-free readiness endpoint.
    #[arg(long, default_value_t = false)]
    pub probe: bool,

    #[arg(long)]
    pub db: Option<PathBuf>,

    #[arg(long, default_value_t = DEFAULT_PORT)]
    pub port: u16,

    #[arg(long, hide = true, default_value_t = false)]
    pub enable_test_actions: bool,

    /// Enable SQLite foreign keys only for isolated transaction parity tests.
    #[arg(long, hide = true, default_value_t = false)]
    pub enable_test_foreign_keys: bool,

    #[arg(long, hide = true)]
    pub test_queue_entries: Option<usize>,

    #[arg(long, hide = true)]
    pub test_queue_bytes: Option<usize>,

    #[arg(long, hide = true)]
    pub test_request_deadline_ms: Option<u64>,
}

#[derive(Debug, Clone)]
pub struct WriterConfig {
    pub bind: SocketAddr,
    pub db_path: PathBuf,
    pub auth_key: Vec<u8>,
    pub queue_entries: usize,
    pub queue_bytes: usize,
    pub shutdown_grace: Duration,
    pub request_deadline: Duration,
    pub enable_test_actions: bool,
    pub enable_test_foreign_keys: bool,
}

impl WriterConfig {
    pub fn from_args(args: WriterArgs) -> Result<Self> {
        if args.enable_test_foreign_keys && !args.enable_test_actions {
            bail!("--enable-test-foreign-keys requires --enable-test-actions");
        }
        let db_path = match args.db {
            Some(path) => path,
            None => {
                let instance = std::env::var_os("PODLY_INSTANCE_DIR")
                    .map(PathBuf::from)
                    .unwrap_or_else(|| PathBuf::from("/app/src/instance"));
                instance.join("sqlite3.db")
            }
        };
        let auth_key = std::env::var("PODLY_IPC_AUTHKEY")
            .context("PODLY_IPC_AUTHKEY must be set in Rust writer mode")?
            .into_bytes();
        if auth_key.is_empty() {
            bail!("PODLY_IPC_AUTHKEY must be nonempty in Rust writer mode");
        }

        Ok(Self {
            bind: SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), args.port),
            db_path,
            auth_key,
            queue_entries: args.test_queue_entries.unwrap_or(128),
            queue_bytes: args.test_queue_bytes.unwrap_or(64 * 1024 * 1024),
            shutdown_grace: Duration::from_secs(30),
            request_deadline: args
                .test_request_deadline_ms
                .map(Duration::from_millis)
                .unwrap_or_else(|| {
                    std::env::var("PODLY_WRITER_TIMEOUT_SECONDS")
                        .ok()
                        .and_then(|value| value.parse::<u64>().ok())
                        .filter(|seconds| *seconds > 0)
                        .map(Duration::from_secs)
                        .unwrap_or(Duration::from_secs(30))
                }),
            enable_test_actions: args.enable_test_actions,
            enable_test_foreign_keys: args.enable_test_foreign_keys,
        })
    }
}

#[cfg(test)]
mod tests {
    use clap::Parser;

    use super::{WriterArgs, WriterConfig};

    #[test]
    fn deferred_foreign_key_override_requires_test_action_mode() {
        let args =
            WriterArgs::try_parse_from(["podly_writer", "--enable-test-foreign-keys"]).unwrap();

        let error = WriterConfig::from_args(args).unwrap_err();
        assert!(error.to_string().contains("requires --enable-test-actions"));
    }
}
