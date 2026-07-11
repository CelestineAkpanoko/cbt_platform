"""Device inventory derived from the raw sensor bucket — no table, no CLI.

The pull Lambdas already land data keyed by device id:
    <bucket>/fitbit/raw/<fitbit_id>/<date>/...
    <bucket>/cosinuss/raw/<device_id>/<date>/...
    <bucket>/clarity/raw/<date>/<clarity_id>_*.json

So the bucket itself is the authoritative list of devices the platform has
ever seen — anything that has uploaded at least once appears as a prefix.
The enrollment form derives its dropdowns from here instead of a manually
maintained list, with a free-text "Other (new device)" fallback for
hardware so new it hasn't uploaded yet.

Note the asymmetry: fitbit/cosinuss ids are path segments (one cheap
delimiter listing); clarity ids live inside filenames under date folders,
so we scan the most recent date prefixes only.
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


def list_clarity_ids(s3_client, bucket: str, recent_dates: int = 30) -> list[str]:
    """Clarity station ids seen in the most recent `recent_dates` date
    folders (ids are filename prefixes: <clarity_id>_*.json)."""
    paginator = s3_client.get_paginator("list_objects_v2")

    date_prefixes: list[str] = []
    for page in paginator.paginate(Bucket=bucket, Prefix="clarity/raw/",
                                   Delimiter="/"):
        date_prefixes.extend(cp["Prefix"] for cp in page.get("CommonPrefixes", []))
    # date folder names sort chronologically (YYYY-MM-DD)
    date_prefixes = sorted(date_prefixes)[-recent_dates:]

    ids: set[str] = set()
    for prefix in date_prefixes:
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                filename = obj["Key"].rsplit("/", 1)[-1]
                clarity_id = filename.split("_")[0]
                if clarity_id:
                    ids.add(clarity_id)
    return sorted(ids)
