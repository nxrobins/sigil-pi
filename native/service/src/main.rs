//! Bounded loopback HTTP transport. No product routes or authorization policy.
use http_body_util::{BodyExt, Full};
use hyper::body::{Bytes, Incoming};
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper::{Request as HttpRequest, Response};
use hyper_util::rt::{TokioIo, TokioTimer};
use sigil_application_host::{Config, Engine, Reply, Request, Result};
use sigil_durable_store::store::OpenMode;
use sigil_worker_bridge::strict;
use std::convert::Infallible;
use std::fs::File;
use std::io::{Read, Write};
use std::net::{IpAddr, Ipv4Addr, SocketAddr};
use std::path::Path;
use std::sync::{Arc, mpsc};
use std::time::Duration;
use tokio::net::TcpListener;
use tokio::sync::{Semaphore, oneshot};

mod public_assets;

#[cfg(test)]
mod transport_tests;

struct BrowserTransport {
    assets: public_assets::PublicAssets,
    authority: String,
}

struct Job {
    request: Request,
    reply: oneshot::Sender<Result<Reply>>,
}
fn response(status: u16, body: impl Into<Bytes>) -> Response<Full<Bytes>> {
    Response::builder()
        .status(status)
        .header("Content-Type", "application/json")
        .header("Cache-Control", "no-store")
        .header("X-Content-Type-Options", "nosniff")
        .header("Connection", "close")
        .body(Full::new(body.into()))
        .expect("static response")
}
fn refused(status: u16) -> Response<Full<Bytes>> {
    response(status, "{\"error\":{\"code\":\"host_refused\"}}")
}
fn application_response(reply: Reply) -> Response<Full<Bytes>> {
    let mut response = Response::builder()
        .status(reply.status)
        .header("Content-Type", reply.content_type)
        .header("Cache-Control", "no-store")
        .header("X-Content-Type-Options", "nosniff")
        .header("Connection", "close");
    // These were decoded under the exact admitted metadata inventory. Native
    // HTTP framing and browser hardening cannot be selected by application data.
    for (name, value) in reply.headers {
        response = response.header(name, value);
    }
    response
        .body(Full::new(Bytes::from(reply.body)))
        .unwrap_or_else(|_| refused(503))
}
fn request_id_hints(headers: &hyper::HeaderMap, enabled: bool) -> Vec<Vec<u8>> {
    if !enabled {
        return Vec::new();
    }
    // Preserve raw values and order; SIGIL, not native, selects the caller label.
    headers
        .get_all("x-request-id")
        .iter()
        .map(|value| value.as_bytes().to_vec())
        .collect()
}
async fn handle(
    request: HttpRequest<Incoming>,
    queue: mpsc::SyncSender<Job>,
    web: Option<Arc<BrowserTransport>>,
    http_metadata: bool,
) -> Response<Full<Bytes>> {
    let mut reply = if web
        .as_ref()
        .is_some_and(|web| !public_assets::valid_origin(request.headers(), &web.authority))
    {
        refused(403)
    } else {
        handle_inner(request, queue, web.as_deref(), http_metadata).await
    };
    if web.is_some() {
        public_assets::harden(&mut reply);
    }
    reply
}
async fn handle_inner(
    request: HttpRequest<Incoming>,
    queue: mpsc::SyncSender<Job>,
    web: Option<&BrowserTransport>,
    http_metadata: bool,
) -> Response<Full<Bytes>> {
    let (parts, mut body) = request.into_parts();
    // Origin-form only; do not reinterpret proxy targets or ambiguous headers.
    if parts.uri.scheme().is_some()
        || parts.uri.authority().is_some()
        || parts.headers.get_all("authorization").iter().count() > 1
        || parts.headers.get_all("content-length").iter().count() > 1
        || parts.headers.contains_key("transfer-encoding")
    {
        return refused(400);
    }
    let authorization = match parts.headers.get("authorization") {
        Some(v) => match v.to_str() {
            Ok(s) if s.len() <= 4103 => Some(s.to_owned()),
            _ => return refused(400),
        },
        None => None,
    };
    let path = parts.uri.path_and_query().map(|v| v.as_str()).unwrap_or("");
    if path.len() > 4096 {
        return refused(414);
    }
    let read = async {
        let mut bytes = Vec::new();
        while let Some(frame) = body.frame().await {
            let frame = frame.map_err(|_| 400u16)?;
            if frame.is_trailers() {
                return Err(400);
            }
            if let Ok(data) = frame.into_data() {
                if bytes.len() + data.len() > 1024 * 1024 {
                    return Err(413);
                }
                bytes.extend_from_slice(&data);
            }
        }
        String::from_utf8(bytes).map_err(|_| 400)
    };
    let body = match tokio::time::timeout(Duration::from_secs(5), read).await {
        Ok(Ok(v)) => v,
        Ok(Err(status)) => return refused(status),
        Err(_) => return refused(408),
    };
    if let Some(reply) =
        web.and_then(|web| web.assets.response(&parts.method, path, body.is_empty()))
    {
        return reply;
    }
    let (send, receive) = oneshot::channel();
    let job = Job {
        request: Request {
            method: parts.method.as_str().to_owned(),
            path: path.to_owned(),
            authorization,
            body,
            request_id_hints: request_id_hints(&parts.headers, http_metadata),
        },
        reply: send,
    };
    if queue.try_send(job).is_err() {
        return refused(503);
    }
    match tokio::time::timeout(Duration::from_secs(30), receive).await {
        Ok(Ok(Ok(reply))) => application_response(reply),
        _ => refused(503),
    }
}
async fn run() -> Result<()> {
    let args: Vec<_> = std::env::args().collect();
    if args.len() != 4 && (args.len() != 6 || args[4] != "--public-assets") {
        return Err("arguments");
    }
    // Explicit opt-in, before opening or creating any application state. Older
    // binaries reject the longer invocation instead of silently ignoring it.
    let public_assets = if args.len() == 6 {
        Some(public_assets::PublicAssets::load(Path::new(&args[5]))?)
    } else {
        None
    };
    let mode = match args[1].as_str() {
        "init" => OpenMode::CreateNew,
        "open" => OpenMode::Existing,
        _ => return Err("arguments"),
    };
    let port: u16 = args[3].parse().map_err(|_| "arguments")?;
    let mut raw = Vec::new();
    File::open(&args[2])
        .map_err(|_| "config")?
        .take(262145)
        .read_to_end(&mut raw)
        .map_err(|_| "config")?;
    if raw.len() > 262144 {
        return Err("config");
    }
    let config: Config =
        serde_json::from_value(strict::parse(&raw).map_err(|_| "config")?).map_err(|_| "config")?;
    let protocol = format!("sigil-application-host/v{}", config.version);
    let http_metadata = matches!(config.version, 7..=9);
    let (queue, jobs) = mpsc::sync_channel::<Job>(16);
    let (ready, readiness) = mpsc::channel();
    std::thread::spawn(move || {
        let mut engine = match Engine::open(config, mode) {
            Ok(e) => {
                let _ = ready.send(Ok(()));
                e
            }
            Err(e) => {
                let _ = ready.send(Err(e));
                return;
            }
        };
        let mut last_error = None;
        loop {
            match jobs.recv_timeout(Duration::from_millis(25)) {
                Ok(job) if !job.reply.is_closed() => {
                    // A disconnected client may not learn whether a commit happened.
                    // Never repeat a native action to recover its HTTP reply.
                    let _ = job.reply.send(engine.request(job.request));
                }
                Err(mpsc::RecvTimeoutError::Disconnected) => break,
                _ => {}
            }
            let error = engine.tick().err();
            if error != last_error {
                if let Some(code) = error {
                    eprintln!(
                        "{}",
                        serde_json::json!({"event":"coordinator_refused","code":code})
                    );
                }
                last_error = error;
            }
        }
    });
    readiness
        .recv_timeout(Duration::from_secs(35))
        .map_err(|_| "startup")??;
    // Development transport only. No flag permits external binding before M8.
    let listener = TcpListener::bind(SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), port))
        .await
        .map_err(|_| "listen")?;
    let address = listener.local_addr().map_err(|_| "listen")?.to_string();
    let web = public_assets.map(|assets| {
        Arc::new(BrowserTransport {
            assets,
            authority: address.clone(),
        })
    });
    let mut ready = serde_json::json!({"status":"ready","protocol":protocol,"address":address});
    if let Some(web) = &web {
        ready["public_assets_sha256"] = serde_json::json!(web.assets.digest());
    }
    println!("{}", ready);
    std::io::stdout().flush().map_err(|_| "transport")?;
    let permits = Arc::new(Semaphore::new(32));
    loop {
        let (stream, _) = listener.accept().await.map_err(|_| "listen")?;
        let permit = match permits.clone().try_acquire_owned() {
            Ok(p) => p,
            Err(_) => {
                drop(stream);
                continue;
            }
        };
        let queue = queue.clone();
        let web = web.clone();
        tokio::spawn(async move {
            let _permit = permit;
            let service = service_fn(move |r| {
                let queue = queue.clone();
                let web = web.clone();
                async move { Ok::<_, Infallible>(handle(r, queue, web, http_metadata).await) }
            });
            let mut builder = http1::Builder::new();
            builder
                .keep_alive(false)
                .max_headers(64)
                .max_buf_size(65536)
                .timer(TokioTimer::new())
                .header_read_timeout(Duration::from_secs(5));
            let connection = builder.serve_connection(TokioIo::new(stream), service);
            let _ = tokio::time::timeout(Duration::from_secs(36), connection).await;
        });
    }
}
#[tokio::main(worker_threads = 2)]
async fn main() {
    if let Err(code) = run().await {
        eprintln!("{}", serde_json::json!({"status":"error","code":code}));
        std::process::exit(2);
    }
}
