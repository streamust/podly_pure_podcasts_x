use std::ffi::c_void;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::OnceLock;

type Mallctl = unsafe extern "C" fn(
    *const libc::c_char,
    *mut c_void,
    *mut libc::size_t,
    *mut c_void,
    libc::size_t,
) -> libc::c_int;

static MALLCTL: OnceLock<Option<Mallctl>> = OnceLock::new();
static MISSING_LOGGED: AtomicBool = AtomicBool::new(false);
static AVAILABLE_LOGGED: AtomicBool = AtomicBool::new(false);
static SUCCESS_LOGGED: AtomicBool = AtomicBool::new(false);
static FAILURE_LOGGED: AtomicBool = AtomicBool::new(false);

const DEFAULT_IDLE_TRIM_INTERVAL_SECONDS: i64 = 900;
// Keep Instant-based waits comfortably inside the platform's representable range;
// oversized positive settings are capped at one year rather than risking overflow.
const MAX_IDLE_TRIM_INTERVAL_SECONDS: i64 = 365 * 24 * 60 * 60;

pub(super) fn idle_trim_interval_seconds(value: Option<&str>) -> Option<u64> {
    let parsed = value
        .unwrap_or("900")
        .trim()
        .parse::<i64>()
        .unwrap_or(DEFAULT_IDLE_TRIM_INTERVAL_SECONDS);
    if parsed <= 0 {
        return None;
    }
    Some(parsed.min(MAX_IDLE_TRIM_INTERVAL_SECONDS) as u64)
}

// Matches the Python writer: every named action purges after its payload is
// released, except frequent timestamp touches that the idle tick covers.
const DEFERRED_TRIM_ACTIONS: [&str; 3] = [
    "dequeue_job",
    "touch_feed_access_token",
    "update_user_last_active",
];

pub(super) fn purge_after_action(action: &str) -> bool {
    !DEFERRED_TRIM_ACTIONS.contains(&action)
}

pub(super) fn memory_trim_enabled(value: Option<&str>) -> bool {
    match value {
        None => true,
        Some(value) => matches!(
            value.trim().to_ascii_lowercase().as_str(),
            "1" | "true" | "yes" | "on"
        ),
    }
}

fn resolve_mallctl() -> Option<Mallctl> {
    // jemalloc is optional: /etc/ld.so.preload may be removed at startup.
    let symbol = unsafe { libc::dlsym(libc::RTLD_DEFAULT, c"mallctl".as_ptr()) };
    if symbol.is_null() {
        None
    } else {
        Some(unsafe { std::mem::transmute::<*mut c_void, Mallctl>(symbol) })
    }
}

fn purge_all_arenas_with(mallctl: Option<Mallctl>) -> Result<(), MallctlUnavailable> {
    let Some(mallctl) = mallctl else {
        return Err(MallctlUnavailable::missing());
    };
    let result = unsafe {
        mallctl(
            c"arena.4096.purge".as_ptr(),
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            0,
        )
    };
    if result == 0 {
        Ok(())
    } else {
        Err(MallctlUnavailable::with_code(result))
    }
}

#[derive(Debug, PartialEq, Eq)]
struct MallctlUnavailable(Option<libc::c_int>);

impl MallctlUnavailable {
    const fn missing() -> Self {
        Self(None)
    }

    const fn with_code(code: libc::c_int) -> Self {
        Self(Some(code))
    }
}

pub(super) fn purge_all_arenas() {
    let mallctl = MALLCTL.get_or_init(resolve_mallctl);
    if mallctl.is_some() && !AVAILABLE_LOGGED.swap(true, Ordering::Relaxed) {
        eprintln!("[WRITER_MEMORY_TRIM] jemalloc_mallctl=available");
    }
    match purge_all_arenas_with(*mallctl) {
        Ok(()) => {
            if !SUCCESS_LOGGED.swap(true, Ordering::Relaxed) {
                eprintln!("[WRITER_MEMORY_TRIM] arena_purge=all rc=0");
            }
        }
        Err(error) if error.0.is_none() => {
            if !MISSING_LOGGED.swap(true, Ordering::Relaxed) {
                eprintln!("[WRITER_MEMORY_TRIM] jemalloc_mallctl=unavailable purge=skipped");
            }
        }
        Err(error) => {
            if !FAILURE_LOGGED.swap(true, Ordering::Relaxed) {
                eprintln!(
                    "[WRITER_MEMORY_TRIM] arena_purge=failed rc={}",
                    error.0.expect("nonzero mallctl result has a code")
                );
            }
        }
    }
}

