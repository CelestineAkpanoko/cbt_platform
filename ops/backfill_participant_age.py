"""One-time backfill: set Participants.age from legacy users.json
demographics for records created before the age field existed
(2026-07-12). New enrollments capture age in the form; this covers
everyone enrolled/migrated before that, including natively re-registered
legacy users (e.g. user14) whose form pre-dated the field.

Only ever fills a MISSING age — never overwrites an existing value (a
value captured on the form is fresher than the legacy export). Dry-run by
default.

Usage:
    python -m ops.backfill_participant_age ./legacy-users.json --org org1 [--commit]
"""

from __future__ import annotations

import argparse
import json
import sys

import boto3

from cbt_shared.tenancy import ScopedTable


def run(users_json_path: str, org: str, commit: bool) -> int:
    with open(users_json_path) as f:
        users = json.load(f)

    participants = ScopedTable(
        boto3.resource("dynamodb").Table("Participants"), org)

    print("MODE:", "COMMIT" if commit else "DRY RUN (add --commit to write)")
    changed = 0
    for user_id, rec in users.items():
        if user_id == "_meta":
            continue
        age = (rec.get("demographics") or {}).get("age")
        rows = participants.query("user_id_pk", participants.scoped(user_id),
                                  index_name="ByUserId")
        if not rows:
            print(f"{user_id:<10} skipped — no Participant record")
            continue
        person = rows[0]
        if person.get("age") is not None:
            print(f"{user_id:<10} skipped — age already set ({person['age']})")
            continue
        if age is None:
            print(f"{user_id:<10} skipped — legacy record has no age either")
            continue
        print(f"{user_id:<10} age <- {age} (participant {person['participant_id']})")
        changed += 1
        if commit:
            participants.update_item(
                key={"pk": person["pk"]},
                UpdateExpression="SET age = :a",
                ConditionExpression="attribute_not_exists(age)",
                ExpressionAttributeValues={":a": int(age)},
            )
    print(f"\n{changed} record(s) {'updated' if commit else 'would be updated'}.")
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    ap = argparse.ArgumentParser()
    ap.add_argument("users_json_path")
    ap.add_argument("--org", required=True)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    sys.exit(run(args.users_json_path, args.org, args.commit))
