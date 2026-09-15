use super::*;
use crate::fields;
use std::time::Duration;

#[test]
fn monotonic_facts_preserve_exact_nanoseconds_and_equal_samples_without_rounding() {
    let mut clock = MonotonicFacts::new().unwrap();
    let origin = clock.origin;
    let domain = clock.domain.clone();
    for nanos in [
        0,
        0,
        1,
        999_999_999,
        1_000_000_000,
        59_999_999_999,
        60_000_000_000,
    ] {
        let wire = clock
            .observe_at(origin + Duration::from_nanos(nanos))
            .unwrap();
        let expected = nanos.to_string();
        assert_eq!(
            fields(&wire, "MC1\n", 2).unwrap(),
            [domain.as_str(), expected.as_str()]
        );
        assert_eq!(clock.high, nanos);
        assert!(wire.len() <= 103);
    }
}

#[test]
fn backward_samples_are_refused_without_lowering_the_high_water_mark() {
    let mut clock = MonotonicFacts::new().unwrap();
    let origin = clock.origin;
    clock.observe_at(origin + Duration::from_nanos(10)).unwrap();
    assert_eq!(
        clock.observe_at(origin + Duration::from_nanos(9)),
        Err("clock")
    );
    assert_eq!(clock.high, 10);
    assert_eq!(
        clock.observe_at(origin - Duration::from_nanos(1)),
        Err("clock")
    );
    assert_eq!(clock.high, 10);
    assert!(clock.observe_at(origin + Duration::from_nanos(10)).is_ok());
}

#[test]
fn signed_64_bit_elapsed_limit_is_exact_and_cannot_wrap_or_reset() {
    let mut clock = MonotonicFacts::new().unwrap();
    // Test conversion at its full integer bound without assuming this platform's
    // Instant representation supports adding almost three hundred years.
    let at = Duration::from_nanos(i64::MAX as u64);
    let wire = clock.record_elapsed(at).unwrap();
    assert_eq!(fields(&wire, "MC1\n", 2).unwrap()[1], "9223372036854775807");
    assert_eq!(wire.len(), 103);
    assert_eq!(
        clock.record_elapsed(at + Duration::from_nanos(1)),
        Err("clock")
    );
    assert_eq!(clock.high, i64::MAX as u64);
    assert_eq!(clock.record_elapsed(Duration::ZERO), Err("clock"));
}

#[test]
fn new_instances_have_fresh_domains_and_cannot_silently_alias_old_elapsed_time() {
    let mut first = MonotonicFacts::new().unwrap();
    let mut second = MonotonicFacts::new().unwrap();
    assert_ne!(first.domain, second.domain);
    for clock in [&mut first, &mut second] {
        assert_eq!(clock.domain.len(), 64);
        assert!(
            clock
                .domain
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        );
        let wire = clock.observe_at(clock.origin).unwrap();
        assert_eq!(fields(&wire, "MC1\n", 2).unwrap()[1], "0");
    }
}

#[test]
fn a_changed_process_owner_cannot_observe_or_update_clock_state() {
    let mut clock = MonotonicFacts::new().unwrap();
    clock.owner = std::process::id().wrapping_add(1);
    assert_eq!(clock.observe(), Err("capability"));
    assert_eq!(clock.high, 0);
}

#[test]
fn actual_instant_observations_stay_in_one_domain_and_never_move_backwards() {
    let mut clock = MonotonicFacts::new().unwrap();
    let mut previous = 0;
    for _ in 0..32 {
        let wire = clock.observe().unwrap();
        let parts = fields(&wire, "MC1\n", 2).unwrap();
        assert_eq!(parts[0], clock.domain);
        let elapsed: u64 = parts[1].parse().unwrap();
        assert!(elapsed >= previous);
        previous = elapsed;
    }
}
