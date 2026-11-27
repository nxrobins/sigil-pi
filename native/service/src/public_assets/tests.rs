use super::*;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use std::fs;
use tempfile::TempDir;

struct Fixture {
    root: TempDir,
    manifest: Value,
}
impl Fixture {
    fn new() -> Self {
        let root = TempDir::new().unwrap();
        let file = root.path().join("index.html");
        fs::write(&file, "<!doctype html><title>Public</title>").unwrap();
        let manifest = json!({"version":1,"assets":[{"route":"/","path":file,
            "sha256":digest(&fs::read(&file).unwrap()),"content_type":"text/html"}]});
        Self { root, manifest }
    }
    fn path(&self) -> PathBuf {
        self.root.path().join("assets.json")
    }
    fn write(&self) {
        fs::write(self.path(), serde_json::to_vec(&self.manifest).unwrap()).unwrap();
    }
    fn load(&self) -> Result<PublicAssets> {
        self.write();
        PublicAssets::load(&self.path())
    }
    fn asset_path(&self) -> PathBuf {
        PathBuf::from(self.manifest["assets"][0]["path"].as_str().unwrap())
    }
}

#[tokio::test]
async fn retained_verified_bytes_do_not_reopen_changed_or_deleted_paths() {
    let f = Fixture::new();
    let assets = f.load().unwrap();
    assert_eq!(assets.digest(), digest(&fs::read(f.path()).unwrap()));
    fs::write(f.asset_path(), "PRIVATE-CHANGED-CANARY").unwrap();
    fs::remove_file(f.asset_path()).unwrap();
    let reply = assets.response(&Method::GET, "/", true).unwrap();
    assert_eq!(reply.status(), 200);
    assert_eq!(reply.headers()["content-type"], "text/html; charset=utf-8");
    let bytes = reply.into_body().collect().await.unwrap().to_bytes();
    assert_eq!(bytes, "<!doctype html><title>Public</title>");
}

#[test]
fn only_declared_exact_routes_are_mounts_and_methods_bodies_are_bounded() {
    let assets = Fixture::new().load().unwrap();
    for name in [
        "/../index.html",
        "/index.html",
        "/?credential=private",
        "//",
        "/%2f",
        "/app/unknown.js",
    ] {
        assert!(
            assets.response(&Method::GET, name, true).is_none(),
            "{name}"
        );
    }
    assert_eq!(
        assets.response(&Method::GET, "/", false).unwrap().status(),
        400
    );
    for method in [
        Method::HEAD,
        Method::POST,
        Method::PUT,
        Method::DELETE,
        Method::OPTIONS,
    ] {
        let reply = assets.response(&method, "/", true).unwrap();
        assert_eq!(reply.status(), 405);
        assert_eq!(reply.headers()["allow"], "GET");
    }
}

#[test]
fn route_grammar_rejects_normalization_and_aliases() {
    for value in [
        "",
        "index.html",
        "//",
        "/_assets/",
        "/_assets/a//b",
        "/_assets/a/./b",
        "/_assets/a/../b",
        "/_assets/..",
        "/_assets/a?b",
        "/_assets/a#b",
        "/_assets/%61",
        "/_assets/a\\b",
        "/_assets/a:b",
        "/_assets/a b",
        "/_assets/a\nb",
        "/_assets/😀",
        "/v1/operations",
        "/v1/sessions",
        "/app/app.mjs",
    ] {
        assert!(!route(value), "{value}");
    }
    for value in ["/", "/_assets/app.mjs", "/_assets/one_two-3.css"] {
        assert!(route(value), "{value}");
    }
    assert!(route(&format!("/_assets/{}", "a".repeat(119))));
    assert!(!route(&format!("/_assets/{}", "a".repeat(120))));
}

