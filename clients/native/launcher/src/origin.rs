//! Main-frame and new-window decisions for the shell's single webview.
//!
//! Decisions (shell-6.md §B.3 step 6, §D.3 6b-3):
//! - The main frame may show the record's origin (scheme `http`, the same host
//!   literal and port, no userinfo) and the bundled status page. The status page
//!   is matched by scheme, host, port and path; its fragment carries the state
//!   (`#starting`, `#error=…`) and is ignored, a query is refused.
//! - Before the record is known only the status page is allowed.
//! - A new-window request for an `http(s)` URL of another origin goes to the
//!   system browser; everything else is denied.
//! - The status page's error text is form-encoded into the fragment, capped,
//!   and any `grant=` value in it is replaced, so a grant can never reach it.

use url::form_urlencoded;
use url::Url;

const MAX_STATUS_ERROR_CHARS: usize = 1000;

pub fn same_origin(left: &Url, right: &Url) -> bool {
    left.scheme() == right.scheme()
        && left.host_str() == right.host_str()
        && left.port_or_known_default() == right.port_or_known_default()
}

pub fn is_status_page(candidate: &Url, status_page: &Url) -> bool {
    same_origin(candidate, status_page)
        && candidate.path() == status_page.path()
        && candidate.query().is_none()
        && candidate.username().is_empty()
        && candidate.password().is_none()
}

pub fn allowed_main_frame(candidate: &Url, origin: Option<&Url>, status_page: &Url) -> bool {
    if is_status_page(candidate, status_page) {
        return true;
    }
    origin.is_some_and(|origin| {
        candidate.scheme() == "http"
            && same_origin(candidate, origin)
            && candidate.username().is_empty()
            && candidate.password().is_none()
    })
}

pub fn external_target(candidate: &Url, origin: Option<&Url>) -> Option<Url> {
    if !matches!(candidate.scheme(), "http" | "https") {
        return None;
    }
    if origin.is_some_and(|origin| same_origin(candidate, origin)) {
        return None;
    }
    Some(candidate.clone())
}

pub fn status_page_url(status_page: &Url, error: Option<&str>) -> Url {
    let mut url = status_page.clone();
    url.set_query(None);
    match error {
        None => url.set_fragment(Some("starting")),
        Some(message) => {
            let fragment = form_urlencoded::Serializer::new(String::new())
                .append_pair("error", &redact(message))
                .finish();
            url.set_fragment(Some(&fragment));
        }
    }
    url
}

