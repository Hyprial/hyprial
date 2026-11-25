//! Operating-system half of the child supervisor: how `hyprial gui` is
//! started, reaped and stopped together with everything it started.
//!
//! Decisions (W1C-LAUNCHER, Windows portability 2026-11-21):
//! - `child.rs` owns the stop policy (close stdin, wait, gentle stop, wait,
//!   kill, reclaim leftovers) and the exit-status cache; this module only
//!   provides the primitives, one implementation per platform family:
//!   `unix.rs` (process group, `waitpid`, signals) and `windows.rs` (Job
//!   Object, `std::process::Child::try_wait`, `TerminateJobObject`).
//! - Every implementation exposes the same surface: `spawn_child` returning
//!   `Spawned`, and on `Process`: `id`, `try_wait` (never blocks; the caller
//!   serialises it and caches the first status), `request_stop` (the gentle
//!   stage; returns whether anything was delivered), `kill_group` and
//!   `group_alive`.
//! - "Group" is the set the shell must not leave behind: the POSIX process
//!   group of the leader, or the Windows Job Object the leader was created in.
//!   Children that deliberately leave it (`setsid`/`start_new_session` on
//!   POSIX, `CREATE_BREAKAWAY_FROM_JOB` on Windows; the daemon `hyprial gui`
//!   ensures) are out of reach on both, by design.
//! - No platform refuses to run: there is no "unsupported" branch.

use std::fs::File;

#[cfg(unix)]
mod unix;
#[cfg(unix)]
pub use unix::{spawn_child, Process};

#[cfg(windows)]
mod windows;
#[cfg(windows)]
pub use windows::{spawn_child, Process};

pub struct Spawned {
    pub process: Process,
    pub stdin: File,
    pub stdout: File,
}
