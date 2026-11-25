//! Custody-token bookkeeping for pool actors (spec section 9).
//!
//! A token is created by reserve/submit and removed by exactly one of:
//! acknowledge, cancel-before-submission, or owner-death dead-lettering.
//! Capacity bounds the number of live tokens at all times.

use std::collections::HashMap;

pub type CustodyKey = (String, u64);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TokenState {
    /// Created by `pool_reserve`; owns a capacity slot, not yet queued.
    Reserved,
    /// Submitted: the payload is queued or executing or awaiting ack.
    Submitted,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReserveOutcome {
    Reserved,
    /// A live token for this exact key already exists: idempotent, capacity
    /// is not re-checked (EFF-10).
    AlreadyHeld,
    AtCapacity,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SubmitOutcome {
    Submitted,
    /// Duplicate submit of an already-submitted live token: nothing queued,
    /// the first payload wins (EFF-10).
    AlreadySubmitted,
    NoSuchToken,
}

#[derive(Debug, Default)]
pub struct CustodyTable {
    capacity: usize,
    states: HashMap<CustodyKey, TokenState>,
}

impl CustodyTable {
    pub fn new(capacity: usize) -> CustodyTable {
        CustodyTable {
            capacity: capacity.max(1),
            states: HashMap::new(),
        }
    }

    /// O(1): answered from the token table, never behind worker backlog.
    pub fn reserve(&mut self, key: &CustodyKey) -> ReserveOutcome {
        match self.states.get(key) {
            Some(_) => ReserveOutcome::AlreadyHeld,
            None => {
                if self.states.len() >= self.capacity {
                    ReserveOutcome::AtCapacity
                } else {
                    self.states.insert(key.clone(), TokenState::Reserved);
                    ReserveOutcome::Reserved
                }
            }
        }
    }

    pub fn submit(&mut self, key: &CustodyKey) -> SubmitOutcome {
        match self.states.get(key).copied() {
            None => SubmitOutcome::NoSuchToken,
            Some(TokenState::Reserved) => {
                self.states.insert(key.clone(), TokenState::Submitted);
                SubmitOutcome::Submitted
            }
            Some(TokenState::Submitted) => SubmitOutcome::AlreadySubmitted,
        }
    }

    /// Frees a live, unsubmitted token. After submission this returns false
    /// AND the submitted token REMAINS (the payload may already run; only
    /// the owner's acknowledge or owner-death settles it, EFF-05).
    pub fn cancel(&mut self, key: &CustodyKey) -> bool {
        match self.states.get(key) {
            Some(TokenState::Reserved) => {
                self.states.remove(key);
                true
            }
            _ => false,
        }
    }

    /// The owner handled the completion (including stale-generation
    /// rejection): settles a SUBMITTED token only, mirroring the shipped
    /// EffectLane semantics (ack of an unsubmitted token is false).
    pub fn acknowledge(&mut self, key: &CustodyKey) -> bool {
        match self.states.get(key) {
            Some(TokenState::Submitted) => {
                self.states.remove(key);
                true
            }
            _ => false,
        }
    }

    /// Owner-death dead-lettering settles any live token in the same step.
    pub fn settle(&mut self, key: &CustodyKey) -> bool {
        self.states.remove(key).is_some()
    }

    pub fn state_of(&self, key: &CustodyKey) -> Option<TokenState> {
        self.states.get(key).copied()
    }

    pub fn len(&self) -> usize {
        self.states.len()
    }

    pub fn is_empty(&self) -> bool {
        self.states.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key(op: &str, gen: u64) -> CustodyKey {
        (op.to_string(), gen)
    }

    // CUS-06: operations against non-existent keys are rejected, no silent
    // re-creation.
    #[test]
    fn missing_keys_rejected() {
        let mut t = CustodyTable::new(4);
        assert_eq!(t.submit(&key("op", 1)), SubmitOutcome::NoSuchToken);
        assert!(!t.cancel(&key("op", 1)));
        assert!(!t.acknowledge(&key("op", 1)));
        assert!(!t.settle(&key("op", 1)));
    }

    // EFF-10: duplicate reserve is idempotent without re-checking capacity.
    #[test]
    fn duplicate_reserve_is_idempotent() {
        let mut t = CustodyTable::new(1);
        assert_eq!(t.reserve(&key("a", 1)), ReserveOutcome::Reserved);
        assert_eq!(t.reserve(&key("a", 1)), ReserveOutcome::AlreadyHeld);
        assert_eq!(t.reserve(&key("b", 1)), ReserveOutcome::AtCapacity);
        assert_eq!(t.len(), 1);
    }

    // EFF-10: duplicate submit_reserved queues nothing, first payload wins.
    #[test]
    fn duplicate_submit_is_idempotent() {
        let mut t = CustodyTable::new(2);
        t.reserve(&key("a", 1));
        assert_eq!(t.submit(&key("a", 1)), SubmitOutcome::Submitted);
        assert_eq!(t.submit(&key("a", 1)), SubmitOutcome::AlreadySubmitted);
        assert_eq!(t.submit(&key("x", 9)), SubmitOutcome::NoSuchToken);
    }

    // EFF-05: cancel after submit returns false; live unsubmitted -> true.
    #[test]
    fn cancel_only_before_submission() {
        let mut t = CustodyTable::new(2);
        t.reserve(&key("a", 1));
        t.reserve(&key("b", 1));
        assert!(t.cancel(&key("a", 1)));
        assert!(!t.cancel(&key("a", 1)));
        t.submit(&key("b", 1));
        assert!(!t.cancel(&key("b", 1)));
        // A cancel probe against a submitted token must NOT settle it: the
        // owner can still acknowledge afterwards.
        assert!(t.acknowledge(&key("b", 1)));
    }

    // EFF-06/08: ack settles a submitted token once (idempotent -> false);
    // ack of a merely-reserved token is false.
    #[test]
    fn acknowledge_settles_submitted_only() {
        let mut t = CustodyTable::new(2);
        t.reserve(&key("r", 1));
        assert!(!t.acknowledge(&key("r", 1)));
        t.submit(&key("r", 1));
        assert!(t.acknowledge(&key("r", 1)));
        assert!(!t.acknowledge(&key("r", 1)));
    }

    #[test]
    fn settle_removes_any_live_token() {
        let mut t = CustodyTable::new(2);
        t.reserve(&key("a", 1));
        t.reserve(&key("b", 2));
        t.submit(&key("b", 2));
        assert!(t.settle(&key("a", 1)));
        assert!(t.settle(&key("b", 2)));
        assert!(!t.settle(&key("b", 2)));
        assert!(t.is_empty());
    }

    // EFF-01/08: capacity counts every live token until it settles.
    #[test]
    fn capacity_covers_all_live_tokens() {
        let mut t = CustodyTable::new(2);
        assert_eq!(t.reserve(&key("a", 1)), ReserveOutcome::Reserved);
        assert_eq!(t.reserve(&key("b", 1)), ReserveOutcome::Reserved);
        assert_eq!(t.reserve(&key("c", 1)), ReserveOutcome::AtCapacity);
        assert!(t.settle(&key("b", 1)), "owner-death settles any live token");
        assert_eq!(t.reserve(&key("c", 1)), ReserveOutcome::Reserved);
    }
}