#[test]
fn duplicate_mount_unknown_version_field_mime_and_malformed_hash_refuse() {
    let mut f = Fixture::new();
    let valid = f.manifest.clone();
    for version in [0, 2] {
        f.manifest["version"] = json!(version);
        assert!(f.load().is_err());
    }
    f.manifest = valid.clone();
    f.manifest["unknown"] = json!(true);
    assert!(f.load().is_err());
    f.manifest = valid.clone();
    f.manifest["assets"][0]["tenant"] = json!("private");
    assert!(f.load().is_err());
    for content_type in [
        "application/json",
        "text/html\r\nSet-Cookie: secret",
        "text/javascript; charset=utf-8",
    ] {
        f.manifest = valid.clone();
        f.manifest["assets"][0]["content_type"] = json!(content_type);
        assert!(f.load().is_err());
    }
    for sha in [
        "A".repeat(64),
        "g".repeat(64),
        "a".repeat(63),
        "a".repeat(65),
        "0".repeat(64),
    ] {
        f.manifest = valid.clone();
        f.manifest["assets"][0]["sha256"] = json!(sha);
        assert!(f.load().is_err());
    }
    f.manifest = valid.clone();
    f.manifest["assets"] = json!([valid["assets"][0], valid["assets"][0]]);
    assert!(f.load().is_err());
}

#[test]
fn duplicate_json_keys_and_extra_bytes_refuse_before_file_mounting() {
    let f = Fixture::new();
    for raw in [
        "{\"version\":1,\"version\":1,\"assets\":[]}",
        "{\"version\":1,\"assets\":[]}trailing",
    ] {
        fs::write(f.path(), raw).unwrap();
        assert!(PublicAssets::load(&f.path()).is_err());
    }
}

#[test]
fn manifest_and_asset_count_have_independent_bounds() {
    let mut f = Fixture::new();
    let mut declared = Vec::new();
    for i in 0..MAX_ASSETS {
        let mut value = f.manifest["assets"][0].clone();
        value["route"] = json!(format!("/_assets/item{i}"));
        declared.push(value);
    }
    f.manifest["assets"] = json!(declared);
    assert!(f.load().is_ok());
    let mut extra = declared[0].clone();
    extra["route"] = json!("/_assets/extra");
    declared.push(extra);
    f.manifest["assets"] = json!(declared);
    assert!(f.load().is_err());
    f.manifest["assets"] = json!([]);
    assert!(f.load().is_err());
    fs::write(f.path(), " ".repeat(MAX_MANIFEST + 1)).unwrap();
    assert!(PublicAssets::load(&f.path()).is_err());
}

#[test]
fn asset_byte_and_utf8_limits_apply_to_actual_opened_file() {
    let mut f = Fixture::new();
    for raw in [
        vec![b'a'; MAX_ASSET],
        vec![b'a'; MAX_ASSET + 1],
        vec![0xff],
        vec![],
    ] {
        fs::write(f.asset_path(), &raw).unwrap();
        f.manifest["assets"][0]["sha256"] = json!(digest(&raw));
        assert_eq!(f.load().is_ok(), raw.len() == MAX_ASSET);
    }
}

#[test]
fn directories_links_missing_and_relative_files_are_not_public_mounts() {
    let mut f = Fixture::new();
    let original = f.asset_path();
    let link = f.root.path().join("linked.html");
    std::os::unix::fs::symlink(&original, &link).unwrap();
    for path in [
        link,
        f.root.path().to_path_buf(),
        f.root.path().join("missing"),
        PathBuf::from("index.html"),
    ] {
        f.manifest["assets"][0]["path"] = json!(path);
        assert!(f.load().is_err());
    }
    f.manifest["assets"][0]["path"] = json!(original);
    f.write();
    let manifest_link = f.root.path().join("manifest-link.json");
    std::os::unix::fs::symlink(f.path(), &manifest_link).unwrap();
    assert!(PublicAssets::load(&manifest_link).is_err());
    assert!(PublicAssets::load(Path::new("assets.json")).is_err());
}

