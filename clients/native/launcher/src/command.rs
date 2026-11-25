//! Which program the shell starts as `hyprial gui`.
//!
//! Decisions (shell-6.md §B.3 step 3, OD-11):
//! - `HYPRIAL_SHELL_LAUNCHER`, when set and not empty, names the program and is
//!   started with the fixed CLI arguments only (tests, development, and release
//!   builds alike: same trust as the user's environment).
//! - Otherwise the bundled runtime `<resources>/runtime/bin/python3.12` is
//!   started as `python3.12 -I -m hyprial.cli` followed by the fixed arguments.
//! - Neither present is a typed error the shell shows on its status page; the
//!   shell never falls back to a `python3.12` found on `PATH`.
//! - Resolution is a pure function of its inputs (no environment reads), so
//!   tests need no process-global state.
//! - A command carries environment changes (`home::app_environment`) that the
//!   spawn applies on top of the shell's own environment, whichever program
//!   was resolved; the shell's process environment itself is never modified.

use crate::home::EnvChange;
use std::ffi::{OsStr, OsString};
use std::path::{Path, PathBuf};

pub const RUNTIME_INTERPRETER: &str = "runtime/bin/python3.12";
pub const RUNTIME_PREFIX: &[&str] = &["-I", "-m", "hyprial.cli"];

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LaunchCommand {
    program: PathBuf,
    prefix: Vec<OsString>,
    environment: Vec<EnvChange>,
}

impl LaunchCommand {
    pub fn new(program: impl Into<PathBuf>) -> Self {
        Self {
            program: program.into(),
            prefix: Vec::new(),
            environment: Vec::new(),
        }
    }

    pub fn with_prefix<I, S>(mut self, prefix: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: AsRef<OsStr>,
    {
        self.prefix = prefix
            .into_iter()
            .map(|value| value.as_ref().to_owned())
            .collect();
        self
    }

    pub fn with_environment(mut self, changes: Vec<EnvChange>) -> Self {
        self.environment = changes;
        self
    }

    pub fn environment(&self) -> &[EnvChange] {
        &self.environment
    }

    pub fn program(&self) -> &Path {
        &self.program
    }

    pub fn prefix(&self) -> &[OsString] {
        &self.prefix
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NoRuntime;

impl std::fmt::Display for NoRuntime {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(
            "no bundled runtime was found and HYPRIAL_SHELL_LAUNCHER is not set; \
             this build cannot start hyprial gui",
        )
    }
}

impl std::error::Error for NoRuntime {}

pub fn resolve_command(
    launcher_override: Option<&OsStr>,
    resource_dir: Option<&Path>,
) -> Result<LaunchCommand, NoRuntime> {
    if let Some(program) = launcher_override.filter(|value| !value.is_empty()) {
        return Ok(LaunchCommand::new(program));
    }
    let interpreter = resource_dir
        .map(|dir| dir.join(RUNTIME_INTERPRETER))
        .filter(|path| path.is_file())
        .ok_or(NoRuntime)?;
    Ok(LaunchCommand::new(interpreter).with_prefix(RUNTIME_PREFIX))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use tempfile::tempdir;

    #[test]
    fn override_wins_and_has_no_prefix() {
        let dir = tempdir().unwrap();
        fs::create_dir_all(dir.path().join("runtime/bin")).unwrap();
        fs::write(dir.path().join(RUNTIME_INTERPRETER), "").unwrap();
        let command =
            resolve_command(Some(OsStr::new("/opt/fake-hyprial")), Some(dir.path())).unwrap();
        assert_eq!(command.program(), Path::new("/opt/fake-hyprial"));
        assert!(command.prefix().is_empty());
    }

    #[test]
    fn bundled_runtime_runs_the_cli_module_isolated() {
        let dir = tempdir().unwrap();
        fs::create_dir_all(dir.path().join("runtime/bin")).unwrap();
        fs::write(dir.path().join(RUNTIME_INTERPRETER), "").unwrap();
        let command = resolve_command(None, Some(dir.path())).unwrap();
        assert_eq!(command.program(), dir.path().join("runtime/bin/python3.12"));
        assert_eq!(command.prefix(), ["-I", "-m", "hyprial.cli"]);
    }

    #[test]
    fn empty_override_is_unset() {
        let dir = tempdir().unwrap();
        assert_eq!(
            resolve_command(Some(OsStr::new("")), Some(dir.path())),
            Err(NoRuntime)
        );
    }

    #[test]
    fn missing_runtime_is_a_clear_error_not_a_path_lookup() {
        let dir = tempdir().unwrap();
        fs::create_dir_all(dir.path().join("runtime/bin/python3.12")).unwrap();
        assert_eq!(resolve_command(None, Some(dir.path())), Err(NoRuntime));
        assert_eq!(resolve_command(None, None), Err(NoRuntime));
        assert!(NoRuntime.to_string().contains("HYPRIAL_SHELL_LAUNCHER"));
    }
}
