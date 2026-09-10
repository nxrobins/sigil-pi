use super::*;

#[test]
fn presentation_describes_prefix_only_not_token_validity_or_permissions() {
    for raw in [
        "Bearer ",
        "Bearer unknown",
        "Bearer  ",
        "Bearer é",
        "Bearer x\n",
    ] {
        let seen = Presentation::observe(Some(raw));
        assert_eq!(seen, Presentation::ExactBearer);
        assert_eq!(seen.wire(), "1");
    }
    for raw in [
        None,
        Some(""),
        Some("bearer x"),
        Some("BEARER x"),
        Some(" Bearer x"),
        Some("Bearer"),
        Some("Bearer\tx"),
        Some("Basic x"),
    ] {
        let seen = Presentation::observe(raw);
        assert_eq!(seen, Presentation::Other);
        assert_eq!(seen.wire(), "0");
    }
}

#[test]
fn presentation_does_not_retain_token_material() {
    let observed = {
        let raw = format!("Bearer {}", "opaque-credential-canary".repeat(4096));
        Presentation::observe(Some(&raw))
    };
    assert_eq!(format!("{observed:?}"), "ExactBearer");
    assert_eq!(std::mem::size_of::<Presentation>(), 1);
}

#[test]
fn seconds_and_fraction_come_from_one_sample_without_rounding_or_unit_changes() {
    for seconds in [0, 59, 60, 119, 120, 9_007_199_254_740_991] {
        for nanoseconds in [0, 1, 1_000_000, 999_999_999] {
            let observed = ClockSample::from_elapsed(Duration::new(seconds, nanoseconds));
            assert_eq!(observed.seconds, seconds);
            assert_eq!(
                observed.fraction_wire(),
                if nanoseconds == 0 { "0" } else { "1" }
            );
        }
    }
}

#[test]
fn clock_rejects_pre_epoch_samples_and_preserves_exact_boundary() {
    assert_eq!(
        ClockSample::from_time(UNIX_EPOCH - Duration::from_nanos(1)),
        Err("clock")
    );
    let at = ClockSample::from_time(UNIX_EPOCH).unwrap();
    assert_eq!(at.seconds, 0);
    assert_eq!(at.fraction_wire(), "0");
    let next = ClockSample::from_time(UNIX_EPOCH + Duration::new(60, 1)).unwrap();
    assert_eq!(next.seconds, 60);
    assert_eq!(next.fraction_wire(), "1");
}
