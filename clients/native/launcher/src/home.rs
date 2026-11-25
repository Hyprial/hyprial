//! Which `HYPRIAL_HOME` the app's `hyprial gui` child uses.
//!
//! Decisions (macos-handoff.md stage 3; #2840 merge comment, obligation 3;
//! packaging/clients/README.md "App home"):
//! - The app has its own home, separate from the CLI's default `~/.hyprial`:
//!   the daemon requires an exact package-version match and one lock holder
//!   per home, and the app carries its own hyprial version.
//! - Location `<user home>/.hyprialos` (`len(<user home>) + 11` bytes). Unix
//!   sockets the product binds under a home: `<home>/state/daemon.sock`
//!   (home + 18 bytes, against the macOS AF_UNIX limit of 103, i.e. 104 with
//!   the NUL) and, when a DSH TUI agent runtime starts,
//!   `<home>/state/dsh-tui-<16 hex>/bridge.sock` (home + 43, which DSH caps at
//!   100 bytes itself); `tailnet/zenoh.sock` is shorter, smolvm and tmux
//!   sockets live under `/tmp`. With `~/.hyprialos` the daemon socket fits a
//!   user home of up to 74 bytes and the DSH socket one of up to 46 bytes
//!   (a 39-character user name under the 7-byte macOS user-home prefix).
//!   `~/Library/Application Support/com.hyprial.hyprialos` (the user home
//!   plus 50 bytes) would fit the daemon socket only up to a 35-byte user home and the
//!   DSH socket up to 7 bytes, i.e. never, so it was not taken.
//!   Measurements: evidence `02-socket-limit.txt` of PR 6c.
//! - An explicit, non-empty `HYPRIAL_HOME` in the app's environment wins and
//!   is passed through together with whatever `HARNESS_STATE_DIR` came with
//!   it: tests and smokes redirect the app that way and never touch the real
//!   per-user app home.
//! - Without an explicit home, the app sets `HYPRIAL_HOME` and removes
//!   `HARNESS_STATE_DIR`, so a state directory inherited from a terminal (for
//!   example the default home's) can never be paired with the app home; the
//!   state then lives at `<app home>/state`.
//! - The home the app chooses is created (mode 0700, the user home must
//!   already exist) before the child starts: `hyprial gui` refuses a missing
//!   home with `GUI_HOME_NOT_INITIALIZED` (measured on the bundle, PR 6c
//!   evidence 22). An explicit home is never created by the app.
//! - What is at the app home path is looked at with `lstat` (never followed):
//!   a symbolic link is refused (`AppHomeError::Symlink`) and so is a
//!   regular or other non-directory file (`AppHomeError::NotADirectory`),
//!   each with the path in the message and nothing created or changed. An
//!   existing real directory is kept exactly as it is: its mode and owner are
//!   neither inspected nor repaired (no silent `chmod`); whether a loose
//!   mode or a foreign owner is acceptable is left to `hyprial gui` and the
//!   daemon. Creation uses mode 0700; when it fails with `AlreadyExists`
//!   (another process won the race) the path is looked at again and a real
//!   directory is accepted, a symbolic link or file is refused as above. Any
//!   other failure (for example a missing user home) is
//!   `AppHomeError::Io` with the path and the error kind. All of these reach
//!   the status page as `LauncherError::AppHome`, not as the generic
//!   "launcher process I/O failed".
//! - No user home (none resolvable, or not absolute) is a typed error shown on
//!   the status page; the app never falls back to the CLI's default home.
//! - Pure function of its inputs (no environment reads), like `command`.
//! - Windows (PR #3106 real-machine run, 2026-11-23): "absolute" is the
//!   platform's own notion, so a user home must carry a drive or UNC prefix
//!   (`C:\Users\someone`); a rooted path without one (`\home\someone`) is
//!   `NoHome`. The socket-length budget above is a Unix (AF_UNIX) constraint
//!   only: on Windows `state\daemon.sock` is a regular file naming a named
//!   pipe and has no such limit, so its test is Unix-only. Directory
//!   symbolic links and junctions are both refused as `Symlink`. The 0700
//!   mode has no Windows counterpart: the launcher creates the directory with
//!   the default `DirBuilder` and asserts nothing about its privacy.
//! - Windows privacy (review B-3, 2026-11-23): whether the app home is private
//!   enough is decided and, where needed, hardened by hyprial itself (the
//!   `fix/windows-bootstrap-private-acl` line), not by the launcher. Whether a
//!   directory created here is then accepted by `hyprial gui` on Windows is
//!   UNKNOWN until that line lands and a real Windows run confirms it. The
//!   creation test therefore only claims "created"; the 0700 claim is a
//!   separate Unix-only test.

