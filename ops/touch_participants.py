"""Force a first materialization after the materializer is deployed.

DynamoDB Streams only deliver records for writes that happen *after* the
event-source mapping is created. So immediately after deploying the
materializer, `users.json` is whatever was last there by hand and
`config/user_mapping.json` and `config/assignments.json` do not exist at
all — and nothing will create them until somebody happens to enroll.

That gap is the dangerous kind: the puller reads `user_mapping.json` to
decide who to pull, and its documented fallback for a missing file is
"pull everyone", so the system looks fine while quietly running on the
wrong roster.

This writes a `materialized_at` timestamp to every participant, which is a
real (if trivial) modification, so the stream fires once per participant
and the materializer rebuilds all three artifacts.

    python -m ops.touch_participants --org org1
    python -m ops.touch_participants --org org1 --dry-run
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

import boto3

from assignment_ledger.queries import all_participants
from cbt_shared.tenancy import ScopedTable


def touch(participants: ScopedTable, dry_run: bool = False) -> list[str]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    touched = []
    people = all_participants(participants)
    if not people:
        raise SystemExit(
            "No participants found on the ByOrg index. Either the index is "
            "still backfilling, or ops/migrate_fitbit_to_participants has "
            "not been run — without org_pk a participant is invisible here "
            "AND to the materializer. Run step 2 first."
        )
    for person in people:
        if not dry_run:
            participants.update_item(
                key={"pk": person["pk"]},
                UpdateExpression="SET materialized_at = :t",
                ExpressionAttributeValues={":t": now},
            )
        touched.append(person["user_id"])
    return touched


def main():  # pragma: no cover - CLI
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    table = ScopedTable(boto3.resource("dynamodb").Table("Participants"), args.org)
    names = touch(table, args.dry_run)
    verb = "would touch" if args.dry_run else "touched"
    print(f"{verb} {len(names)} participant(s): {', '.join(sorted(names))}")
    if not args.dry_run:
        print("Materializer should rebuild within ~30s. Verify with ops/verify.sh")


if __name__ == "__main__":  # pragma: no cover
    main()
