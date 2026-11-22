use super::*;

#[test]
fn transport_profile_selection_requires_its_exact_explicit_features() {
    for version in 0..=10 {
        for automatic in [false, true] {
            for http in [false, true] {
                let expected = match (version, automatic, http) {
                    (3, false, false) | (4, true, false) => Some(EntryContract::Legacy),
                    (5, false, false) | (6, true, false) => Some(EntryContract::Metadata),
                    (7, true, true) => Some(EntryContract::HttpExchange),
                    (8, true, true) => Some(EntryContract::RequestAdmission),
                    (9, true, true) => Some(EntryContract::ProcessFacts),
                    _ => None,
                };
                assert_eq!(entry_profile(version, automatic, http).ok(), expected);
            }
        }
    }
}

#[test]
fn new_http_configuration_distinguishes_absence_from_null_or_unknown_fields() {
    #[derive(serde::Deserialize)]
    #[serde(deny_unknown_fields)]
    struct Probe {
        #[serde(default, deserialize_with = "explicit_http_config")]
        http: Option<HttpConfig>,
    }
    assert!(serde_json::from_str::<Probe>("{}").unwrap().http.is_none());
    let valid = r#"{"http":{"response_headers":["x-request-id"]}}"#;
    assert_eq!(
        serde_json::from_str::<Probe>(valid)
            .unwrap()
            .http
            .unwrap()
            .response_headers,
        ["x-request-id"]
    );
    for raw in [
        r#"{"http":null}"#,
        r#"{"http":{}}"#,
        r#"{"http":{"response_headers":null}}"#,
        r#"{"http":{"response_headers":[],"allow_all":true}}"#,
    ] {
        assert!(serde_json::from_str::<Probe>(raw).is_err(), "{raw}");
    }
}

#[test]
fn bundle_http_manifest_binds_canonical_names_and_actual_mechanism_limits() {
    let a = http_exchange::HeaderPolicy::new(&["x-request-id", "retry-after"]).unwrap();
    let b = http_exchange::HeaderPolicy::new(&["retry-after", "x-request-id"]).unwrap();
    let narrower = http_exchange::HeaderPolicy::new(&["x-request-id"]).unwrap();
    assert_eq!(http_manifest(&a), http_manifest(&b));
    assert_ne!(http_manifest(&a), http_manifest(&narrower));
    let manifest = http_manifest(&a);
    assert_eq!(
        manifest["request_headers"],
        serde_json::json!(["x-request-id"])
    );
    assert_eq!(manifest["request_header_bytes"], 65536);
    assert_eq!(manifest["request_header_count"], 64);
    assert_eq!(manifest["fresh_request_identity_bits"], 128);
    assert_eq!(manifest["response_frame_bytes"], 2097152);
    assert_eq!(manifest["header_count"], 16);
    assert_eq!(manifest["header_bytes"], 8192);
    assert_eq!(manifest["header_value_bytes"], 1024);
    assert_eq!(
        manifest["response_types"],
        serde_json::json!([
            "application/json",
            "text/plain; charset=utf-8",
            "text/plain; version=0.0.4; charset=utf-8"
        ])
    );
    assert_eq!(manifest["envelope"], "AH5");
    assert_eq!(manifest["command"], "HC5");
}

#[test]
fn versioned_bundle_preserves_v7_and_binds_v8_facts_read_set_and_existing_limits() {
    let policy = http_exchange::HeaderPolicy::new(&["x-request-id", "retry-after"]).unwrap();
    let base = serde_json::json!({"steps":8,"entry":{"source":"bound-source"}});
    let mut old = base.clone();
    bind_http_manifest(&mut old, EntryContract::HttpExchange, &policy);
    assert_eq!(
        old,
        serde_json::json!({
            "steps":8,"entry":{"source":"bound-source"},
            "contract":"sigil-application-host/v7","http":http_manifest(&policy)
        })
    );
    let mut new = base.clone();
    bind_http_manifest(&mut new, EntryContract::RequestAdmission, &policy);
    assert_eq!(new["steps"], 8);
    assert_eq!(new["entry"], base["entry"]);
    assert_eq!(new["contract"], "sigil-application-host/v8");
    assert_eq!(new["http"]["contract"], "sigil-http-exchange/v2");
    assert_eq!(new["http"]["envelope"], "AH6");
    assert_eq!(new["http"]["command"], "HC6");
    assert_eq!(new["http"]["envelope_fields"], 17);
    assert_eq!(
        new["http"]["presentation"],
        serde_json::json!({
            "index":15,"source":"authorization-exact-Bearer-space-prefix",
            "exact_prefix":"1","other":"0","boot":"","authorization_verdict":false
        })
    );
    assert_eq!(
        new["http"]["clock_fraction"],
        serde_json::json!({
            "index":16,"same_sample_as_index":4,"units":"nonzero-subsecond-nanoseconds",
            "present":"1","absent":"0"
        })
    );
    for (name, expected) in old["http"].as_object().unwrap() {
        if !matches!(name.as_str(), "contract" | "envelope" | "command") {
            assert_eq!(&new["http"][name], expected, "{name}");
        }
    }
    assert_eq!(
        new["read_set"],
        serde_json::json!({
            "contract":"sigil-read-set/v1","command":"read_many","observation":"RM1",
            "batch":"RB1","record":"RR1","query_bytes":4096,"items":3,
            "value_bytes":2097152,"response_bytes":2097152,"snapshot":"single-read-transaction",
            "scope":"held-native-scope-all-addresses","errors":"all-or-nothing"
        })
    );
    let digest = |value: &serde_json::Value| Sha256::digest(serde_json::to_vec(value).unwrap());
    assert_ne!(digest(&new), digest(&old));
    let narrower = http_exchange::HeaderPolicy::new(&["x-request-id"]).unwrap();
    let mut restricted = base;
    bind_http_manifest(&mut restricted, EntryContract::RequestAdmission, &narrower);
    assert_ne!(digest(&new), digest(&restricted));
    for altered in [
        {
            let mut v = new.clone();
            v["read_set"]["items"] = 4.into();
            v
        },
        {
            let mut v = new.clone();
            v["http"]["clock_fraction"]["same_sample_as_index"] = 5.into();
            v
        },
    ] {
        assert_ne!(digest(&new), digest(&altered));
    }
}
