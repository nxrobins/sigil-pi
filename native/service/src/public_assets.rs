//! Opt-in public byte mounts. No application routes, credentials or tenant data.
//! Operator-authored paths are opened once, bounded and digest checked at startup.
//! HTTP paths only select retained bytes; they are never filesystem paths.
use http_body_util::Full;
use hyper::body::Bytes;
use hyper::{HeaderMap, Method, Response};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use sigil_worker_bridge::strict;
use std::collections::BTreeMap;
use std::fs::OpenOptions;
use std::io::Read;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};

type Result<T> = std::result::Result<T, &'static str>;
const MAX_MANIFEST: usize = 16384;
const MAX_ASSET: usize = 65536;
const MAX_ASSETS: usize = 8;
const CSP: &str = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'";

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Manifest {
    version: u32,
    assets: Vec<AssetConfig>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct AssetConfig {
    route: String,
    path: PathBuf,
    sha256: String,
    content_type: String,
}

struct Asset {
    bytes: Bytes,
    content_type: &'static str,
}

pub struct PublicAssets {
    assets: BTreeMap<String, Asset>,
    digest: String,
}

fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn read_regular(path: &Path, limit: usize) -> Result<Vec<u8>> {
    if !path.is_absolute() || path.as_os_str().len() > 4096 {
        return Err("public_assets");
    }
    // O_NONBLOCK prevents an operator path raced/replaced with a FIFO from
    // hanging admission. No-follow rejects a final-component symlink. Bytes
    // are not published until the actual opened regular file matches its hash.
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK)
        .open(path)
        .map_err(|_| "public_assets")?;
    let metadata = file.metadata().map_err(|_| "public_assets")?;
    if !metadata.is_file() || metadata.len() > limit as u64 {
        return Err("public_assets");
    }
    let mut raw = Vec::new();
    file.take(limit as u64 + 1)
        .read_to_end(&mut raw)
        .map_err(|_| "public_assets")?;
    if raw.is_empty() || raw.len() > limit {
        return Err("public_assets");
    }
    Ok(raw)
}

fn route(value: &str) -> bool {
    if value == "/" {
        return true;
    }
    // The transport reserves only its landing page and asset namespace. An
    // operator cannot mount a public file over an application API endpoint.
    if !value.starts_with("/_assets/") || value.len() > 128 {
        return false;
    }
    value[1..].split('/').all(|part| {
        !part.is_empty()
            && part != "."
            && part != ".."
            && part
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.'))
    })
}

impl PublicAssets {
    pub fn load(path: &Path) -> Result<Self> {
        let raw = read_regular(path, MAX_MANIFEST)?;
        let manifest: Manifest =
            serde_json::from_value(strict::parse(&raw).map_err(|_| "public_assets")?)
                .map_err(|_| "public_assets")?;
        if manifest.version != 1 || manifest.assets.is_empty() || manifest.assets.len() > MAX_ASSETS
        {
            return Err("public_assets");
        }
        let mut assets = BTreeMap::new();
        for declared in manifest.assets {
            if !route(&declared.route)
                || declared.sha256.len() != 64
                || !declared
                    .sha256
                    .bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            {
                return Err("public_assets");
            }
            let content_type = match declared.content_type.as_str() {
                "text/html" => "text/html; charset=utf-8",
                "text/css" => "text/css; charset=utf-8",
                "text/javascript" => "text/javascript; charset=utf-8",
                _ => return Err("public_assets"),
            };
            let bytes = read_regular(&declared.path, MAX_ASSET)?;
            if std::str::from_utf8(&bytes).is_err() || digest(&bytes) != declared.sha256 {
                return Err("public_assets");
            }
            if assets
                .insert(
                    declared.route,
                    Asset {
                        bytes: Bytes::from(bytes),
                        content_type,
                    },
                )
                .is_some()
            {
                return Err("public_assets");
            }
        }
        Ok(Self {
            assets,
            digest: digest(&raw),
        })
    }

    pub fn digest(&self) -> &str {
        &self.digest
    }

    pub fn response(
        &self,
        method: &Method,
        path: &str,
        empty_body: bool,
    ) -> Option<Response<Full<Bytes>>> {
        let asset = self.assets.get(path)?;
        if method != Method::GET {
            let mut reply = super::refused(405);
            reply
                .headers_mut()
                .insert("Allow", "GET".parse().expect("static header"));
            return Some(reply);
        }
        if !empty_body {
            return Some(super::refused(400));
        }
        Some(
            Response::builder()
                .status(200)
                .header("Content-Type", asset.content_type)
                .header("Connection", "close")
                .body(Full::new(asset.bytes.clone()))
                .expect("static asset response"),
        )
    }
}

// These checks are transport-origin checks, not credential/tenant authorization.
// Native callers may omit Origin/Sec-Fetch-Site; browser callers may not assert a
// foreign or opaque origin. In this loopback-only profile the advertised address
// is also the sole Host value, preventing a DNS-rebound hostname from serving UI.
pub fn valid_origin(headers: &HeaderMap, authority: &str) -> bool {
    if headers.get_all("host").iter().count() != 1
        || headers.get("host").and_then(|v| v.to_str().ok()) != Some(authority)
    {
        return false;
    }
    let expected = format!("http://{authority}");
    let origins: Vec<_> = headers.get_all("origin").iter().collect();
    if origins.len() > 1
        || origins
            .first()
            .is_some_and(|v| v.to_str().ok() != Some(expected.as_str()))
    {
        return false;
    }
    let sites: Vec<_> = headers.get_all("sec-fetch-site").iter().collect();
    sites.len() <= 1
        && sites
            .first()
            .is_none_or(|v| matches!(v.to_str(), Ok("same-origin" | "none")))
}

pub fn harden(reply: &mut Response<Full<Bytes>>) {
    let headers = reply.headers_mut();
    for (name, value) in [
        ("cache-control", "no-store"),
        ("content-security-policy", CSP),
        ("referrer-policy", "no-referrer"),
        ("x-content-type-options", "nosniff"),
        ("x-frame-options", "DENY"),
        ("cross-origin-opener-policy", "same-origin"),
        ("cross-origin-resource-policy", "same-origin"),
        (
            "permissions-policy",
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        ),
    ] {
        headers.insert(name, value.parse().expect("static browser header"));
    }
}

#[cfg(test)]
mod tests;
