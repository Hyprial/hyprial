//! POSIX primitives: the leader runs in its own process group, is reaped with
//! `waitpid` and the group is signalled with SIGTERM / SIGKILL.
//!
//! Decisions (shell-6.md §B.3 steps 3, 4, 7, 8 and §D.3 6b-4; moved here from
//! `child.rs` unchanged by W1C-LAUNCHER 2026-11-21):
//! - The child runs in its own process group, with stdin a pipe the shell keeps
//!   open, stdout a pipe and stderr appended to `launcher.log` or null.
//! - `try_wait` is `waitpid(pid, WNOHANG)`; the caller caches the first status,
//!   so the pid is never waited for twice.
//! - `request_stop` is SIGTERM to the group, `kill_group` SIGKILL to the group,
//!   `group_alive` is signal 0 to the group. The leader's pid is not reused
//!   while the group still has members, and the window in which an empty
//!   group's number could be reallocated is accepted.
//!
//! Spawning (flake rework 2026-11-16, measured on macOS 26.5, evidence
//! `12-flake-rework.txt` in the PR):
//! - On macOS, `std::process::Command` creates pipes with `pipe()` followed by
//!   a separate `FD_CLOEXEC` (macOS has no `pipe2`) and spawns with
//!   `posix_spawn` without `POSIX_SPAWN_CLOEXEC_DEFAULT`. A spawn on another
//!   thread between those two calls makes the other child inherit this child's
//!   stdin write end across `exec`; EOF is then not delivered until that other
//!   process exits. Measured: the leader slept in `read` (state `S`, no CPU)
//!   for 370–2800 ms after stdin was closed, only when spawns ran concurrently.
//! - The launcher therefore spawns on macOS with its own `posix_spawnp` and
//!   `POSIX_SPAWN_CLOEXEC_DEFAULT`: the child receives exactly stdin, stdout
//!   and stderr, nothing else from the shell, whatever another thread is doing.
//!   It also sets its own process group (`POSIX_SPAWN_SETPGROUP`), an empty
//!   signal mask and default SIGPIPE, as std does.
//! - Elsewhere (Linux), std creates pipes with `pipe2(O_CLOEXEC)` and opens
//!   files with `O_CLOEXEC`, so std's `Command` is used unchanged; its `Child`
//!   is dropped without waiting and the pid is reaped by `try_wait`.
//! - The child's environment is the shell's (`std::env::vars_os`, read under
//!   std's environment lock) with the command's `EnvChange`s applied
//!   (`home::apply_environment`); on macOS it is passed as an explicit envp.

use super::Spawned;
use crate::command::LaunchCommand;
use std::fs::File;
use std::io;
use std::os::unix::process::ExitStatusExt;
use std::process::ExitStatus;

pub struct Process {
    pid: libc::pid_t,
}

impl Process {
    pub fn id(&self) -> u32 {
        self.pid as u32
    }

    pub fn try_wait(&self) -> io::Result<Option<ExitStatus>> {
        let mut raw = 0;
        let result = unsafe { libc::waitpid(self.pid, &mut raw, libc::WNOHANG) };
        match result {
            0 => Ok(None),
            value if value == self.pid => Ok(Some(ExitStatus::from_raw(raw))),
            _ => {
                let error = io::Error::last_os_error();
                if error.kind() == io::ErrorKind::Interrupted {
                    Ok(None)
                } else {
                    Err(error)
                }
            }
        }
    }

    pub fn request_stop(&self) -> bool {
        self.signal_group(libc::SIGTERM);
        true
    }

    pub fn kill_group(&self) {
        self.signal_group(libc::SIGKILL);
    }

    pub fn group_alive(&self) -> bool {
        unsafe { libc::kill(-self.pid, 0) == 0 }
    }

    fn signal_group(&self, signal: libc::c_int) {
        let _ = unsafe { libc::kill(-self.pid, signal) };
    }
}

