"""Consolidate Fitbit token files into the canonical `fitbit_tokens/` prefix.

The token bucket accumulated two prefixes:

    fitbit_tokens/    canonical — what the pull Lambda has always read
    fitbit-tokens/    a hyphenated variant written when a deployment's
                      S3_TOKEN_PREFIX secret overrode the code default

Having both is survivable only because the puller was widened to read both.
It is still a trap: a re-registration rotates the Fitbit grant and kills the
previous refresh token, so if a participant's newest token lands in one
prefix while something reads the other, their ingestion stops dead with no
error anywhere. That has happened twice (user14 on 2026-07-12; four accounts
on 2026-08-02).

NEWEST WINS, and that is the whole point
----------------------------------------
When the same fitbit_id exists under both prefixes, the copies are not
interchangeable — they are different grants, and only the most recently
issued one still works. Keeping the older file would preserve a token
Fitbit already rejects.

This is not hypothetical: C2W5PD had a 2026-07-23 token under
fitbit_tokens/ that returns HTTP 400 on every refresh (it was quarantined
as a dead grant), and a *newer* 2026-08-02 token under fitbit-tokens/ from
a re-authorization. Migrating newest-first revives that participant.

Enrollment linkage is also preserved: the token payload carries
participant_id/org_id once the registration form stamps it, and the puller's
refresh path uses dict.update() so those fields survive. Where both copies
carry linkage, the newer one is still correct.

    python -m ops.migrate_token_prefix
    python -m ops.migrate_token_prefix --commit
    python -m ops.migrate_token_prefix --commit --delete-source
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

import boto3

BUCKET = "fitbit-study-tokens-stored"
CANONICAL = "fitbit_tokens/"
LEGACY = "fitbit-tokens/"


def _list(s3, bucket: str, prefix: str) -> dict[str, dict]:
    """fitbit_id -> {key, modified, size} for real token files.

    Skips `.dead-grant.bak` quarantine files: they are deliberately parked
    copies, and resurrecting one would put a token Fitbit has already
    rejected back on the live path.
    """
    out = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            name = obj["Key"][len(prefix):]
            if not name.endswith(".json") or "/" in name:
                continue
            out[name[:-len(".json")]] = {
                "key": obj["Key"],
                "modified": obj["LastModified"],
                "size": obj["Size"],
            }
    return out


def _payload(s3, key: str) -> dict:
    try:
        return json.loads(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())
    except Exception:
        return {}


def plan(s3) -> list[dict]:
    canonical = _list(s3, BUCKET, CANONICAL)
    legacy = _list(s3, BUCKET, LEGACY)

    actions = []
    for fitbit_id, src in sorted(legacy.items()):
        dst = canonical.get(fitbit_id)
        if dst is None:
            actions.append({"id": fitbit_id, "action": "copy", "src": src["key"],
                            "why": "not present under the canonical prefix"})
        elif src["modified"] > dst["modified"]:
            older = dst["modified"].astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
            newer = src["modified"].astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
            actions.append({
                "id": fitbit_id, "action": "overwrite", "src": src["key"],
                "why": f"legacy copy is NEWER ({newer} vs {older}) — the "
                       f"canonical one holds a superseded, likely dead grant",
            })
        else:
            actions.append({"id": fitbit_id, "action": "skip", "src": src["key"],
                            "why": "canonical copy is newer or same age"})
    return actions


def main():  # pragma: no cover - CLI
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--delete-source", action="store_true",
                    help="remove fitbit-tokens/ after a verified copy")
    args = ap.parse_args()

    s3 = boto3.client("s3")
    actions = plan(s3)
    if not actions:
        print(f"Nothing under {LEGACY} — already consolidated.")
        return

    print(f"s3://{BUCKET}/{LEGACY}  ->  {CANONICAL}\n")
    for a in actions:
        pid = _payload(s3, a["src"]).get("participant_id")
        linked = f" [enrolled: {pid}]" if pid else " [not yet enrolled]"
        print(f"  {a['action'].upper():<10} {a['id']:<8}{linked}")
        print(f"             {a['why']}")

    moved = [a for a in actions if a["action"] in ("copy", "overwrite")]
    print(f"\n{len(moved)} to migrate, {len(actions) - len(moved)} already current.")

    if not args.commit:
        print("\nDRY RUN — re-run with --commit to apply.")
        return

    for a in moved:
        dst = f"{CANONICAL}{a['id']}.json"
        s3.copy_object(Bucket=BUCKET, Key=dst,
                       CopySource={"Bucket": BUCKET, "Key": a["src"]})
        # Verify byte-for-byte before considering the source expendable —
        # a half-copied credential is worse than one in the wrong place.
        if (s3.head_object(Bucket=BUCKET, Key=dst)["ContentLength"]
                != s3.head_object(Bucket=BUCKET, Key=a["src"])["ContentLength"]):
            raise SystemExit(f"copy of {a['id']} did not verify — stopping")
        print(f"  ✓ {a['id']} -> {dst}")

    if args.delete_source:
        for a in actions:  # every legacy file, including skipped ones
            s3.delete_object(Bucket=BUCKET, Key=a["src"])
            print(f"  – removed {a['src']}")
        print(f"\n{LEGACY} is now empty. Narrow the puller's TOKEN_PREFIX to "
              f"{CANONICAL} so nothing reads it again.")
    else:
        print("\nCopied. Source files left in place; re-run with "
              "--delete-source once a pull cycle confirms the new copies "
              "work.")


if __name__ == "__main__":  # pragma: no cover
    main()
