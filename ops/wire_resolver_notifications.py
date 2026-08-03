"""Route new raw-bucket objects to the ingestion resolver, via EventBridge.

Why not an S3 bucket notification
---------------------------------
S3 forbids two notification rules whose prefixes overlap for the same
event type — even when they target different functions:

    Configuration is ambiguously defined. Cannot have overlapping suffixes
    in two rules if the prefixes are overlapping for the same event type.

`fitbit/raw/` and `clarity/raw` already route to heat-stress-predict, so
resolver rules on those prefixes are rejected outright. The alternatives
were to fan out through SNS or chain off heat-stress-predict, both of which
restructure the live prediction path in order to add a read-only check.

EventBridge has no such restriction. Turning it on for the bucket is purely
additive — `EventBridgeConfiguration` sits alongside the existing
`LambdaFunctionConfigurations`, which keep working untouched — and
EventBridge rules may overlap freely. It also supports a retry policy and a
DLQ, which raw S3 notifications do not.

What this creates
-----------------
  1. `EventBridgeConfiguration` on the bucket (idempotent; preserves every
     existing notification, and refuses to run if it would drop one).
  2. An EventBridge rule matching Object Created under the three sensor
     prefixes, targeting the resolver.
  3. The `lambda:InvokeFunction` permission EventBridge needs.

    python -m ops.wire_resolver_notifications --bucket <raw-bucket>
    python -m ops.wire_resolver_notifications --bucket <raw-bucket> --commit
    python -m ops.wire_resolver_notifications --bucket <raw-bucket> --remove --commit
"""

from __future__ import annotations

import argparse
import json

import boto3
from botocore.exceptions import ClientError

RULE_NAME = "cbt-ingestion-resolver-raw-objects"
STATEMENT_ID = "cbt-eventbridge-invoke"

# Only the three sensor prefixes. A bucket-wide rule would also fire on the
# resolver's own unattributed/ quarantine writes (an invocation loop), on
# the pullers' state checkpoints, and on the generated config/ artifacts.
# The handler filters those defensively too, but not paying for the
# invocation is better than filtering it.
WATCHED_PREFIXES = ["fitbit/raw/", "cosinuss/raw/", "clarity/raw/"]


def event_pattern(bucket: str) -> dict:
    return {
        "source": ["aws.s3"],
        "detail-type": ["Object Created"],
        "detail": {
            "bucket": {"name": [bucket]},
            "object": {"key": [{"prefix": p} for p in WATCHED_PREFIXES]},
        },
    }


def describe_notifications(config: dict) -> str:
    lines = []
    for kind, arn_key in (("LambdaFunctionConfigurations", "LambdaFunctionArn"),
                          ("QueueConfigurations", "QueueArn"),
                          ("TopicConfigurations", "TopicArn")):
        for c in config.get(kind, []):
            rules = c.get("Filter", {}).get("Key", {}).get("FilterRules", [])
            filt = " ".join(f"{r['Name']}={r['Value']}" for r in rules) or "(all)"
            lines.append(f"    {c.get('Id'):<40} {c[arn_key].split(':')[-1]:<28} {filt}")
    if "EventBridgeConfiguration" in config:
        lines.append(f"    {'(EventBridge)':<40} {'-> EventBridge bus':<28} (all)")
    return "\n".join(lines) or "    (none)"


def _ids(config: dict) -> set[str]:
    return {c["Id"] for k in ("LambdaFunctionConfigurations",
                              "QueueConfigurations", "TopicConfigurations")
            for c in config.get(k, [])}


def main():  # pragma: no cover - CLI
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--function", default="ingestion-resolver")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    s3 = boto3.client("s3", region_name=args.region)
    events = boto3.client("events", region_name=args.region)
    lam = boto3.client("lambda", region_name=args.region)
    account = boto3.client("sts").get_caller_identity()["Account"]
    fn_arn = f"arn:aws:lambda:{args.region}:{account}:function:{args.function}"
    rule_arn = f"arn:aws:events:{args.region}:{account}:rule/{RULE_NAME}"

    existing = s3.get_bucket_notification_configuration(Bucket=args.bucket)
    existing.pop("ResponseMetadata", None)
    updated = {k: v for k, v in existing.items()
               if k in ("TopicConfigurations", "QueueConfigurations",
                        "LambdaFunctionConfigurations", "EventBridgeConfiguration")}
    if args.remove:
        updated.pop("EventBridgeConfiguration", None)
    else:
        updated["EventBridgeConfiguration"] = {}

    print("Bucket notifications BEFORE:")
    print(describe_notifications(existing))
    print("\nBucket notifications AFTER:")
    print(describe_notifications(updated))

    # Guard: never write a config that drops an existing rule. Losing
    # fitbit-new-data or clarity-new-data would stop predictions with no
    # error anywhere.
    lost = _ids(existing) - _ids(updated)
    if lost:
        raise SystemExit(f"\nREFUSING: this would delete existing rule(s) {sorted(lost)}")

    print(f"\nEventBridge rule: {RULE_NAME}")
    print(f"  prefixes: {', '.join(WATCHED_PREFIXES)}")
    print(f"  target:   {args.function}")
    if args.remove:
        print("  action:   REMOVE")

    if not args.commit:
        print("\nDRY RUN — re-run with --commit to apply.")
        return

    if args.remove:
        try:
            events.remove_targets(Rule=RULE_NAME, Ids=["resolver"])
            events.delete_rule(Name=RULE_NAME)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
        try:
            lam.remove_permission(FunctionName=args.function,
                                  StatementId=STATEMENT_ID)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
        # EventBridge delivery for the bucket is left on unless nothing else
        # uses it; turning it off is the caller's call via --remove.
        s3.put_bucket_notification_configuration(
            Bucket=args.bucket, NotificationConfiguration=updated)
        print("\nRemoved.")
        return

    # 1. bucket -> EventBridge
    s3.put_bucket_notification_configuration(
        Bucket=args.bucket, NotificationConfiguration=updated)
    print("\n  ✓ bucket EventBridge delivery enabled")

    # 2. the rule
    events.put_rule(Name=RULE_NAME, State="ENABLED",
                    Description="Raw sensor objects -> ingestion-resolver "
                                "(attribution check only; never modifies data)",
                    EventPattern=json.dumps(event_pattern(args.bucket)))
    print(f"  ✓ rule {RULE_NAME}")

    # 3. permission BEFORE the target, so the first matching event is not
    #    dropped for AccessDenied — the exact silent-failure mode that left
    #    the cosinuss puller invoking twice a day.
    try:
        lam.add_permission(FunctionName=args.function,
                           StatementId=STATEMENT_ID,
                           Action="lambda:InvokeFunction",
                           Principal="events.amazonaws.com",
                           SourceArn=rule_arn)
        print("  ✓ lambda invoke permission")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceConflictException":
            raise
        print("  ✓ lambda invoke permission (already present)")

    # 4. the target
    events.put_targets(Rule=RULE_NAME,
                       Targets=[{"Id": "resolver", "Arn": fn_arn}])
    print("  ✓ target -> " + args.function)
    print("\nApplied.")


if __name__ == "__main__":  # pragma: no cover
    main()