use std::ffi::{OsStr, OsString};
use std::path::{Path, PathBuf};

pub const APP_HOME_DIR: &str = ".hyprialos";
pub const HOME_VARIABLE: &str = "HYPRIAL_HOME";
pub const STATE_VARIABLE: &str = "HARNESS_STATE_DIR";

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum EnvChange {
    Set(OsString, OsString),
    Remove(OsString),
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NoHome;

impl std::fmt::Display for NoHome {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(
            "no user home directory was found and HYPRIAL_HOME is not set; \
             the app cannot choose its own hyprial home",
        )
    }
}

impl std::error::Error for NoHome {}

pub fn app_environment(
    explicit_home: Option<&OsStr>,
    user_home: Option<&Path>,
) -> Result<Vec<EnvChange>, NoHome> {
    if explicit_home.is_some_and(|value| !value.is_empty()) {
        return Ok(Vec::new());
    }
    let user_home = user_home.filter(|path| path.is_absolute()).ok_or(NoHome)?;
    Ok(vec![
        EnvChange::Set(
            HOME_VARIABLE.into(),
            user_home.join(APP_HOME_DIR).into_os_string(),
        ),
        EnvChange::Remove(STATE_VARIABLE.into()),
    ])
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum AppHomeError {
    Symlink(PathBuf),
    NotADirectory(PathBuf),
    Io {
        path: PathBuf,
        kind: std::io::ErrorKind,
    },
}

impl std::fmt::Display for AppHomeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Symlink(path) => write!(
                f,
                "the app home {} is a symbolic link; the app does not follow it \
                 (remove it, or set HYPRIAL_HOME to use another home)",
                path.display()
            ),
            Self::NotADirectory(path) => write!(
                f,
                "the app home {} exists but is not a directory \
                 (remove it, or set HYPRIAL_HOME to use another home)",
                path.display()
            ),
            Self::Io { path, kind } => {
                write!(
                    f,
                    "the app home {} cannot be created: {kind}",
                    path.display()
                )
            }
        }
    }
}

impl std::error::Error for AppHomeError {}

fn existing_directory(home: &Path) -> Result<bool, AppHomeError> {
    match std::fs::symlink_metadata(home) {
        Ok(metadata) if metadata.file_type().is_symlink() => {
            Err(AppHomeError::Symlink(home.to_path_buf()))
        }
        Ok(metadata) if metadata.is_dir() => Ok(true),
        Ok(_) => Err(AppHomeError::NotADirectory(home.to_path_buf())),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(AppHomeError::Io {
            path: home.to_path_buf(),
            kind: error.kind(),
        }),
    }
}

fn ensure_directory(home: &Path, before_create: impl FnOnce()) -> Result<(), AppHomeError> {
    if existing_directory(home)? {
        return Ok(());
    }
    before_create();
    match private_directory_builder().create(home) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
            if existing_directory(home)? {
                Ok(())
            } else {
                Err(AppHomeError::Io {
                    path: home.to_path_buf(),
                    kind: error.kind(),
                })
            }
        }
        Err(error) => Err(AppHomeError::Io {
            path: home.to_path_buf(),
            kind: error.kind(),
        }),
    }
}

fn private_directory_builder() -> std::fs::DirBuilder {
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        let mut builder = std::fs::DirBuilder::new();
        builder.mode(0o700);
        builder
    }
    #[cfg(not(unix))]
    {
        std::fs::DirBuilder::new()
    }
}

pub fn ensure_app_home(changes: &[EnvChange]) -> Result<(), AppHomeError> {
    for change in changes {
        if let EnvChange::Set(name, value) = change {
            if name == HOME_VARIABLE {
                ensure_directory(Path::new(value), || {})?;
            }
        }
    }
    Ok(())
}

