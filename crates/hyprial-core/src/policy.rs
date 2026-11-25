//! Restart policy profiles (spec section 3.1).
//!
//! The adapter passes the policy table as data; this module validates it and
//! hands out immutable [`RestartPolicy`] values.

use std::collections::HashMap;
use std::fmt;
use std::time::Duration;

/// One validated policy profile:
/// `(max_restarts, restart_window_s, base_backoff_s, max_backoff_s, jitter_ratio)`.
#[derive(Debug, Clone, PartialEq)]
pub struct RestartPolicy {
    pub max_restarts: u64,
    pub restart_window: Duration,
    pub base_backoff: Duration,
    pub max_backoff: Duration,
    pub jitter_ratio: f64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PolicyTableError(pub String);

impl fmt::Display for PolicyTableError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "invalid policy table: {}", self.0)
    }
}

impl std::error::Error for PolicyTableError {}

#[derive(Debug, Clone, Default)]
pub struct PolicyTable {
    entries: HashMap<String, RestartPolicy>,
}

impl PolicyTable {
    /// Build and validate a policy table from the raw adapter tuple form.
    ///
    /// The table MUST be non-empty; every profile name MUST be non-blank;
    /// durations MUST be finite and non-negative; `jitter_ratio` MUST lie
    /// in `[0, 1]`. Violations are `ValueError` on the Python side (E-8).
    pub fn build(
        raw: HashMap<String, (i64, f64, f64, f64, f64)>,
    ) -> Result<PolicyTable, PolicyTableError> {
        if raw.is_empty() {
            return Err(PolicyTableError("policies must be non-empty".into()));
        }
        let mut entries = HashMap::with_capacity(raw.len());
        for (name, (max_restarts, window_s, base_s, max_s, jitter)) in raw {
            if name.trim().is_empty() {
                return Err(PolicyTableError("profile name must be non-blank".into()));
            }
            for (field, value) in [
                ("restart_window_s", window_s),
                ("base_backoff_s", base_s),
                ("max_backoff_s", max_s),
            ] {
                if !value.is_finite() || value < 0.0 {
                    return Err(PolicyTableError(format!(
                        "profile {name:?}: {field} must be a finite non-negative number"
                    )));
                }
            }
            if !(0.0..=1.0).contains(&jitter) || !jitter.is_finite() {
                return Err(PolicyTableError(format!(
                    "profile {name:?}: jitter_ratio must lie in [0, 1]"
                )));
            }
            if max_restarts < 0 {
                return Err(PolicyTableError(format!(
                    "profile {name:?}: max_restarts must be non-negative"
                )));
            }
            entries.insert(
                name,
                RestartPolicy {
                    max_restarts: max_restarts as u64,
                    restart_window: Duration::from_secs_f64(window_s),
                    base_backoff: Duration::from_secs_f64(base_s),
                    max_backoff: Duration::from_secs_f64(max_s),
                    jitter_ratio: jitter,
                },
            );
        }
        Ok(PolicyTable { entries })
    }

    pub fn lookup(&self, profile: &str) -> Option<&RestartPolicy> {
        self.entries.get(profile)
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }
}
