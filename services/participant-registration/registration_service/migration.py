"""One-time legacy participant migration (Section 3b).

Seeds the new Participants table from the current users.json (the
users-heat-stress config bucket). Every pre-existing "user{number}"
participant is written with identity_source="legacy_migrated" and, unless a
roster supplies one, email=None (never a synthetic placeholder). Matching
for these people works on user_id alone until their next registration
attaches a real email (handled by registration_service.service._relink_existing,
which flips the flag to "legacy_migrated_email_attached").

Run once, before general rollout:
    python -m registration_service.migration users.json --org org1
"""

from __future__ import annotations

import json
from typing import Optional
import uuid

from cbt_shared.models import Participant
from cbt_shared.tenancy import ScopedTable


def migrate_legacy_users(participants: ScopedTable, users_json: dict,
                         email_roster: Optional[dict] = None) -> list[str]:
    """Import legacy users.json entries as flagged participants.

    users_json shape (schema_version 2, the real production export):
        {
          "_meta": {...},           # not a participant, skipped
          "user15": {
            "user_id": "user15", "display_name": "user15",
            "fitbit_id": "D58MBD", "cosinuss_id": "FKCWHM",
            "demographics": null | {"age", "sex", "height_in",
                                    "weight_lbs", "bmi", "race": [...]},
            "baselines": null | {"resting_hr", "baseline_skin", ...},
          },
          ...
        }
    Keyed by user_id (not fitbit_id); demographics/baselines are nested and
    may be null (survey/calibration not yet done at export time — the
    inference Lambda already falls back to population means in that case,
    per the file's own _meta note). email_roster (optional) maps
    user_id -> email from a source outside this system.

    Idempotent: an already-migrated (or natively registered) user_id is
    skipped, never overwritten.
    """
    email_roster = email_roster or {}
    migrated = []
    for user_id, rec in users_json.items():
        if user_id == "_meta":
            continue
        existing = participants.query(
            "user_id_pk", participants.scoped(user_id), index_name="ByUserId"
        )
        if existing:
            continue

        demographics = rec.get("demographics") or {}
        race = demographics.get("race") or ["unknown"]
        if isinstance(race, list):
            race = ", ".join(race)

        baselines = rec.get("baselines") or {}
        current_baseline = None
        if baselines.get("resting_hr") is not None:
            current_baseline = {
                "resting_hr": baselines["resting_hr"],
                "baseline_skin_temp": baselines.get("baseline_skin"),
            }

        email = email_roster.get(user_id)
        # users.json already records each participant's Fitbit account id;
        # it is the same value the OAuth exchange returns and the same one
        # raw data lands under, so it binds directly onto the participant.
        # No DeviceAssignment window is (or ever was) needed for it.
        fitbit_id = rec.get("fitbit_id") or None
        p = Participant(
            org_id=participants.org_id,
            participant_id=f"p-{uuid.uuid4().hex[:12]}",
            user_id=user_id,
            email=email.lower() if email else None,
            fitbit_id=fitbit_id,
            display_name=rec.get("display_name", user_id),
            sex=demographics.get("sex", "unknown"),
            height_in=demographics.get("height_in", 0),
            weight_lbs=demographics.get("weight_lbs", 0),
            bmi=demographics.get("bmi", 0),
            race=race,
            enrollment_mode="research" if rec.get("cosinuss_id") else "production",
            consent_status="consented",
            enrolled_at="1970-01-01T00:00:00Z",
            identity_source="legacy_migrated",
            age=demographics.get("age"),
            current_baseline=current_baseline,
        )
        item = p.to_item()
        item["user_id_pk"] = participants.scoped(user_id)
        if email:
            item["email_pk"] = participants.scoped(email.lower())
        if fitbit_id:
            item["fitbit_id_pk"] = participants.scoped(fitbit_id)
        # Kept for the runbook's cosinuss-reassignment step (Section 3b):
        # the last known receiver this user had, before any effective-dated
        # DeviceAssignment window exists for them. There is no
        # legacy_fitbit_id equivalent — fitbit_id is a live field now, not
        # something needing backfill into a separate ledger.
        if rec.get("cosinuss_id"):
            item["legacy_cosinuss_id"] = rec["cosinuss_id"]
        participants.put_item(item)
        # claim the uniqueness markers (same guards native creates use)
        for kind, value in (("user_id", user_id), ("email", email and email.lower()),
                            ("fitbit_id", fitbit_id)):
            if value:
                participants.put_item({
                    "pk": participants.scoped("uniq", kind, value),
                    "participant_id": p.participant_id,
                })
        migrated.append(user_id)
    return migrated


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    import argparse

    import boto3

    ap = argparse.ArgumentParser()
    ap.add_argument("users_json_path")
    ap.add_argument("--org", required=True)
    ap.add_argument("--roster", help="optional JSON file: user_id -> email")
    args = ap.parse_args()

    with open(args.users_json_path) as f:
        users = json.load(f)
    roster = None
    if args.roster:
        with open(args.roster) as f:
            roster = json.load(f)

    scoped = ScopedTable(boto3.resource("dynamodb").Table("Participants"), args.org)
    done = migrate_legacy_users(scoped, users, roster)
    print(f"migrated {len(done)} legacy participants: {done}")
