//! Proposed process-local monotonic facts, not an enabled host entry contract.
//! No wall-clock substitution, rate window, request allowance or expiry policy.
use crate::{Result, frame, identity};
use std::time::Instant;

/// One origin and random domain for one admitted process lifetime. Construction
/// is trusted host bootstrap, never a guest/reset command or a per-request step.
/// The domain is only a temporary clock identity, not an operation identity,
/// persisted authority, trusted wall time or a cross-process clock guarantee.
pub struct MonotonicFacts {
    origin: Instant,
    domain: String,
    owner: u32,
    high: u64,
}

impl MonotonicFacts {
    pub fn new() -> Result<Self> {
        Ok(Self {
            origin: Instant::now(),
            domain: identity()?,
            owner: std::process::id(),
            high: 0,
        })
    }

    /// MC1 fields: opaque 256-bit lower-hex clock domain, elapsed nanoseconds.
    /// A sample is an observation, not permission or a completion-time promise.
    /// The embedding host must retain this instance alongside its temporary cells
    /// and bind this exact observation into the admitted application's envelope.
    pub fn observe(&mut self) -> Result<String> {
        self.observe_at(Instant::now())
    }

    fn observe_at(&mut self, at: Instant) -> Result<String> {
        if self.owner != std::process::id() {
            return Err("capability");
        }
        let elapsed = at.checked_duration_since(self.origin).ok_or("clock")?;
        self.record_elapsed(elapsed)
    }

    fn record_elapsed(&mut self, elapsed: std::time::Duration) -> Result<String> {
        let nanos = elapsed.as_nanos();
        if nanos > i64::MAX as u128 || nanos < self.high as u128 {
            return Err("clock");
        }
        let seen = frame("MC1\n", &[&self.domain, &nanos.to_string()])?;
        self.high = nanos as u64;
        Ok(seen)
    }
}

#[cfg(test)]
#[path = "monotonic_facts_tests.rs"]
mod tests;
