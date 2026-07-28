"""Concurrency-safe SCD Type 2 write path for the assignment ledgers.

Every (re)assignment is a single TransactWriteItems call over three items:

  1. HEAD pointer (pk, sk="#HEAD") — one mutex item per device/entity that
     records the effective_from of the open window. The transaction
     conditions on the HEAD state the caller observed (absent for a free
     device, or the expected current window). This is what serializes two
     concurrent assignments of the same *free* device — their new rows have
     different sort keys, so only a shared item can make them conflict.
  2. Conditionally close the currently-active row (is_current must still be
     present on the exact row we read).
  3. Put the new active row (conditioned on not already existing, so a
     replay can't resurrect a closed window).

A deterministic ClientRequestToken is passed for idempotency. On condition
failure we raise DeviceJustReassignedError — callers surface "this device
was just reassigned, refresh and retry". No silent retries, no unguarded
overwrite fallback.
"""

from __future__ import annotations

import hashlib
from typing import Optional

from botocore.exceptions import ClientError

from cbt_shared.models import DeviceAssignment, SiteAssignment
from cbt_shared.tenancy import ScopedTable

from .queries import current_device_assignment, current_site_assignment

HEAD_SK = "#HEAD"

_READ_NOW = object()  # sentinel: no caller-observed state; read at write time


class DeviceJustReassignedError(Exception):
    """The active assignment changed under us. Refresh and retry."""


def _request_token(*parts: str) -> str:
    # Deterministic per logical operation so an exact client replay within
    # DynamoDB's 10-minute idempotency window is a no-op, not a duplicate.
    return hashlib.sha256("#".join(parts).encode()).hexdigest()[:36]


def _s(item: dict) -> dict:
    """dict -> DynamoDB AttributeValue map (our items are string-valued)."""
    return {k: {"S": str(v)} for k, v in item.items()}


def _transact_reassign(client, table_name: str, pk: str,
                       current_item: Optional[dict], new_item: dict, token: str):
    head_update = {
        "Update": {
            "TableName": table_name,
            "Key": _s({"pk": pk, "sk": HEAD_SK}),
            "UpdateExpression": "SET current_from = :new",
            "ExpressionAttributeValues": _s({":new": new_item["effective_from"]}),
        }
    }
    if current_item is None:
        head_update["Update"]["ConditionExpression"] = (
            "attribute_not_exists(current_from)"
        )
    else:
        head_update["Update"]["ConditionExpression"] = "current_from = :expected"
        head_update["Update"]["ExpressionAttributeValues"][":expected"] = {
            "S": current_item["effective_from"]
        }

    actions = [head_update]
    if current_item is not None:
        actions.append({
            "Update": {
                "TableName": table_name,
                "Key": _s({"pk": current_item["pk"], "sk": current_item["sk"]}),
                "UpdateExpression": "SET effective_to = :to REMOVE is_current",
                "ConditionExpression": "attribute_exists(is_current)",
                "ExpressionAttributeValues": _s({":to": new_item["effective_from"]}),
            }
        })
    actions.append({
        "Put": {
            "TableName": table_name,
            "Item": _s(new_item),
            "ConditionExpression": "attribute_not_exists(pk)",
        }
    })
    try:
        client.transact_write_items(TransactItems=actions, ClientRequestToken=token)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("TransactionCanceledException", "ConditionalCheckFailedException"):
            raise DeviceJustReassignedError(
                "this device was just reassigned, refresh and retry"
            ) from e
        raise


def assign_device(client, device_table: ScopedTable, *, device_type: str,
                  device_id: str, participant_id: str, role: str,
                  effective_from: str,
                  expected_current=_READ_NOW) -> DeviceAssignment:
    """Open a new device→participant window, closing any active one.

    Exclusive-wear hardware only (cbt_shared.models.DEVICE_TYPES). Calling
    this with "fitbit" raises DeviceTypeError from DeviceAssignment — that
    used to be the account-takeover path: the transaction below would
    dutifully close the first participant's window and hand their Fitbit
    (and therefore their raw data) to whoever registered next.

    expected_current: the assignment state the caller observed (e.g. when
    the enrollment form rendered its device pick list) — None for "device
    was free". The transaction conditions on it, so a stale view fails with
    DeviceJustReassignedError instead of silently double-assigning. Omit it
    to read the current state at write time.
    """
    if expected_current is _READ_NOW:
        current = current_device_assignment(device_table, device_type, device_id)
    else:
        current = expected_current
    if current is not None and current["participant_id"] == participant_id:
        # Same wearer re-submitted; keep the existing window open.
        return DeviceAssignment(
            org_id=device_table.org_id, device_type=device_type,
            device_id=device_id, participant_id=participant_id,
            role=current["role"], effective_from=current["effective_from"],
            is_current=True,
        )
    new = DeviceAssignment(
        org_id=device_table.org_id, device_type=device_type,
        device_id=device_id, participant_id=participant_id, role=role,
        effective_from=effective_from, is_current=True,
    )
    token = _request_token(new.pk(), participant_id, effective_from)
    _transact_reassign(client, device_table.name, new.pk(), current,
                       new.to_item(), token)
    return new


def assign_site(client, site_table: ScopedTable, *, entity_kind: str,
                entity_id: str, site_id: str, effective_from: str,
                expected_current=_READ_NOW) -> SiteAssignment:
    """Open a new participant/station→site window, closing any active one.
    See assign_device for expected_current semantics."""
    if expected_current is _READ_NOW:
        current = current_site_assignment(site_table, entity_kind, entity_id)
    else:
        current = expected_current
    if current is not None and current["site_id"] == site_id:
        return SiteAssignment(
            org_id=site_table.org_id, entity_kind=entity_kind,
            entity_id=entity_id, site_id=site_id,
            effective_from=current["effective_from"], is_current=True,
        )
    new = SiteAssignment(
        org_id=site_table.org_id, entity_kind=entity_kind, entity_id=entity_id,
        site_id=site_id, effective_from=effective_from, is_current=True,
    )
    token = _request_token(new.pk(), site_id, effective_from)
    _transact_reassign(client, site_table.name, new.pk(), current,
                       new.to_item(), token)
    return new
