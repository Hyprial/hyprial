//! Supervise the `hyprial gui` child: spawn, read its stdout, stop it.
//!
//! Decisions (shell-6.md §B.3 steps 3, 4, 7, 8 and §D.3 6b-4):
//! - The child runs with stdin a pipe the shell keeps open, stdout a pipe and
//!   stderr appended to `launcher.log` (created 0600 and never followed through
//!   a symlink on POSIX) or null. How it is started, reaped and signalled is in
//!   `crate::platform` (POSIX process group, Windows Job Object); this file is
//!   the platform-independent policy.
//! - The leader is reaped through `platform::Process::try_wait`, serialised by
//!   the status lock; its first status is cached here.
//! - `ChildProcess` is a cheap, cloneable handle. Every lock it takes is held
//!   only for a non-blocking call (`try_wait`, `kill`, taking stdin), except the
//!   stdout receiver, which only the thread reading the startup line and the
//!   session end uses. Stopping therefore never waits behind a 60 s startup read.
//! - Stop is the planned three-stage sequence: close stdin, wait 8 s, gentle
//!   stop of the group (SIGTERM on POSIX; nothing on Windows, where the child
//!   keeps the second budget to finish its EOF exit), wait 7 s, kill the group
//!   (SIGKILL / `TerminateJobObject`). After the leader has exited (at any
//!   stage) a group that still has members is stopped again (gentle stop, then
//!   kill after the second budget; on Windows killed at once) so a grandchild
//!   left behind cannot survive.
//! - Dropping the last handle while the child runs closes stdin and requests
//!   the gentle stop; on Windows the job handle closes with it and
//!   `KILL_ON_JOB_CLOSE` ends every member, as it does when the shell exits.
//! - A startup-read timeout only returns `Timeout`; the caller (`start`) then
//!   runs the same three-stage stop as any other stop, so `hyprial gui` can
//!   clean up its own children (they live in their own sessions, or outside
//!   the job, out of reach of a group stop). Review round 2, GUI line decision.
//! - stdout is read line by line with a 64 KiB bound enforced while reading
//!   (`read_bounded_line`), never after buffering an unbounded line; an
//!   over-long line is `InvalidRecord(TooLong)` and reading stops.
//! - After the startup record, at most one more line may follow: a failure line
//!   explaining why the session ended (contract/cli/gui-startup-record.v1.json).
//!   `wait_session_end` returns it together with the exit status.

use crate::command::LaunchCommand;
use crate::platform;
use crate::record::{
    parse_line, FailureRecord, LaunchLine, RecordError, StartupRecord, MAX_LINE_BYTES,
};
use std::fmt::{Display, Formatter};
use std::fs::{File, OpenOptions};
use std::io::{BufRead, BufReader};
use std::path::Path;
use std::process::ExitStatus;
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::sync::{Arc, Mutex, MutexGuard};
use std::thread;
use std::time::{Duration, Instant};

pub const CLI_ARGS: &[&str] = &["gui", "--no-open", "--json", "--port", "0", "--stdin-stop"];
const POLL: Duration = Duration::from_millis(25);
const EXIT_REPORT_GRACE: Duration = Duration::from_secs(2);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct StopBudget {
    pub stdin_grace: Duration,
    pub term_grace: Duration,
}

impl StopBudget {
    pub const PLANNED: StopBudget = StopBudget {
        stdin_grace: Duration::from_secs(8),
        term_grace: Duration::from_secs(7),
    };
}

#[derive(Debug)]
pub enum LauncherError {
    Io,
    NoRuntime,
    NoHome,
    AppHome(crate::home::AppHomeError),
    Timeout,
    InvalidRecord(RecordError),
    InvalidHandoff(crate::handoff::HandoffError),
    Failure { code: Option<String>, error: String },
    Exited(Option<i32>),
}