fn redact(message: &str) -> String {
    let mut output = String::with_capacity(message.len().min(MAX_STATUS_ERROR_CHARS));
    let mut rest = message;
    while let Some(index) = rest.find("grant=") {
        output.push_str(&rest[..index]);
        output.push_str("grant=<redacted>");
        let tail = &rest[index + "grant=".len()..];
        let end = tail
            .find(|value: char| value.is_whitespace() || value == '"' || value == '\'')
            .unwrap_or(tail.len());
        rest = &tail[end..];
    }
    output.push_str(rest);
    output.chars().take(MAX_STATUS_ERROR_CHARS).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn url(value: &str) -> Url {
        Url::parse(value).unwrap()
    }

    fn status() -> Url {
        url("tauri://localhost/index.html")
    }

    #[test]
    fn status_page_is_allowed_with_any_state_fragment() {
        let origin = url("http://127.0.0.1:41234");
        for value in [
            "tauri://localhost/index.html",
            "tauri://localhost/index.html#starting",
            "tauri://localhost/index.html#error=launcher%20failed",
            "tauri://localhost/index.html#error=x&code=GUI_RUNTIME_BUSY",
        ] {
            assert!(allowed_main_frame(&url(value), None, &status()), "{value}");
            assert!(
                allowed_main_frame(&url(value), Some(&origin), &status()),
                "{value}"
            );
        }
    }

    #[test]
    fn other_bundled_pages_and_status_queries_are_refused() {
        for value in [
            "tauri://localhost/other.html",
            "tauri://localhost/index.html?x=1#starting",
            "tauri://evil/index.html",
            "http://tauri.localhost/index.html",
        ] {
            assert!(!allowed_main_frame(&url(value), None, &status()), "{value}");
        }
    }

    #[test]
    fn record_origin_is_exact() {
        let origin = url("http://127.0.0.1:41234");
        assert!(allowed_main_frame(
            &url("http://127.0.0.1:41234/a?b#c"),
            Some(&origin),
            &status()
        ));
        assert!(allowed_main_frame(
            &url("http://127.0.0.1:41234"),
            Some(&origin),
            &status()
        ));
        for value in [
            "http://127.0.0.1:41235/a",
            "https://127.0.0.1:41234/a",
            "http://localhost:41234/a",
            "http://127.0.0.1@evil.example:41234/",
            "http://127.0.0.1:41234@evil.example/",
            "http://user@127.0.0.1:41234/",
            "http://user:pw@127.0.0.1:41234/",
            "http://[::1]:41234/",
            "http://127.0.0.2:41234/",
            "http://example.com/",
            "file:///etc/passwd",
            "about:blank",
            "data:text/html,x",
            "javascript:alert(1)",
        ] {
            assert!(
                !allowed_main_frame(&url(value), Some(&origin), &status()),
                "{value}"
            );
        }
    }

    #[test]
    fn origin_is_not_allowed_before_the_record() {
        assert!(!allowed_main_frame(
            &url("http://127.0.0.1:41234/"),
            None,
            &status()
        ));
    }

    #[test]
    fn ipv6_record_origin_is_exact() {
        let origin = url("http://[::1]:41234");
        assert!(allowed_main_frame(
            &url("http://[::1]:41234/x"),
            Some(&origin),
            &status()
        ));
        assert!(allowed_main_frame(
            &url("http://[0:0:0:0:0:0:0:1]:41234/x"),
            Some(&origin),
            &status()
        ));
        for value in [
            "http://[::1]:41235/x",
            "http://127.0.0.1:41234/x",
            "http://localhost:41234/x",
            "http://[::2]:41234/x",
        ] {
            assert!(
                !allowed_main_frame(&url(value), Some(&origin), &status()),
                "{value}"
            );
        }
    }

    #[test]
    fn redirect_targets_are_judged_like_any_main_frame_url() {
        let origin = url("http://127.0.0.1:41234");
        assert!(!allowed_main_frame(
            &url("https://login.example/authorize?redirect_uri=http://127.0.0.1:41234/"),
            Some(&origin),
            &status()
        ));
        assert!(allowed_main_frame(
            &url("http://127.0.0.1:41234/after-login"),
            Some(&origin),
            &status()
        ));
    }

    #[test]
    fn external_http_targets_are_returned() {
        let origin = url("http://127.0.0.1:41234");
        assert_eq!(
            external_target(&url("https://example.com/a"), Some(&origin))
                .unwrap()
                .as_str(),
            "https://example.com/a"
        );
        assert!(external_target(&url("http://localhost:41234/a"), Some(&origin)).is_some());
        for value in [
            "about:blank",
            "file:///tmp/a",
            "javascript:alert(1)",
            "data:text/plain,x",
            "tauri://localhost/a",
            "http://127.0.0.1:41234/a",
        ] {
            assert!(
                external_target(&url(value), Some(&origin)).is_none(),
                "{value}"
            );
        }
        assert!(external_target(&url("https://example.com/a"), None).is_some());
    }

    #[test]
    fn status_page_url_encodes_state_and_never_carries_a_grant() {
        let starting = status_page_url(&status(), None);
        assert_eq!(starting.as_str(), "tauri://localhost/index.html#starting");
        let error = status_page_url(
            &status(),
            Some("launcher failed (X): see http://127.0.0.1:1/#grant=s3cr3t&x and #grant=other"),
        );
        assert!(!error.as_str().contains("s3cr3t"));
        assert!(!error.as_str().contains("other"));
        assert!(allowed_main_frame(&error, None, &status()));
        let fragment = error.fragment().unwrap();
        let decoded: Vec<(String, String)> = form_urlencoded::parse(fragment.as_bytes())
            .into_owned()
            .collect();
        assert_eq!(decoded.len(), 1);
        assert_eq!(decoded[0].0, "error");
        assert!(decoded[0].1.starts_with("launcher failed (X): see"));
        assert!(decoded[0].1.contains("grant=<redacted>"));
        let long = status_page_url(&status(), Some(&"x".repeat(5000)));
        let decoded: Vec<(String, String)> =
            form_urlencoded::parse(long.fragment().unwrap().as_bytes())
                .into_owned()
                .collect();
        assert_eq!(decoded[0].1.chars().count(), 1000);
    }
}
