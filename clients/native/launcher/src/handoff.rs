//! Read the one-time `launch.url` handoff written by `hyprial gui --no-open`.
//!
//! Decisions (shell-6.md §C.4, §D.3 6b-2):
//! - The file is opened first (`O_NOFOLLOW`, and `O_NONBLOCK` so a FIFO cannot
//!   block the shell), then every check runs on the opened descriptor
//!   (`fstat`): regular file, mode exactly 0600, owned by the effective uid,
//!   at most 4 KiB. The read itself is bounded to 4 KiB + 1 byte.
//! - Exactly one line: `<record origin>/#grant=<value>` + `\n`, no query, no
//!   userinfo. The fragment is exactly one `grant=` parameter: the value is
//!   not empty and contains no `&`, `=` or `#`. A symlink is refused as "not a
//!   regular file".
//! - The grant leaves this module only inside `LaunchUrl`, whose `Debug` and
//!   `Display` are redacted; no error carries file content.
//! - Windows (PR #3106 real-machine run, 2026-11-23): the file is opened with
//!   `FILE_FLAG_OPEN_REPARSE_POINT` (the `O_NOFOLLOW` counterpart: a symbolic
//!   link or junction is opened as itself and then refused as "not a regular
//!   file") and `FILE_FLAG_BACKUP_SEMANTICS` (a directory opens instead of
//!   failing with access denied, so it is refused as "not a regular file" like
//!   on Unix rather than as "could not be opened"). Mode and owner are not
//!   checked on Windows; the handoff directory's ACL is the host's concern.
//! - Windows tests (review B-1, 2026-11-23): a junction can only point at a
//!   directory, and following it still yields "not a regular file", so the
//!   junction test proves only that a directory reparse point is refused. The
//!   no-follow property is proved by a file symbolic link to a valid handoff
//!   file, which would read back `Ok` if followed. Creating it needs Developer
//!   Mode or `SeCreateSymbolicLinkPrivilege`; without either the test panics
//!   with "BLOCKED: symlink privilege" instead of passing silently.
//! - Not done (review B-4): Windows has no `O_NONBLOCK` counterpart here and
//!   no `GetFileType == FILE_TYPE_DISK` check, so a device or pipe path that
//!   opens and reports a regular file could block the read. The path comes
//!   from the launcher's own child; the risk is recorded, not tested.

use std::fmt::{Display, Formatter};
use std::fs::{self, File, OpenOptions};
use std::io::Read;
use std::path::Path;
use url::Url;

const MAX_HANDOFF_BYTES: u64 = 4 * 1024;

#[derive(Clone, PartialEq, Eq)]
pub struct LaunchUrl(String);