impl Display for LauncherError {
    fn fmt(&self, f: &mut Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Io => f.write_str("launcher process I/O failed"),
            Self::NoRuntime => Display::fmt(&crate::command::NoRuntime, f),
            Self::NoHome => Display::fmt(&crate::home::NoHome, f),
            Self::AppHome(error) => Display::fmt(error, f),
            Self::Timeout => f.write_str("launcher did not report a startup record in time"),
            Self::InvalidRecord(error) => write!(f, "invalid launcher record: {error}"),
            Self::InvalidHandoff(error) => write!(f, "invalid launch handoff: {error}"),
            Self::Failure { code, error } => match code {
                Some(code) => write!(f, "launcher failed ({code}): {error}"),
                None => write!(f, "launcher failed: {error}"),
            },
            Self::Exited(Some(code)) => write!(f, "launcher exited with status {code}"),
            Self::Exited(None) => f.write_str("launcher exited without a status"),
        }
    }
}

impl std::error::Error for LauncherError {}

impl From<crate::home::NoHome> for LauncherError {
    fn from(_: crate::home::NoHome) -> Self {
        Self::NoHome
    }
}

impl From<crate::home::AppHomeError> for LauncherError {
    fn from(error: crate::home::AppHomeError) -> Self {
        Self::AppHome(error)
    }
}

