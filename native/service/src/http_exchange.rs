//! Bounded HTTP metadata mechanism for an explicit development host profile.
//! No routes, credential validity, correlation policy or readiness verdicts.
use std::collections::BTreeSet;

type Result<T> = std::result::Result<T, &'static str>;
pub(crate) const FRAME_BYTES: usize = 2 * 1024 * 1024;
pub(crate) const HEADER_BYTES: usize = 8192;
pub(crate) const HEADER_COUNT: usize = 16;
pub(crate) const HEADER_VALUE_BYTES: usize = 1024;
pub(crate) const REQUEST_HEADER_BYTES: usize = 65536;
pub(crate) const REQUEST_HEADER_COUNT: usize = 64;
pub(crate) const FRESH_ID_BYTES: usize = 16;
pub(crate) const MEDIA_TYPES: &[&str] = &[
    "application/json",
    "text/plain; charset=utf-8",
    "text/plain; version=0.0.4; charset=utf-8",
];

fn encode(marker: &str, fields: &[&str]) -> String {
    let mut out = marker.to_owned();
    for field in fields {
        out.push_str(&format!("{:08}{}", field.len(), field));
    }
    out
}

struct Cursor<'a> {
    raw: &'a str,
    at: usize,
}

impl<'a> Cursor<'a> {
    fn new(raw: &'a str, marker: &str) -> Result<Self> {
        if raw.len() > FRAME_BYTES || !raw.starts_with(marker) {
            return Err("protocol");
        }
        Ok(Self { raw, at: 4 })
    }

    fn field(&mut self) -> Result<&'a str> {
        let length = self.raw.get(self.at..self.at + 8).ok_or("protocol")?;
        if !length.bytes().all(|b| b.is_ascii_digit()) {
            return Err("protocol");
        }
        let length: usize = length.parse().map_err(|_| "protocol")?;
        self.at += 8;
        let end = self.at.checked_add(length).ok_or("protocol")?;
        let value = self.raw.get(self.at..end).ok_or("protocol")?;
        self.at = end;
        Ok(value)
    }

    fn finish(self) -> Result<()> {
        if self.at == self.raw.len() {
            Ok(())
        } else {
            Err("protocol")
        }
    }
}

/// Capture ONLY the caller-supplied correlation header's raw bytes, not the
/// bearer header or an arbitrary HTTP-header map. The HTTP adapter must make
/// that fixed selection. The independently generated ID is not an operation ID.
pub fn request_facts(fresh: &str, hints: &[&[u8]]) -> Result<String> {
    if fresh.len() != FRESH_ID_BYTES * 2
        || !fresh
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        || hints.len() > REQUEST_HEADER_COUNT
        || hints
            .iter()
            .try_fold(0usize, |n, v| n.checked_add(v.len()))
            .ok_or("limit")?
            > REQUEST_HEADER_BYTES
    {
        return Err("protocol");
    }
    let first = hints.first().copied().unwrap_or_default();
    let hex: String = first.iter().map(|b| format!("{b:02x}")).collect();
    Ok(encode("RF1\n", &[fresh, &hints.len().to_string(), &hex]))
}

fn header_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 64
        && name
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
        && name.as_bytes()[0].is_ascii_lowercase()
}

fn configurable_header(name: &str) -> bool {
    // The owner must declare each exact name. HTTP framing, active content,
    // cookies, redirects, CORS and browser security headers are not delegated.
    // Also forbid common proxy effect/control headers even on the local path.
    header_name(name)
        && (matches!(name, "retry-after" | "www-authenticate" | "server-timing")
            || (name.starts_with("x-")
                && !matches!(
                    name,
                    "x-frame-options"
                        | "x-content-type-options"
                        | "x-xss-protection"
                        | "x-dns-prefetch-control"
                        | "x-download-options"
                        | "x-permitted-cross-domain-policies"
                        | "x-sendfile"
                        | "x-lighttpd-send-file"
                )
                && !name.starts_with("x-accel-")))
}

/// Exact immutable operator-selected response-header inventory. The new service
/// profile binds its sorted names and limits; SIGIL bootstrap checks the names.
#[derive(Debug)]
pub struct HeaderPolicy {
    names: BTreeSet<String>,
}

impl HeaderPolicy {
    pub fn names(&self) -> impl Iterator<Item = &str> {
        self.names.iter().map(String::as_str)
    }

    pub fn new(names: &[&str]) -> Result<Self> {
        if names.len() > HEADER_COUNT || names.iter().any(|n| !configurable_header(n)) {
            return Err("config");
        }
        let unique: BTreeSet<_> = names.iter().map(|n| (*n).to_owned()).collect();
        if unique.len() != names.len() {
            return Err("config");
        }
        Ok(Self { names: unique })
    }

