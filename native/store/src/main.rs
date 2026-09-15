//! Scoped stdio adapter for a trusted embedding host. Not a network service.
//! One bootstrap scope per process; requests cannot select/mint another scope.
use serde::Deserialize;
use serde_json::{Value, json};
use sigil_durable_store::store::{
    Access, Batch, Check, Error, Limits, Mutation, OpenMode, Scope, Store,
};
use std::collections::BTreeMap;
use std::fs::File;
use std::io::{self, BufRead, Read, Write};
use std::path::Path;

const MAX_FRAME: u64 = 16 * 1024 * 1024;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Grant {
    namespace: String,
    access: Access,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Config {
    version: u32,
    limits: Limits,
    grants: Vec<Grant>,
}

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
enum Request {
    Metadata {
        namespace: String,
        after: Option<String>,
        limit: usize,
    },
    Keys {
        namespace: String,
        after: Option<String>,
        limit: usize,
    },
    Get {
        namespace: String,
        key: String,
    },
    Commit {
        checks: Vec<Check>,
        writes: Vec<Mutation>,
    },
}

fn handle(store: &mut Store, scope: &Scope, request: Request) -> Result<Value, Error> {
    match request {
        Request::Metadata {
            namespace,
            after,
            limit,
        } => Ok(json!({"page": store.metadata(scope, &namespace, after.as_deref(), limit)?})),
        Request::Keys {
            namespace,
            after,
            limit,
        } => Ok(json!({"page": store.keys(scope, &namespace, after.as_deref(), limit)?})),
        Request::Get { namespace, key } => {
            Ok(json!({"record": store.get(scope, &namespace, &key)?}))
        }
        Request::Commit { checks, writes } => {
            Ok(json!({"receipt": store.commit(scope, &Batch {checks,writes})?}))
        }
    }
}

fn handle_versioned(
    store: &mut Store,
    scope: &Scope,
    version: u32,
    request: Request,
) -> Result<Value, Error> {
    // Declare support before opening storage: old v1 executables reject a v2
    // bootstrap. The new executable also refuses metadata in its legacy profile.
    if !matches!(version, 1 | 2) || (version == 1 && matches!(&request, Request::Metadata { .. })) {
        return Err(Error::Invalid);
    }
    handle(store, scope, request)
}

fn run() -> Result<(), Error> {
    let args: Vec<_> = std::env::args().collect();
    if args.len() != 4 {
        return Err(Error::Invalid);
    }
    let mode = match args[1].as_str() {
        "init" => OpenMode::CreateNew,
        "open" => OpenMode::Existing,
        _ => return Err(Error::Invalid),
    };
    let mut raw = Vec::new();
    File::open(&args[3])
        .map_err(|_| Error::Invalid)?
        .take(65_537)
        .read_to_end(&mut raw)
        .map_err(|_| Error::Invalid)?;
    if raw.len() > 65_536 {
        return Err(Error::Limit);
    }
    let config: Config = serde_json::from_slice(&raw).map_err(|_| Error::Invalid)?;
    if !matches!(config.version, 1 | 2) {
        return Err(Error::Invalid);
    }
    let mut grants = BTreeMap::new();
    for grant in config.grants {
        if grants.insert(grant.namespace, grant.access).is_some() {
            return Err(Error::Invalid);
        }
    }
    Store::validate_grants(&grants)?;
    let mut store = Store::open(Path::new(&args[2]), mode, config.limits)?;
    let scope = store.scope(grants)?;
    let mut output = io::stdout().lock();
    writeln!(
        output,
        "{}",
        json!({"status":"ready","protocol":format!("sigil-store/v{}", config.version),"sqlite":rusqlite::version()})
    )
    .map_err(|_| Error::Storage)?;
    output.flush().map_err(|_| Error::Storage)?;
    let mut input = io::stdin().lock();
    loop {
        let mut frame = Vec::new();
        let size = (&mut input)
            .take(MAX_FRAME + 1)
            .read_until(b'\n', &mut frame)
            .map_err(|_| Error::Invalid)?;
        if size == 0 {
            return Ok(());
        }
        if size as u64 > MAX_FRAME || frame.last() != Some(&b'\n') {
            return Err(Error::Limit);
        }
        let outcome = serde_json::from_slice::<Request>(&frame)
            .map_err(|_| Error::Invalid)
            .and_then(|r| handle_versioned(&mut store, &scope, config.version, r));
        let response = match outcome {
            Ok(mut value) => {
                value["status"] = json!("ok");
                value
            }
            Err(code) => json!({"status":"error","code":code}),
        };
        writeln!(output, "{response}").map_err(|_| Error::Storage)?;
        output.flush().map_err(|_| Error::Storage)?;
    }
}

fn main() {
    if let Err(code) = run() {
        // No input, database path, record bytes or credentials in diagnostics.
        eprintln!("{}", json!({"status":"error","code":code}));
        std::process::exit(2);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    #[test]
    fn metadata_request_decoding_refuses_extra_fields_and_ambiguous_types() {
        for valid in [
            r#"{"op":"metadata","namespace":"app","after":null,"limit":1}"#,
            r#"{"op":"metadata","namespace":"app","limit":128}"#,
        ] {
            assert!(serde_json::from_str::<Request>(valid).is_ok());
        }
        for bad in [
            r#"{"op":"metadata","namespace":"app","after":0,"limit":1}"#,
            r#"{"op":"metadata","namespace":"app","limit":true}"#,
            r#"{"op":"metadata","namespace":"app","limit":1.0}"#,
            r#"{"op":"metadata","namespace":"app","limit":"1"}"#,
            r#"{"op":"metadata","namespace":"app","limit":1,"limit":2}"#,
            r#"{"op":"metadata","namespace":"app","limit":1,"grants":["*"]}"#,
            r#"{"op":"metadata","namespace":"app","limit":-1}"#,
            r#"{"op":"metadata","namespace":"app","limit":1,"sql":"SELECT * FROM records"}"#,
        ] {
            assert!(serde_json::from_str::<Request>(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn metadata_requires_a_distinct_profile_and_actual_read_scope() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
        let mut store = Store::open(dir.path(), OpenMode::CreateNew, Limits::default()).unwrap();
        let creator = store
            .scope(BTreeMap::from([("app".into(), Access::CreateOnly)]))
            .unwrap();
        store
            .commit(
                &creator,
                &Batch {
                    checks: vec![],
                    writes: vec![Mutation {
                        namespace: "app".into(),
                        key: "one".into(),
                        revision: 0,
                        value: Some("private-canary".into()),
                    }],
                },
            )
            .unwrap();
        let reader = store
            .scope(BTreeMap::from([("app".into(), Access::Read)]))
            .unwrap();
        let request = || Request::Metadata {
            namespace: "app".into(),
            after: None,
            limit: 1,
        };
        for version in [0, 1, 3, u32::MAX] {
            assert_eq!(
                handle_versioned(&mut store, &reader, version, request()),
                Err(Error::Invalid)
            );
        }
        assert_eq!(
            handle_versioned(&mut store, &creator, 2, request()),
            Err(Error::Denied)
        );
        assert_eq!(
            handle_versioned(&mut store, &reader, 2, request()).unwrap(),
            json!({
                "page":{"entries":[{"key":"one","revision":"1","present":true,"value_bytes":14}],"next":"one"}
            })
        );
        for version in [1, 2] {
            assert_eq!(
                handle_versioned(
                    &mut store,
                    &reader,
                    version,
                    Request::Keys {
                        namespace: "app".into(),
                        after: None,
                        limit: 1
                    }
                )
                .unwrap(),
                json!({"page":{"keys":["one"],"next":"one"}})
            );
        }
        assert_eq!(store.get(&reader, "app", "one").unwrap().revision, 1);
    }

    #[test]
    fn key_request_decoding_rejects_structural_ambiguity() {
        assert!(
            serde_json::from_str::<Request>(
                r#"{"op":"keys","namespace":"app","after":null,"limit":1}"#
            )
            .is_ok()
        );
        for bad in [
            r#"{"op":"keys","namespace":"app","after":0,"limit":1}"#,
            r#"{"op":"keys","namespace":"app","limit":"1"}"#,
            r#"{"op":"keys","namespace":"app","limit":1,"limit":2}"#,
            r#"{"op":"keys","namespace":"app","limit":1,"grants":["*"]}"#,
            r#"{"op":"keys","namespace":"app","limit":-1}"#,
        ] {
            assert!(serde_json::from_str::<Request>(bad).is_err());
        }
    }

    #[test]
    fn stdio_key_handler_cannot_mint_read_grants_or_return_record_values() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
        let mut store = Store::open(dir.path(), OpenMode::CreateNew, Limits::default()).unwrap();
        let scope = store
            .scope(BTreeMap::from([("app".into(), Access::CreateOnly)]))
            .unwrap();
        store
            .commit(
                &scope,
                &Batch {
                    checks: vec![],
                    writes: vec![Mutation {
                        namespace: "app".into(),
                        key: "one".into(),
                        revision: 0,
                        value: Some("private payload".into()),
                    }],
                },
            )
            .unwrap();
        let request = || Request::Keys {
            namespace: "app".into(),
            after: None,
            limit: 1,
        };
        assert_eq!(handle(&mut store, &scope, request()), Err(Error::Denied));
        let reader = store
            .scope(BTreeMap::from([("app".into(), Access::Read)]))
            .unwrap();
        assert_eq!(
            handle(&mut store, &reader, request()).unwrap(),
            json!({"page":{"keys":["one"],"next":"one"}})
        );
        assert_eq!(
            handle(
                &mut store,
                &reader,
                Request::Keys {
                    namespace: "app".into(),
                    after: None,
                    limit: 129
                }
            ),
            Err(Error::Invalid)
        );
    }
}
