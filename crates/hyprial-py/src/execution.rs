//! Thread-local marker for "the caller is this actor's current handler
//! execution" — the self-stop recognition of spec section 5.4 (R3-2).
//!
//! One execution runs on one thread at a time, so a thread-local is exact.

use std::cell::RefCell;
use std::sync::Arc;

use crate::cell::Cell;
use crate::pool::Pool;

pub(crate) enum ExecutionContext {
    Serial { cell: Arc<Cell> },
    Pool { pool: Arc<Pool> },
}

thread_local! {
    static CURRENT: RefCell<Option<ExecutionContext>> = const { RefCell::new(None) };
}

/// Set for the duration of one handler execution; restores the previous
/// value on drop.
pub(crate) struct ExecutionGuard {
    _priv: (),
}

impl ExecutionGuard {
    pub(crate) fn serial(cell: Arc<Cell>) -> ExecutionGuard {
        CURRENT.with(|slot| {
            *slot.borrow_mut() = Some(ExecutionContext::Serial { cell });
        });
        ExecutionGuard { _priv: () }
    }

    pub(crate) fn pool(pool: Arc<Pool>) -> ExecutionGuard {
        CURRENT.with(|slot| {
            *slot.borrow_mut() = Some(ExecutionContext::Pool { pool });
        });
        ExecutionGuard { _priv: () }
    }
}

impl Drop for ExecutionGuard {
    fn drop(&mut self) {
        CURRENT.with(|slot| {
            *slot.borrow_mut() = None;
        });
    }
}

/// The execution context of the calling thread, if a handler is running.
pub(crate) fn current_execution() -> Option<ExecutionContext> {
    CURRENT.with(|slot| {
        slot.borrow().as_ref().map(|ctx| match ctx {
            ExecutionContext::Serial { cell } => ExecutionContext::Serial { cell: cell.clone() },
            ExecutionContext::Pool { pool } => ExecutionContext::Pool { pool: pool.clone() },
        })
    })
}
