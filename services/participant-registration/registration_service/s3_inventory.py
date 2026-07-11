"""Wearable device inventory derived from the raw sensor bucket — no
table, no CLI.

The pull Lambdas land Fitbit/Cosinuss data keyed by device id:
    <bucket>/fitbit/raw/<fitbit_id>/<date>/...
    <bucket>/cosinuss/raw/<device_id>/<date>/...

So the bucket itself is the authoritative list of wearables the platform
has ever seen — anything that has uploaded at least once appears as a
prefix. The enrollment form derives its Cosinuss dropdown from here
instead of a manually maintained list, with a free-text "Other (new
device)" fallback for hardware so new it hasn't uploaded yet.

Clarity is NOT covered here — its raw data is per-minute CSVs with
columns like datasourceid/sourceid, not a per-device folder, so there is
no reliable way to list known stations from bucket structure alone. The
Clarity/site id is entered directly in the enrollment form instead.
"""

from __future__ import annotations

WEARABLE_TYPES = ("fitbit", "cosinuss")


def list_wearable_ids(s3_client, bucket: str, device_type: str) -> list[str]:
    """Every device id that has ever landed raw data, from the
    <device_type>/raw/<id>/ prefix structure."""
    if device_type not in WEARABLE_TYPES:
        raise ValueError(f"unknown wearable type {device_type!r}")
    ids: set[str] = set()
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{device_type}/raw/",
                                   Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            # "fitbit/raw/D58MBD/" -> "D58MBD"
            device_id = cp["Prefix"].rstrip("/").rsplit("/", 1)[-1]
            if device_id:
                ids.add(device_id)
    return sorted(ids)
