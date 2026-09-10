use super::*;

#[test]
fn transport_profile_selection_requires_its_exact_explicit_features() {
    for version in 0..=9 {
        for automatic in [false, true] {
            for http in [false, true] {
                let expected = match (version, automatic, http) {
                    (3, false, false) | (4, true, false) => Some(EntryContract::Legacy),
                    (5, false, false) | (6, true, false) => Some(EntryContract::Metadata),
                    (7, true, true) => Some(EntryContract::HttpExchange),
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
