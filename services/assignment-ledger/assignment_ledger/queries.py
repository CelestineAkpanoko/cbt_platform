"""Read side of the assignment ledger.

All queries go through ScopedTable, so every key condition carries the
org_id prefix — this file must never accept a pre-built raw key from a
caller without scoping it.
"""

from __future__ import annotations

from typing import Optional

from boto3.dynamodb.conditions import Key


def current_device_assignment(device_table, device_type: str,
                              device_id: str) -> Optional[dict]:
    """Active assignment row for a device, or None (in inventory / between wearers)."""
    pk = device_table.scoped(device_type, device_id)
    items = device_table.query("pk", pk, ScanIndexForward=False, Limit=1)
    if items and "is_current" in items[0]:
        return items[0]
    return None


def current_site_assignment(site_table, entity_kind: str,
                            entity_id: str) -> Optional[dict]:
    pk = site_table.scoped(entity_kind, entity_id)
    items = site_table.query("pk", pk, ScanIndexForward=False, Limit=1)
    if items and "is_current" in items[0]:
        return items[0]
    return None


def participants_at_site(site_table, site_id: str) -> list[dict]:
    """Every participant currently linked to this site_id — which IS a
    Clarity device id (see docs/data-model.md). Used by the ingestion
    resolver to confirm a landed Clarity raw file belongs to a currently
    monitored location: a file resolves iff at least one active
    participant references this exact site_id right now."""
    rows = site_table.query("site_pk", site_table.scoped("site", site_id),
                            index_name="BySite")
    return [r for r in rows
            if r.get("entity_kind") == "participant" and "is_current" in r]


def current_site_for_participant(site_table, participant_id: str) -> Optional[str]:
    """A participant's current site_id — which, by design, IS the Clarity
    device id currently covering them (entered directly at enrollment by
    the research admin; see docs/data-model.md). There is no separate
    station→site indirection: swapping the physical Clarity unit at a
    location does not retroactively update already-enrolled participants
    — that's an accepted tradeoff of skipping a station-admin workflow."""
    row = current_site_assignment(site_table, "participant", participant_id)
    return row["site_id"] if row else None


def participant_at(device_table, device_type: str, device_id: str,
                   timestamp: str) -> Optional[dict]:
    """Full-history point-in-time resolution: who wore this device at
    `timestamp` (ISO 8601)?

    This is the query the future offline-retraining phase will use to route
    historical raw files to the right participant. Returns the assignment
    row, or None if the device was unassigned at that moment.
    """
    pk = device_table.scoped(device_type, device_id)
    # Latest window opened at or before `timestamp`
    items = device_table.query(
        "pk", pk,
        sk_condition=Key("sk").lte(f"assign#{timestamp}"),
        ScanIndexForward=False, Limit=1,
    )
    if not items or not items[0]["sk"].startswith("assign#"):
        return None  # only the #HEAD pointer matched — no window open by then
    row = items[0]
    effective_to = row.get("effective_to")
    if effective_to is not None and timestamp >= effective_to:
        return None  # window had already closed; device was between wearers
    return row


def all_current_device_assignments(device_table) -> list[dict]:
    """Every device with an active wearer in this org (sparse Current GSI)."""
    return device_table.query("is_current", device_table.org_id,
                              index_name="Current")


def assignments_for_participant(device_table, participant_id: str) -> list[dict]:
    """All device windows (past and present) for a participant."""
    return device_table.query(
        "participant_pk", device_table.scoped(participant_id),
        index_name="ByParticipant",
    )
