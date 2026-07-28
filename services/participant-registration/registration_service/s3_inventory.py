"""Device inventory derived from the raw sensor bucket and the pullers'
own state files — no inventory table, no CLI.

Three device kinds, three different sources, because the three pullers
land data three different ways:

  Cosinuss  cosinuss/raw/<user_id>/<receiver_id>/<file>.csv   (current)
            cosinuss/raw/<receiver_id>/<file>.csv             (legacy)
            Receivers are the exclusive-wear pool the enrollment form
            picks from. Both layouts are read, plus the puller's state
            file (cosinuss/state/cosinuss_state.json), which lists every
            receiver seen on the last poll — including ones that produced
            zero rows and therefore never created a prefix. "Has ever
            uploaded a non-empty file" is a bad definition of "exists".

  Clarity   clarity/raw/<date>/<timestamp>.csv
            The station id is NOT in the key — it is the `datasourceId`
            column inside the CSV, and one file can carry rows for several
            stations. Enumerating stations therefore means reading a
            recent file, not listing prefixes. This is why the form used
            to fall back to a hand-maintained env whitelist.

  Fitbit    fitbit/raw/<fitbit_id>/<date>/...
            NOT an inventory. A fitbit_id is an OAuth account id captured
            during enrollment, unique to one person — there is no pool to
            pick from, so listing it would only invite the UI to offer one
            participant's account to another. Asking for it raises.
"""

from __future__ import annotations

import csv
import io
import json
import logging

log = logging.getLogger(__name__)

# Exclusive-wear hardware with an enumerable id space (mirrors
# cbt_shared.models.DEVICE_TYPES; kept as a local constant so this module
# stays importable without the shared lib in the Lambda bundle).
WEARABLE_TYPES = ("cosinuss",)

COSINUSS_STATE_KEY = "cosinuss/state/cosinuss_state.json"

# Reserved first-path-segments under cosinuss/raw/ that are not user ids.
# The puller routes sessions it cannot attribute here (see the cosinuss
# puller's UNASSIGNED_USER); they must never surface as a participant.
_RESERVED_SEGMENTS = {"_unassigned", "_unknown"}


def _common_prefixes(s3_client, bucket: str, prefix: str) -> list[str]:
    """Immediate sub-directory names under `prefix`."""
    out: list[str] = []
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            name = cp["Prefix"].rstrip("/").rsplit("/", 1)[-1]
            if name:
                out.append(name)
    return out


def list_cosinuss_receivers(s3_client, bucket: str) -> list[str]:
    """Every Cosinuss receiver id the platform knows about.

    Union of three sources so a receiver is never missing from the
    enrollment dropdown just because of where (or whether) its data
    landed:

      1. the puller's state file — authoritative, refreshed every poll,
         and the only source that includes a receiver whose last session
         had zero rows;
      2. the current user-scoped layout, cosinuss/raw/<user>/<receiver>/;
      3. the legacy flat layout, cosinuss/raw/<receiver>/.

    (2) and (3) are distinguished by depth, not by name: a segment that
    itself contains sub-directories is a user, and its children are
    receivers.
    """
    ids: set[str] = set()

    # 1. puller state — every receiver seen on the last successful poll
    try:
        body = s3_client.get_object(Bucket=bucket, Key=COSINUSS_STATE_KEY)["Body"].read()
        state = json.loads(body)
        for result in state.get("last_results_sample", []):
            for key in ("receiver", "device"):
                value = (result.get(key) or "").strip()
                if value and value.lower() not in ("unknown", "none", "null"):
                    ids.add(value)
    except Exception:
        # The state file is an optimisation, not a requirement — a missing
        # or malformed one must not empty the enrollment dropdown.
        log.warning("cosinuss state file unreadable; falling back to bucket "
                    "layout only", exc_info=True)

    # 2 + 3. bucket layout, both generations
    for segment in _common_prefixes(s3_client, bucket, "cosinuss/raw/"):
        if segment in _RESERVED_SEGMENTS:
            # _unassigned/<receiver>/ — the receivers are real even though
            # the parent segment is not a user
            ids.update(_common_prefixes(
                s3_client, bucket, f"cosinuss/raw/{segment}/"))
            continue
        children = _common_prefixes(s3_client, bucket, f"cosinuss/raw/{segment}/")
        if children:
            ids.update(children)   # user-scoped: segment is a user_id
        else:
            ids.add(segment)       # legacy flat: segment is a receiver id

    return sorted(ids)


def list_wearable_ids(s3_client, bucket: str, device_type: str) -> list[str]:
    """Pool inventory for an exclusive-wear device type.

    Kept as the generic entry point the enrollment form calls, but there is
    exactly one valid device_type today. Passing "fitbit" raises rather
    than returning a list: a Fitbit account belongs to one person and must
    never appear in a pick list (see the module docstring).
    """
    if device_type not in WEARABLE_TYPES:
        raise ValueError(
            f"{device_type!r} has no pool inventory. Valid: {WEARABLE_TYPES}. "
            f"fitbit -> captured via OAuth onto Participant.fitbit_id; "
            f"clarity -> list_clarity_station_ids()."
        )
    return list_cosinuss_receivers(s3_client, bucket)


def list_clarity_station_ids(s3_client, bucket: str, *,
                             max_days: int = 3,
                             max_files_per_day: int = 1) -> list[str]:
    """Clarity station ids (== site_ids), read from recent raw files.

    Clarity's raw layout is clarity/raw/<date>/<timestamp>.csv with the
    station id in the `datasourceId` column, so — unlike a per-device
    folder — the only way to enumerate stations is to open a file. We read
    at most `max_files_per_day` files from each of the newest `max_days`
    date partitions: stations report every minute, so any recent file
    lists every station currently online, and looking at a few days covers
    one that was briefly offline.

    Returns [] rather than raising if nothing is readable — the caller
    (enrollment form) falls back to its configured whitelist and its
    free-text entry, so an unreachable bucket must not block enrollment.
    """
    stations: set[str] = set()
    try:
        dates = sorted(_common_prefixes(s3_client, bucket, "clarity/raw/"),
                       reverse=True)[:max_days]
        for date in dates:
            resp = s3_client.list_objects_v2(
                Bucket=bucket, Prefix=f"clarity/raw/{date}/",
                MaxKeys=max_files_per_day,
            )
            for obj in resp.get("Contents", []):
                body = s3_client.get_object(
                    Bucket=bucket, Key=obj["Key"])["Body"].read()
                stations.update(station_ids_in_clarity_csv(body))
    except Exception:
        log.warning("could not derive Clarity stations from %s", bucket,
                    exc_info=True)
    return sorted(stations)


def station_ids_in_clarity_csv(body: bytes) -> set[str]:
    """Distinct station ids in one Clarity raw CSV.

    The header is `datasourceId` in the files the puller writes today, but
    the vendor's csv-wide export has been seen with `sourceid` casing
    variants, so match case-insensitively and accept either name.
    `sourceId` is a *different* column (the reading's source record, e.g.
    AV7QXFXC) and is deliberately not treated as a station id.
    """
    text = body.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return set()
    column = next(
        (f for f in reader.fieldnames
         if f and f.strip().strip('"').lower() == "datasourceid"),
        None,
    )
    if column is None:
        return set()
    return {
        row[column].strip()
        for row in reader
        if row.get(column) and row[column].strip()
    }