pub(super) fn should_purge(
    active_handlers: usize,
    pending_work: usize,
    activity_count: usize,
    last_purge_activity_count: usize,
    quiet_seconds: u64,
    interval_seconds: u64,
) -> bool {
    active_handlers == 0
        && pending_work == 0
        && activity_count != last_purge_activity_count
        && quiet_seconds >= interval_seconds
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn idle_trim_interval_matches_integer_seconds_and_disable_rules() {
        assert_eq!(idle_trim_interval_seconds(None), Some(900));
        assert_eq!(idle_trim_interval_seconds(Some(" 2 ")), Some(2));
        assert_eq!(idle_trim_interval_seconds(Some("2.5")), Some(900));
        assert_eq!(idle_trim_interval_seconds(Some("NaN")), Some(900));
        assert_eq!(idle_trim_interval_seconds(Some("inf")), Some(900));
        assert_eq!(idle_trim_interval_seconds(Some("invalid")), Some(900));
        assert_eq!(idle_trim_interval_seconds(Some("0")), None);
        assert_eq!(idle_trim_interval_seconds(Some("-2")), None);
    }

    #[test]
    fn global_memory_trim_flag_matches_python_boolean_rules() {
        assert!(memory_trim_enabled(None));
        for value in ["1", " true ", "YES", "On"] {
            assert!(memory_trim_enabled(Some(value)));
        }
        for value in ["0", "false", "off", "invalid"] {
            assert!(!memory_trim_enabled(Some(value)));
        }
    }

    #[test]
    fn named_actions_purge_except_deferred_timestamp_touches() {
        for action in [
            "replace_transcription",
            "insert_transcript_segments",
            "create_user",
        ] {
            assert!(purge_after_action(action));
        }
        for action in DEFERRED_TRIM_ACTIONS {
            assert!(!purge_after_action(action));
        }
    }

    #[test]
    fn huge_positive_idle_interval_is_bounded() {
        assert_eq!(
            idle_trim_interval_seconds(Some("9223372036854775807")),
            Some(MAX_IDLE_TRIM_INTERVAL_SECONDS as u64)
        );
    }

    unsafe extern "C" fn fake_mallctl(
        name: *const libc::c_char,
        old_value: *mut c_void,
        old_len: *mut libc::size_t,
        new_value: *mut c_void,
        new_len: libc::size_t,
    ) -> libc::c_int {
        assert_eq!(
            unsafe { std::ffi::CStr::from_ptr(name) }.to_bytes(),
            b"arena.4096.purge"
        );
        assert!(old_value.is_null());
        assert!(old_len.is_null());
        assert!(new_value.is_null());
        assert_eq!(new_len, 0);
        0
    }

    unsafe extern "C" fn failing_mallctl(
        _name: *const libc::c_char,
        _old_value: *mut c_void,
        _old_len: *mut libc::size_t,
        _new_value: *mut c_void,
        _new_len: libc::size_t,
    ) -> libc::c_int {
        17
    }

    #[test]
    fn optional_mallctl_is_nonfatal_and_uses_all_arena_key() {
        assert_eq!(
            purge_all_arenas_with(None),
            Err(MallctlUnavailable::missing())
        );
        assert_eq!(purge_all_arenas_with(Some(fake_mallctl)), Ok(()));
        assert_eq!(
            purge_all_arenas_with(Some(failing_mallctl)),
            Err(MallctlUnavailable(Some(17)))
        );
    }

    #[test]
    fn idle_purge_requires_new_quiet_fully_quiescent_activity() {
        assert!(!should_purge(0, 0, 1, 0, 89, 90));
        assert!(!should_purge(1, 0, 1, 0, 90, 90));
        assert!(!should_purge(0, 1, 1, 0, 90, 90));
        assert!(!should_purge(0, 0, 1, 1, 900, 90));
        assert!(should_purge(0, 0, 2, 1, 90, 90));
    }
}
