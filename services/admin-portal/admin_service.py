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
    current_site_assignment,
)
from assignment_ledger.writes import (
    HEAD_SK,
    DeviceJustReassignedError,
    release_device,
    release_site,
)
from cbt_shared.tenancy import ScopedTable

# The table of Lambdas that must know the full tenant list. Lives in
# cbt_shared (deployed with the git checkout) — NOT in ops/, which is
# operator-only tooling that Streamlit Cloud does not have.
from cbt_shared.org_functions import ORG_AWARE_FUNCTIONS, current_orgs


class ParticipantNotFoundError(KeyError):
    pass


def org_rollout_status(lambda_client, org_id: str) -> list[dict]:
    """Which Lambdas already know this org. One row per function:
    {function, status: known|missing|not_deployed, orgs}."""
    rows = []
    for fn, orgs in current_orgs(lambda_client).items():
        if orgs is None:
            status = "not_deployed"
        elif org_id in orgs:
            status = "known"
        else:
            status = "missing"
        rows.append({"function": fn, "status": status, "orgs": orgs or []})
    return rows


def sync_org_to_lambdas(lambda_client, org_id: str) -> list[dict]:
    """Append org_id to every org-aware Lambda's tenant list (the same
    merge `python -m ops.add_org --org <id> --commit` performs, so admins
    never need a terminal). Idempotent; never removes an org. One row per
    function: {function, status: added|already|not_deployed|error, detail}.
    """
    results = []
    for fn, var in ORG_AWARE_FUNCTIONS.items():
        try:
            env = lambda_client.get_function_configuration(
                FunctionName=fn).get("Environment", {}).get("Variables", {})
        except Exception as exc:
            # boto3 raises ClientError whose str() carries the error code.
            if "ResourceNotFound" in f"{type(exc).__name__} {exc}":
                results.append({"function": fn, "status": "not_deployed",
                                "detail": ""})
            else:
                results.append({"function": fn, "status": "error",
                                "detail": str(exc)})
            continue
        raw = env.get(var) or env.get("CBT_ORG_ID") or ""
        orgs = [o.strip() for o in raw.split(",") if o.strip()]
        if org_id in orgs:
            results.append({"function": fn, "status": "already",
                            "detail": ",".join(orgs)})
            continue
        env[var] = ",".join(orgs + [org_id])
        try:
            lambda_client.update_function_configuration(
                FunctionName=fn, Environment={"Variables": env})
            lambda_client.get_waiter("function_updated").wait(FunctionName=fn)
            results.append({"function": fn, "status": "added",
                            "detail": env[var]})
        except Exception as exc:
            results.append({"function": fn, "status": "error",
                            "detail": str(exc)})
    return results


def list_participants(participants: ScopedTable) -> list[dict]:
    """Person rows for one org (ByOrg GSI — markers excluded)."""
    return sorted(all_participants(participants),
                  key=lambda p: p.get("user_id") or p["participant_id"])


CALIBRATION_SWEEP_FUNCTION = "calibration-sweep"


def calibration_overview(participants: ScopedTable,
                         calibration: ScopedTable) -> list[dict]:
    """Per-participant calibration state for the admin portal.

    The sweep Lambda itself is the look-back mechanism — it re-evaluates
    every participant from their enrolled_at until a baseline completes —
    so this is the matching visibility: who is calibrated, who is still
    accumulating nights, and who can never calibrate (no Fitbit or no
    usable enrollment date).
    """
    rows = []
    for person in list_participants(participants):
        pid = person["participant_id"]
        entry = {
            "user_id": person.get("user_id"),
            "participant_id": pid,
            "enrolled_at": (person.get("enrolled_at") or "")[:10],
            "status": "no fitbit",
            "nights": None,
            "last_checked": None,
        }
        if person.get("fitbit_id"):
            history = calibration.query("pk", calibration.scoped(pid))
            window = [h for h in history
                      if h.get("assignment_effective_from")
                      == person.get("enrolled_at")]
            window.sort(key=lambda h: h.get("computed_at") or "")
            if not window:
                entry["status"] = "waiting for first sweep"
            else:
                latest = window[-1]
                entry["status"] = latest.get("calibration_status", "?")
                entry["nights"] = latest.get("nights_used")
                entry["last_checked"] = (latest.get("computed_at") or "")[:16]
        rows.append(entry)
    return rows


def trigger_calibration_sweep(lambda_client) -> dict:
    """Fire the sweep Lambda now instead of waiting for its schedule.
    Async invoke — results land in CalibrationHistory within minutes."""
    resp = lambda_client.invoke(FunctionName=CALIBRATION_SWEEP_FUNCTION,
                                InvocationType="Event")
    return {"status_code": resp.get("StatusCode")}


def _current_device_rows(devices: ScopedTable, participant_id: str) -> list[dict]:
    return [row for row in assignments_for_participant(devices, participant_id)
            if "is_current" in row]


def current_assignments(devices: ScopedTable, sites: ScopedTable,
                        participant_id: str) -> dict:
    """What this participant currently holds — the admin portal shows this
    before unassigning them, so "unassign" never surprises anyone with what
    it's about to close. Same rows unassign_participant() would act on.

    Returns {"devices": [{"device_type", "device_id"}, ...], "site": str|None}.
    """
    site_row = current_site_assignment(sites, "participant", participant_id)
    return {
        "devices": [{"device_type": r["device_type"], "device_id": r["device_id"]}
                    for r in _current_device_rows(devices, participant_id)],
        "site": site_row["site_id"] if site_row else None,
    }


def unassign_participant(
    dynamo_client,
    participants: ScopedTable,
    devices: ScopedTable,
    sites: ScopedTable,
    participant_id: str,
    effective_to: str,
) -> dict:
    """End a participant's study coverage without deleting their profile.

    The "no longer in the study" operation: closes whatever exclusive-wear
    device window (cosinuss) and Clarity site window are currently open for
    this participant, at `effective_to`. The device goes back into
    unassigned_devices()'s free pool for the next participant to pick up
    immediately — the departing participant's profile, calibration
    history, and identity markers are all left intact, so they can be
    re-linked later if they return to the study (unlike delete_participant,
    which erases the whole person and frees their identity for reuse).

    Idempotent: a participant with nothing currently open returns empty
    results rather than erroring. Raises DeviceJustReassignedError (from
    assignment_ledger) if the device/site changed between the portal
    rendering its state and this call — same "refresh and retry" contract
    as assign_device.

    Returns {"devices": [{"device_type", "device_id"}, ...], "site": str|None}
    describing what was actually closed.
    """
    if not participants.get_item("pk", participants.scoped(participant_id)):
        raise ParticipantNotFoundError(participant_id)

    released_devices = []
    for row in _current_device_rows(devices, participant_id):
        release_device(
            dynamo_client, devices, device_type=row["device_type"],
            device_id=row["device_id"], effective_to=effective_to,
            expected_current=row,
        )
        released_devices.append({"device_type": row["device_type"],
                                 "device_id": row["device_id"]})

    released_site = None
    current_site = current_site_assignment(sites, "participant", participant_id)
    if current_site is not None:
        release_site(
            dynamo_client, sites, entity_kind="participant",
            entity_id=participant_id, effective_to=effective_to,
            expected_current=current_site,
        )
        released_site = current_site["site_id"]

    return {"devices": released_devices, "site": released_site}


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
