//! `hyprial_shell_launcher`: everything the native shell does that is not a
//! window. No Tauri, no webview.
//!
//! Decisions (shell-6.md §B.3, §C.4, §D.3):
//! - `start` reads the startup record, derives the origin and reads the
//!   handoff. On any error it stops the child before returning, so a failed
//!   start never leaves `hyprial gui` running behind a status page.
//! - `STARTUP_TIMEOUT` is 200 s (GUI line decision, review round 2): above the
//!   sum of the CLI's own stage budgets (daemon start 30 s, restore wait 60 s,
//!   backend ready 60 s, frontend 30 s = 180 s), so the CLI's own failure line
//!   (for example `GUI_READY_TIMEOUT` with `stages_seconds`) can reach the
//!   status page first. A timeout stops the child with the planned gentle
//!   sequence (close stdin, SIGTERM, SIGKILL), never an immediate SIGKILL.
//!   The status page stays on "starting" while waiting.
//! - The returned launch URL is redacted in `Debug`/`Display`; only
//!   `LaunchUrl::as_str` exposes it, for the webview.

mod child;
mod command;
mod handoff;
mod home;
mod origin;
mod platform;
mod record;

pub use child::{
    spawn, ChildProcess, ChildStatus, LauncherError, SessionEnd, StopBudget, CLI_ARGS,
};
pub use command::{resolve_command, LaunchCommand, NoRuntime, RUNTIME_INTERPRETER, RUNTIME_PREFIX};
pub use handoff::{read_handoff, HandoffError, LaunchUrl};
pub use home::{
    app_environment, apply_environment, ensure_app_home, AppHomeError, EnvChange, NoHome,
    APP_HOME_DIR, HOME_VARIABLE, STATE_VARIABLE,
};
pub use origin::{
    allowed_main_frame, external_target, is_status_page, same_origin, status_page_url,
};
pub use record::{parse_line, FailureRecord, LaunchLine, RecordError, StartupRecord};

use std::time::Duration;
use url::Url;

pub const STARTUP_TIMEOUT: Duration = Duration::from_secs(200);

#[derive(Debug)]
pub struct Session {
    pub origin: Url,
    pub launch_url: LaunchUrl,
}

pub fn start(
    process: &ChildProcess,
    timeout: Duration,
    budget: StopBudget,
) -> Result<Session, LauncherError> {
    let result = read_session(process, timeout);
    if result.is_err() {
        let _ = process.stop_with(budget);
    }
    result
}

fn read_session(process: &ChildProcess, timeout: Duration) -> Result<Session, LauncherError> {
    let record = process.read_startup(timeout)?;
    let origin = Url::parse(&record.url)
        .map_err(|_| LauncherError::InvalidRecord(RecordError::InvalidUrl))?;
    let handoff = record
        .handoff_path
        .ok_or(LauncherError::InvalidRecord(RecordError::InvalidHandoff))?;
    let launch_url = read_handoff(handoff, &origin).map_err(LauncherError::InvalidHandoff)?;
    Ok(Session { origin, launch_url })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn startup_budget_exceeds_the_cli_stage_budgets() {
        assert_eq!(STARTUP_TIMEOUT, Duration::from_secs(200));
        assert!(STARTUP_TIMEOUT > Duration::from_secs(30 + 60 + 60 + 30));
    }
}
