#![cfg(unix)]
//! Fake `hyprial gui` scripts drive the launcher (shell-6.md §D.3 6b-4 a–d, g, h).
//!
//! The fakes are run as `/bin/sh <script>` through the command prefix, never
//! as freshly written executables: macOS scans a new executable on its first
//! exec, which made a 1 s read budget flaky. The budgets stay as written.
//!
//! Liveness: a killed grandchild is reparented to PID 1. Where PID 1 does not
//! reap (a CI job container whose init is a plain long-running command), the
//! exited process stays in the table as a zombie and `kill(pid, 0)` still
//! succeeds, so "gone" means not running: signal-0 fails, or the process has
//! exited and only awaits a reap the launcher does not own. The launcher's
//! own child is still required to be reaped through `status()`.

use hyprial_shell_launcher::{spawn, start, LaunchCommand, LauncherError, StopBudget, CLI_ARGS};
use serde_json::json;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};
use tempfile::tempdir;

const QUICK: StopBudget = StopBudget {
    stdin_grace: Duration::from_millis(500),
    term_grace: Duration::from_millis(500),
};

fn fake(dir: &Path, body: &str) -> LaunchCommand {
    let script = dir.join("fake-hyprial.sh");
    fs::write(&script, body).unwrap();
    LaunchCommand::new("/bin/sh").with_prefix([script])
}

fn quoted(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\\''"))
}

fn startup_line(handoff_path: Option<&Path>) -> String {
    serde_json::to_string(&json!({
        "ok": true,
        "record_version": 1,
        "url": "http://127.0.0.1:41234",
        "host": "127.0.0.1",
        "port": 41234,
        "handoff_path": handoff_path.map(|path| path.to_string_lossy().into_owned()),
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
        std::thread::sleep(Duration::from_millis(20));
    }
}

fn assert_gone(pid: i32) {
    let deadline = Instant::now() + Duration::from_secs(5);
    while alive(pid) && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(20));
    }
    let survived = alive(pid);
    if survived {
        unsafe { libc::kill(pid, libc::SIGKILL) };
    }
    assert!(!survived, "process {pid} survived");
}

#[test]
fn a_record_and_handoff_start_a_session_and_stop_on_stdin_eof() {
    let dir = tempdir().unwrap();
    let path = handoff(
        dir.path(),
        "http://127.0.0.1:41234/#grant=fake%2Bgrant\n",
        0o600,
    );
    let body = format!(
        "printf '%s\\n' {}\nwhile read line; do :; done\nexit 0\n",
        quoted(&startup_line(Some(&path)))
    );
    let process = spawn(&fake(dir.path(), &body), None).unwrap();
    let session = start(&process, Duration::from_secs(1), QUICK).unwrap();
    assert_eq!(session.origin.as_str(), "http://127.0.0.1:41234/");
    assert_eq!(
        session.launch_url.as_str(),
        "http://127.0.0.1:41234/#grant=fake%2Bgrant"
    );
    assert!(!format!("{session:?}").contains("fake%2Bgrant"));
    let started = Instant::now();
    let status = process.stop().unwrap();
    assert!(status.success());
    assert!(started.elapsed() < StopBudget::PLANNED.stdin_grace);
}

#[test]
fn b_failure_line_preserves_the_code_and_exit_one() {
    let dir = tempdir().unwrap();
    let pid_file = dir.path().join("leader.pid");
    let body = format!(
        "echo $$ > {}\nprintf '%s\\n' '{{\"ok\":false,\"code\":\"GUI_RUNTIME_BUSY\",\"error\":\"already running\"}}'\nexit 1\n",
        quoted(&pid_file.to_string_lossy())
    );
    let process = spawn(&fake(dir.path(), &body), None).unwrap();
    let error = start(&process, Duration::from_secs(1), QUICK).unwrap_err();
    assert!(
        matches!(&error, LauncherError::Failure { code: Some(code), .. } if code == "GUI_RUNTIME_BUSY"),
        "{error:?}"
    );
    assert_eq!(
        error.to_string(),
        "launcher failed (GUI_RUNTIME_BUSY): already running"
    );
    assert_gone(wait_pid_file(&pid_file));
}

#[test]
fn c_exit_without_output_reports_the_status() {
    let dir = tempdir().unwrap();
    let process = spawn(&fake(dir.path(), "exit 3\n"), None).unwrap();
    let error = start(&process, Duration::from_secs(1), QUICK).unwrap_err();
    assert!(matches!(error, LauncherError::Exited(Some(3))), "{error:?}");
}

#[test]
fn d_silent_launcher_times_out_and_its_group_is_killed_and_reaped() {
    let dir = tempdir().unwrap();
    let leader_file = dir.path().join("leader.pid");
    let grandchild_file = dir.path().join("grandchild.pid");
    let body = format!(
        "echo $$ > {}\nsleep 300 &\necho $! > {}\nwait\n",
        quoted(&leader_file.to_string_lossy()),
        quoted(&grandchild_file.to_string_lossy())
    );
    let process = spawn(&fake(dir.path(), &body), None).unwrap();
    let leader = wait_pid_file(&leader_file);
    let grandchild = wait_pid_file(&grandchild_file);
    let error = start(&process, Duration::from_millis(100), QUICK).unwrap_err();
    assert!(matches!(error, LauncherError::Timeout), "{error:?}");
    assert!(matches!(
        process.status().unwrap(),
        hyprial_shell_launcher::ChildStatus::Exited(None)
    ));
    assert!(!alive(leader));
    assert_gone(grandchild);
}