    /// HR1: inert content type, HH1 header record, UTF-8 body. HH1 contains a
    /// canonical count followed by name/value fields. No status or time policy.
    pub fn parse<'a>(&self, raw: &'a str) -> Result<ResponseMetadata<'a>> {
        let mut frame = Cursor::new(raw, "HR1\n")?;
        let content_type = frame.field()?;
        if !MEDIA_TYPES.contains(&content_type) {
            return Err("protocol");
        }
        let encoded = frame.field()?;
        if encoded.len() > HEADER_BYTES {
            return Err("limit");
        }
        let body = frame.field()?;
        frame.finish()?;
        let mut fields = Cursor::new(encoded, "HH1\n")?;
        let count = fields.field()?;
        if count.is_empty()
            || count.len() > 2
            || !count.bytes().all(|b| b.is_ascii_digit())
            || (count.len() > 1 && count.starts_with('0'))
        {
            return Err("protocol");
        }
        let count: usize = count.parse().map_err(|_| "protocol")?;
        if count > HEADER_COUNT {
            return Err("limit");
        }
        let mut headers = Vec::new();
        let mut seen = BTreeSet::new();
        for _ in 0..count {
            let name = fields.field()?;
            let value = fields.field()?;
            if !self.names.contains(name) || !seen.insert(name) {
                return Err("capability");
            }
            if value.len() > HEADER_VALUE_BYTES
                || !value.bytes().all(|b| (32..=126).contains(&b))
                || value.starts_with(' ')
                || value.ends_with(' ')
            {
                return Err("protocol");
            }
            headers.push((name, value));
        }
        fields.finish()?;
        Ok(ResponseMetadata {
            content_type,
            headers,
            body,
        })
    }
}

#[derive(Debug, PartialEq)]
pub struct ResponseMetadata<'a> {
    pub content_type: &'a str,
    pub headers: Vec<(&'a str, &'a str)>,
    pub body: &'a str,
}

#[cfg(test)]
mod tests {
    use super::*;
    const ID: &str = "0123456789abcdef0123456789abcdef";

    fn wire(mime: &str, pairs: &[(&str, &str)], body: &str) -> String {
        let count = pairs.len().to_string();
        let mut fields = vec![count.as_str()];
        for (name, value) in pairs {
            fields.extend([*name, *value]);
        }
        encode("HR1\n", &[mime, &encode("HH1\n", &fields), body])
    }

    #[test]
    fn raw_hint_bytes_and_first_value_are_facts_not_native_policy() {
        assert_eq!(
            request_facts(ID, &[]).unwrap(),
            encode("RF1\n", &[ID, "0", ""])
        );
        assert_eq!(
            request_facts(ID, &[b"a\xff", b"second-value"]).unwrap(),
            encode("RF1\n", &[ID, "2", "61ff"])
        );
        assert_eq!(
            request_facts(ID, &[b""]).unwrap(),
            encode("RF1\n", &[ID, "1", ""])
        );
    }

    #[test]
    fn request_metadata_bounds_hints_separately_from_whole_http_header_block() {
        let at_limit = vec![b'a'; 65536];
        let over_limit = vec![b'a'; 65537];
        let first = vec![b'a'; 40000];
        let second = vec![b'b'; 30000];
        assert!(request_facts(ID, &[&at_limit]).is_ok());
        assert!(request_facts(ID, &[&over_limit]).is_err());
        assert!(request_facts(ID, &[b"".as_slice(); 64]).is_ok());
        assert!(request_facts(ID, &[b"".as_slice(); 65]).is_err());
        assert!(request_facts(ID, &[&first, &second]).is_err());
        for bad in [
            "",
            "short",
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
            "g123456789abcdef0123456789abcdef",
        ] {
            assert!(request_facts(bad, &[]).is_err());
        }
    }

    #[test]
    fn all_legacy_response_metadata_is_representable_without_route_policy() {
        let pairs = [
            ("x-request-id", ID),
            ("retry-after", "3"),
            ("www-authenticate", "Bearer realm=\"sigil-pi\""),
            ("server-timing", "queue;dur=1.234"),
            ("x-sigil-tool-calls", "2"),
            ("x-sigil-retries", "0"),
        ];
        let policy = HeaderPolicy::new(&pairs.iter().map(|p| p.0).collect::<Vec<_>>()).unwrap();
        for mime in [
            "application/json",
            "text/plain; charset=utf-8",
            "text/plain; version=0.0.4; charset=utf-8",
        ] {
            let raw = wire(mime, &pairs, "fixture body é😀\n");
            assert_eq!(
                policy.parse(&raw).unwrap(),
                ResponseMetadata {
                    content_type: mime,
                    headers: pairs.to_vec(),
                    body: "fixture body é😀\n"
                }
            );
        }
    }

