//! The fixed per-actor state machine states (spec section 5.1).
//!
//! There is exactly one machine and it is not configurable. The transitions
//! themselves live in the binding crate's supervision logic; this module
//! holds the state vocabulary and its string form (used by `snapshot`).

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CellState {
    Running,
    RestartPending,
    Quarantined,
    Stopping,
    Stopped,
}

impl CellState {
    pub fn as_str(&self) -> &'static str {
        match self {
            CellState::Running => "running",
            CellState::RestartPending => "restart_pending",
            CellState::Quarantined => "quarantined",
            CellState::Stopping => "stopping",
            CellState::Stopped => "stopped",
        }
    }

    /// Live states accept `stop` acceptance; `stopped` short-circuits to true.
    pub fn is_live(&self) -> bool {
        matches!(
            self,
            CellState::Running | CellState::RestartPending | CellState::Quarantined
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn state_strings_match_the_spec_vocabulary() {
        assert_eq!(CellState::Running.as_str(), "running");
        assert_eq!(CellState::RestartPending.as_str(), "restart_pending");
        assert_eq!(CellState::Quarantined.as_str(), "quarantined");
        assert_eq!(CellState::Stopping.as_str(), "stopping");
        assert_eq!(CellState::Stopped.as_str(), "stopped");
    }
}
