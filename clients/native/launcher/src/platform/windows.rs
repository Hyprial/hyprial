//! Windows primitives: the leader and everything it starts live in one Job
//! Object that the shell owns; the leader is polled with
//! `std::process::Child::try_wait` and the job is terminated as a whole.
//!
//! Decisions (W1C-LAUNCHER, Windows portability 2026-11-21):
//! - The job is created per child with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`:
//!   its only handle is held by `Process` and is not inheritable, so when the
//!   shell exits, crashes or drops the last `ChildProcess`, the system kills
//!   every process still in the job. That is the POSIX "group" guarantee: the
//!   shell never leaves `hyprial gui` or its descendants behind.
//! - The job also sets `JOB_OBJECT_LIMIT_BREAKAWAY_OK` (and not
//!   `SILENT_BREAKAWAY_OK`): a descendant leaves the job only when it is
//!   created with `CREATE_BREAKAWAY_FROM_JOB`, the Windows counterpart of
//!   `start_new_session` on POSIX. The long-lived daemon `hyprial gui` ensures
//!   must outlive the GUI ("closing the GUI does not stop the daemon"), so the
//!   Python side has to spawn it that way; everything else stays inside.
//! - No race between spawn and assignment: the child is created with
//!   `CREATE_SUSPENDED`, assigned to the job, and only then its (single,
//!   initial) thread is resumed, so it cannot start a grandchild outside the
//!   job. std does not expose the primary thread handle, so the thread is
//!   found with a Toolhelp thread snapshot filtered by the child's pid. Only
//!   `ERROR_NO_MORE_FILES` ends that walk; any other `Thread32First` or
//!   `Thread32Next` failure propagates `GetLastError` so a mid-snapshot error
//!   cannot leave threads suspended. If assignment or resume fails the
//!   suspended child is terminated and reaped and the spawn fails with that
//!   error; nothing runs outside a job.
//! - `CREATE_NO_WINDOW`: the shell is a GUI-subsystem process and `hyprial
//!   gui` is a console program; without it Windows would open a console
//!   window for the child.
//! - Stdio is std's: std holds its process-creation lock while the child's
//!   inheritable pipe ends exist, and the shell's ends (stdin write, stdout
//!   read) are never inheritable, so closing stdin delivers EOF to the child
//!   regardless of other spawns. That EOF is the graceful stop (`--stdin-stop`).
//! - Windows has no SIGTERM for a console-less child, so `request_stop`
//!   delivers nothing and returns `false`: the policy in `child.rs` then keeps
//!   waiting for the EOF exit through the second budget too, and only after
//!   both budgets terminates the job. Leftover job members after the leader
//!   has exited are terminated at once (no gentle signal could precede it).
//! - A terminated job's processes exit with `TERMINATED_EXIT_CODE` (1); on
//!   Windows `ExitStatus::code` is always `Some`.

use super::Spawned;
use crate::command::LaunchCommand;
use std::ffi::c_void;
use std::fs::File;
use std::io;
use std::mem::size_of;
use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
use std::os::windows::process::CommandExt;
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::{Mutex, MutexGuard};
use windows_sys::Win32::Foundation::{
    GetLastError, ERROR_NO_MORE_FILES, HANDLE, INVALID_HANDLE_VALUE, WIN32_ERROR,
};
use windows_sys::Win32::System::Diagnostics::ToolHelp::{
    CreateToolhelp32Snapshot, Thread32First, Thread32Next, TH32CS_SNAPTHREAD, THREADENTRY32,
};
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JobObjectBasicAccountingInformation,
    JobObjectExtendedLimitInformation, QueryInformationJobObject, SetInformationJobObject,
    TerminateJobObject, JOBOBJECT_BASIC_ACCOUNTING_INFORMATION,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT, JOB_OBJECT_LIMIT_BREAKAWAY_OK,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};
use windows_sys::Win32::System::Threading::{
    OpenThread, ResumeThread, CREATE_NO_WINDOW, CREATE_SUSPENDED, THREAD_SUSPEND_RESUME,
};

pub const TERMINATED_EXIT_CODE: u32 = 1;
const JOB_LIMITS: JOB_OBJECT_LIMIT =
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK;

pub struct Process {
    child: Mutex<Child>,
    job: Job,
    pid: u32,
}

impl Process {
    pub fn id(&self) -> u32 {
        self.pid
    }

    pub fn try_wait(&self) -> io::Result<Option<ExitStatus>> {
        lock(&self.child).try_wait()
    }

    pub fn request_stop(&self) -> bool {
        false
    }

    pub fn kill_group(&self) {
        let _ = self.job.terminate();
    }

    pub fn group_alive(&self) -> bool {
        self.job.active_processes().map_or(true, |count| count > 0)
    }
}

struct Job(OwnedHandle);

