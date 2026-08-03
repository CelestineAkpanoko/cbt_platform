"""One-time backfill for the fitbit-out-of-the-ledger change.

Existing Participants rows predate three attributes this refactor
introduced, and until they are backfilled the affected participants are
invisible to the new code paths:

  fitbit_id / fitbit_id_pk   the Fitbit account binding, moved off
                             DeviceAssignments. Without it, `?fitbit_id=`
                             lookups miss, the ingestion resolver
                             quarantines every Fitbit file, and the
                             calibration sweep skips the participant.
  org_pk                     hash key of the new ByOrg GSI. Without it a
                             participant is absent from the sparse index,
                             so the materializer omits them from
                             users.json and user_mapping.json entirely —
                             the most damaging of the three, because it
                             fails silently.
  uniq#fitbit_id markers     the permanent claim that stops someone else
                             enrolling on that Fitbit account.

Where fitbit_id comes from: the open `fitbit` window in DeviceAssignments
(the ledger's own answer for "who has this account right now"), falling
back to the `legacy_fitbit_id` attribute that
registration_service.migration used to stash.

The old fitbit DeviceAssignments rows are NOT deleted — they are dead
weight but they are also the only record of what the ledger believed, and
nothing reads them any more (queries.py refuses device_type="fitbit"). Use
--purge-fitbit-rows once you have verified the backfill.

Dry-run by default:
    python -m ops.migrate_fitbit_to_participants --org org1
    python -m ops.migrate_fitbit_to_participants --org org1 --commit
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Key

from cbt_shared.tenancy import ScopedTable


@dataclass
class Outcome:
    participant_id: str
    user_id: str
    action: str
    detail: str


def _fitbit_windows(devices: ScopedTable) -> dict[str, str]:
    """participant_id -> fitbit_id, from the pre-refactor ledger rows.

    Read straight off the Current GSI rather than through
    all_current_device_assignments(), which now filters to the ledger's
    supported types. These rows are exactly the legacy shape this script
    exists to drain.
    """
    rows = devices.query("is_current", devices.org_id, index_name="Current")
    return {
        row["participant_id"]: row["device_id"]
        for row in rows if row.get("device_type") == "fitbit"
    }


def _participant_rows(participants: ScopedTable) -> list[dict]:
    """Every person row, including ones missing org_pk.

    Cannot use all_participants(): that reads the ByOrg GSI, and the rows
    needing a backfill are precisely the ones absent from it. This is the
    one place a Scan is justified — it is a one-shot migration over a
    table of tens of items, and the org filter is applied explicitly.
    """
    out = []
    kwargs = {}
    while True:
        resp = participants.raw.scan(**kwargs)
        for item in resp.get("Items", []):
            pk = item.get("pk", "")
            if not pk.startswith(f"{participants.org_id}#"):
                continue
            # Uniqueness markers share this table and DO carry a
            # participant_id (that is how a re-claim by the same person is
            # recognised), so presence of that field cannot distinguish
            # them. The pk shape can: markers are org#uniq#<kind>#<value>.
            if pk.startswith(f"{participants.org_id}#uniq#"):
                continue
            if "user_id" not in item:
                continue
            out.append(item)
        if "LastEvaluatedKey" not in resp:
            return out
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def migrate(participants: ScopedTable, devices: ScopedTable,
            commit: bool) -> list[Outcome]:
    from_ledger = _fitbit_windows(devices)
    outcomes = []

    for person in _participant_rows(participants):
        pid = person["participant_id"]
        user_id = person.get("user_id", "?")
        fitbit_id = (person.get("fitbit_id")
                     or from_ledger.get(pid)
                     or person.get("legacy_fitbit_id"))

        updates = {}
        if "org_pk" not in person:
            updates["org_pk"] = f"{participants.org_id}#participant"
        if fitbit_id and person.get("fitbit_id") != fitbit_id:
            updates["fitbit_id"] = fitbit_id
        if fitbit_id and "fitbit_id_pk" not in person:
            updates["fitbit_id_pk"] = participants.scoped(fitbit_id)

        if not updates:
            outcomes.append(Outcome(pid, user_id, "skipped", "already current"))
            continue

        source = ("participant record" if person.get("fitbit_id")
                  else "fitbit assignment window" if pid in from_ledger
                  else "legacy_fitbit_id" if person.get("legacy_fitbit_id")
                  else "no Fitbit on file")
        detail = f"{', '.join(sorted(updates))} (fitbit from: {source})"

        if commit:
            expr = "SET " + ", ".join(f"#a{i} = :v{i}"
                                      for i in range(len(updates)))
            participants.update_item(
                key={"pk": person["pk"]},
                UpdateExpression=expr,
                ExpressionAttributeNames={f"#a{i}": k
                                          for i, k in enumerate(updates)},
                ExpressionAttributeValues={f":v{i}": v for i, v
                                           in enumerate(updates.values())},
            )
            if fitbit_id:
                # Permanent claim on the account. Conditioned so a marker
                # already held by someone else is reported, never stolen.
                try:
                    participants.put_item(
                        {"pk": participants.scoped("uniq", "fitbit_id", fitbit_id),
                         "participant_id": pid},
                        ConditionExpression=(
                            "attribute_not_exists(pk) OR participant_id = :pid"),
                        ExpressionAttributeValues={":pid": pid},
                    )
                except Exception as exc:  # noqa: BLE001 - reported, not raised
                    outcomes.append(Outcome(
                        pid, user_id, "CONFLICT",
                        f"fitbit {fitbit_id} is already claimed by another "
                        f"participant — resolve by hand ({exc})"))
                    continue
        outcomes.append(Outcome(pid, user_id,
                                "updated" if commit else "would update", detail))
    return outcomes


def purge_fitbit_rows(devices: ScopedTable, commit: bool) -> list[Outcome]:
    """Delete the now-unreadable fitbit rows from DeviceAssignments.

    Nothing queries them (queries.py raises on device_type="fitbit"), so
    they are inert — but leaving them makes the table lie about what it
    models. Run only after verifying the backfill.
    """
    outcomes = []
    rows = devices.query("is_current", devices.org_id, index_name="Current")
    for row in [r for r in rows if r.get("device_type") == "fitbit"]:
        # the whole partition, including the #HEAD mutex and closed windows
        partition = devices.query("pk", row["pk"])
        for item in partition:
            if commit:
                devices.raw.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})
            outcomes.append(Outcome(
                row.get("participant_id", "-"), "-",
                "deleted" if commit else "would delete",
                f"{item['pk']} {item['sk']}"))
    return outcomes


def main():  # pragma: no cover - thin CLI wrapper
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", required=True)
    ap.add_argument("--commit", action="store_true",
                    help="apply changes (default: dry run)")
    ap.add_argument("--purge-fitbit-rows", action="store_true",
                    help="also delete the dead fitbit DeviceAssignments rows")
    args = ap.parse_args()

    dynamodb = boto3.resource("dynamodb")
    participants = ScopedTable(dynamodb.Table("Participants"), args.org)
    devices = ScopedTable(dynamodb.Table("DeviceAssignments"), args.org)

    outcomes = migrate(participants, devices, args.commit)
    if args.purge_fitbit_rows:
        outcomes += purge_fitbit_rows(devices, args.commit)

    for o in outcomes:
        print(f"{o.action:<14} {o.user_id:<10} {o.participant_id:<18} {o.detail}")
    if not args.commit:
        print("\nDRY RUN — re-run with --commit to apply.")


if __name__ == "__main__":  # pragma: no cover
    main()
