//! Non-secret presentation and clock observations. No API authorization policy.
use crate::Result;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Presentation {
    Other,
    ExactBearer,
}

impl Presentation {
    pub(crate) fn observe(authorization: Option<&str>) -> Self {
        if authorization.is_some_and(|value| value.starts_with("Bearer ")) {
            Self::ExactBearer
        } else {
            Self::Other
        }
    }

    pub(crate) fn wire(self) -> &'static str {
        match self {
            Self::Other => "0",
            Self::ExactBearer => "1",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct ClockSample {
    pub(crate) seconds: u64,
    has_fraction: bool,
}

impl ClockSample {
    fn from_elapsed(elapsed: Duration) -> Self {
        Self {
            seconds: elapsed.as_secs(),
            has_fraction: elapsed.subsec_nanos() != 0,
        }
    }

    pub(crate) fn observe() -> Result<Self> {
        Self::from_time(SystemTime::now())
    }

    fn from_time(time: SystemTime) -> Result<Self> {
        time.duration_since(UNIX_EPOCH)
            .map(Self::from_elapsed)
            .map_err(|_| "clock")
    }

    pub(crate) fn fraction_wire(self) -> &'static str {
        if self.has_fraction { "1" } else { "0" }
    }
}

#[cfg(test)]
mod tests;