impl LaunchUrl {
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl std::fmt::Debug for LaunchUrl {
    fn fmt(&self, f: &mut Formatter<'_>) -> std::fmt::Result {
        f.write_str("LaunchUrl(<redacted>)")
    }
}

impl Display for LaunchUrl {
    fn fmt(&self, f: &mut Formatter<'_>) -> std::fmt::Result {
        f.write_str("<redacted launch URL>")
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum HandoffError {
    PathNotAbsolute,
    OpenFailed,
    NotRegular,
    WrongMode,
    WrongOwner,
    TooLarge,
    ReadFailed,
    InvalidUtf8,
    WrongLineCount,
    InvalidUrl,
    WrongOrigin,
    InvalidFragment,
    QueryNotAllowed,
}

impl Display for HandoffError {
    fn fmt(&self, f: &mut Formatter<'_>) -> std::fmt::Result {
        let value = match self {
            Self::PathNotAbsolute => "handoff path is not absolute",
            Self::OpenFailed => "handoff file could not be opened",
            Self::NotRegular => "handoff path is not a regular file",
            Self::WrongMode => "handoff file mode is not 0600",
            Self::WrongOwner => "handoff file owner is not the current user",
            Self::TooLarge => "handoff file is too large",
            Self::ReadFailed => "handoff file could not be read",
            Self::InvalidUtf8 => "handoff file is not UTF-8",
            Self::WrongLineCount => "handoff file does not contain one line",
            Self::InvalidUrl => "handoff URL is invalid",
            Self::WrongOrigin => "handoff URL origin does not match startup record",
            Self::InvalidFragment => "handoff URL fragment is invalid",
            Self::QueryNotAllowed => "handoff URL query is not allowed",
        };
        f.write_str(value)
    }
}

impl std::error::Error for HandoffError {}

pub fn read_handoff(
    path: impl AsRef<Path>,
    expected_origin: &Url,
) -> Result<LaunchUrl, HandoffError> {
    let path = path.as_ref();
    if !path.is_absolute() {
        return Err(HandoffError::PathNotAbsolute);
    }
    let file = open_nofollow(path)?;
    let metadata = file.metadata().map_err(|_| HandoffError::OpenFailed)?;
    if !metadata.file_type().is_file() {
        return Err(HandoffError::NotRegular);
    }
    if metadata.len() > MAX_HANDOFF_BYTES {
        return Err(HandoffError::TooLarge);
    }
    verify_metadata(&metadata)?;
    let mut bytes = Vec::with_capacity(metadata.len() as usize);
    file.take(MAX_HANDOFF_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| HandoffError::ReadFailed)?;
    if bytes.len() as u64 > MAX_HANDOFF_BYTES {
        return Err(HandoffError::TooLarge);
    }
    if bytes.len() < 2 || bytes.last() != Some(&b'\n') || bytes[..bytes.len() - 1].contains(&b'\n')
    {
        return Err(HandoffError::WrongLineCount);
    }
    let line =
        std::str::from_utf8(&bytes[..bytes.len() - 1]).map_err(|_| HandoffError::InvalidUtf8)?;
    if line.ends_with('\r') || line.is_empty() {
        return Err(HandoffError::WrongLineCount);
    }
    let parsed = Url::parse(line).map_err(|_| HandoffError::InvalidUrl)?;
    if parsed.scheme() != "http"
        || !matches!(parsed.path(), "" | "/")
        || !parsed.username().is_empty()
        || parsed.password().is_some()
        || !same_origin(&parsed, expected_origin)
    {
        return Err(HandoffError::WrongOrigin);
    }
    if parsed.query().is_some() {
        return Err(HandoffError::QueryNotAllowed);
    }
    let fragment = parsed.fragment().ok_or(HandoffError::InvalidFragment)?;
    let grant = fragment
        .strip_prefix("grant=")
        .ok_or(HandoffError::InvalidFragment)?;
    if grant.is_empty() || grant.contains(['&', '=', '#']) {
        return Err(HandoffError::InvalidFragment);
    }
    Ok(LaunchUrl(line.to_owned()))
}

fn same_origin(left: &Url, right: &Url) -> bool {
    left.scheme() == right.scheme()
        && left.host_str() == right.host_str()
        && left.port_or_known_default() == right.port_or_known_default()
}

fn verify_metadata(metadata: &fs::Metadata) -> Result<(), HandoffError> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if metadata.mode() & 0o7777 != 0o600 {
            return Err(HandoffError::WrongMode);
        }
        let uid = unsafe { libc::geteuid() };
        if metadata.uid() != uid {
            return Err(HandoffError::WrongOwner);
        }
    }
    #[cfg(not(unix))]
    {
        let _ = metadata;
    }
    Ok(())
}

fn open_nofollow(path: &Path) -> Result<File, HandoffError> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
            .open(path)
            .map_err(|error| match error.raw_os_error() {
                Some(libc::ELOOP) => HandoffError::NotRegular,
                _ => HandoffError::OpenFailed,
            })
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        use windows_sys::Win32::Storage::FileSystem::{
            FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT,
        };
        OpenOptions::new()
            .read(true)
            .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS)
            .open(path)
            .map_err(|_| HandoffError::OpenFailed)
    }
    #[cfg(not(any(unix, windows)))]
    {
        OpenOptions::new()
            .read(true)
            .open(path)
            .map_err(|_| HandoffError::OpenFailed)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use tempfile::tempdir;

    fn make_file(value: &str) -> (tempfile::TempDir, PathBuf) {
        let dir = tempdir().unwrap();
        let path = dir.path().join("launch.url");
        fs::write(&path, value).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        }
        (dir, path)
    }

    #[test]
    fn accepts_one_valid_handoff_line() {
        let (dir, path) = make_file("http://127.0.0.1:41234/#grant=abc%2B123\n");
        let origin = Url::parse("http://127.0.0.1:41234").unwrap();
        let value = read_handoff(&path, &origin).unwrap();
        assert_eq!(value.as_str(), "http://127.0.0.1:41234/#grant=abc%2B123");
        assert!(format!("{value:?}").find("abc").is_none());
        assert!(format!("{value}").find("abc").is_none());
        drop(dir);
    }

    #[test]
    fn rejects_wrong_mode_and_query() {
        let origin = Url::parse("http://127.0.0.1:41234").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let (_dir, path) = make_file("http://127.0.0.1:41234/#grant=abc\n");
            fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();
            assert_eq!(read_handoff(&path, &origin), Err(HandoffError::WrongMode));
        }
        let (_dir, path) = make_file("http://127.0.0.1:41234/?x=1#grant=abc\n");
        assert_eq!(
            read_handoff(&path, &origin),
            Err(HandoffError::QueryNotAllowed)
        );
    }

    #[cfg(unix)]
    #[test]
    fn rejects_symlink() {
        let dir = tempdir().unwrap();
        let target = dir.path().join("target");
        let link = dir.path().join("launch.url");
        fs::write(&target, "http://127.0.0.1:41234/#grant=abc\n").unwrap();
        std::os::unix::fs::symlink(&target, &link).unwrap();
        let origin = Url::parse("http://127.0.0.1:41234").unwrap();
        assert_eq!(read_handoff(&link, &origin), Err(HandoffError::NotRegular));
    }

    #[test]
    fn rejects_directory_large_file_and_line_shapes() {
        let origin = Url::parse("http://127.0.0.1:41234").unwrap();
        let dir = tempdir().unwrap();
        assert_eq!(
            read_handoff(dir.path(), &origin),
            Err(HandoffError::NotRegular)
        );
        let big = format!("http://127.0.0.1:41234/#grant={}\n", "a".repeat(5000));
        let (_big_dir, path) = make_file(&big);
        assert_eq!(read_handoff(&path, &origin), Err(HandoffError::TooLarge));
        for (value, expected) in [
            (
                "http://127.0.0.1:41234/#grant=a\nhttp://127.0.0.1:41234/#grant=b\n",
                HandoffError::WrongLineCount,
            ),
            (
                "http://127.0.0.1:41234/#grant=a",
                HandoffError::WrongLineCount,
            ),
            (
                "http://127.0.0.1:41234/#grant=a\r\n",
                HandoffError::WrongLineCount,
            ),
            ("\n", HandoffError::WrongLineCount),
            (
                "http://127.0.0.1:41235/#grant=a\n",
                HandoffError::WrongOrigin,
            ),
            (
                "http://localhost:41234/#grant=a\n",
                HandoffError::WrongOrigin,
            ),
            (
                "http://u@127.0.0.1:41234/#grant=a\n",
                HandoffError::WrongOrigin,
            ),
            (
                "http://127.0.0.1:41234/path#grant=a\n",
                HandoffError::WrongOrigin,
            ),
            (
                "http://127.0.0.1:41234/#token=a\n",
                HandoffError::InvalidFragment,
            ),
            (
                "http://127.0.0.1:41234/#grant=\n",
                HandoffError::InvalidFragment,
            ),
            ("http://127.0.0.1:41234/\n", HandoffError::InvalidFragment),
        ] {
            let (_line_dir, path) = make_file(value);
            assert_eq!(read_handoff(&path, &origin), Err(expected), "{value:?}");
        }
    }

    #[cfg(windows)]
    #[test]
    fn a_directory_junction_at_the_handoff_path_is_refused() {
        let dir = tempdir().unwrap();
        let target = dir.path().join("target");
        fs::create_dir(&target).unwrap();
        fs::write(target.join("inner"), "http://127.0.0.1:41234/#grant=abc\n").unwrap();
        let link = dir.path().join("launch.url");
        let status = std::process::Command::new("cmd")
            .arg("/c")
            .arg("mklink")
            .arg("/J")
            .arg(&link)
            .arg(&target)
            .stdout(std::process::Stdio::null())
            .status()
            .unwrap();
        assert!(status.success());
        let origin = Url::parse("http://127.0.0.1:41234").unwrap();
        assert_eq!(read_handoff(&link, &origin), Err(HandoffError::NotRegular));
    }

    #[cfg(windows)]
    #[test]
    fn a_file_symlink_at_the_handoff_path_is_refused_not_followed() {
        const ERROR_PRIVILEGE_NOT_HELD: i32 = 1314;
        let dir = tempdir().unwrap();
        let target = dir.path().join("target.url");
        fs::write(&target, "http://127.0.0.1:41234/#grant=abc\n").unwrap();
        let origin = Url::parse("http://127.0.0.1:41234").unwrap();
        assert!(read_handoff(&target, &origin).is_ok());
        let link = dir.path().join("launch.url");
        match std::os::windows::fs::symlink_file(&target, &link) {
            Ok(()) => {}
            Err(error) if error.raw_os_error() == Some(ERROR_PRIVILEGE_NOT_HELD) => panic!(
                "BLOCKED: symlink privilege not held (enable Developer Mode or grant \
                 SeCreateSymbolicLinkPrivilege); the no-follow property was not tested"
            ),
            Err(error) => panic!("creating the file symlink failed: {error}"),
        }
        assert_eq!(read_handoff(&link, &origin), Err(HandoffError::NotRegular));
    }

    #[cfg(unix)]
    #[test]
    fn fifo_is_refused_without_blocking() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("launch.url");
        let name = std::ffi::CString::new(path.to_str().unwrap()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(name.as_ptr(), 0o600) }, 0);
        let origin = Url::parse("http://127.0.0.1:41234").unwrap();
        assert_eq!(read_handoff(&path, &origin), Err(HandoffError::NotRegular));
    }

    #[test]
    fn errors_never_format_the_grant() {
        let origin = Url::parse("http://127.0.0.1:41234").unwrap();
        for value in [
            "http://127.0.0.1:41235/#grant=SECRETGRANT\n",
            "http://127.0.0.1:41234/?q=1#grant=SECRETGRANT\n",
            "http://127.0.0.1:41234/#grant=SECRETGRANT\nmore\n",
        ] {
            let (_dir, path) = make_file(value);
            let error = read_handoff(&path, &origin).unwrap_err();
            assert!(!format!("{error:?} {error}").contains("SECRETGRANT"));
        }
    }

    #[test]
    fn fragment_must_be_exactly_one_grant_parameter() {
        let origin = Url::parse("http://127.0.0.1:41234").unwrap();
        for value in [
            "http://127.0.0.1:41234/#grant=x&y=z\n",
            "http://127.0.0.1:41234/#grant=x&grant=y\n",
            "http://127.0.0.1:41234/#grant=x=y\n",
            "http://127.0.0.1:41234/#grant=x#y\n",
            "http://127.0.0.1:41234/#xgrant=y\n",
        ] {
            let (_dir, path) = make_file(value);
            assert_eq!(
                read_handoff(&path, &origin),
                Err(HandoffError::InvalidFragment),
                "{value:?}"
            );
        }
        let (_dir, path) = make_file("http://127.0.0.1:41234/#grant=abc%2B%3D%26def\n");
        assert!(read_handoff(&path, &origin).is_ok());
    }
}