#[test]
fn g_failure_line_after_the_record_is_delivered_with_the_exit_status() {
    let dir = tempdir().unwrap();
    let path = handoff(dir.path(), "http://127.0.0.1:41234/#grant=g\n", 0o600);
    let release = dir.path().join("release");
    let body = format!(
        "printf '%s\\n' {}\nwhile [ ! -e {} ]; do sleep 0.02; done\nprintf '%s\\n' '{{\"ok\":false,\"code\":\"GUI_SESSION_EXPIRED\",\"error\":\"view expired\"}}'\nexit 1\n",
        quoted(&startup_line(Some(&path))),
        quoted(&release.to_string_lossy())
    );
    let process = spawn(&fake(dir.path(), &body), None).unwrap();
    start(&process, Duration::from_secs(1), QUICK).unwrap();
    fs::write(&release, "").unwrap();
    let end = process.wait_session_end().unwrap();
    assert_eq!(end.status, Some(1));
    let failure = end.failure.clone().unwrap();
    assert_eq!(failure.code.as_deref(), Some("GUI_SESSION_EXPIRED"));
    assert_eq!(
        end.to_string(),
        "launcher failed (GUI_SESSION_EXPIRED): view expired"
    );
}

#[test]
fn g_exit_after_the_record_without_a_line_reports_the_status() {
    let dir = tempdir().unwrap();
    let path = handoff(dir.path(), "http://127.0.0.1:41234/#grant=g\n", 0o600);
    let body = format!(
        "printf '%s\\n' {}\nexit 7\n",
        quoted(&startup_line(Some(&path)))
    );
    let process = spawn(&fake(dir.path(), &body), None).unwrap();
    start(&process, Duration::from_secs(1), QUICK).unwrap();
    let end = process.wait_session_end().unwrap();
    assert_eq!(end.failure, None);
    assert_eq!(end.status, Some(7));
    assert_eq!(end.to_string(), "launcher exited with status 7");
}

#[test]
fn h_argv_is_prefix_then_the_fixed_cli_arguments() {
    let dir = tempdir().unwrap();
    let args = dir.path().join("args");
    let script = format!(
        "printf '%s\\n' \"$0\" \"$@\" > {}\nprintf '%s\\n' '{{\"ok\":false,\"error\":\"done\"}}'\n",
        quoted(&args.to_string_lossy())
    );
    let command = LaunchCommand::new("/bin/sh").with_prefix(["-c", script.as_str(), "argv0"]);
    let process = spawn(&command, None).unwrap();
    let _ = start(&process, Duration::from_secs(1), QUICK);
    let received: Vec<String> = fs::read_to_string(args)
        .unwrap()
        .lines()
        .map(str::to_owned)
        .collect();
    let mut expected = vec!["argv0".to_owned()];
    expected.extend(CLI_ARGS.iter().map(|value| (*value).to_owned()));
    assert_eq!(received, expected);
    assert_eq!(
        CLI_ARGS,
        ["gui", "--no-open", "--json", "--port", "0", "--stdin-stop"]
    );
}

#[test]
fn stderr_goes_to_a_private_log_file() {
    let dir = tempdir().unwrap();
    let log = dir.path().join("launcher.log");
    let body = "echo diagnostic >&2\nprintf '%s\\n' '{\"ok\":false,\"error\":\"done\"}'\nexit 1\n";
    let process = spawn(&fake(dir.path(), body), Some(&log)).unwrap();
    let _ = start(&process, Duration::from_secs(1), QUICK);
    assert_eq!(fs::read_to_string(&log).unwrap(), "diagnostic\n");
    assert_eq!(
        fs::metadata(&log).unwrap().permissions().mode() & 0o777,
        0o600
    );
    let link = dir.path().join("linked.log");
    std::os::unix::fs::symlink(&log, &link).unwrap();
    assert!(matches!(
        spawn(&fake(dir.path(), "exit 0\n"), Some(&link)),
        Err(LauncherError::Io)
    ));
}

#[cfg(target_os = "macos")]
#[test]
fn child_inherits_only_stdio_even_when_the_shell_holds_inheritable_fds() {
    let mut fds = [0; 2];
    assert_eq!(unsafe { libc::pipe(fds.as_mut_ptr()) }, 0);
    let probe = "my @open = grep { open(my $r, q(<&=), $_) || open(my $w, q(>&=), $_) } 3..255; \
                 print qq({\"ok\":false,\"error\":\"fds:@open\"}\\n);";
    let command = LaunchCommand::new("/usr/bin/perl").with_prefix(["-e", probe]);
    let process = spawn(&command, None).unwrap();
    let error = start(&process, Duration::from_secs(5), QUICK).unwrap_err();
    unsafe {
        libc::close(fds[0]);
        libc::close(fds[1]);
    }
    assert_eq!(error.to_string(), "launcher failed: fds:");
}

#[test]
fn an_endless_stdout_line_is_refused_while_it_is_still_being_written() {
    let command = LaunchCommand::new("/usr/bin/perl").with_prefix([
        "-e",
        "$| = 1; for (1..200) { print 'x' x 65536; } sleep 30;",
    ]);
    let process = spawn(&command, None).unwrap();
    let started = Instant::now();
    let error = start(&process, Duration::from_secs(20), QUICK).unwrap_err();
    assert!(
        matches!(
            error,
            LauncherError::InvalidRecord(hyprial_shell_launcher::RecordError::TooLong)
        ),
        "{error:?}"
    );
    assert!(started.elapsed() < Duration::from_secs(10));
}