impl Job {
    fn new() -> io::Result<Job> {
        let raw = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
        if raw.is_null() {
            return Err(io::Error::last_os_error());
        }
        let job = Job(unsafe { OwnedHandle::from_raw_handle(raw) });
        let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        limits.BasicLimitInformation.LimitFlags = JOB_LIMITS;
        cvt(unsafe {
            SetInformationJobObject(
                job.raw(),
                JobObjectExtendedLimitInformation,
                (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast::<c_void>(),
                size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
        })?;
        Ok(job)
    }

    fn raw(&self) -> HANDLE {
        self.0.as_raw_handle()
    }

    fn assign(&self, child: &Child) -> io::Result<()> {
        cvt(unsafe { AssignProcessToJobObject(self.raw(), child.as_raw_handle()) })
    }

    fn terminate(&self) -> io::Result<()> {
        cvt(unsafe { TerminateJobObject(self.raw(), TERMINATED_EXIT_CODE) })
    }

    fn active_processes(&self) -> io::Result<u32> {
        let mut info = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION::default();
        cvt(unsafe {
            QueryInformationJobObject(
                self.raw(),
                JobObjectBasicAccountingInformation,
                (&mut info as *mut JOBOBJECT_BASIC_ACCOUNTING_INFORMATION).cast::<c_void>(),
                size_of::<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION>() as u32,
                std::ptr::null_mut(),
            )
        })?;
        Ok(info.ActiveProcesses)
    }
}

pub fn spawn_child(
    command: &LaunchCommand,
    args: &[&str],
    stderr: Option<File>,
) -> io::Result<Spawned> {
    let job = Job::new()?;
    let stderr = match stderr {
        Some(file) => Stdio::from(file),
        None => Stdio::null(),
    };
    let mut process = Command::new(command.program());
    process
        .args(command.prefix())
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(stderr)
        .creation_flags(CREATE_SUSPENDED | CREATE_NO_WINDOW);
    for change in command.environment() {
        match change {
            crate::home::EnvChange::Set(name, value) => process.env(name, value),
            crate::home::EnvChange::Remove(name) => process.env_remove(name),
        };
    }
    let mut child = process.spawn()?;
    let pid = child.id();
    if let Err(error) = job.assign(&child).and_then(|()| resume_threads(pid)) {
        let _ = child.kill();
        let _ = child.wait();
        return Err(error);
    }
    let stdin = child.stdin.take().ok_or(io::ErrorKind::BrokenPipe)?;
    let stdout = child.stdout.take().ok_or(io::ErrorKind::BrokenPipe)?;
    Ok(Spawned {
        process: Process {
            child: Mutex::new(child),
            job,
            pid,
        },
        stdin: File::from(OwnedHandle::from(stdin)),
        stdout: File::from(OwnedHandle::from(stdout)),
    })
}

fn resume_threads(pid: u32) -> io::Result<()> {
    let raw = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0) };
    if raw == INVALID_HANDLE_VALUE {
        return Err(io::Error::last_os_error());
    }
    let snapshot = unsafe { OwnedHandle::from_raw_handle(raw) };
    let mut entry = THREADENTRY32 {
        dwSize: size_of::<THREADENTRY32>() as u32,
        ..THREADENTRY32::default()
    };
    let mut resumed = 0;
    let mut more = thread_step(unsafe { Thread32First(snapshot.as_raw_handle(), &mut entry) })?;
    while more {
        if entry.th32OwnerProcessID == pid {
            let thread = unsafe { OpenThread(THREAD_SUSPEND_RESUME, 0, entry.th32ThreadID) };
            if thread.is_null() {
                return Err(io::Error::last_os_error());
            }
            let thread = unsafe { OwnedHandle::from_raw_handle(thread) };
            if unsafe { ResumeThread(thread.as_raw_handle()) } == u32::MAX {
                return Err(io::Error::last_os_error());
            }
            resumed += 1;
        }
        more = thread_step(unsafe { Thread32Next(snapshot.as_raw_handle(), &mut entry) })?;
    }
    if resumed == 0 {
        return Err(io::Error::from(io::ErrorKind::NotFound));
    }
    Ok(())
}

fn thread_step(result: windows_sys::core::BOOL) -> io::Result<bool> {
    if result != 0 {
        Ok(true)
    } else {
        thread_step_error(unsafe { GetLastError() })
    }
}

fn thread_step_error(error: WIN32_ERROR) -> io::Result<bool> {
    if error == ERROR_NO_MORE_FILES {
        Ok(false)
    } else {
        Err(io::Error::from_raw_os_error(error as i32))
    }
}

