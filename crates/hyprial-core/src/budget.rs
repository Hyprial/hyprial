//! Restart budget: a windowed failure deque (spec section 6.3).

use std::collections::VecDeque;
use std::time::{Duration, Instant};

/// What happened after recording one child failure.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BudgetOutcome {
    /// Restart is allowed; `attempt` is the 1-based failure count inside the
    /// window and feeds the backoff formula.
    Restart { attempt: u64 },
    /// `len(deque) > max_restarts`: quarantine, no restart this time.
    Quarantine,
}

#[derive(Debug)]
pub struct RestartBudget {
    max_restarts: u64,
    window: Duration,
    failures: VecDeque<Instant>,
}

impl RestartBudget {
    pub fn new(max_restarts: u64, window: Duration) -> RestartBudget {
        RestartBudget {
            max_restarts,
            window,
            failures: VecDeque::new(),
        }
    }

    /// Record a failure at `now`: push, prune entries older than
    /// `now - window`, then decide restart vs quarantine.
    ///
    /// The window prunes, it does not accumulate across windows (SUP-04).
    pub fn record(&mut self, now: Instant) -> BudgetOutcome {
        self.failures.push_back(now);
        if let Some(cutoff) = now.checked_sub(self.window) {
            while self.failures.front().is_some_and(|t| *t < cutoff) {
                self.failures.pop_front();
            }
        }
        let len = self.failures.len() as u64;
        if len > self.max_restarts {
            BudgetOutcome::Quarantine
        } else {
            BudgetOutcome::Restart { attempt: len }
        }
    }

    /// Clear the deque (used by `reset` on a quarantined actor, 6.4).
    pub fn clear(&mut self) {
        self.failures.clear();
    }

    /// Current in-window failure count; read-only, no pruning.
    pub fn len(&self) -> usize {
        self.failures.len()
    }

    pub fn is_empty(&self) -> bool {
        self.failures.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn budget(max: u64, window: Duration) -> RestartBudget {
        RestartBudget::new(max, window)
    }

    // SUP-04: the window prunes old failures; they stop counting.
    #[test]
    fn window_prunes_old_failures() {
        let mut b = budget(2, Duration::from_millis(50));
        let t0 = Instant::now();
        assert_eq!(b.record(t0), BudgetOutcome::Restart { attempt: 1 });
        assert_eq!(
            b.record(t0 + Duration::from_millis(10)),
            BudgetOutcome::Restart { attempt: 2 }
        );
        assert_eq!(
            b.record(t0 + Duration::from_millis(20)),
            BudgetOutcome::Quarantine
        );
        // Everything from t0..t0+20ms is older than the 50 ms window at t0+80ms.
        assert_eq!(
            b.record(t0 + Duration::from_millis(80)),
            BudgetOutcome::Restart { attempt: 1 }
        );
        assert_eq!(b.len(), 1);
    }

    #[test]
    fn max_restarts_zero_quarantines_on_first_failure() {
        let mut b = budget(0, Duration::from_secs(60));
        assert_eq!(b.record(Instant::now()), BudgetOutcome::Quarantine);
    }

    #[test]
    fn clear_empties_the_deque() {
        let mut b = budget(1, Duration::from_secs(60));
        let t = Instant::now();
        assert_eq!(b.record(t), BudgetOutcome::Restart { attempt: 1 });
        assert_eq!(b.record(t), BudgetOutcome::Quarantine);
        b.clear();
        assert!(b.is_empty());
        assert_eq!(b.record(t), BudgetOutcome::Restart { attempt: 1 });
    }
}
