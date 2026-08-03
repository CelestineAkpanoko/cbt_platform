"""Change an EventBridge Scheduler schedule's rate without disturbing its target.

`aws scheduler update-schedule` is a full replace: omit `--target` and the
call fails, pass a partially-reconstructed one and you silently drop the
target's role, retry policy or input. Both failure modes are quiet — the
schedule keeps existing and simply stops invoking anything, which is
exactly the class of problem that left the cosinuss puller running twice a
day on a rate(1 minute) schedule.

So: read the schedule, change only the expression, write it back.

    python -m ops.set_schedule fitbit-collector-every-1-minute "rate(2 minutes)"
    python -m ops.set_schedule fitbit-collector-every-1-minute "rate(2 minutes)" --commit
"""

from __future__ import annotations

import argparse

import boto3

# Fields update_schedule accepts back, in the shape it wants them.
_PASSTHROUGH = (
    "Name", "GroupName", "ScheduleExpression", "ScheduleExpressionTimezone",
    "StartDate", "EndDate", "Description", "State", "KmsKeyArn",
    "FlexibleTimeWindow", "Target",
)


def main():  # pragma: no cover - CLI
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("expression", help='e.g. "rate(2 minutes)"')
    ap.add_argument("--group", default="default")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    client = boto3.client("scheduler", region_name=args.region)
    current = client.get_schedule(Name=args.name, GroupName=args.group)
    current.pop("ResponseMetadata", None)

    print(f"{args.name}: {current['ScheduleExpression']} -> {args.expression}")
    print(f"  target: {current['Target']['Arn'].split(':')[-1]}")
    print(f"  state:  {current.get('State')}")
    if current["ScheduleExpression"] == args.expression:
        print("  already set — nothing to do.")
        return
    if not args.commit:
        print("\nDRY RUN — re-run with --commit to apply.")
        return

    payload = {k: current[k] for k in _PASSTHROUGH if k in current}
    payload["ScheduleExpression"] = args.expression
    client.update_schedule(**payload)
    print("  applied.")


if __name__ == "__main__":  # pragma: no cover
    main()