impl From<crate::command::NoRuntime> for LauncherError {
    fn from(_: crate::command::NoRuntime) -> Self {
        Self::NoRuntime
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ChildStatus {
    Running,
    Exited(Option<i32>),
}

#[derive(Clone, Debug, PartialEq)]
pub struct SessionEnd {
    pub failure: Option<FailureRecord>,
    pub status: Option<i32>,
}

impl Display for SessionEnd {
    fn fmt(&self, f: &mut Formatter<'_>) -> std::fmt::Result {
        match (&self.failure, self.status) {
            (Some(failure), _) => Display::fmt(
                &LauncherError::Failure {
                    code: failure.code.clone(),
                    error: failure.error.clone(),
                },
                f,
            ),
            (None, status) => Display::fmt(&LauncherError::Exited(status), f),
        }
    }
}

struct Inner {
    status: Mutex<Option<ExitStatus>>,
    stdin: Mutex<Option<File>>,
    lines: Mutex<Receiver<Result<String, LauncherError>>>,
    process: platform::Process,
}

impl Drop for Inner {
    fn drop(&mut self) {
        lock(&self.stdin).take();
        let running = reap(&self.process, &self.status).ok().flatten().is_none();
        if running {
            self.process.request_stop();
        }
    }
}

#[derive(Clone)]
pub struct ChildProcess {
    inner: Arc<Inner>,
}

impl ChildProcess {
    pub fn pid(&self) -> u32 {
        self.inner.process.id()
    }

    pub fn read_startup(&self, timeout: Duration) -> Result<StartupRecord, LauncherError> {
        let line = self.read_line(timeout)?;
        match parse_line(&line).map_err(LauncherError::InvalidRecord)? {
            LaunchLine::Startup(record) => Ok(record),
            LaunchLine::Failure(failure) => Err(LauncherError::Failure {
                code: failure.code,
                error: failure.error,
            }),
        }
    }

    pub fn read_line(&self, timeout: Duration) -> Result<String, LauncherError> {
        let received = lock(&self.inner.lines).recv_timeout(timeout);
        match received {
            Ok(Ok(line)) => Ok(line),
            Ok(Err(error)) => Err(error),
            Err(RecvTimeoutError::Timeout) => Err(LauncherError::Timeout),
            Err(RecvTimeoutError::Disconnected) => {
                let status = self.wait_for_exit(EXIT_REPORT_GRACE)?;
                Err(LauncherError::Exited(status.and_then(|value| value.code())))
            }
        }
    }

    pub fn wait_session_end(&self) -> Result<SessionEnd, LauncherError> {
        let mut failure = None;
        loop {
            let received = lock(&self.inner.lines).recv();
            match received {
                Ok(Ok(line)) => {
                    if let Ok(LaunchLine::Failure(record)) = parse_line(&line) {
                        failure.get_or_insert(record);
                    }
                }
                Ok(Err(_)) | Err(_) => break,
            }
        }
        let status = self.wait_blocking()?;
        Ok(SessionEnd {
            failure,
            status: status.code(),
        })
    }

    pub fn status(&self) -> Result<ChildStatus, LauncherError> {
        Ok(match self.try_wait()? {
            Some(status) => ChildStatus::Exited(status.code()),
            None => ChildStatus::Running,
        })
    }

    pub fn stop(&self) -> Result<ExitStatus, LauncherError> {
        self.stop_with(StopBudget::PLANNED)
    }

    pub fn stop_with(&self, budget: StopBudget) -> Result<ExitStatus, LauncherError> {
        lock(&self.inner.stdin).take();
        let status = match self.wait_for_exit(budget.stdin_grace)? {
            Some(status) => status,
            None => {
                self.inner.process.request_stop();
                match self.wait_for_exit(budget.term_grace)? {
                    Some(status) => status,
                    None => {
                        self.inner.process.kill_group();
                        self.wait_blocking()?
                    }
                }
            }
        };
        self.reclaim_group(budget.term_grace);
        Ok(status)
    }

    fn reclaim_group(&self, grace: Duration) {
        let process = &self.inner.process;
        if !process.group_alive() {
            return;
        }
        if process.request_stop() {
            let deadline = Instant::now() + grace;
            while process.group_alive() && Instant::now() < deadline {
                thread::sleep(POLL);
            }
        }
        if process.group_alive() {
            process.kill_group();
        }
    }

    fn try_wait(&self) -> Result<Option<ExitStatus>, LauncherError> {
        reap(&self.inner.process, &self.inner.status).map_err(|_| LauncherError::Io)
    }

    fn wait_for_exit(&self, timeout: Duration) -> Result<Option<ExitStatus>, LauncherError> {
        let deadline = Instant::now() + timeout;
        loop {
            if let Some(status) = self.try_wait()? {
                return Ok(Some(status));
            }
            if Instant::now() >= deadline {
                return Ok(None);
            }
            thread::sleep(POLL);
        }
    }

    fn wait_blocking(&self) -> Result<ExitStatus, LauncherError> {
        loop {
            if let Some(status) = self.try_wait()? {
                return Ok(status);
            }
            thread::sleep(POLL);
        }
    }
}

pub fn spawn(command: &LaunchCommand, log: Option<&Path>) -> Result<ChildProcess, LauncherError> {
    let stderr = match log {
        Some(path) => Some(open_log(path)?),
        None => None,
    };
    let spawned =
        platform::spawn_child(command, CLI_ARGS, stderr).map_err(|_| LauncherError::Io)?;
    let stdout = spawned.stdout;
    let process = spawned.process;
    let stdin = Some(spawned.stdin);
    let (sender, receiver) = mpsc::channel();
    thread::spawn(move || {
        let mut reader = BufReader::new(stdout);
        loop {
            match read_bounded_line(&mut reader, MAX_LINE_BYTES + 2) {
                Ok(None) => break,
                Ok(Some(line)) => {
                    if sender.send(Ok(line)).is_err() {
                        break;
                    }
                }
                Err(error) => {
                    let _ = sender.send(Err(error));
                    break;
                }
            }
        }
    });
    Ok(ChildProcess {
        inner: Arc::new(Inner {
            status: Mutex::new(None),
            stdin: Mutex::new(stdin),
            lines: Mutex::new(receiver),
            process,
        }),
    })
}

fn open_log(path: &Path) -> Result<File, LauncherError> {
    let mut options = OpenOptions::new();
    options.create(true).append(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600).custom_flags(libc::O_NOFOLLOW);
    }
    let file = options.open(path).map_err(|_| LauncherError::Io)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        file.set_permissions(std::fs::Permissions::from_mode(0o600))
            .map_err(|_| LauncherError::Io)?;
    }
    Ok(file)
}

