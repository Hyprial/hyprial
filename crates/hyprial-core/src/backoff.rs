//! Capped exponential backoff with deterministic jitter (spec section 6.3.3).

use std::time::SystemTime;

use crate::policy::RestartPolicy;

/// Small splitmix64 generator. `jitter_seed: None` seeds from OS entropy;
/// an explicit seed makes `sample01` sequences reproducible for tests.
#[derive(Debug, Clone)]
pub struct Jitter {
    state: u64,
}

impl Jitter {
    pub fn new(seed: Option<u64>) -> Jitter {
        let state = match seed {
            Some(s) => s,
            None => {
                let nanos = SystemTime::now()
                    .duration_since(SystemTime::UNIX_EPOCH)
                    .map(|d| d.as_nanos() as u64)
                    .unwrap_or(0x9E37_79B9_7F4A_7C15);
                nanos ^ (nanos.wrapping_mul(0x9E37_79B9_7F4A_7C15) | 1)
            }
        };
        Jitter { state }
    }

    /// Next uniform sample in `[0, 1)`.
    pub fn sample01(&mut self) -> f64 {
        self.state = self.state.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.state;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^= z >> 31;
        // 53 significant bits: uniform on [0, 1) without rounding to 1.0.
        (z >> 11) as f64 / (1u64 << 53) as f64
    }
}

/// Restart delay for the 1-based `attempt` (the failure-deque length after
/// recording) and one jitter `sample` in `[0, 1)`:
///
/// ```text
/// nominal = min(max_backoff, base_backoff * 2^(attempt-1))
/// offset  = nominal * jitter_ratio * (2*sample - 1)
/// delay   = max(0.0, nominal + offset)
/// ```
///
/// With `jitter_ratio = 0` the delay equals the nominal value exactly.
pub fn restart_delay(policy: &RestartPolicy, attempt: u64, sample: f64) -> f64 {
    let base = policy.base_backoff.as_secs_f64();
    // Cap the exponent so the doubling cannot overflow to NaN/inf garbage
    // before the min() with max_backoff settles it.
    let exponent = attempt.saturating_sub(1).min(62) as i32;
    let grown = base * 2f64.powi(exponent);
    let nominal = grown.min(policy.max_backoff.as_secs_f64()).max(0.0);
    let offset = nominal * policy.jitter_ratio * (2.0 * sample - 1.0);
    (nominal + offset).max(0.0)
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use super::*;

    fn policy(base: f64, max: f64, jitter: f64) -> RestartPolicy {
        RestartPolicy {
            max_restarts: 3,
            restart_window: Duration::from_secs(60),
            base_backoff: Duration::from_secs_f64(base),
            max_backoff: Duration::from_secs_f64(max),
            jitter_ratio: jitter,
        }
    }

    // SUP-05: the backoff formula is exact at jitter_ratio = 0.
    #[test]
    fn zero_jitter_is_exact_nominal() {
        let p = policy(0.1, 2.0, 0.0);
        assert_eq!(restart_delay(&p, 1, 0.0), 0.1);
        assert_eq!(restart_delay(&p, 2, 0.75), 0.2);
        assert_eq!(restart_delay(&p, 3, 0.5), 0.4);
        assert_eq!(restart_delay(&p, 4, 1.0 - 1e-12), 0.8);
        assert_eq!(restart_delay(&p, 9, 0.25), 2.0, "capped at max_backoff");
    }

    // SUP-05: jitter stays inside [max(0, nominal*(1-r)), nominal*(1+r)).
    #[test]
    fn jitter_bounds_hold() {
        let p = policy(0.25, 10.0, 0.2);
        let mut rng = Jitter::new(Some(7));
        for attempt in 1..=12u64 {
            let nominal = restart_delay(&p, attempt, 0.5); // sample 0.5 -> zero offset
            for _ in 0..2000 {
                let delay = restart_delay(&p, attempt, rng.sample01());
                let lower = (nominal * 0.8).max(0.0);
                let upper = nominal * 1.2;
                assert!(
                    delay >= lower - 1e-12 && delay <= upper + 1e-9,
                    "attempt {attempt}: {delay} outside [{lower}, {upper}]"
                );
            }
        }
    }

    #[test]
    fn jitter_seed_is_reproducible() {
        let mut a = Jitter::new(Some(42));
        let mut b = Jitter::new(Some(42));
        for _ in 0..100 {
            assert_eq!(a.sample01(), b.sample01());
        }
        assert!((0.0..1.0).contains(&a.sample01()));
    }

    #[test]
    fn huge_attempt_count_caps_at_max_backoff() {
        let p = policy(0.5, 5.0, 0.0);
        assert_eq!(restart_delay(&p, 500, 0.0), 5.0);
    }
}
