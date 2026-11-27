use super::*;
use crate::tests::fixture;

fn script(before_reply: &str) -> String {
    let wire = json!({"jsonrpc":"2.0","id":0,"result":{"content":[{"type":"text",
        "text":json!({"status":"ok","data":{"output_text":"pure"}}).to_string()}]}})
    .to_string();
    let (prefix, suffix) = wire.split_once("\"id\":0").unwrap();
    format!(
        r#"
printf '%s\n' "$$" >> '@MARKER@'
read -r line
printf '%s\n' '{{"jsonrpc":"2.0","id":1,"result":{{}}}}'
n=2
while read -r line; do
printf '%s\n' "$line" > '@MARKER@-request'
{before_reply}
printf '%s%s%s\n' '{prefix}"id":' "$n" '{suffix}'
n=$((n+1))
done
"#
    )
}

#[test]
fn fixed_grantless_process_is_reused_with_distinct_requests() {
    let (directory, bridge) = fixture(&script(""));
    let mut pure = PureBridge::new(bridge.config).unwrap();
    for input in ["first", "second", "third"] {
        assert_eq!(
            pure.invoke(input.into(), 100, 2000).unwrap()["data"]["output_text"],
            "pure"
        );
    }
    let starts = std::fs::read_to_string(directory.path().join("seen")).unwrap();
    assert_eq!(starts.lines().count(), 1);
    let raw = std::fs::read_to_string(directory.path().join("seen-request")).unwrap();
    let request: Value = serde_json::from_str(&raw).unwrap();
    assert_eq!(request["id"], 4);
    assert_eq!(request["params"]["arguments"]["input"], "third");
    assert_eq!(
        request["params"]["arguments"]["grants"],
        json!({"net":[],"fs":[],"secret":[]})
    );
    assert_eq!(request["params"]["arguments"]["source"], "module fixture;");
    pure.retire().unwrap();
    assert!(pure.worker.is_none());
}

#[test]
fn any_effect_grant_is_refused_before_spawn() {
    let (directory, bridge) = fixture(&script(""));
    for field in 0..3 {
        let mut config = bridge.config.clone();
        match field {
            0 => config.net.push("example.test".into()),
            1 => config.fs.push("/tmp".into()),
            _ => {
                config.secret_env.insert("secret".into(), "NOT_READ".into());
            }
        }
        assert!(matches!(PureBridge::new(config), Err(Fault::Invalid)));
    }
    assert!(!directory.path().join("seen").exists());
}

#[test]
fn partial_or_late_reply_is_reaped_without_resending_the_evaluation() {
    let (directory, bridge) = fixture(&script("if [ \"$n\" = 3 ]; then /bin/sleep 20; fi"));
    let mut pure = PureBridge::new(bridge.config).unwrap();
    pure.invoke("first".into(), 100, 2000).unwrap();
    assert_eq!(
        pure.invoke("second".into(), 100, 100).err(),
        Some(Fault::Deadline)
    );
    assert!(pure.worker.is_none() && !pure.admitted.poisoned);
    let starts = std::fs::read_to_string(directory.path().join("seen")).unwrap();
    assert_eq!(
        starts.lines().count(),
        1,
        "failure must not trigger an automatic resend"
    );
    pure.invoke("new evaluation".into(), 100, 2000).unwrap();
    let starts = std::fs::read_to_string(directory.path().join("seen")).unwrap();
    let pids: Vec<_> = starts.lines().collect();
    assert_eq!(pids.len(), 2);
    assert_ne!(pids[0], pids[1]);
}

#[test]
fn immutable_runtime_replacement_retires_even_an_existing_cached_process() {
    let (_directory, bridge) = fixture(&script(""));
    let mut pure = PureBridge::new(bridge.config).unwrap();
    pure.invoke("first".into(), 100, 2000).unwrap();
    std::fs::write(&pure.admitted.config.runtime, "#!/bin/sh\nexit 9\n").unwrap();
    assert_eq!(
        pure.invoke("second".into(), 100, 2000).err(),
        Some(Fault::Invalid)
    );
    assert!(pure.worker.is_none());
}

#[test]
fn process_lifetime_ceiling_reaps_before_replacement() {
    let (directory, bridge) = fixture(&script(""));
    let mut pure = PureBridge::new(bridge.config).unwrap();
    pure.invoke("first".into(), 100, 2000).unwrap();
    pure.calls = CALL_LIMIT;
    pure.invoke("after recycling".into(), 100, 2000).unwrap();
    assert_eq!(pure.calls, 1);
    let starts = std::fs::read_to_string(directory.path().join("seen")).unwrap();
    assert_eq!(starts.lines().count(), 2);
}

#[test]
fn limits_wrong_owner_and_unconfirmed_cleanup_never_invoke() {
    let (directory, bridge) = fixture(&script(""));
    let mut pure = PureBridge::new(bridge.config).unwrap();
    for (input, fuel, timeout) in [
        (String::new(), 0, 100),
        (String::new(), 1, 0),
        (String::new(), 1, 5001),
        ("x".repeat(4 * 1024 * 1024 + 1), 1, 100),
    ] {
        assert_eq!(pure.invoke(input, fuel, timeout).err(), Some(Fault::Limit));
    }
    pure.admitted.poisoned = true;
    assert_eq!(
        pure.invoke("x".into(), 100, 100).err(),
        Some(Fault::CleanupUnconfirmed)
    );
    pure.admitted.poisoned = false;
    pure.admitted.owner = pure.admitted.owner.wrapping_add(1);
    assert_eq!(
        pure.invoke("x".into(), 100, 100).err(),
        Some(Fault::WrongProcess)
    );
    assert!(!directory.path().join("seen").exists());
}
