use super::*;
use std::io::Cursor;

const ECHO: &str =
    "module echo; pub fn tool_main(p: i64, n: i64) -> i64 @Internal { return (p << 32) | n; }";
const FRESH: &str = r#"module fresh;
pub fn tool_main(p: i64, n: i64) -> i64 @Internal ! { Alloc } {
    if n != 1 { return -400; }
    let mut i: i64 @Internal = 0;
    while i < 5 { i += 1; }
    let previous: i64 @Internal = load8(60000);
    store8(60000, 83);
    let out: i64 @Internal = alloc(2);
    store8(out, load8(p)); store8(out + 1, previous + 48);
    return (out << 32) | 2;
}"#;

fn initialized() -> Evaluator {
    let mut evaluator = Evaluator::default();
    assert!(
        evaluator
            .request(br#"{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}"#)
            .is_ok()
    );
    evaluator
}
fn request(id: u64, source: &str, input: &str, fuel: u64) -> Value {
    json!({"jsonrpc":"2.0", "id":id, "method":"tools/call", "params":{
        "name":"sigil_forge", "arguments":{"source":source, "input":input, "fuel":fuel,
        "host_profile":"ephemeral", "grants":{"net":[], "fs":[], "secret":[]}}}})
}
fn call(evaluator: &mut Evaluator, request: Value) -> Result<Value> {
    evaluator
        .request(request.to_string().as_bytes())
        .map(|outer| {
            strict::parse(
                outer["result"]["content"][0]["text"]
                    .as_str()
                    .unwrap()
                    .as_bytes(),
            )
            .unwrap()
        })
}

#[test]
fn compiles_one_source_but_evaluates_each_input_in_fresh_memory() {
    let mut evaluator = initialized();
    for (id, input, expected) in [(2, "A", "A0"), (3, "B", "B0"), (4, "A", "A0")] {
        let result = call(&mut evaluator, request(id, FRESH, input, 1_000_000)).unwrap();
        assert_eq!(result["status"], "ok", "{result}");
        assert_eq!(result["data"]["output_text"], expected);
        let facts = &result["data"]["fixed_evaluator"];
        assert_eq!(facts["compilations"], 1);
        assert_eq!(facts["evaluations"], id - 1);
        assert_eq!(facts["solver_verified"], true);
        assert_eq!(facts["sigil_pin"], SIGIL_PIN);
        assert_eq!(
            facts["source_sha256"],
            format!("{:x}", Sha256::digest(FRESH.as_bytes()))
        );
    }
}

#[test]
fn changed_source_is_never_compiled_or_executed_in_a_held_process() {
    let mut evaluator = initialized();
    call(&mut evaluator, request(2, ECHO, "first", 1_000_000)).unwrap();
    assert_eq!(
        call(&mut evaluator, request(3, FRESH, "A", 1_000_000)),
        Err("source_changed")
    );
    assert_eq!(evaluator.calls, 1);
    assert_eq!(evaluator.artifact.as_ref().unwrap().source, ECHO);
}

#[test]
fn a_new_fuel_budget_applies_to_every_fresh_execution() {
    let mut evaluator = initialized();
    assert_eq!(
        call(&mut evaluator, request(2, FRESH, "A", 1_000_000)).unwrap()["status"],
        "ok"
    );
    assert_eq!(
        call(&mut evaluator, request(3, FRESH, "B", 1)).unwrap()["status"],
        "error"
    );
    let result = call(&mut evaluator, request(4, FRESH, "C", 1_000_000)).unwrap();
    assert_eq!(result["data"]["output_text"], "C0");
    assert_eq!(result["data"]["fixed_evaluator"]["compilations"], 1);
}

#[test]
fn invalid_source_never_creates_an_admitted_artifact() {
    let mut evaluator = initialized();
    assert_eq!(
        call(&mut evaluator, request(2, "this is not SIGIL", "", 1)),
        Err("verification")
    );
    assert!(evaluator.artifact.is_none());
    assert_eq!(evaluator.calls, 0);
}

#[test]
fn a_real_file_tool_cannot_read_with_empty_grants() {
    let mut evaluator = initialized();
    let source = include_str!("../../../tools/read_file.sigil");
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/src/main.rs");
    assert!(std::path::Path::new(path).is_file());
    let result = call(&mut evaluator, request(2, source, path, 1_000_000)).unwrap();
    assert_eq!(result["status"], "error");
    assert_eq!(result["data"]["output_text"], Value::Null);
}

#[test]
fn output_bytes_are_bounded_independently_of_guest_memory() {
    let source = "module output; pub fn tool_main(p: i64, n: i64) -> i64 @Internal ! { Alloc } { let out: i64 @Internal = alloc(4194305); return (out << 32) | 4194305; }";
    let mut evaluator = initialized();
    assert_eq!(
        call(&mut evaluator, request(2, source, "", 1_000_000)),
        Err("output_limit")
    );
}

#[test]
fn all_effect_grants_and_alternate_host_profiles_are_refused() {
    for grants in [
        json!({}),
        json!({"net":["127.0.0.1"], "fs":[], "secret":[]}),
        json!({"net":[], "fs":["/tmp"], "secret":[]}),
        json!({"net":[], "fs":[], "secret":["key=value"]}),
        json!({"net":[], "fs":[], "secret":[], "kv":[]}),
    ] {
        let mut evaluator = initialized();
        let mut command = request(2, ECHO, "", 1);
        command["params"]["arguments"]["grants"] = grants;
        assert_eq!(call(&mut evaluator, command), Err("admission"));
        assert!(evaluator.artifact.is_none());
    }
    let mut evaluator = initialized();
    let mut command = request(2, ECHO, "", 1);
    command["params"]["arguments"]["host_profile"] = json!("default");
    assert_eq!(call(&mut evaluator, command), Err("admission"));
}

#[test]
fn source_input_and_fuel_caps_apply_before_compilation() {
    for (source, input, fuel) in [
        (String::new(), String::new(), 1),
        ("x".repeat(SOURCE_MAX + 1), String::new(), 1),
        (ECHO.into(), "x".repeat(INPUT_MAX + 1), 1),
        (ECHO.into(), String::new(), 0),
        (ECHO.into(), String::new(), FUEL_MAX + 1),
    ] {
        let mut evaluator = initialized();
        assert_eq!(
            call(&mut evaluator, request(2, &source, &input, fuel)),
            Err("admission")
        );
        assert!(evaluator.artifact.is_none());
    }
}

#[test]
fn caller_asserted_proof_unknown_fields_and_duplicate_keys_are_refused() {
    let mut evaluator = initialized();
    let mut command = request(2, ECHO, "", 1);
    command["params"]["arguments"]["solver_verified"] = json!(true);
    assert_eq!(call(&mut evaluator, command), Err("protocol"));
    assert_eq!(
        evaluator.request(br#"{"jsonrpc":"2.0","id":2,"id":2,"method":"tools/call","params":{}}"#),
        Err("protocol")
    );
    assert!(evaluator.artifact.is_none());
}

#[test]
fn initialization_and_monotonic_request_ids_are_required() {
    let mut evaluator = Evaluator::default();
    assert_eq!(
        call(&mut evaluator, request(2, ECHO, "", 1)),
        Err("protocol")
    );
    let mut evaluator = initialized();
    assert_eq!(
        call(&mut evaluator, request(1, ECHO, "", 1)),
        Err("protocol")
    );
    assert_eq!(
        call(&mut evaluator, request(3, ECHO, "", 1)),
        Err("protocol")
    );
    let mut command = request(2, ECHO, "", 1);
    command["method"] = json!("sigil_check");
    assert_eq!(call(&mut evaluator, command), Err("protocol"));
}

#[test]
fn lifetime_limit_prevents_another_execution() {
    let mut evaluator = initialized();
    evaluator.calls = CALL_MAX;
    assert_eq!(
        call(&mut evaluator, request(CALL_MAX + 2, ECHO, "", 1)),
        Err("protocol")
    );
    assert!(evaluator.artifact.is_none());
}

#[test]
fn incomplete_or_oversized_frames_are_not_executed() {
    for input in [b"{\"jsonrpc\":\"2.0\"}".to_vec(), vec![b'x'; FRAME_MAX + 1]] {
        let mut output = Vec::new();
        assert_eq!(run(Cursor::new(input), &mut output), Err("frame_limit"));
        assert!(output.is_empty());
    }
}

#[test]
fn stdio_transports_current_results_not_cached_answers() {
    let mut wire =
        String::from("{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{}}\n");
    for (id, text) in [(2, "one"), (3, "two")] {
        wire.push_str(&request(id, ECHO, text, 1_000_000).to_string());
        wire.push('\n');
    }
    let mut output = Vec::new();
    run(Cursor::new(wire), &mut output).unwrap();
    let text = String::from_utf8(output).unwrap();
    let replies: Vec<Value> = text
        .lines()
        .map(|line| strict::parse(line.as_bytes()).unwrap())
        .collect();
    assert_eq!(replies.len(), 3);
    for (index, expected) in [(1, "one"), (2, "two")] {
        let result = strict::parse(
            replies[index]["result"]["content"][0]["text"]
                .as_str()
                .unwrap()
                .as_bytes(),
        )
        .unwrap();
        assert_eq!(result["data"]["output_text"], expected);
    }
}