fn cvt(result: windows_sys::core::BOOL) -> io::Result<()> {
    if result == 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{Duration, Instant};
    use windows_sys::Win32::Foundation::{ERROR_INVALID_HANDLE, WAIT_OBJECT_0};
    use windows_sys::Win32::System::JobObjects::{
        IsProcessInJob, JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK,
    };
    use windows_sys::Win32::System::Threading::{
        OpenProcess, WaitForSingleObject, PROCESS_SYNCHRONIZE,
    };

    fn spawn_cmd(script: &[&str]) -> Spawned {
        let command = LaunchCommand::new("cmd.exe").with_prefix(["/d", "/c"]);
        spawn_child(&command, script, None).unwrap()
    }

    fn wait_exit(process: &Process, timeout: Duration) -> Option<ExitStatus> {
        let deadline = Instant::now() + timeout;
        loop {
            if let Some(status) = process.try_wait().unwrap() {
                return Some(status);
            }
            if Instant::now() >= deadline {
                return None;
            }
            std::thread::sleep(Duration::from_millis(25));
        }
    }

    fn wait_members(process: &Process, minimum: u32) {
        let deadline = Instant::now() + Duration::from_secs(10);
        while process.job.active_processes().unwrap() < minimum {
            assert!(Instant::now() < deadline, "job never reached {minimum}");
            std::thread::sleep(Duration::from_millis(25));
        }
    }

    #[test]
    fn thread_step_distinguishes_end_of_snapshot_from_errors() {
        assert!(!thread_step_error(ERROR_NO_MORE_FILES).unwrap());
        let err = thread_step_error(ERROR_INVALID_HANDLE).unwrap_err();
        assert_eq!(err.raw_os_error(), Some(ERROR_INVALID_HANDLE as i32));

        let mut entry = THREADENTRY32 {
            dwSize: size_of::<THREADENTRY32>() as u32,
            ..THREADENTRY32::default()
        };
        assert!(thread_step(unsafe { Thread32First(INVALID_HANDLE_VALUE, &mut entry) }).is_err());
        assert!(thread_step(unsafe { Thread32Next(INVALID_HANDLE_VALUE, &mut entry) }).is_err());
    }

    #[test]
    fn job_kills_on_close_and_allows_only_explicit_breakaway() {
        let job = Job::new().unwrap();
        let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        cvt(unsafe {
            QueryInformationJobObject(
                job.raw(),
                JobObjectExtendedLimitInformation,
                (&mut limits as *mut JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast::<c_void>(),
                size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
                std::ptr::null_mut(),
            )
        })
        .unwrap();
        let flags = limits.BasicLimitInformation.LimitFlags;
        assert_ne!(flags & JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, 0);
        assert_ne!(flags & JOB_OBJECT_LIMIT_BREAKAWAY_OK, 0);
        assert_eq!(flags & JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK, 0);
    }

    #[test]
    fn spawned_child_is_in_the_job_and_exits_on_stdin_eof() {
        let spawned = spawn_cmd(&["sort"]);
        let mut in_job = 0;
        let child = lock(&spawned.process.child);
        cvt(unsafe {
            IsProcessInJob(
                child.as_raw_handle(),
                spawned.process.job.raw(),
                &mut in_job,
            )
        })
        .unwrap();
        drop(child);
        assert_ne!(in_job, 0);
        assert!(spawned.process.try_wait().unwrap().is_none());
        drop(spawned.stdin);
        let status = wait_exit(&spawned.process, Duration::from_secs(10)).unwrap();
        assert!(status.success(), "{status:?}");
    }

    #[test]
    fn request_stop_delivers_nothing() {
        let spawned = spawn_cmd(&["sort"]);
        assert!(!spawned.process.request_stop());
        assert!(spawned.process.try_wait().unwrap().is_none());
        spawned.process.kill_group();
        assert!(wait_exit(&spawned.process, Duration::from_secs(10)).is_some());
    }

    #[test]
    fn kill_group_terminates_the_leader_and_its_grandchildren() {
        let spawned = spawn_cmd(&[
            "start",
            "/b",
            "ping",
            "-n",
            "300",
            "127.0.0.1",
            "&",
            "ping",
            "-n",
            "300",
            "127.0.0.1",
        ]);
        wait_members(&spawned.process, 3);
        assert!(spawned.process.group_alive());
        spawned.process.kill_group();
        let status = wait_exit(&spawned.process, Duration::from_secs(10)).unwrap();
        assert_eq!(status.code(), Some(TERMINATED_EXIT_CODE as i32));
        let deadline = Instant::now() + Duration::from_secs(10);
        while spawned.process.group_alive() {
            assert!(Instant::now() < deadline, "job members survived");
            std::thread::sleep(Duration::from_millis(25));
        }
    }

    #[test]
    fn dropping_the_process_closes_the_job_and_kills_its_members() {
        let spawned = spawn_cmd(&["ping", "-n", "300", "127.0.0.1"]);
        let raw = unsafe { OpenProcess(PROCESS_SYNCHRONIZE, 0, spawned.process.id()) };
        assert!(!raw.is_null());
        let watched = unsafe { OwnedHandle::from_raw_handle(raw) };
        assert_ne!(
            unsafe { WaitForSingleObject(watched.as_raw_handle(), 0) },
            WAIT_OBJECT_0
        );
        drop(spawned);
        assert_eq!(
            unsafe { WaitForSingleObject(watched.as_raw_handle(), 10_000) },
            WAIT_OBJECT_0
        );
    }
}
