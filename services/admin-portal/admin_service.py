"""Admin-portal business logic (unit-testable; no Streamlit imports).

The one non-trivial operation is delete_participant(): remove a person and
every DynamoDB row that references them, so their identity (user_id,
email, fitbit_id) becomes claimable again by a fresh registration. S3 is
NEVER touched — raw sensor data and predictions are append-only and stay
where they are.
"""

from __future__ import annotations

from boto3.dynamodb.conditions import Attr

from assignment_ledger.queries import (
    all_participants,
    assignments_for_participant,
)
from assignment_ledger.writes import HEAD_SK
from cbt_shared.tenancy import ScopedTable


class ParticipantNotFoundError(KeyError):
    pass


def list_participants(participants: ScopedTable) -> list[dict]:
    """Person rows for one org (ByOrg GSI — markers excluded)."""
    return sorted(all_participants(participants),
                  key=lambda p: p.get("user_id") or p["participant_id"])


def delete_participant(
    participants: ScopedTable,
    devices: ScopedTable,
    sites: ScopedTable,
    calibration: ScopedTable,
    participant_id: str,
) -> dict:
    """Delete one participant's rows across all four tables.

    Returns per-category counts. Idempotent-ish: re-running on a deleted
    participant raises ParticipantNotFoundError.
    """
    org = participants.org_id
    person = participants.get_item("pk", participants.scoped(participant_id))
    if not person:
        raise ParticipantNotFoundError(participant_id)

    counts = {"participant": 0, "markers": 0, "device_assignments": 0,
              "device_heads": 0, "site_assignments": 0, "calibration": 0}

    # --- uniqueness markers -------------------------------------------------
    # A scan filtered to this org's marker prefix catches every marker that
    # points at this participant — including retired fitbit markers whose
    # value no longer appears on the person row.
    marker_prefix = f"{org}#uniq#"
    scan_kwargs = {
        "FilterExpression": Attr("pk").begins_with(marker_prefix)
        & Attr("participant_id").eq(participant_id),
        "ProjectionExpression": "pk",
    }
    raw_participants = participants.raw
    while True:
        resp = raw_participants.scan(**scan_kwargs)
        for item in resp.get("Items", []):
            raw_participants.delete_item(Key={"pk": item["pk"]})
            counts["markers"] += 1
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        scan_kwargs["ExclusiveStartKey"] = lek

    # --- device assignment windows (+ HEAD mutex rows) ----------------------
    raw_devices = devices.raw
    deleted_windows: dict[str, set] = {}
    for row in assignments_for_participant(devices, participant_id):
        raw_devices.delete_item(Key={"pk": row["pk"], "sk": row["sk"]})
        counts["device_assignments"] += 1
        deleted_windows.setdefault(row["pk"], set()).add(row["effective_from"])
    for pk, froms in deleted_windows.items():
        # The HEAD mutex row records current_from of the open window. Only
        # remove it when it points at one of this participant's (now
        # deleted) windows — a receiver since reassigned to someone else
        # keeps its HEAD, so their open window stays protected.
        head = raw_devices.get_item(Key={"pk": pk, "sk": HEAD_SK}).get("Item")
        if head and head.get("current_from") in froms:
            raw_devices.delete_item(Key={"pk": pk, "sk": HEAD_SK})
            counts["device_heads"] += 1

    # --- site assignments (all rows under the participant's pk, incl. HEAD) -
    raw_sites = sites.raw
    site_pk = sites.scoped("participant", participant_id)
    for row in sites.query("pk", site_pk):
        raw_sites.delete_item(Key={"pk": row["pk"], "sk": row["sk"]})
        counts["site_assignments"] += 1

    # --- calibration history ------------------------------------------------
    raw_cal = calibration.raw
    cal_pk = calibration.scoped(participant_id)
    for row in calibration.query("pk", cal_pk):
        raw_cal.delete_item(Key={"pk": row["pk"],
                                 "computed_at": row["computed_at"]})
        counts["calibration"] += 1

    # --- the person row last, so a partial failure stays discoverable -------
    participants.delete_item("pk", participants.scoped(participant_id))
    counts["participant"] = 1
    return counts
