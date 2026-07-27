/**
 * Shared five-level heat-stress alert logic + haptic mapping.
 *
 * SINGLE SOURCE OF TRUTH: this file is imported by BOTH the research
 * dashboard (services/feedback-layer/dashboard) and the Fitbit watch app
 * (services/feedback-layer/watch). Do not copy these thresholds anywhere
 * else — tests/test_alert_logic.py fails if threshold literals appear in
 * any other file, or if either consumer stops importing this module.
 *
 * Thresholds are the documented study values; do not change without
 * protocol review.
 */

// [minInclusive, maxExclusive) CBT bands in °C, index = level.
export const CBT_BANDS = [
  { level: 0, name: "Low",      min: -Infinity, max: 37.8 },
  { level: 1, name: "Watch",    min: 37.8,      max: 38.0 },
  { level: 2, name: "Elevated", min: 38.0,      max: 38.5 },
  { level: 3, name: "High",     min: 38.5,      max: 39.0 },
  { level: 4, name: "Critical", min: 39.0,      max: Infinity },
];

// Haptic mapping, watch-only (the dashboard is visual-only). `pattern` is a
// Fitbit SDK haptics pattern name; `repeat` is how many times to fire per
// alert cycle; `continuous: true` means re-fire every poll cycle while the
// level persists.
export const HAPTIC_MAP = {
  0: { pattern: null,     repeat: 0, continuous: false }, // no vibration
  1: { pattern: "bump",   repeat: 1, continuous: false }, // single brief
  2: { pattern: "nudge",  repeat: 1, continuous: false }, // stronger
  3: { pattern: "ping",   repeat: 3, continuous: false }, // repeated
  4: { pattern: "alert",  repeat: 3, continuous: true  }, // continuous/repeated
};

/**
 * Map a predicted CBT (°C) to its alert level. Returns the band object
 * ({level, name, min, max}) or null when cbt is missing/not a number.
 *
 * IMPORTANT: baseline_provisional does NOT factor in here and must never
 * suppress alerting — provisional affects confidence in the number, not
 * whether a genuine high reading alerts the wearer.
 */
export function alertLevel(cbt) {
  if (cbt === null || cbt === undefined || Number.isNaN(Number(cbt))) {
    return null;
  }
  const v = Number(cbt);
  for (const band of CBT_BANDS) {
    if (v >= band.min && v < band.max) return band;
  }
  return null; // unreachable given the bands cover (-Inf, +Inf)
}

/** Haptic spec for a level (or null for no haptics / unknown level). */
export function hapticsFor(level) {
  const spec = HAPTIC_MAP[level];
  return spec && spec.pattern ? spec : null;
}

/**
 * Secondary NIOSH heart-rate check: fitbit_hr > (180 - participant_age).
 * Additive display only — never changes the CBT-derived level. Returns
 * null (unknown) when either input is missing, e.g. participant_age is
 * null because the ledger has no age on file or the lookup failed.
 */
export function hrLimitExceeded(fitbitHr, participantAge) {
  if (fitbitHr === null || fitbitHr === undefined) return null;
  if (participantAge === null || participantAge === undefined) return null;
  return Number(fitbitHr) > 180 - Number(participantAge);
}