    #[test]
    fn header_inventory_cannot_delegate_framing_active_content_or_browser_authority() {
        for name in [
            "authorization",
            "proxy-authenticate",
            "host",
            "connection",
            "content-length",
            "transfer-encoding",
            "trailer",
            "content-type",
            "content-encoding",
            "cache-control",
            "set-cookie",
            "location",
            "refresh",
            "link",
            "access-control-allow-origin",
            "content-security-policy",
            "strict-transport-security",
            "x-frame-options",
            "x-content-type-options",
            "x-sendfile",
            "x-lighttpd-send-file",
            "x-accel-redirect",
            "x-accel-buffering",
            "X-Request-ID",
            "x-é",
            "x:\n",
        ] {
            assert!(HeaderPolicy::new(&[name]).is_err(), "{name}");
        }
        assert!(HeaderPolicy::new(&["x-a", "x-a"]).is_err());
        assert!(HeaderPolicy::new(&[&"x".repeat(65)]).is_err());
        let names: Vec<_> = (0..17).map(|i| format!("x-{i}")).collect();
        assert!(HeaderPolicy::new(&names.iter().map(String::as_str).collect::<Vec<_>>()).is_err());
    }

    #[test]
    fn responses_require_exact_inventory_and_unique_headers() {
        let policy = HeaderPolicy::new(&["x-request-id"]).unwrap();
        for pairs in [
            vec![("x-foreign", "a")],
            vec![("X-Request-ID", ID)],
            vec![("x-request-id", ID), ("x-request-id", ID)],
        ] {
            assert!(
                policy
                    .parse(&wire("application/json", &pairs, "{}"))
                    .is_err()
            );
        }
    }

    #[test]
    fn response_header_values_cannot_inject_wire_bytes() {
        let policy = HeaderPolicy::new(&["x-note"]).unwrap();
        for byte in (0..32u8).chain(127..=255) {
            let value = format!("before{}after", char::from(byte));
            assert!(
                policy
                    .parse(&wire("application/json", &[("x-note", &value)], "{}"))
                    .is_err()
            );
        }
        for value in [" leading", "trailing ", " "] {
            assert!(
                policy
                    .parse(&wire("application/json", &[("x-note", value)], "{}"))
                    .is_err()
            );
        }
        assert!(
            policy
                .parse(&wire("application/json", &[("x-note", "")], "{}"))
                .is_ok()
        );
        assert!(
            policy
                .parse(&wire(
                    "application/json",
                    &[("x-note", &"a".repeat(1024))],
                    "{}"
                ))
                .is_ok()
        );
        assert!(
            policy
                .parse(&wire(
                    "application/json",
                    &[("x-note", &"a".repeat(1025))],
                    "{}"
                ))
                .is_err()
        );
    }

    #[test]
    fn only_inert_utf8_content_types_are_admitted() {
        let policy = HeaderPolicy::new(&[]).unwrap();
        for mime in [
            "",
            "text/html",
            "image/svg+xml",
            "application/javascript",
            "application/json\r\nX: y",
            "application/json; charset=utf-8",
            "APPLICATION/JSON",
        ] {
            assert!(policy.parse(&wire(mime, &[], "payload")).is_err());
        }
    }

    #[test]
    fn framing_is_strict_and_byte_counted() {
        let policy = HeaderPolicy::new(&[]).unwrap();
        let valid = wire("application/json", &[], "é");
        for raw in [
            format!("{valid}extra"),
            valid[..valid.len() - 2].to_owned(),
            valid.replace("HR1\n", "HR2\n"),
            valid.replacen("00000016", "0000000x", 1),
            valid.replace("00000002é", "00000001é"),
        ] {
            assert!(policy.parse(&raw).is_err(), "{raw}");
        }
        for count in ["", "00", "01", "-1", "17", "64", "1.0", " 0"] {
            let headers = encode("HH1\n", &[count]);
            assert!(
                policy
                    .parse(&encode("HR1\n", &["application/json", &headers, "{}"]))
                    .is_err()
            );
        }
        let extra = encode("HH1\n", &["0", "unexpected"]);
        assert!(
            policy
                .parse(&encode("HR1\n", &["application/json", &extra, "{}"]))
                .is_err()
        );
    }

    #[test]
    fn overall_envelope_ceiling_is_not_widened_by_metadata() {
        let policy = HeaderPolicy::new(&[]).unwrap();
        let overhead = wire("application/json", &[], "").len();
        let body = "a".repeat(FRAME_BYTES - overhead);
        assert!(policy.parse(&wire("application/json", &[], &body)).is_ok());
        assert!(
            policy
                .parse(&wire("application/json", &[], &(body + "a")))
                .is_err()
        );
    }

    #[test]
    fn aggregate_metadata_bound_is_independent_of_individual_value_bounds() {
        let names: Vec<_> = (0..16).map(|i| format!("x-{i}")).collect();
        let policy =
            HeaderPolicy::new(&names.iter().map(String::as_str).collect::<Vec<_>>()).unwrap();
        let value = "a".repeat(1000);
        let pairs: Vec<_> = names.iter().map(|n| (n.as_str(), value.as_str())).collect();
        assert!(
            policy
                .parse(&wire("application/json", &pairs[..7], "{}"))
                .is_ok()
        );
        assert!(
            policy
                .parse(&wire("application/json", &pairs, "{}"))
                .is_err()
        );
        let empty: Vec<_> = names.iter().map(|n| (n.as_str(), "")).collect();
        assert!(
            policy
                .parse(&wire("application/json", &empty, "{}"))
                .is_ok()
        );
    }
}
