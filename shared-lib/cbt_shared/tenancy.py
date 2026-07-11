"""Tenant-scoped DynamoDB access.

Every read and write in this codebase goes through ScopedTable, which
requires an org_id at construction and refuses key expressions that are not
prefixed with it. Individual call sites must never build their own
un-scoped queries — tests/test_tenancy.py fails the build if any module
outside this one calls boto3 query/scan/get_item directly.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from boto3.dynamodb.conditions import Key


def _dynamo_safe(value):
    """Recursively convert floats to Decimal (DynamoDB requirement)."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _dynamo_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_dynamo_safe(v) for v in value]
    return value


class TenantScopeError(Exception):
    """Raised when an operation would escape its org_id scope."""


class ScopedTable:
    """Wraps a boto3 Table resource; all key values must carry the org_id prefix."""

    def __init__(self, table, org_id: str):
        if not org_id or "#" in org_id:
            raise TenantScopeError(f"invalid org_id: {org_id!r}")
        self._table = table
        self.org_id = org_id
        self.name = table.name

    # -- helpers -----------------------------------------------------------
    def scoped(self, *parts: str) -> str:
        """Build a key value guaranteed to lead with this tenant's org_id."""
        return "#".join([self.org_id, *parts])

    def _check(self, value: str) -> str:
        if not isinstance(value, str) or not (
            value == self.org_id or value.startswith(self.org_id + "#")
        ):
            raise TenantScopeError(
                f"key {value!r} is not scoped to org {self.org_id!r}"
            )
        return value

    # -- operations --------------------------------------------------------
    def get_item(self, pk_name: str, pk_value: str, **kwargs) -> Optional[dict]:
        self._check(pk_value)
        resp = self._table.get_item(Key={pk_name: pk_value, **kwargs.pop("extra_key", {})}, **kwargs)
        return resp.get("Item")

    def put_item(self, item: dict, **kwargs):
        for key_attr in ("pk",):
            if key_attr in item:
                self._check(item[key_attr])
        return self._table.put_item(Item=_dynamo_safe(item), **kwargs)

    def query(self, pk_name: str, pk_value: str, index_name: str | None = None,
              sk_condition=None, **kwargs) -> list[dict]:
        self._check(pk_value)
        cond = Key(pk_name).eq(pk_value)
        if sk_condition is not None:
            cond = cond & sk_condition
        params = {"KeyConditionExpression": cond, **kwargs}
        if index_name:
            params["IndexName"] = index_name
        items: list[dict] = []
        while True:
            resp = self._table.query(**params)
            items.extend(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                return items
            params["ExclusiveStartKey"] = lek

    def delete_item(self, pk_name: str, pk_value: str, **kwargs):
        self._check(pk_value)
        return self._table.delete_item(Key={pk_name: pk_value}, **kwargs)

    def update_item(self, key: dict, **kwargs):
        for v in key.values():
            if isinstance(v, str):
                self._check(v)
                break
        if "ExpressionAttributeValues" in kwargs:
            kwargs["ExpressionAttributeValues"] = _dynamo_safe(
                kwargs["ExpressionAttributeValues"]
            )
        return self._table.update_item(Key=key, **kwargs)

    @property
    def raw(self):
        """Escape hatch for transactions (client-level API). Callers must
        still build every key via self.scoped()."""
        return self._table
