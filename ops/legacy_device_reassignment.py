"""Completes DEPLOY.md Section 4 / runbook.md's "Legacy migration" step,
option (b): a one-time batch of assign_device() calls for legacy-migrated
participants who have never re-registered through the OAuth form.

Why this exists: registration_service.migration only creates Participant
rows (see its own docstring/DEPLOY.md warning) — it deliberately does not
open DeviceAssignments windows, because the legacy users.json export
carries no per-device effective-dates. Until a window is opened,
`?cosinuss_id=` lookups (ingestion-resolver, offline re-attribution)
cannot resolve these participants at all.

Cosinuss ONLY. `?fitbit_id=` needs nothing from this script any more:
fitbit_id is now an attribute of the Participant record, written directly
by registration_service.migration from users.json, so it resolves as soon
as migration has run.

Effective-from: the day this participant's COSINUSS device first landed
raw data (earliest object under cosinuss/raw/<id>/ in the raw bucket) —
per the study owner's 2026-07-12 direction, that is the ground truth for
"when this person's monitored period began," and it applies to BOTH their
cosinuss and fitbit windows. Participants with no cosinuss data fall back
to the enrolled_at sentinel. The window's end is deliberately left open
(is_current): SCD Type 2 closes it automatically at the moment the device
is ever reassigned to someone else via assign_device — the profile itself
is untouched by that closure, and the person can re-register any time to
relink (matching on user_id/email).

Assumption that must hold (verified by construction — assign_device's
transaction re-checks at write time): each target device has NO prior
assignment history. If devices were rotated between legacy participants
before this system existed, this script is the wrong tool — that needs
the real historical device<->participant<->date mapping.

Idempotent and non-destructive by construction: only acts on participants
whose identity_source starts with "legacy_migrated" AND who have no
current assignment for that device; anyone already carrying a live
assignment (i.e. option (a), re-registered through the form, like user14
in this deployment) is left untouched. Dry-run by default.

Usage:
    python -m ops.legacy_device_reassignment ./legacy-users.json --org org1
    python -m ops.legacy_device_reassignment ./legacy-users.json --org org1 --commit
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from typing import Optional

import boto3

from assignment_ledger.queries import current_device_assignment
from assignment_ledger.writes import DeviceJustReassignedError, assign_device
from cbt_shared.tenancy import ScopedTable

# Filenames look like 2026-05-21_07-19-02_FKCWHM_temperature_... (some with
# a _UTC_ marker). The leading date is the capture date.
_COSINUSS_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})_")


def cosinuss_data_start(s3, raw_bucket: str, cosinuss_id: str) -> Optional[str]:
    """Earliest capture date with cosinuss raw data for this device, as an
    ISO instant at midnight UTC (day precision — starting the window a few
    hours before the first sample is harmless; ending it early would not be).
    """
    dates = []
    token = None
    while True:
        kwargs = {"Bucket": raw_bucket,
                  "Prefix": f"cosinuss/raw/{cosinuss_id}/"}
        if token:
            kwargs["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            m = _COSINUSS_DATE_RE.search(obj["Key"].rsplit("/", 1)[-1])
            if m:
                dates.append(m.group(1))
        token = resp.get("NextContinuationToken")
        if not token:
            break
    return f"{min(dates)}T00:00:00Z" if dates else None

LEGACY_PREFIX = "legacy_migrated"  # covers "legacy_migrated" and
                                    # "legacy_migrated_email_attached"


@dataclass
class Outcome:
    user_id: str
    device_type: str
    device_id: str
    action: str  # "assigned" | "skipped" | "conflict"
    detail: str


def _effective_from(s3, raw_bucket: str, person: dict, rec: dict) -> tuple[str, str]:
    """(effective_from, how) — cosinuss first-data date when available."""
    cid = rec.get("cosinuss_id")
    if cid:
        start = cosinuss_data_start(s3, raw_bucket, cid)
        if start:
            return start, f"cosinuss {cid} first data"
    return person["enrolled_at"], "enrolled_at fallback (no cosinuss data)"


def _plan_for_participant(participants: ScopedTable, devices: ScopedTable,
                          s3, raw_bucket: str,
                          user_id: str, rec: dict) -> list[Outcome]:
    rows = participants.query("user_id_pk", participants.scoped(user_id),
                              index_name="ByUserId")
    if not rows:
        return [Outcome(user_id, "-", "-", "skipped", "no Participant record (not migrated)")]
    person = rows[0]
    if not person.get("identity_source", "").startswith(LEGACY_PREFIX):
        return [Outcome(user_id, "-", "-", "skipped",
                        f"identity_source={person.get('identity_source')!r} — "
                        "already native/re-registered, leave alone")]

    outcomes = []
    enrollment_mode = person.get("enrollment_mode", "production")
    effective_from, how = _effective_from(s3, raw_bucket, person, rec)
    # Cosinuss only. Fitbit accounts are no longer assignment windows —
    # registration_service.migration binds fitbit_id straight onto the
    # Participant record (with a ByFitbitId index and a uniqueness marker),
    # so `?fitbit_id=` lookups work for legacy participants the moment
    # migration runs, with no backfill step at all.
    for field, device_type, role in (
        ("cosinuss_id", "cosinuss", "research"),  # matches registration_service.service
    ):
        device_id = rec.get(field)
        if not device_id:
            continue
        current = current_device_assignment(devices, device_type, device_id)
        if current is not None:
            if current["participant_id"] == person["participant_id"]:
                outcomes.append(Outcome(
                    user_id, device_type, device_id, "skipped",
                    "already assigned to this same participant (likely "
                    "already re-registered via option (a)) — nothing to do"))
            else:
                outcomes.append(Outcome(
                    user_id, device_type, device_id, "conflict",
                    f"device already assigned to a DIFFERENT participant_id="
                    f"{current['participant_id']!r} — resolve manually, not touched"))
            continue
        outcomes.append(Outcome(
            user_id, device_type, device_id, "assigned",
            f"-> participant_id={person['participant_id']} role={role} "
            f"effective_from={effective_from} ({how})"))
    return outcomes or [Outcome(user_id, "-", "-", "skipped", "no device ids in legacy record")]


def run(users_json_path: str, org: str, commit: bool,
        raw_bucket: str = "raw-data-all-sensors-782329476642-us-east-1-an") -> int:
    with open(users_json_path) as f:
        users = json.load(f)

    dynamodb = boto3.resource("dynamodb")
    client = boto3.client("dynamodb")
    s3 = boto3.client("s3")
    participants = ScopedTable(dynamodb.Table("Participants"), org)
    devices = ScopedTable(dynamodb.Table("DeviceAssignments"), org)

    plan: list[Outcome] = []
    for user_id, rec in users.items():
        if user_id == "_meta":
            continue
        plan.extend(_plan_for_participant(participants, devices, s3, raw_bucket,
                                          user_id, rec))

    print(f"{'MODE: COMMIT' if commit else 'MODE: DRY RUN (add --commit to write)'}")
    print(f"{'user_id':<10} {'device':<10} {'id':<10} {'action':<10} detail")
    for o in plan:
        print(f"{o.user_id:<10} {o.device_type:<10} {o.device_id:<10} {o.action:<10} {o.detail}")

    to_write = [o for o in plan if o.action == "assigned"]
    print(f"\n{len(to_write)} device windows to open, "
         f"{sum(1 for o in plan if o.action == 'conflict')} conflicts, "
         f"{sum(1 for o in plan if o.action == 'skipped')} skipped.")

    if not commit or not to_write:
        return 1 if any(o.action == "conflict" for o in plan) else 0

    rows_by_user = {u: r for u, r in users.items() if u != "_meta"}
    failures = 0
    for o in to_write:
        rec = rows_by_user[o.user_id]
        rows = participants.query("user_id_pk", participants.scoped(o.user_id),
                                  index_name="ByUserId")
        person = rows[0]
        role = person["enrollment_mode"] if o.device_type == "fitbit" else "research"
        effective_from, _ = _effective_from(s3, raw_bucket, person, rec)
        try:
            assign_device(
                client, devices, device_type=o.device_type, device_id=o.device_id,
                participant_id=person["participant_id"], role=role,
                effective_from=effective_from, expected_current=None,
            )
            print(f"  wrote {o.device_type}/{o.device_id} -> {person['participant_id']}")
        except DeviceJustReassignedError as e:
            failures += 1
            print(f"  FAILED {o.device_type}/{o.device_id}: {e} "
                 "(state changed since the plan was built — re-run to re-check)")
    return 1 if failures else 0


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    ap = argparse.ArgumentParser()
    ap.add_argument("users_json_path")
    ap.add_argument("--org", required=True)
    ap.add_argument("--commit", action="store_true",
                    help="actually write; default is dry-run/plan-only")
    ap.add_argument("--raw-bucket",
                    default="raw-data-all-sensors-782329476642-us-east-1-an")
    args = ap.parse_args()
    sys.exit(run(args.users_json_path, args.org, args.commit, args.raw_bucket))