pub fn apply_environment<I>(base: I, changes: &[EnvChange]) -> Vec<(OsString, OsString)>
where
    I: IntoIterator<Item = (OsString, OsString)>,
{
    let mut environment: Vec<(OsString, OsString)> = base.into_iter().collect();
    for change in changes {
        let name = match change {
            EnvChange::Set(name, _) | EnvChange::Remove(name) => name,
        };
        environment.retain(|(key, _)| key != name);
        if let EnvChange::Set(name, value) = change {
            environment.push((name.clone(), value.clone()));
        }
    }
    environment
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn absolute(unix_path: &str) -> PathBuf {
        if cfg!(windows) {
            PathBuf::from(format!("C:{unix_path}"))
        } else {
            PathBuf::from(unix_path)
        }
    }

    fn someone() -> PathBuf {
        absolute("/home/someone")
    }

    #[test]
    fn explicit_home_wins_and_changes_nothing() {
        let changes = app_environment(
            Some(OsStr::new("/tmp/isolated")),
            Some(Path::new("/home/a")),
        );
        assert_eq!(changes, Ok(Vec::new()));
    }

    #[test]
    fn without_explicit_home_the_app_home_is_set_and_state_dropped() {
        for explicit in [None, Some(OsStr::new(""))] {
            let changes = app_environment(explicit, Some(&someone())).unwrap();
            assert_eq!(
                changes,
                vec![
                    EnvChange::Set(
                        "HYPRIAL_HOME".into(),
                        someone().join(".hyprialos").into_os_string()
                    ),
                    EnvChange::Remove("HARNESS_STATE_DIR".into()),
                ]
            );
        }
    }

    #[test]
    fn app_home_is_never_the_cli_default_home() {
        let changes = app_environment(None, Some(&someone())).unwrap();
        let EnvChange::Set(_, value) = &changes[0] else {
            panic!("first change sets the home");
        };
        assert_ne!(Path::new(value), someone().join(".hyprial"));
    }

    #[test]
    fn missing_or_relative_user_home_is_a_clear_error() {
        assert_eq!(app_environment(None, None), Err(NoHome));
        assert_eq!(
            app_environment(None, Some(Path::new("relative/home"))),
            Err(NoHome)
        );
        assert!(NoHome.to_string().contains("HYPRIAL_HOME"));
    }

    #[cfg(windows)]
    #[test]
    fn a_rooted_user_home_without_a_drive_is_not_absolute_on_windows() {
        assert_eq!(
            app_environment(None, Some(Path::new(r"\home\someone"))),
            Err(NoHome)
        );
        assert!(app_environment(None, Some(Path::new(r"C:\Users\someone"))).is_ok());
        assert!(app_environment(None, Some(Path::new(r"\\server\share\someone"))).is_ok());
    }

    #[cfg(unix)]
    #[test]
    fn home_sockets_fit_their_limits_up_to_the_documented_user_home_lengths() {
        let home_of = |user_home_bytes: usize| {
            let user_home = format!("/home/{}", "u".repeat(user_home_bytes - "/home/".len()));
            let changes = app_environment(None, Some(Path::new(&user_home))).unwrap();
            let EnvChange::Set(_, value) = &changes[0] else {
                panic!("first change sets the home");
            };
            PathBuf::from(value)
        };
        let daemon = home_of(74).join("state/daemon.sock");
        assert_eq!(daemon.as_os_str().len(), 103);
        let bridge = home_of(46).join(format!("state/dsh-tui-{}/bridge.sock", "0".repeat(16)));
        assert_eq!(bridge.as_os_str().len(), 100);
    }

    #[test]
    fn the_chosen_home_is_created_and_an_explicit_one_is_left_alone() {
        let dir = tempfile::tempdir().unwrap();
        let changes = app_environment(None, Some(dir.path())).unwrap();
        ensure_app_home(&changes).unwrap();
        let home = dir.path().join(".hyprialos");
        assert!(home.is_dir());
        ensure_app_home(&changes).unwrap();

        let explicit_home = dir.path().join("explicit");
        let explicit = app_environment(Some(explicit_home.as_os_str()), Some(dir.path()));
        ensure_app_home(&explicit.unwrap()).unwrap();
        assert!(!explicit_home.exists());
    }

    #[cfg(unix)]
    #[test]
    fn the_chosen_home_is_created_with_mode_0700_on_unix() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        ensure_app_home(&app_environment(None, Some(dir.path())).unwrap()).unwrap();
        let home = dir.path().join(".hyprialos");
        let mode = std::fs::metadata(&home).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o700);
    }

    #[cfg(unix)]
    #[test]
    fn an_existing_directory_is_kept_as_it_is_whatever_its_mode() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let home = dir.path().join(".hyprialos");
        std::fs::create_dir(&home).unwrap();
        std::fs::write(home.join("keep"), b"x").unwrap();
        std::fs::set_permissions(&home, std::fs::Permissions::from_mode(0o755)).unwrap();
        ensure_app_home(&app_environment(None, Some(dir.path())).unwrap()).unwrap();
        let mode = std::fs::metadata(&home).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o755);
        assert!(home.join("keep").is_file());
    }

    #[cfg(unix)]
    #[test]
    fn a_symlink_at_the_app_home_is_refused_not_followed() {
        let dir = tempfile::tempdir().unwrap();
        let target = dir.path().join("elsewhere");
        std::fs::create_dir(&target).unwrap();
        let home = dir.path().join(".hyprialos");
        std::os::unix::fs::symlink(&target, &home).unwrap();
        let error = ensure_app_home(&app_environment(None, Some(dir.path())).unwrap());
        assert_eq!(error, Err(AppHomeError::Symlink(home.clone())));
        assert!(error.unwrap_err().to_string().contains("symbolic link"));
        assert!(std::fs::symlink_metadata(&home)
            .unwrap()
            .file_type()
            .is_symlink());
        assert_eq!(std::fs::read_dir(&target).unwrap().count(), 0);

        let dangling = dir.path().join("dangling");
        std::os::unix::fs::symlink(dir.path().join("missing"), &dangling).unwrap();
        assert_eq!(
            ensure_directory(&dangling, || {}),
            Err(AppHomeError::Symlink(dangling.clone()))
        );
        assert!(!dir.path().join("missing").exists());
    }

    #[cfg(windows)]
    #[test]
    fn a_junction_at_the_app_home_is_refused_not_followed() {
        let dir = tempfile::tempdir().unwrap();
        let target = dir.path().join("elsewhere");
        std::fs::create_dir(&target).unwrap();
        let home = dir.path().join(".hyprialos");
        let status = std::process::Command::new("cmd")
            .arg("/c")
            .arg("mklink")
            .arg("/J")
            .arg(&home)
            .arg(&target)
            .stdout(std::process::Stdio::null())
            .status()
            .unwrap();
        assert!(status.success());
        let error = ensure_app_home(&app_environment(None, Some(dir.path())).unwrap());
        assert_eq!(error, Err(AppHomeError::Symlink(home)));
        assert_eq!(std::fs::read_dir(&target).unwrap().count(), 0);
    }

    #[test]
    fn a_regular_file_at_the_app_home_is_refused() {
        let dir = tempfile::tempdir().unwrap();
        let home = dir.path().join(".hyprialos");
        std::fs::write(&home, b"not a directory").unwrap();
        let error = ensure_app_home(&app_environment(None, Some(dir.path())).unwrap());
        assert_eq!(error, Err(AppHomeError::NotADirectory(home.clone())));
        assert!(error.unwrap_err().to_string().contains("not a directory"));
        assert_eq!(std::fs::read(&home).unwrap(), b"not a directory");
    }

    #[test]
    fn a_directory_that_appears_between_the_check_and_the_create_is_accepted() {
        let dir = tempfile::tempdir().unwrap();
        let home = dir.path().join(".hyprialos");
        let concurrent = home.clone();
        ensure_directory(&home, move || std::fs::create_dir(&concurrent).unwrap()).unwrap();
        assert!(home.is_dir());
    }

    #[cfg(unix)]
    #[test]
    fn a_symlink_that_appears_between_the_check_and_the_create_is_refused() {
        let dir = tempfile::tempdir().unwrap();
        let home = dir.path().join(".hyprialos");
        let (target, link) = (dir.path().to_path_buf(), home.clone());
        let error = ensure_directory(&home, move || {
            std::os::unix::fs::symlink(&target, &link).unwrap()
        });
        assert_eq!(error, Err(AppHomeError::Symlink(home)));
    }

    #[test]
    fn app_home_errors_reach_the_status_page_as_their_own_launcher_error() {
        use crate::child::LauncherError;
        let error = LauncherError::from(AppHomeError::NotADirectory("/x/.hyprialos".into()));
        assert!(matches!(error, LauncherError::AppHome(_)));
        let text = error.to_string();
        assert!(text.contains("/x/.hyprialos") && text.contains("not a directory"));
        assert!(!text.contains("launcher process I/O failed"));
    }

    #[test]
    fn a_missing_user_home_is_a_typed_io_error() {
        let dir = tempfile::tempdir().unwrap();
        let home = dir.path().join("no-such-user-home").join(".hyprialos");
        let error = ensure_directory(&home, || {}).unwrap_err();
        assert_eq!(
            error,
            AppHomeError::Io {
                path: home,
                kind: std::io::ErrorKind::NotFound
            }
        );
        assert!(error.to_string().contains("cannot be created"));
    }

    #[test]
    fn apply_sets_replaces_and_removes() {
        let base = vec![
            (OsString::from("PATH"), OsString::from("/usr/bin")),
            (OsString::from("HYPRIAL_HOME"), OsString::from("/old")),
            (
                OsString::from("HARNESS_STATE_DIR"),
                OsString::from("/old/state"),
            ),
        ];
        let changes = app_environment(None, Some(&someone())).unwrap();
        let applied = apply_environment(base, &changes);
        assert_eq!(
            applied,
            vec![
                (OsString::from("PATH"), OsString::from("/usr/bin")),
                (
                    OsString::from("HYPRIAL_HOME"),
                    someone().join(".hyprialos").into_os_string()
                ),
            ]
        );
    }
}
