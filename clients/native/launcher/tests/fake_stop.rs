#![cfg(unix)]
//! Stop and failed-start behaviour against fake `hyprial gui` scripts
//! (shell-6.md §D.3 6b-4 e–f; review findings 5, 6, 7).
//!
//! Every test records the pids it expects to disappear and asserts they are
//! gone; a survivor is killed by pid before the assertion fails.
//!
//! Liveness: a killed grandchild is reparented to PID 1. Where PID 1 does not
//! reap (a CI job container whose init is a plain long-running command), the
//! exited process stays in the table as a zombie and `kill(pid, 0)` still
//! succeeds, so "gone" means not running: signal-0 fails, or the process has
//! exited and only awaits a reap the launcher does not own. The launcher's
//! own child is still required to be reaped through `status()`.

use hyprial_shell_launcher::{spawn, start, ChildStatus, LaunchCommand, LauncherError, StopBudget};
use serde_json::json;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};
use std::thread;
use std::time::{Duration, Instant};
use tempfile::tempdir;

const QUICK: StopBudget = StopBudget {
    stdin_grace: Duration::from_millis(300),
    term_grace: Duration::from_millis(300),
};

fn fake(dir: &Path, body: &str) -> LaunchCommand {
    let script = dir.join("fake-hyprial.sh");
    fs::write(&script, body).unwrap();
    LaunchCommand::new("/bin/sh").with_prefix([script])
}

fn quoted(value: &Path) -> String {
    format!("'{}'", value.to_string_lossy().replace('\'', "'\\''"))
}

fn record(handoff: Option<&Path>, port: u16) -> String {
    serde_json::to_string(&json!({
        "ok": true,
        "record_version": 1,
        "url": format!("http://127.0.0.1:{port}"),
        "host": "127.0.0.1",
        "port": port,
        "handoff_path": handoff.map(|path| path.to_string_lossy().into_owned()),
        "mode": "normal",
        "backend_driver": "contract-mock",
        "frontend_pid": 4242,
        "backend_pid": null
    }))
    .unwrap()
}

fn handoff(dir: &Path, content: &str, mode: u32) -> PathBuf {
    let path = dir.join("launch.url");
    fs::write(&path, content).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(mode)).unwrap();
    path
}

fn alive(pid: i32) -> bool {
    let signalled = unsafe { libc::kill(pid, 0) == 0 };
    signalled && !exited_unreaped(pid)
}

fn exited_unreaped(pid: i32) -> bool {
    fs::read_to_string(format!("/proc/{pid}/stat"))
        .ok()
        .and_then(|stat| {
            stat.rsplit_once(')')
                .map(|(_, rest)| rest.trim_start().starts_with('Z'))
        })
        .unwrap_or(false)
}

fn wait_pid_file(path: &Path) -> i32 {
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if let Ok(text) = fs::read_to_string(path) {
            if let Ok(pid) = text.trim().parse() {
                return pid;
            }
        }
        assert!(
            Instant::now() < deadline,
            "{} never written",
            path.display()
        );
        thread::sleep(Duration::from_millis(20));
    }
}

fn assert_gone(pid: i32) {
    let deadline = Instant::now() + Duration::from_secs(5);
    while alive(pid) && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(20));
    }
    let survived = alive(pid);
    if survived {
        unsafe { libc::kill(pid, libc::SIGKILL) };
    }
    assert!(!survived, "process {pid} survived");
}

fn stubborn_body(dir: &Path, first_line: Option<&str>) -> (String, PathBuf, PathBuf) {
    let leader = dir.join("leader.pid");
    let grandchild = dir.join("grandchild.pid");
    let print = first_line
        .map(|line| format!("printf '%s\\n' '{}'\n", line.replace('\'', "'\\''")))
        .unwrap_or_default();
    let body = format!(
        "trap '' TERM\necho $$ > {}\nsleep 300 &\necho $! > {}\n{print}exec 0</dev/null\nwhile :; do sleep 1; done\n",
        quoted(&leader),
        quoted(&grandchild)
    );
    (body, leader, grandchild)
}