#[test]
fn fifo_does_not_block_manifest_or_asset_admission() {
    use std::ffi::CString;
    use std::os::unix::ffi::OsStrExt;
    let mut f = Fixture::new();
    let path = f.root.path().join("fifo");
    let name = CString::new(path.as_os_str().as_bytes()).unwrap();
    assert_eq!(unsafe { libc::mkfifo(name.as_ptr(), 0o600) }, 0);
    assert!(PublicAssets::load(&path).is_err());
    f.manifest["assets"][0]["path"] = json!(path);
    assert!(f.load().is_err());
}

#[test]
fn exact_origin_accepts_same_origin_browser_and_explicit_native_clients_only() {
    let mut headers = HeaderMap::new();
    headers.insert("host", "127.0.0.1:42123".parse().unwrap());
    assert!(valid_origin(&headers, "127.0.0.1:42123"));
    headers.insert("origin", "http://127.0.0.1:42123".parse().unwrap());
    for value in ["none", "same-origin"] {
        headers.insert("sec-fetch-site", value.parse().unwrap());
        assert!(valid_origin(&headers, "127.0.0.1:42123"));
    }
    for value in ["cross-site", "same-site", "", "unknown"] {
        headers.insert("sec-fetch-site", value.parse().unwrap());
        assert!(!valid_origin(&headers, "127.0.0.1:42123"));
    }
    headers.remove("sec-fetch-site");
    for value in [
        "null",
        "https://attacker.invalid",
        "http://127.0.0.1:42124",
        "http://localhost:42123",
        "http://127.0.0.1:42123/",
        "http://127.0.0.1:42123 http://attacker.invalid",
    ] {
        headers.insert("origin", value.parse().unwrap());
        assert!(!valid_origin(&headers, "127.0.0.1:42123"));
    }
}

#[test]
fn rebound_or_ambiguous_headers_are_refused() {
    let mut headers = HeaderMap::new();
    assert!(!valid_origin(&headers, "127.0.0.1:42123"));
    for value in [
        "localhost:42123",
        "attacker.invalid:42123",
        "127.0.0.1",
        "127.0.0.1:42124",
    ] {
        headers.insert("host", value.parse().unwrap());
        assert!(!valid_origin(&headers, "127.0.0.1:42123"));
    }
    headers.insert("host", "127.0.0.1:42123".parse().unwrap());
    let original = headers.clone();
    headers.append("host", "127.0.0.1:42123".parse().unwrap());
    assert!(!valid_origin(&headers, "127.0.0.1:42123"));
    for name in ["origin", "sec-fetch-site"] {
        let mut duplicate = original.clone();
        let value = if name == "origin" {
            "http://127.0.0.1:42123"
        } else {
            "same-origin"
        };
        duplicate.append(name, value.parse().unwrap());
        duplicate.append(name, value.parse().unwrap());
        assert!(!valid_origin(&duplicate, "127.0.0.1:42123"));
    }
}

#[test]
fn browser_headers_constrain_static_and_refused_responses_without_enabling_cors() {
    let assets = Fixture::new().load().unwrap();
    for mut reply in [
        assets.response(&Method::GET, "/", true).unwrap(),
        super::super::refused(403),
    ] {
        harden(&mut reply);
        let headers = reply.headers();
        assert_eq!(headers["content-security-policy"], CSP);
        assert_eq!(headers["cache-control"], "no-store");
        assert_eq!(headers["referrer-policy"], "no-referrer");
        assert_eq!(headers["x-content-type-options"], "nosniff");
        assert_eq!(headers["x-frame-options"], "DENY");
        assert_eq!(headers["cross-origin-opener-policy"], "same-origin");
        assert_eq!(headers["cross-origin-resource-policy"], "same-origin");
        assert!(!headers.contains_key("access-control-allow-origin"));
        assert!(!headers.contains_key("set-cookie"));
    }
}
