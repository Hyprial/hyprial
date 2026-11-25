//! Decision record (2026-11-23): the startup-record contract admits POSIX and
//! Windows absolute handoff paths, but a launcher only accepts a path that is
//! absolute on its own platform. The contract-example test therefore parses the
//! examples whose handoff path has this platform's shape and expects the others
//! to be refused with InvalidHandoff; no example is rewritten to fit.

use serde::Deserialize;
use serde_json::Value;
use std::fmt::{Display, Formatter};
use std::path::Path;
use url::Url;

pub(crate) const MAX_LINE_BYTES: usize = 64 * 1024;

#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
pub struct StartupRecord {
    pub ok: bool,
    pub record_version: u64,
    pub url: String,
    pub host: String,
    pub port: u16,
    pub handoff_path: Option<String>,
    pub mode: String,
    pub backend_driver: String,
    pub frontend_pid: Option<u32>,
    pub backend_pid: Option<u32>,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct FailureRecord {
    pub ok: bool,
    pub code: Option<String>,
    pub error: String,
    #[serde(default)]
    pub data: Option<Value>,
}

#[derive(Clone, Debug, PartialEq)]
pub enum LaunchLine {
    Startup(StartupRecord),
    Failure(FailureRecord),
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RecordError {
    Empty,
    TooLong,
    InvalidJson,
    InvalidShape,
    UnsupportedVersion,
    InvalidUrl,
    InvalidHost,
    InvalidPort,
    InvalidHandoff,
}

impl Display for RecordError {
    fn fmt(&self, f: &mut Formatter<'_>) -> std::fmt::Result {
        let value = match self {
            Self::Empty => "startup record is empty",
            Self::TooLong => "startup record is too long",
            Self::InvalidJson => "startup record is not valid JSON",
            Self::InvalidShape => "startup record has an invalid shape",
            Self::UnsupportedVersion => "startup record version is unsupported",
            Self::InvalidUrl => "startup record URL is invalid",
            Self::InvalidHost => "startup record host is invalid",
            Self::InvalidPort => "startup record port is invalid",
            Self::InvalidHandoff => "startup handoff path is invalid",
        };
        f.write_str(value)
    }
}

impl std::error::Error for RecordError {}

pub fn parse_line(line: &str) -> Result<LaunchLine, RecordError> {
    let trimmed = line.strip_suffix('\n').unwrap_or(line);
    if trimmed.ends_with('\r') {
        return Err(RecordError::InvalidShape);
    }
    if trimmed.is_empty() {
        return Err(RecordError::Empty);
    }
    if trimmed.len() > MAX_LINE_BYTES {
        return Err(RecordError::TooLong);
    }
    let value: Value = serde_json::from_str(trimmed).map_err(|_| RecordError::InvalidJson)?;
    let ok = value
        .get("ok")
        .and_then(Value::as_bool)
        .ok_or(RecordError::InvalidShape)?;
    if ok {
        let record: StartupRecord =
            serde_json::from_value(value).map_err(|_| RecordError::InvalidShape)?;
        validate_startup(&record)?;
        Ok(LaunchLine::Startup(record))
    } else {
        let failure: FailureRecord =
            serde_json::from_value(value).map_err(|_| RecordError::InvalidShape)?;
        if failure.data.as_ref().is_some_and(|data| !data.is_object()) {
            return Err(RecordError::InvalidShape);
        }
        Ok(LaunchLine::Failure(failure))
    }
}

fn validate_startup(record: &StartupRecord) -> Result<(), RecordError> {
    if !record.ok {
        return Err(RecordError::InvalidShape);
    }
    if record.record_version != 1 {
        return Err(RecordError::UnsupportedVersion);
    }
    if record.frontend_pid.is_none()
        || !matches!(record.mode.as_str(), "normal" | "dev")
        || !matches!(record.backend_driver.as_str(), "daemon" | "contract-mock")
    {
        return Err(RecordError::InvalidShape);
    }
    if record.port == 0 {
        return Err(RecordError::InvalidPort);
    }
    let parsed = Url::parse(&record.url).map_err(|_| RecordError::InvalidUrl)?;
    if parsed.scheme() != "http"
        || parsed.query().is_some()
        || parsed.fragment().is_some()
        || !parsed.username().is_empty()
        || parsed.password().is_some()
        || !matches!(parsed.path(), "" | "/")
    {
        return Err(RecordError::InvalidUrl);
    }
    let host = parsed.host_str().ok_or(RecordError::InvalidHost)?;
    let normalized_host = host.trim_matches(['[', ']']);
    if !matches!(normalized_host, "127.0.0.1" | "::1") || record.host != normalized_host {
        return Err(RecordError::InvalidHost);
    }
    if parsed.port().is_none() || parsed.port_or_known_default() != Some(record.port) {
        return Err(RecordError::InvalidPort);
    }
    if let Some(handoff_path) = &record.handoff_path {
        let path = Path::new(handoff_path);
        if !path.is_absolute() {
            return Err(RecordError::InvalidHandoff);
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(unix)]
    const HANDOFF: &str = "/tmp/launch.url";
    #[cfg(windows)]
    const HANDOFF: &str = r"C:\\Users\\someone\\launch.url";
    #[cfg(unix)]
    const FOREIGN_HANDOFF: &str = r"C:\\Users\\someone\\launch.url";
    #[cfg(windows)]
    const FOREIGN_HANDOFF: &str = "/tmp/launch.url";

    fn record(url: &str, host: &str, port: u16) -> String {
        format!(
            r#"{{"ok":true,"record_version":1,"url":"{url}","host":"{host}","port":{port},"handoff_path":"{HANDOFF}","mode":"normal","backend_driver":"contract-mock","frontend_pid":4242,"backend_pid":null}}"#
        )
    }

    #[test]
    fn handoff_path_must_be_absolute_on_the_running_platform() {
        let line = record("http://127.0.0.1:41234", "127.0.0.1", 41234);
        let Ok(LaunchLine::Startup(parsed)) = parse_line(&line) else {
            panic!("a platform-absolute handoff path parses");
        };
        assert!(Path::new(parsed.handoff_path.as_deref().unwrap()).is_absolute());
        let foreign = line.replace(HANDOFF, FOREIGN_HANDOFF);
        assert_eq!(parse_line(&foreign), Err(RecordError::InvalidHandoff));
    }

    #[test]
    fn parses_valid_startup_record() {
        let line = record("http://127.0.0.1:41234", "127.0.0.1", 41234);
        assert!(matches!(parse_line(&line), Ok(LaunchLine::Startup(_))));
    }

    #[test]
    fn rejects_unsupported_versions() {
        let line = record("http://127.0.0.1:41234", "127.0.0.1", 41234)
            .replace("\"record_version\":1", "\"record_version\":2");
        assert_eq!(parse_line(&line), Err(RecordError::UnsupportedVersion));
    }

    #[test]
    fn rejects_non_loopback_and_extra_url_parts() {
        for url in [
            "http://example.com:41234",
            "http://127.0.0.1:41234/path",
            "http://127.0.0.1:41234/?x=1",
            "http://127.0.0.1:41234/#grant=x",
            "http://user@127.0.0.1:41234",
        ] {
            assert!(matches!(
                parse_line(&record(url, "127.0.0.1", 41234)),
                Err(RecordError::InvalidUrl | RecordError::InvalidHost)
            ));
        }
    }

    #[test]
    fn parses_failure_line_without_echoing_payload() {
        let line = r#"{"ok":false,"code":"GUI_RUNTIME_BUSY","error":"busy"}"#;
        let parsed = parse_line(line).unwrap();
        assert!(matches!(parsed, LaunchLine::Failure(_)));
    }

    fn native_to_this_platform(handoff_path: &str) -> bool {
        handoff_path.starts_with('/') == cfg!(unix)
    }

    #[test]
    fn parses_every_contract_example() {
        let document: Value = serde_json::from_str(include_str!(
            "../../../../contract/cli/gui-startup-record.v1.json"
        ))
        .unwrap();
        let startup = document["examples"]["startup"].as_array().unwrap();
        let failure = document["examples"]["failure"].as_array().unwrap();
        assert!(!startup.is_empty() && !failure.is_empty());
        let (mut native, mut foreign) = (0, 0);
        for example in startup {
            let line = serde_json::to_string(example).unwrap();
            match example["handoff_path"].as_str() {
                Some(path) if !native_to_this_platform(path) => {
                    foreign += 1;
                    assert_eq!(
                        parse_line(&line),
                        Err(RecordError::InvalidHandoff),
                        "foreign-platform handoff example parsed: {line}"
                    );
                }
                handoff => {
                    native += usize::from(handoff.is_some());
                    assert!(
                        matches!(parse_line(&line), Ok(LaunchLine::Startup(_))),
                        "contract startup example did not parse: {line}"
                    );
                }
            }
        }
        assert!(native > 0 && foreign > 0);
        for example in failure {
            let line = serde_json::to_string(example).unwrap();
            assert!(
                matches!(parse_line(&line), Ok(LaunchLine::Failure(_))),
                "contract failure example did not parse: {line}"
            );
        }
    }

    #[test]
    fn rejects_bad_ports_hosts_and_line_shapes() {
        assert_eq!(
            parse_line(&record("http://127.0.0.1:41234", "127.0.0.1", 0)),
            Err(RecordError::InvalidPort)
        );
        assert_eq!(
            parse_line(&record("http://127.0.0.1:41235", "127.0.0.1", 41234)),
            Err(RecordError::InvalidPort)
        );
        assert_eq!(
            parse_line(&record("http://127.0.0.1", "127.0.0.1", 80)),
            Err(RecordError::InvalidPort)
        );
        assert_eq!(
            parse_line(&record("http://127.0.0.1:41234", "::1", 41234)),
            Err(RecordError::InvalidHost)
        );
        assert_eq!(
            parse_line(&record("http://localhost:41234", "localhost", 41234)),
            Err(RecordError::InvalidHost)
        );
        assert!(matches!(
            parse_line(&record("http://[::1]:41234", "::1", 41234)),
            Ok(LaunchLine::Startup(_))
        ));
        assert_eq!(parse_line("not json"), Err(RecordError::InvalidJson));
        assert_eq!(parse_line("\n"), Err(RecordError::Empty));
        assert_eq!(
            parse_line(&format!(
                "{}\r\n",
                record("http://127.0.0.1:41234", "127.0.0.1", 41234)
            )),
            Err(RecordError::InvalidShape)
        );
        let long = format!("{{\"ok\":false,\"error\":\"{}\"}}", "x".repeat(64 * 1024));
        assert_eq!(parse_line(&long), Err(RecordError::TooLong));
        assert_eq!(
            parse_line(r#"{"ok":false,"error":"x","data":[1]}"#),
            Err(RecordError::InvalidShape)
        );

        let relative =
            record("http://127.0.0.1:41234", "127.0.0.1", 41234).replace(HANDOFF, "launch.url");
        assert_eq!(parse_line(&relative), Err(RecordError::InvalidHandoff));
    }

    #[test]
    fn failure_line_with_an_empty_error_is_accepted_as_the_schema_allows() {
        let parsed = parse_line(r#"{"ok":false,"error":""}"#).unwrap();
        assert!(matches!(parsed, LaunchLine::Failure(failure) if failure.error.is_empty()));
    }

    #[test]
    fn failure_line_without_an_error_field_is_refused() {
        assert_eq!(
            parse_line(r#"{"ok":false}"#),
            Err(RecordError::InvalidShape)
        );
    }
}