#[test]
fn e_launcher_exiting_on_stdin_eof_stops_within_the_first_budget() {
    let dir = tempdir().unwrap();
    let process = spawn(
        &fake(dir.path(), "while read line; do :; done\nexit 0\n"),
        None,
    )
    .unwrap();
    let started = Instant::now();
    let status = process
        .stop_with(StopBudget {
            stdin_grace: Duration::from_secs(5),
            term_grace: Duration::from_millis(100),
        })
        .unwrap();
    assert!(status.success(), "{status:?}");
    assert!(started.elapsed() < Duration::from_secs(5));
}

#[test]
fn f_launcher_ignoring_eof_and_sigterm_is_killed_with_its_grandchild() {
    let dir = tempdir().unwrap();
    let (body, leader_file, grandchild_file) = stubborn_body(dir.path(), None);
    let process = spawn(&fake(dir.path(), &body), None).unwrap();
    let leader = wait_pid_file(&leader_file);
    let grandchild = wait_pid_file(&grandchild_file);
    let started = Instant::now();
    let status = process.stop_with(QUICK).unwrap();
    assert_eq!(status.signal(), Some(libc::SIGKILL));
    assert!(started.elapsed() >= QUICK.stdin_grace + QUICK.term_grace);
    assert!(matches!(
        process.status().unwrap(),
        ChildStatus::Exited(None)
    ));
    assert!(!alive(leader));
    assert_gone(grandchild);
}

#[test]
fn finding_6_grandchild_left_after_a_clean_eof_exit_is_reclaimed() {
    let dir = tempdir().unwrap();
    let grandchild_file = dir.path().join("grandchild.pid");
    let body = format!(
        "sleep 300 &\necho $! > {}\nwhile read line; do :; done\nexit 0\n",
        quoted(&grandchild_file)
    );
    let process = spawn(&fake(dir.path(), &body), None).unwrap();
    let grandchild = wait_pid_file(&grandchild_file);
    let status = process.stop_with(QUICK).unwrap();
    assert!(status.success(), "{status:?}");
    assert_gone(grandchild);
}

#[test]
fn finding_6_grandchild_ignoring_sigterm_after_the_leader_exits_is_killed() {
    let dir = tempdir().unwrap();
    let grandchild_file = dir.path().join("grandchild.pid");
    let body = format!(
        "sh -c 'trap \"\" TERM; exec 0</dev/null; while :; do sleep 1; done' &\necho $! > {}\nwhile read line; do :; done\nexit 0\n",
        quoted(&grandchild_file)
    );
    let process = spawn(&fake(dir.path(), &body), None).unwrap();
    let grandchild = wait_pid_file(&grandchild_file);
    let status = process.stop_with(QUICK).unwrap();
    assert!(status.success(), "{status:?}");
    assert_gone(grandchild);
}

#[test]
fn finding_5_invalid_record_stops_the_child() {
    let dir = tempdir().unwrap();
    let invalid = record(None, 41234).replace("\"record_version\":1", "\"record_version\":2");
    let (body, leader_file, grandchild_file) = stubborn_body(dir.path(), Some(&invalid));
    let process = spawn(&fake(dir.path(), &body), None).unwrap();
    let leader = wait_pid_file(&leader_file);
    let grandchild = wait_pid_file(&grandchild_file);
    let error = start(&process, Duration::from_secs(5), QUICK).unwrap_err();
    assert!(
        matches!(error, LauncherError::InvalidRecord(_)),
        "{error:?}"
    );
    assert!(!alive(leader));
    assert_gone(grandchild);
}

#[test]
fn finding_5_missing_handoff_path_stops_the_child() {
    let dir = tempdir().unwrap();
    let line = record(None, 41234);
    let (body, leader_file, grandchild_file) = stubborn_body(dir.path(), Some(&line));
    let process = spawn(&fake(dir.path(), &body), None).unwrap();
    let leader = wait_pid_file(&leader_file);
    let grandchild = wait_pid_file(&grandchild_file);
    let error = start(&process, Duration::from_secs(5), QUICK).unwrap_err();
    assert!(
        matches!(error, LauncherError::InvalidRecord(_)),
        "{error:?}"
    );
    assert!(!alive(leader));
    assert_gone(grandchild);
}

