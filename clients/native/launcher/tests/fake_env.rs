#![cfg(unix)]
//! The child receives the app's home environment (PR 6c, #2840 obligation 3).
//!
//! Its own test binary: the test sets two variables in this process before
//! any thread exists, so the inherited `HARNESS_STATE_DIR` that the app must
//! drop is really present in the parent, on both spawn paths (posix_spawnp on
//! macOS, std `Command` elsewhere). The fake records its environment with
//! `/usr/bin/env` and prints a failure line so the launcher returns at once.

use hyprial_shell_launcher::{app_environment, spawn, start, LaunchCommand, StopBudget};
use std::fs;
use std::path::Path;
use std::time::Duration;
use tempfile::tempdir;

const QUICK: StopBudget = StopBudget {
    stdin_grace: Duration::from_millis(500),
    term_grace: Duration::from_millis(500),
};

fn recorded_environment(command: LaunchCommand, dump: &Path) -> Vec<(String, String)> {
    let process = spawn(&command, None).unwrap();
    let _ = start(&process, Duration::from_secs(10), QUICK);
    fs::read_to_string(dump)
        .unwrap()
        .lines()
        .filter_map(|line| line.split_once('='))
        .map(|(key, value)| (key.to_owned(), value.to_owned()))
        .collect()
}

fn fake(dir: &Path, dump: &Path) -> LaunchCommand {
    let script = dir.join("fake-hyprial.sh");
    fs::write(
        &script,
        format!(
            "/usr/bin/env > '{}'\nprintf '%s\\n' '{{\"ok\":false,\"code\":\"FAKE\",\"error\":\"done\"}}'\nexit 1\n",
            dump.display()
        ),
    )
    .unwrap();
    LaunchCommand::new("/bin/sh").with_prefix([script])
}

#[test]
fn the_child_gets_the_app_home_and_never_an_inherited_state_dir() {
    std::env::set_var("HARNESS_STATE_DIR", "/inherited/state");
    std::env::set_var("HYPRIAL_SHELL_FAKE_ENV_MARKER", "kept");
    std::env::remove_var("HYPRIAL_HOME");
    let dir = tempdir().unwrap();
    let user_home = dir.path().join("user");

    let dump = dir.path().join("app-home.env");
    let changes = app_environment(None, Some(&user_home)).unwrap();
    let environment =
        recorded_environment(fake(dir.path(), &dump).with_environment(changes), &dump);
    let value = |name: &str| {
        environment
            .iter()
            .find(|(key, _)| key == name)
            .map(|(_, value)| value.clone())
    };
    assert_eq!(
        value("HYPRIAL_HOME"),
        Some(user_home.join(".hyprialos").display().to_string())
    );
    assert_eq!(value("HARNESS_STATE_DIR"), None);
    assert_eq!(value("HYPRIAL_SHELL_FAKE_ENV_MARKER"), Some("kept".into()));
    assert_eq!(
        environment
            .iter()
            .filter(|(key, _)| key == "HYPRIAL_HOME")
            .count(),
        1
    );

    let dump = dir.path().join("explicit.env");
    let changes = app_environment(Some("/explicit/home".as_ref()), Some(&user_home)).unwrap();
    let environment =
        recorded_environment(fake(dir.path(), &dump).with_environment(changes), &dump);
    assert!(environment.contains(&("HARNESS_STATE_DIR".into(), "/inherited/state".into())));
    assert!(!environment.iter().any(|(key, _)| key == "HYPRIAL_HOME"));
}