#[cfg(target_os = "macos")]
pub fn spawn_child(
    command: &LaunchCommand,
    args: &[&str],
    stderr: Option<File>,
) -> io::Result<Spawned> {
    use std::ffi::CString;
    use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
    use std::os::unix::ffi::OsStrExt;

    fn cvt(value: libc::c_int) -> io::Result<libc::c_int> {
        if value == -1 {
            Err(io::Error::last_os_error())
        } else {
            Ok(value)
        }
    }

    fn cvt_nz(value: libc::c_int) -> io::Result<()> {
        if value == 0 {
            Ok(())
        } else {
            Err(io::Error::from_raw_os_error(value))
        }
    }

    fn pipe() -> io::Result<(OwnedFd, OwnedFd)> {
        let mut fds = [0; 2];
        cvt(unsafe { libc::pipe(fds.as_mut_ptr()) })?;
        let read = unsafe { OwnedFd::from_raw_fd(fds[0]) };
        let write = unsafe { OwnedFd::from_raw_fd(fds[1]) };
        for fd in [&read, &write] {
            cvt(unsafe { libc::fcntl(fd.as_raw_fd(), libc::F_SETFD, libc::FD_CLOEXEC) })?;
        }
        Ok((read, write))
    }

    fn above_stdio(fd: OwnedFd) -> io::Result<OwnedFd> {
        if fd.as_raw_fd() > 2 {
            return Ok(fd);
        }
        let copy = cvt(unsafe { libc::fcntl(fd.as_raw_fd(), libc::F_DUPFD_CLOEXEC, 3) })?;
        Ok(unsafe { OwnedFd::from_raw_fd(copy) })
    }

    fn c_string(value: &[u8]) -> io::Result<CString> {
        CString::new(value).map_err(|_| io::Error::from(io::ErrorKind::InvalidInput))
    }

    struct Actions(libc::posix_spawn_file_actions_t);
    impl Drop for Actions {
        fn drop(&mut self) {
            unsafe { libc::posix_spawn_file_actions_destroy(&mut self.0) };
        }
    }
    struct Attributes(libc::posix_spawnattr_t);
    impl Drop for Attributes {
        fn drop(&mut self) {
            unsafe { libc::posix_spawnattr_destroy(&mut self.0) };
        }
    }

    let (stdin_read, stdin_write) = pipe()?;
    let (stdout_read, stdout_write) = pipe()?;
    let stderr: OwnedFd = match stderr {
        Some(file) => file.into(),
        None => File::options().write(true).open("/dev/null")?.into(),
    };
    let child_stdin = above_stdio(stdin_read)?;
    let child_stdout = above_stdio(stdout_write)?;
    let child_stderr = above_stdio(stderr)?;

    let program = c_string(command.program().as_os_str().as_bytes())?;
    let mut argv_owned = vec![program.clone()];
    for value in command.prefix() {
        argv_owned.push(c_string(value.as_bytes())?);
    }
    for value in args {
        argv_owned.push(c_string(value.as_bytes())?);
    }
    let mut argv: Vec<*mut libc::c_char> = argv_owned
        .iter()
        .map(|value| value.as_ptr() as *mut libc::c_char)
        .collect();
    argv.push(std::ptr::null_mut());

    let mut actions = Actions(std::ptr::null_mut());
    cvt_nz(unsafe { libc::posix_spawn_file_actions_init(&mut actions.0) })?;
    for (fd, target) in [(&child_stdin, 0), (&child_stdout, 1), (&child_stderr, 2)] {
        cvt_nz(unsafe {
            libc::posix_spawn_file_actions_adddup2(&mut actions.0, fd.as_raw_fd(), target)
        })?;
    }

    let mut attributes = Attributes(std::ptr::null_mut());
    cvt_nz(unsafe { libc::posix_spawnattr_init(&mut attributes.0) })?;
    let mut empty: libc::sigset_t = 0;
    let mut pipe_default: libc::sigset_t = 0;
    unsafe {
        libc::sigemptyset(&mut empty);
        libc::sigemptyset(&mut pipe_default);
        libc::sigaddset(&mut pipe_default, libc::SIGPIPE);
    }
    cvt_nz(unsafe { libc::posix_spawnattr_setsigmask(&mut attributes.0, &empty) })?;
    cvt_nz(unsafe { libc::posix_spawnattr_setsigdefault(&mut attributes.0, &pipe_default) })?;
    cvt_nz(unsafe { libc::posix_spawnattr_setpgroup(&mut attributes.0, 0) })?;
    let flags = libc::POSIX_SPAWN_CLOEXEC_DEFAULT
        | libc::POSIX_SPAWN_SETPGROUP
        | libc::POSIX_SPAWN_SETSIGMASK
        | libc::POSIX_SPAWN_SETSIGDEF;
    cvt_nz(unsafe { libc::posix_spawnattr_setflags(&mut attributes.0, flags as libc::c_short) })?;

    let environment_owned =
        crate::home::apply_environment(std::env::vars_os(), command.environment())
            .into_iter()
            .map(|(key, value)| {
                let mut entry = key.as_bytes().to_vec();
                entry.push(b'=');
                entry.extend_from_slice(value.as_bytes());
                c_string(&entry)
            })
            .collect::<io::Result<Vec<CString>>>()?;
    let mut environment: Vec<*mut libc::c_char> = environment_owned
        .iter()
        .map(|value| value.as_ptr() as *mut libc::c_char)
        .collect();
    environment.push(std::ptr::null_mut());

    let mut pid: libc::pid_t = 0;
    cvt_nz(unsafe {
        libc::posix_spawnp(
            &mut pid,
            program.as_ptr(),
            &actions.0,
            &attributes.0,
            argv.as_ptr(),
            environment.as_ptr(),
        )
    })?;
    drop((child_stdin, child_stdout, child_stderr));
    Ok(Spawned {
        process: Process { pid },
        stdin: File::from(stdin_write),
        stdout: File::from(stdout_read),
    })
}

#[cfg(not(target_os = "macos"))]
pub fn spawn_child(
    command: &LaunchCommand,
    args: &[&str],
    stderr: Option<File>,
) -> io::Result<Spawned> {
    use std::os::fd::OwnedFd;
    use std::os::unix::process::CommandExt;
    use std::process::{Command, Stdio};

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
        .process_group(0);
    for change in command.environment() {
        match change {
            crate::home::EnvChange::Set(name, value) => process.env(name, value),
            crate::home::EnvChange::Remove(name) => process.env_remove(name),
        };
    }
    let mut child = process.spawn()?;
    let stdin = child.stdin.take().ok_or(io::ErrorKind::BrokenPipe)?;
    let stdout = child.stdout.take().ok_or(io::ErrorKind::BrokenPipe)?;
    let pid = child.id() as libc::pid_t;
    drop(child);
    Ok(Spawned {
        process: Process { pid },
        stdin: File::from(OwnedFd::from(stdin)),
        stdout: File::from(OwnedFd::from(stdout)),
    })
}