#[test]
fn finding_5_handoff_error_stops_the_child_without_echoing_the_grant() {
    let dir = tempdir().unwrap();
    let path = handoff(
        dir.path(),
        "http://127.0.0.1:41234/#grant=SECRETGRANT\n",
        0o644,
    );
    let line = record(Some(&path), 41234);
    let (body, leader_file, grandchild_file) = stubborn_body(dir.path(), Some(&line));
    let process = spawn(&fake(dir.path(), &body), None).unwrap();
    let leader = wait_pid_file(&leader_file);
    let grandchild = wait_pid_file(&grandchild_file);
    let error = start(&process, Duration::from_secs(5), QUICK).unwrap_err();
    assert!(
        matches!(error, LauncherError::InvalidHandoff(_)),
        "{error:?}"
    );
    assert!(!format!("{error} {error:?}").contains("SECRETGRANT"));
    assert!(!alive(leader));
    assert_gone(grandchild);
}

#[test]
fn finding_5_handoff_origin_mismatch_stops_the_child() {
    let dir = tempdir().unwrap();
    let path = handoff(dir.path(), "http://127.0.0.1:41235/#grant=g\n", 0o600);
    let line = record(Some(&path), 41234);
    let (body, leader_file, _) = stubborn_body(dir.path(), Some(&line));
    let process = spawn(&fake(dir.path(), &body), None).unwrap();
    let leader = wait_pid_file(&leader_file);
    let error = start(&process, Duration::from_secs(5), QUICK).unwrap_err();
    assert!(
        matches!(error, LauncherError::InvalidHandoff(_)),
        "{error:?}"
    );
    assert!(!alive(leader));
}

#[test]
fn finding_7_stop_does_not_wait_behind_a_blocked_startup_read() {
    let dir = tempdir().unwrap();
    let process = spawn(
        &fake(dir.path(), "while read line; do :; done\nexit 0\n"),
        None,
    )
    .unwrap();
    let reader = process.clone();
    let reading = thread::spawn(move || reader.read_startup(Duration::from_secs(30)));
    thread::sleep(Duration::from_millis(200));
    let started = Instant::now();
    let status = process.stop_with(QUICK).unwrap();
    assert!(status.success(), "{status:?}");
    assert!(started.elapsed() < Duration::from_secs(5));
    let result = reading.join().unwrap();
    assert!(
        matches!(result, Err(LauncherError::Exited(Some(0)))),
        "{result:?}"
    );
}

#[test]
fn startup_timeout_runs_the_gentle_stop_and_lets_the_launcher_exit_on_eof() {
    let dir = tempdir().unwrap();
    let marker = dir.path().join("eof");
    let body = format!(
        "while read line; do :; done\necho eof > {}\nexit 0\n",
        quoted(&marker)
    );
    let process = spawn(&fake(dir.path(), &body), None).unwrap();
    let error = start(&process, Duration::from_millis(200), QUICK).unwrap_err();
    assert!(matches!(error, LauncherError::Timeout), "{error:?}");
    assert_eq!(process.status().unwrap(), ChildStatus::Exited(Some(0)));
    assert_eq!(fs::read_to_string(&marker).unwrap(), "eof\n");
}

#[test]
fn startup_timeout_sends_sigterm_before_sigkill() {
    let dir = tempdir().unwrap();
    let marker = dir.path().join("term");
    let body = format!(
        "trap 'echo term > {}; exit 7' TERM\nexec 0</dev/null\nwhile :; do sleep 0.05; done\n",
        quoted(&marker)
    );
    let process = spawn(&fake(dir.path(), &body), None).unwrap();
    let error = start(&process, Duration::from_millis(200), QUICK).unwrap_err();
    assert!(matches!(error, LauncherError::Timeout), "{error:?}");
    assert_eq!(process.status().unwrap(), ChildStatus::Exited(Some(7)));
    assert_eq!(fs::read_to_string(&marker).unwrap(), "term\n");
}

#[test]
fn clean_stop_before_the_record_is_exit_zero_without_output() {
    let dir = tempdir().unwrap();
    let process = spawn(
        &fake(dir.path(), "while read line; do :; done\nexit 0\n"),
        None,
    )
    .unwrap();
    let reader = process.clone();
    let reading = thread::spawn(move || reader.read_startup(Duration::from_secs(30)));
    thread::sleep(Duration::from_millis(100));
    let status = process.stop_with(QUICK).unwrap();
    assert!(status.success(), "{status:?}");
    let result = reading.join().unwrap();
    assert!(
        matches!(result, Err(LauncherError::Exited(Some(0)))),
        "{result:?}"
    );
}
