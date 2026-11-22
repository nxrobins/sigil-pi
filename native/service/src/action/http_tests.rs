use super::*;
use crate::http_exchange::HeaderPolicy;
use std::time::Duration;

fn future() -> Instant {
    Instant::now() + Duration::from_secs(30)
}
fn guard() -> String {
    frame("TG1\n", &["100", "200"]).unwrap()
}
fn response(name: &str, value: &str, body: &str) -> String {
    let headers = frame("HH1\n", &["1", name, value]).unwrap();
    frame("HR1\n", &["application/json", &headers, body]).unwrap()
}
fn reply(body: &str, guard: &str) -> String {
    frame("HC5\n", &["reply", "200", body, "", guard]).unwrap()
}

#[test]
fn http_commands_require_new_abi_and_held_native_header_policy() {
    let policy = HeaderPolicy::new(&["x-request-id"]).unwrap();
    let raw = reply(&response("x-request-id", "trace-id-123", "{}"), &guard());
    for contract in [
        EntryContract::Legacy,
        EntryContract::Metadata,
        EntryContract::HttpExchange,
    ] {
        let mut high = 100;
        assert_eq!(
            perform_with_contract(contract, None, None, &mut high, future(), &raw, || panic!(
                "new command executed without its held policy"
            ))
            .unwrap_err(),
            "protocol"
        );
    }
    for contract in [EntryContract::Legacy, EntryContract::Metadata] {
        assert!(
            perform_with_http_contract(
                (contract, Some(&policy)),
                None,
                None,
                &mut 100,
                future(),
                &raw,
                || panic!("old profile acquired response metadata")
            )
            .is_err()
        );
    }
    assert_eq!(EntryContract::HttpExchange.envelope_marker(), "AH5\n");
    assert_eq!(EntryContract::HttpExchange.command_marker(), "HC5\n");
}

#[test]
fn invalid_metadata_is_refused_before_the_action_clock_boundary() {
    let policy = HeaderPolicy::new(&["x-request-id"]).unwrap();
    let valid = response("x-request-id", "trace-id-123", "protected-body");
    for body in [
        "{}".to_owned(),
        valid.clone() + "trailing",
        response("x-foreign", "trace-id-123", "protected-body"),
        response("x-request-id", "trace\r\nInjected: value", "protected-body"),
        valid.replace("application/json", "text/html"),
    ] {
        let mut high = 100;
        assert!(
            perform_with_http_contract(
                (EntryContract::HttpExchange, Some(&policy)),
                None,
                None,
                &mut high,
                future(),
                &reply(&body, &guard()),
                || panic!("invalid metadata reached native action initiation")
            )
            .is_err()
        );
        assert_eq!(high, 100);
    }
}

#[test]
fn parsed_metadata_retains_the_same_authority_and_monotonic_deadline_checks() {
    let policy = HeaderPolicy::new(&["x-request-id"]).unwrap();
    let raw = reply(
        &response("x-request-id", "trace-id-123", "protected-body"),
        &guard(),
    );
    let mut high = 100;
    assert_eq!(
        perform_with_http_contract(
            (EntryContract::HttpExchange, Some(&policy)),
            None,
            None,
            &mut high,
            future(),
            &raw,
            || Ok(200)
        )
        .unwrap_err(),
        "time_guard"
    );
    assert_eq!(high, 200);
    assert_eq!(
        perform_with_http_contract(
            (EntryContract::HttpExchange, Some(&policy)),
            None,
            None,
            &mut high,
            future(),
            &raw,
            || Ok(150)
        )
        .unwrap_err(),
        "clock"
    );
    assert_eq!(
        perform_with_http_contract(
            (EntryContract::HttpExchange, Some(&policy)),
            None,
            None,
            &mut 100,
            Instant::now(),
            &raw,
            || panic!("expired monotonic deadline read clock")
        )
        .unwrap_err(),
        "deadline"
    );
}

#[test]
fn allowed_response_is_owned_only_after_full_metadata_and_guard_validation() {
    let policy = HeaderPolicy::new(&["x-request-id"]).unwrap();
    let raw = reply(
        &response("x-request-id", "trace-id-123", "protected-body"),
        &guard(),
    );
    let Step::Reply {
        reply,
        guard: observed,
    } = perform_with_http_contract(
        (EntryContract::HttpExchange, Some(&policy)),
        None,
        None,
        &mut 100,
        future(),
        &raw,
        || Ok(150),
    )
    .unwrap()
    else {
        panic!("not a response");
    };
    assert_eq!(reply.status, 200);
    assert_eq!(reply.body, "protected-body");
    assert_eq!(reply.content_type, "application/json");
    assert_eq!(
        reply.headers,
        [("x-request-id".to_owned(), "trace-id-123".to_owned())]
    );
    assert_eq!(observed, Some(Guard::parse(&guard()).unwrap()));
}

#[test]
fn old_reply_body_is_never_reinterpreted_as_new_metadata() {
    let body = response("x-request-id", "trace-id-123", "different-body");
    for (contract, marker) in [
        (EntryContract::Legacy, "HC3\n"),
        (EntryContract::Metadata, "HC4\n"),
    ] {
        let raw = frame(marker, &["reply", "200", &body, "", &guard()]).unwrap();
        let Step::Reply { reply, .. } =
            perform_with_contract(contract, None, None, &mut 100, future(), &raw, || Ok(150))
                .unwrap()
        else {
            panic!("not a response");
        };
        assert_eq!(reply.body, body);
        assert_eq!(reply.content_type, "application/json");
        assert!(reply.headers.is_empty());
    }
}

#[test]
fn new_metadata_cannot_widen_the_outer_command_frame_limit() {
    let policy = HeaderPolicy::new(&["x-request-id"]).unwrap();
    let overhead = response("x-request-id", "trace-id-123", "").len();
    let body = response(
        "x-request-id",
        "trace-id-123",
        &"a".repeat(2097152 - overhead),
    );
    assert_eq!(body.len(), 2097152);
    assert!(policy.parse(&body).is_ok());
    assert_eq!(
        perform_with_http_contract(
            (EntryContract::HttpExchange, Some(&policy)),
            None,
            None,
            &mut 100,
            future(),
            &reply(&body, &guard()),
            || panic!("oversize command reached action initiation")
        )
        .unwrap_err(),
        "protocol"
    );
}

#[test]
fn header_inventory_does_not_grant_storage_or_interpret_function_inputs() {
    let policy = HeaderPolicy::new(&["x-request-id"]).unwrap();
    let raw = frame(
        "HC5\n",
        &[
            "metadata",
            "foreign",
            r#"{"after":null,"limit":1}"#,
            "held",
            &guard(),
        ],
    )
    .unwrap();
    assert_eq!(
        perform_with_http_contract(
            (EntryContract::HttpExchange, Some(&policy)),
            None,
            None,
            &mut 100,
            future(),
            &raw,
            || Ok(150)
        )
        .unwrap_err(),
        "capability"
    );
    let input = response("x-request-id", "trace-id-123", "not-a-command");
    let raw = frame("HC5\n", &["call", "function", &input, "held", &guard()]).unwrap();
    let Step::Call {
        target,
        input: observed,
        continuation,
    } = perform_with_http_contract(
        (EntryContract::HttpExchange, Some(&policy)),
        None,
        None,
        &mut 100,
        future(),
        &raw,
        || Ok(150),
    )
    .unwrap()
    else {
        panic!("not a function call");
    };
    assert_eq!(target, "function");
    assert_eq!(observed, input);
    assert_eq!(continuation, "held");
}
