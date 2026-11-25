//! Pure-Rust policy core of the native actor engine.
//!
//! This crate holds the deterministic decision logic of the engine:
//! restart policies, the restart-budget window, the capped exponential
//! backoff with jitter, custody-token bookkeeping, and the fire-time
//! classification rules for timers and pool completions.
//!
//! It intentionally depends on neither `pyo3` nor any async runtime: every
//! rule here is a pure function of its inputs and is unit-tested in place.
//! The binding crate (`hyprial-py`) composes these rules onto the actor
//! runtime and the Python boundary.

pub mod backoff;
pub mod budget;
pub mod custody;
pub mod policy;
pub mod state;

pub use backoff::{restart_delay, Jitter};
pub use budget::{BudgetOutcome, RestartBudget};
pub use custody::{CustodyKey, CustodyTable, ReserveOutcome, SubmitOutcome, TokenState};
pub use policy::{PolicyTable, PolicyTableError, RestartPolicy};
pub use state::CellState;

/// Outcome of classifying a timer firing against the target's state
/// (spec section 8, fire-time classification).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FireDecision {
    /// Normal admission into the kept mailbox.
    Admit,
    /// Generation mismatch on the restart lineage: dropped silently.
    FenceDrop,
    /// Hand the message to the target's dead-letter sink with this reason.
    DeadLetter(&'static str),
}

pub fn classify_timer_fire(state: CellState, generation_matches: bool) -> FireDecision {
    match state {
        CellState::Running | CellState::RestartPending => {
            if generation_matches {
                FireDecision::Admit
            } else {
                FireDecision::FenceDrop
            }
        }
        CellState::Stopping | CellState::Stopped => FireDecision::DeadLetter("stopped"),
        CellState::Quarantined => FireDecision::DeadLetter("quarantined"),
    }
}

/// Outcome of classifying an undelivered pool completion against the
/// owner's state (spec section 9.4, delivery transition table).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DeliveryDecision {
    /// Owner `running` / `restart_pending`: admit into the owner mailbox.
    /// A draining owner also lands here: completion custody is
    /// engine-internal traffic and bypasses drain-closed external admission.
    Admit,
    /// Owner `stopping` (transient): keep retrying.
    Retry,
    /// Terminal: hand the completion instance to the pool's dead-letter
    /// sink with this reason and settle the token in the same step.
    DeadLetter(&'static str),
}

pub fn classify_completion_delivery(state: CellState) -> DeliveryDecision {
    match state {
        CellState::Running | CellState::RestartPending => DeliveryDecision::Admit,
        CellState::Stopping => DeliveryDecision::Retry,
        CellState::Stopped => DeliveryDecision::DeadLetter("stopped"),
        CellState::Quarantined => DeliveryDecision::DeadLetter("quarantined"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // TMR-02/TMR-04: fire-time classification is exactly one decision.
    #[test]
    fn timer_fire_classification() {
        use CellState::*;
        assert_eq!(classify_timer_fire(Running, true), FireDecision::Admit);
        assert_eq!(
            classify_timer_fire(RestartPending, true),
            FireDecision::Admit
        );
        assert_eq!(classify_timer_fire(Running, false), FireDecision::FenceDrop);
        assert_eq!(
            classify_timer_fire(RestartPending, false),
            FireDecision::FenceDrop
        );
        assert_eq!(
            classify_timer_fire(Stopping, true),
            FireDecision::DeadLetter("stopped")
        );
        assert_eq!(
            classify_timer_fire(Stopped, false),
            FireDecision::DeadLetter("stopped")
        );
        assert_eq!(
            classify_timer_fire(Quarantined, true),
            FireDecision::DeadLetter("quarantined")
        );
    }

    // EFF-06/07: completion delivery transitions (draining owners are not a
    // state: internal custody bypasses drain closure before this classify).
    #[test]
    fn completion_delivery_classification() {
        use CellState::*;
        assert_eq!(
            classify_completion_delivery(Running),
            DeliveryDecision::Admit
        );
        assert_eq!(
            classify_completion_delivery(RestartPending),
            DeliveryDecision::Admit,
            "a restart_pending owner's kept mailbox holds the completion"
        );
        assert_eq!(
            classify_completion_delivery(Stopping),
            DeliveryDecision::Retry,
            "stopping is transient, not terminal"
        );
        assert_eq!(
            classify_completion_delivery(Stopped),
            DeliveryDecision::DeadLetter("stopped")
        );
        assert_eq!(
            classify_completion_delivery(Quarantined),
            DeliveryDecision::DeadLetter("quarantined")
        );
    }
}
