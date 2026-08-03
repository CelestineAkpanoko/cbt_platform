"""Fitbit API request-budget calculator.

Fitbit allows **150 requests per hour per participant**. That ceiling is
per-user, so it cannot be relieved by more Lambdas, more memory or more
concurrency — the only lever is how many endpoints are called how often.
Getting this wrong does not fail loudly: it produces 429s, serial
back-off sleeps inside the invocation, timeouts, and gaps in the data that
look like a device problem.

The deployed puller was over the ceiling by 33% (200/hr/user) while
claiming 120/hr in its own docstring, because a grace-day loop doubled
every count. This module exists so the arithmetic is checked before a
cadence change is applied, not after it starts dropping data.

    python -m ops.fitbit_budget                       # the shipped defaults
    python -m ops.fitbit_budget --schedule-minutes 1  # can we go to 1 min?
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

# Fitbit's documented per-user hourly quota.
FITBIT_HOURLY_LIMIT = 150

# Keep some room for token refreshes, retries after a transient 5xx, and
# manual backfill runs. Sitting exactly at the ceiling means the first
# retry of the hour is a 429.
DEFAULT_HEADROOM = 0.20

# Requests issued per participant-date, by tier. Heart rate costs one
# request and yields the confidence series for free (it is derived from the
# same payload, not fetched separately).
FAST_ENDPOINTS = ("heart_rate_intraday", "steps_intraday")
SLOW_ENDPOINTS = (
    "calories_intraday", "distance_intraday", "body_temp",
    "hrv_intraday", "spo2_intraday", "breathing_rate_intraday",
)


@dataclass
class Budget:
    schedule_minutes: int
    slow_tier_minutes: int
    grace_window_days: int
    runs_per_hour: float
    fast_calls: float
    slow_calls: float
    grace_calls: float
    token_calls: float

    @property
    def total(self) -> float:
        return self.fast_calls + self.slow_calls + self.grace_calls + self.token_calls

    @property
    def limit_with_headroom(self) -> float:
        return FITBIT_HOURLY_LIMIT * (1 - DEFAULT_HEADROOM)

    @property
    def fits(self) -> bool:
        return self.total <= self.limit_with_headroom


def compute(schedule_minutes: int, slow_tier_minutes: int,
            grace_window_days: int = 1, fast_endpoints: int = None) -> Budget:
    """fast_endpoints lets you price the "true 1-minute" variant, where
    steps drops to the slow tier so only heart rate — the series CBT
    prediction actually consumes — runs every minute."""
    n_fast = len(FAST_ENDPOINTS) if fast_endpoints is None else fast_endpoints
    n_slow = len(SLOW_ENDPOINTS) + (len(FAST_ENDPOINTS) - n_fast)
    runs_per_hour = 60 / schedule_minutes
    slow_runs_per_hour = 60 / max(slow_tier_minutes, schedule_minutes)
    return Budget(
        schedule_minutes=schedule_minutes,
        slow_tier_minutes=slow_tier_minutes,
        grace_window_days=grace_window_days,
        runs_per_hour=runs_per_hour,
        fast_calls=n_fast * runs_per_hour,
        slow_calls=n_slow * slow_runs_per_hour,
        # Grace days are pulled once an hour, at full (fast+slow) coverage.
        grace_calls=(len(FAST_ENDPOINTS) + len(SLOW_ENDPOINTS)) * grace_window_days,
        # One refresh per hour per participant; the access token lasts 8h,
        # so this is generous.
        token_calls=1.0,
    )


def render(b: Budget) -> str:
    lines = [
        f"Schedule           every {b.schedule_minutes} min  "
        f"({b.runs_per_hour:.0f} runs/hour)",
        f"Slow tier          every {b.slow_tier_minutes} min",
        f"Grace window       {b.grace_window_days} day(s), hourly",
        "",
        f"  fast   {b.fast_calls / b.runs_per_hour:.0f} endpoints x {b.runs_per_hour:>4.0f} runs "
        f"= {b.fast_calls:>6.0f} req/hour/participant",
        f"  slow   {b.slow_calls / max(60 / max(b.slow_tier_minutes, b.schedule_minutes), 1):.0f} endpoints x "
        f"{60 / max(b.slow_tier_minutes, b.schedule_minutes):>4.0f} runs "
        f"= {b.slow_calls:>6.0f}",
        f"  grace  {len(FAST_ENDPOINTS) + len(SLOW_ENDPOINTS)} endpoints x "
        f"{b.grace_window_days:>4} day  = {b.grace_calls:>6.0f}",
        f"  token refresh                    = {b.token_calls:>6.0f}",
        f"  {'-' * 44}",
        f"  TOTAL                            = {b.total:>6.0f} req/hour/participant",
        "",
        f"Fitbit ceiling     {FITBIT_HOURLY_LIMIT} req/hour/participant",
        f"Safe target        {b.limit_with_headroom:.0f} "
        f"({DEFAULT_HEADROOM:.0%} headroom for retries and backfills)",
    ]
    if b.fits:
        spare = b.limit_with_headroom - b.total
        lines.append(f"\n  FITS — {spare:.0f} req/hour/participant to spare.")
    else:
        over = b.total - b.limit_with_headroom
        lines.append(
            f"\n  OVER BUDGET by {over:.0f} req/hour/participant.\n"
            f"  This will produce 429s, serial back-off inside the Lambda,\n"
            f"  timeouts, and gaps that look like device problems.\n"
            f"  Raise --slow-tier-minutes or --schedule-minutes."
        )
    return "\n".join(lines)


def main():  # pragma: no cover - CLI
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--schedule-minutes", type=int, default=2,
                    help="EventBridge Scheduler rate (default: 2)")
    ap.add_argument("--slow-tier-minutes", type=int, default=15,
                    help="SLOW_TIER_MINUTES env var (default: 15)")
    ap.add_argument("--grace-window-days", type=int, default=1,
                    help="GRACE_WINDOW_DAYS env var (default: 1)")
    ap.add_argument("--fast-endpoints", type=int, default=None,
                    help="endpoints in the fast tier (default 2: heart rate "
                         "+ steps; use 1 to price heart-rate-only)")
    ap.add_argument("--compare-old", action="store_true",
                    help="also show the previously deployed configuration")
    args = ap.parse_args()

    print(render(compute(args.schedule_minutes, args.slow_tier_minutes,
                         args.grace_window_days, args.fast_endpoints)))
    if args.compare_old:
        print("\n" + "=" * 60)
        print("PREVIOUSLY DEPLOYED (every 3 min, no tiering, grace on every "
              "run):\n")
        # 5 endpoints x 2 dates x 20 runs/hour
        old = Budget(schedule_minutes=3, slow_tier_minutes=3,
                     grace_window_days=1, runs_per_hour=20,
                     fast_calls=5 * 20, slow_calls=0, grace_calls=5 * 20,
                     token_calls=1)
        print(f"  5 endpoints x 2 dates x 20 runs/hour = {old.total:.0f} "
              f"req/hour/participant")
        print(f"  vs ceiling {FITBIT_HOURLY_LIMIT} — "
              f"{old.total - FITBIT_HOURLY_LIMIT:.0f} OVER, "
              f"and it pulled no HRV, SpO2 or breathing rate at all.")


if __name__ == "__main__":  # pragma: no cover
    main()
