"""Admin-managed dropdown registry (DynamoDB "Registry" table).

The admin portal writes entries here; the registration form reads them to
populate its dropdowns instead of inferring options from S3. Layout:
pk = entity kind ("org" | "cosinuss" | "clarity"), sk = the id itself.
Items carry name, location, active, created_at / deactivated_at.

The table is deliberately NOT org-scoped (orgs themselves live in it and
device hardware is shared), so callers pass a plain boto3 Table, not a
ScopedTable.
"""

from __future__ import annotations

from datetime import datetime, timezone

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

REGISTRY_TABLE_NAME = "Registry"

KINDS = ("org", "cosinuss", "clarity")


class RegistryError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_kind(kind: str) -> None:
    if kind not in KINDS:
        raise RegistryError(f"unknown registry kind {kind!r}; expected one of {KINDS}")


def list_entries(table, kind: str, *, include_inactive: bool = False) -> list[dict]:
    """All entries of one kind, sorted by id. Active only unless asked."""
    _check_kind(kind)
    items: list[dict] = []
    kwargs = {"KeyConditionExpression": Key("pk").eq(kind)}
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    if not include_inactive:
        items = [i for i in items if i.get("active", True)]
    return sorted(items, key=lambda i: i["sk"])


def list_orgs(table, *, include_inactive: bool = False) -> list[dict]:
    """[{org_id, name, location, active, ...}] for the reg-form dropdown."""
    out = []
    for item in list_entries(table, "org", include_inactive=include_inactive):
        entry = dict(item)
        entry["org_id"] = item["sk"]
        out.append(entry)
    return out


def list_cosinuss_ids(table) -> list[str]:
    return [i["sk"] for i in list_entries(table, "cosinuss")]


def list_clarity_ids(table) -> list[str]:
    return [i["sk"] for i in list_entries(table, "clarity")]


def add_entry(
    table,
    kind: str,
    entry_id: str,
    *,
    name: str = "",
    location: str = "",
    created_by: str = "",
) -> dict:
    """Add a new entry, or reactivate (and update) a deactivated one.

    Raises RegistryError if an ACTIVE entry with this id already exists.
    """
    _check_kind(kind)
    entry_id = entry_id.strip()
    if not entry_id:
        raise RegistryError("entry id must not be empty")
    item = {
        "pk": kind,
        "sk": entry_id,
        "name": name.strip(),
        "location": location.strip(),
        "active": True,
        "created_at": _now(),
    }
    if created_by:
        item["created_by"] = created_by
    try:
        table.put_item(
            Item=item,
            ConditionExpression=(
                "attribute_not_exists(pk) OR active = :inactive"
            ),
            ExpressionAttributeValues={":inactive": False},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise RegistryError(f"{kind} {entry_id!r} already exists") from exc
        raise
    return item


def deactivate_entry(table, kind: str, entry_id: str) -> None:
    """Soft-delete: keeps the row so history stays explainable."""
    _check_kind(kind)
    table.update_item(
        Key={"pk": kind, "sk": entry_id},
        UpdateExpression="SET active = :f, deactivated_at = :t",
        ConditionExpression="attribute_exists(pk)",
        ExpressionAttributeValues={":f": False, ":t": _now()},
    )
