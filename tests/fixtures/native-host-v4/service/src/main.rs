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
use std::sync::{Arc, mpsc};
use std::time::Duration;
use tokio::net::TcpListener;
use tokio::sync::{Semaphore, oneshot};

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
async fn handle(
    request: HttpRequest<Incoming>,
    queue: mpsc::SyncSender<Job>,
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
    let (send, receive) = oneshot::channel();
    let job = Job {
        request: Request {
            method: parts.method.as_str().to_owned(),
            path: path.to_owned(),
            authorization,
            body,
        },
        reply: send,
    };
    if queue.try_send(job).is_err() {
        return refused(503);
    }
    match tokio::time::timeout(Duration::from_secs(30), receive).await {
        Ok(Ok(Ok(reply))) => response(reply.status, reply.body),
        _ => refused(503),
    }
}
async fn run() -> Result<()> {
    let args: Vec<_> = std::env::args().collect();
    if args.len() != 4 {
        return Err("arguments");
    }
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
    println!(
        "{}",
        serde_json::json!({"status":"ready","protocol":protocol,"address":listener.local_addr().map_err(|_|"listen")?.to_string()})
    );
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
        tokio::spawn(async move {
            let _permit = permit;
            let service = service_fn(move |r| {
                let queue = queue.clone();
                async move { Ok::<_, Infallible>(handle(r, queue).await) }
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