fn reap(
    process: &platform::Process,
    cache: &Mutex<Option<ExitStatus>>,
) -> std::io::Result<Option<ExitStatus>> {
    let mut cached = lock(cache);
    if cached.is_none() {
        *cached = process.try_wait()?;
    }
    Ok(*cached)
}

fn read_bounded_line(
    reader: &mut impl BufRead,
    limit: usize,
) -> Result<Option<String>, LauncherError> {
    let mut line = Vec::new();
    loop {
        let available = reader.fill_buf().map_err(|_| LauncherError::Io)?;
        if available.is_empty() {
            if line.is_empty() {
                return Ok(None);
            }
            break;
        }
        let (take, done) = match available.iter().position(|byte| *byte == b'\n') {
            Some(index) => (index + 1, true),
            None => (available.len(), false),
        };
        if line.len() + take > limit {
            return Err(LauncherError::InvalidRecord(RecordError::TooLong));
        }
        line.extend_from_slice(&available[..take]);
        reader.consume(take);
        if done {
            break;
        }
    }
    String::from_utf8(line)
        .map(Some)
        .map_err(|_| LauncherError::InvalidRecord(RecordError::InvalidJson))
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn planned_stop_budget_is_eight_then_seven_seconds() {
        assert_eq!(StopBudget::PLANNED.stdin_grace, Duration::from_secs(8));
        assert_eq!(StopBudget::PLANNED.term_grace, Duration::from_secs(7));
    }

    #[test]
    fn cli_arguments_are_fixed() {
        assert_eq!(
            CLI_ARGS,
            ["gui", "--no-open", "--json", "--port", "0", "--stdin-stop"]
        );
    }

    #[cfg(windows)]
    const QUICK: StopBudget = StopBudget {
        stdin_grace: Duration::from_millis(300),
        term_grace: Duration::from_millis(300),
    };

    #[cfg(windows)]
    fn windows_command(script: &[&str]) -> LaunchCommand {
        let mut prefix = vec!["/d", "/c"];
        prefix.extend_from_slice(script);
        prefix.extend_from_slice(&["&", "rem"]);
        LaunchCommand::new("cmd.exe").with_prefix(prefix)
    }

    #[cfg(windows)]
    #[test]
    fn windows_stop_returns_within_the_first_budget_when_the_child_exits_on_eof() {
        let process = spawn(&windows_command(&["sort"]), None).unwrap();
        let started = Instant::now();
        let status = process
            .stop_with(StopBudget {
                stdin_grace: Duration::from_secs(10),
                term_grace: Duration::from_millis(100),
            })
            .unwrap();
        assert!(status.success(), "{status:?}");
        assert!(started.elapsed() < Duration::from_secs(10));
        assert!(!process.inner.process.group_alive());
    }

    #[cfg(windows)]
    #[test]
    fn windows_stop_terminates_the_job_only_after_both_budgets() {
        let process = spawn(
            &windows_command(&["ping", "-n", "300", "127.0.0.1", ">nul"]),
            None,
        )
        .unwrap();
        let started = Instant::now();
        let status = process.stop_with(QUICK).unwrap();
        assert_eq!(status.code(), Some(1));
        assert!(started.elapsed() >= QUICK.stdin_grace + QUICK.term_grace);
        assert_eq!(process.status().unwrap(), ChildStatus::Exited(Some(1)));
        assert!(!process.inner.process.group_alive());
    }

    #[cfg(windows)]
    #[test]
    fn windows_grandchild_left_after_a_clean_eof_exit_is_terminated() {
        let process = spawn(
            &windows_command(&[
                "start",
                "/b",
                "ping",
                "-n",
                "300",
                "127.0.0.1",
                ">nul",
                "&",
                "sort",
            ]),
            None,
        )
        .unwrap();
        thread::sleep(Duration::from_millis(500));
        let status = process.stop_with(QUICK).unwrap();
        assert!(status.success(), "{status:?}");
        let deadline = Instant::now() + Duration::from_secs(5);
        while process.inner.process.group_alive() {
            assert!(Instant::now() < deadline, "job members survived");
            thread::sleep(POLL);
        }
    }
}
