use std::sync::atomic::{AtomicU8, Ordering};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Lifecycle {
    Starting = 0,
    Accepting = 1,
    Draining = 2,
}

impl Lifecycle {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Starting => "starting",
            Self::Accepting => "accepting",
            Self::Draining => "draining",
        }
    }
}

#[derive(Debug)]
pub struct LifecycleState(AtomicU8);

impl LifecycleState {
    pub fn new() -> Self {
        Self(AtomicU8::new(Lifecycle::Starting as u8))
    }

    pub fn set(&self, lifecycle: Lifecycle) {
        self.0.store(lifecycle as u8, Ordering::Release);
    }

    pub fn get(&self) -> Lifecycle {
        match self.0.load(Ordering::Acquire) {
            1 => Lifecycle::Accepting,
            2 => Lifecycle::Draining,
            _ => Lifecycle::Starting,
        }
    }
}

impl Default for LifecycleState {
    fn default() -> Self {
        Self::new()
    }
}
