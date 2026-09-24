use super::*;
use hyper::{HeaderMap, header::HeaderValue};

#[test]
fn http_adapter_captures_only_correlation_hints_and_preserves_raw_first_value() {
    let mut headers = HeaderMap::new();
    headers.insert(
        "authorization",
        HeaderValue::from_static("Bearer bearer-secret-canary"),
    );
    headers.insert("cookie", HeaderValue::from_static("secret-cookie-canary"));
    headers.insert("x-unrelated", HeaderValue::from_static("unrelated-canary"));
    headers.append(
        "x-request-id",
        HeaderValue::from_bytes(b"first\xff").unwrap(),
    );
    headers.append("x-request-id", HeaderValue::from_static("second"));
    assert!(request_id_hints(&headers, false).is_empty());
    assert_eq!(
        request_id_hints(&headers, true),
        [b"first\xff".to_vec(), b"second".to_vec()]
    );
}

#[test]
fn response_metadata_preserves_host_framing_and_browser_hardening() {
    let mut reply = application_response(Reply {
        status: 200,
        body: "fixture counter 1\n".to_owned(),
        content_type: "text/plain; version=0.0.4; charset=utf-8".to_owned(),
        headers: vec![
            ("x-request-id".to_owned(), "trace-id-123".to_owned()),
            ("retry-after".to_owned(), "3".to_owned()),
        ],
    });
    assert_eq!(reply.status(), 200);
    assert_eq!(
        reply.headers()["content-type"],
        "text/plain; version=0.0.4; charset=utf-8"
    );
    assert_eq!(reply.headers()["x-request-id"], "trace-id-123");
    assert_eq!(reply.headers()["cache-control"], "no-store");
    assert_eq!(reply.headers()["connection"], "close");
    assert_eq!(reply.headers()["x-content-type-options"], "nosniff");
    public_assets::harden(&mut reply);
    assert_eq!(reply.headers()["x-frame-options"], "DENY");
    assert_eq!(
        reply.headers()["cross-origin-resource-policy"],
        "same-origin"
    );
    assert_eq!(reply.headers()["x-request-id"], "trace-id-123");
    assert!(reply.headers().contains_key("content-security-policy"));
    assert!(!reply.headers().contains_key("access-control-allow-origin"));
    assert!(!reply.headers().contains_key("set-cookie"));
}

#[test]
fn response_serialization_failure_is_refused_without_panicking() {
    let reply = application_response(Reply {
        status: 200,
        body: "must-not-escape".to_owned(),
        content_type: "application/json".to_owned(),
        headers: vec![("x-request-id".to_owned(), "invalid\r\nheader".to_owned())],
    });
    assert_eq!(reply.status(), 503);
    assert!(!reply.headers().contains_key("x-request-id"));
    assert_eq!(reply.headers()["content-type"], "application/json");
}
