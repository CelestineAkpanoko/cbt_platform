"""Device inventory derived from the raw bucket and the pullers' state.

Three device kinds, three sources — because the pullers land data three
different ways (see registration_service/s3_inventory.py). The fixtures
here mirror the real bucket exactly: Cosinuss filenames carry no date
folder, and Clarity's station id is a CSV column rather than part of any
key.
"""

import json

import pytest

from assignment_ledger import assign_device
from registration_service import (
    list_clarity_station_ids,
    list_cosinuss_receivers,
    list_wearable_ids,
    station_ids_in_clarity_csv,
    unassigned_devices,
)

RAW_BUCKET = "raw-data-all-sensors-test"

# Header + one row, trimmed from a real clarity/raw/<date>/<ts>.csv. The
# station id is datasourceId; sourceId is a different thing (the reading's
# source record) and must not be mistaken for one.
CLARITY_CSV = (
    b'"datasourceId","sourceId","sourceType","time"\n'
    b"DGFVZ0274,AV7QXFXC,CLARITY_NODE,2026-07-06T00:09:49Z\n"
    b"DGFVZ0274,AV7QXFXC,CLARITY_NODE,2026-07-06T00:10:49Z\n"
)


def _land(s3, key, body=b"{}"):
    s3.put_object(Bucket=RAW_BUCKET, Key=key, Body=body)


# --- Cosinuss ---------------------------------------------------------


def test_receivers_from_legacy_flat_layout(s3):
    # cosinuss/raw/<receiver>/<file>.csv — no date folder, as the puller
    # actually writes it today
    _land(s3, "cosinuss/raw/FKCWHM/2026-05-22_13-17-24_UTC_FKCWHM_temperature_FKCWHM.FKCWHM.csv")
    _land(s3, "cosinuss/raw/ACEJMC/2026-06-04_08-57-34_UTC_ACEJMC_temperature_ACEJMC.ACEJMC.csv")
    assert list_cosinuss_receivers(s3, RAW_BUCKET) == ["ACEJMC", "FKCWHM"]


def test_receivers_from_user_scoped_layout(s3):
    """After the puller cutover the first segment is a user_id, and the
    receiver is one level deeper. Depth, not naming, distinguishes them."""
    _land(s3, "cosinuss/raw/user101/ZC5C5W/2026-07-25_13-26-13_UTC_ZC5C5W_temperature_ZC5C5W.ZC5C5W.csv")
    _land(s3, "cosinuss/raw/user18/GFW7P7/2026-07-25_14-11-14_UTC_GFW7P7_temperature_GFW7P7.GFW7P7.csv")
    assert list_cosinuss_receivers(s3, RAW_BUCKET) == ["GFW7P7", "ZC5C5W"]


def test_receivers_span_both_layouts_and_unassigned(s3):
    """During the cutover both layouts coexist. A receiver whose sessions
    could not be attributed is still real hardware and must stay in the
    pool — otherwise it silently disappears from the enrollment dropdown
    at exactly the moment someone needs to assign it."""
    _land(s3, "cosinuss/raw/FKCWHM/old.csv")
    _land(s3, "cosinuss/raw/user101/ZC5C5W/new.csv")
    _land(s3, "cosinuss/raw/_unassigned/9G27HD/orphan.csv")
    assert list_cosinuss_receivers(s3, RAW_BUCKET) == ["9G27HD", "FKCWHM", "ZC5C5W"]


def test_receivers_include_ones_that_uploaded_nothing(s3):
    """A receiver whose last session had zero rows creates no prefix. The
    puller's state file still lists it, and it is still a device someone
    can be assigned."""
    s3.put_object(
        Bucket=RAW_BUCKET, Key="cosinuss/state/cosinuss_state.json",
        Body=json.dumps({
            "last_results_sample": [
                {"file_id": "XY3PQZ", "receiver": "9G27HD", "device": "9G27HD"},
                {"file_id": "MDS4BS", "receiver": "GFW7P7", "device": "unknown"},
            ]
        }).encode(),
    )
    assert list_cosinuss_receivers(s3, RAW_BUCKET) == ["9G27HD", "GFW7P7"]


def test_missing_state_file_does_not_empty_the_pool(s3):
    _land(s3, "cosinuss/raw/FKCWHM/x.csv")
    assert list_cosinuss_receivers(s3, RAW_BUCKET) == ["FKCWHM"]


def test_empty_bucket(s3):
    assert list_cosinuss_receivers(s3, RAW_BUCKET) == []


# --- Fitbit is not a pool ---------------------------------------------


def test_fitbit_has_no_pool_inventory(s3):
    """A fitbit_id is one person's OAuth account. Listing it would invite
    the UI to offer one participant's account to another, which is the
    exact failure this refactor removed — so this raises rather than
    returning a list."""
    _land(s3, "fitbit/raw/D58MBD/2026-07-01/heart_rate_intraday.json")
    with pytest.raises(ValueError, match="no pool inventory"):
        list_wearable_ids(s3, RAW_BUCKET, "fitbit")


def test_clarity_is_not_a_wearable(s3):
    with pytest.raises(ValueError, match="no pool inventory"):
        list_wearable_ids(s3, RAW_BUCKET, "clarity")


# --- Clarity ----------------------------------------------------------


def test_station_ids_come_from_the_csv_column_not_the_key(s3):
    """The whole point: clarity/raw/<date>/<timestamp>.csv has no station
    id anywhere in the key. The old code read one out of the filename and
    got '20260706T001349Z.csv'."""
    _land(s3, "clarity/raw/2026-07-06/20260706T001349Z.csv", CLARITY_CSV)
    assert list_clarity_station_ids(s3, RAW_BUCKET) == ["DGFVZ0274"]


def test_station_ids_parse_handles_quoted_header_and_dedupes():
    assert station_ids_in_clarity_csv(CLARITY_CSV) == {"DGFVZ0274"}


def test_station_ids_ignores_sourceid_column():
    """sourceId is the reading's source record, not the station."""
    assert "AV7QXFXC" not in station_ids_in_clarity_csv(CLARITY_CSV)


def test_station_ids_newest_days_first(s3):
    _land(s3, "clarity/raw/2026-07-06/20260706T001349Z.csv", CLARITY_CSV)
    _land(s3, "clarity/raw/2026-07-07/20260707T001349Z.csv",
          CLARITY_CSV.replace(b"DGFVZ0274", b"DGFVZ0999"))
    assert list_clarity_station_ids(s3, RAW_BUCKET, max_days=1) == ["DGFVZ0999"]


def test_unreadable_clarity_data_returns_empty_not_error(s3):
    """The enrollment form falls back to its configured list and its
    free-text entry — an unreachable bucket must not block enrollment."""
    assert list_clarity_station_ids(s3, "no-such-bucket") == []


def test_clarity_csv_without_the_column_yields_nothing(s3):
    _land(s3, "clarity/raw/2026-07-06/x.csv", b"time,pm25\n2026-07-06,5\n")
    assert list_clarity_station_ids(s3, RAW_BUCKET) == []


# --- Pool arithmetic ---------------------------------------------------


def test_cosinuss_pool_excludes_currently_worn(dynamo_client, devices, s3):
    _land(s3, "cosinuss/raw/FKCWHM/a.csv")
    _land(s3, "cosinuss/raw/ACEJMC/b.csv")
    assign_device(dynamo_client, devices, device_type="cosinuss",
                  device_id="FKCWHM", participant_id="p-alice",
                  role="research", effective_from="2026-07-01T00:00:00Z")

    pool = unassigned_devices(devices, "cosinuss",
                              list_cosinuss_receivers(s3, RAW_BUCKET))
    assert pool == ["ACEJMC"]


def test_no_pool_for_fitbit_or_clarity(devices):
    from registration_service import ValidationError

    for device_type in ("fitbit", "clarity"):
        with pytest.raises(ValidationError, match="no assignment pool"):
            unassigned_devices(devices, device_type, ["X"])


# --- Fitbit first-seen (registration backdating) ----------------------


def test_first_fitbit_data_date_returns_earliest_day(s3):
    from registration_service import first_fitbit_data_date

    _land(s3, "fitbit/raw/D58MBD/2026-07-03/heart_rate.json")
    _land(s3, "fitbit/raw/D58MBD/2026-06-21/heart_rate.json")
    _land(s3, "fitbit/raw/D58MBD/2026-08-01/heart_rate.json")
    _land(s3, "fitbit/raw/OTHER1/2026-01-01/heart_rate.json")  # someone else

    assert first_fitbit_data_date(s3, RAW_BUCKET, "D58MBD") == \
        "2026-06-21T00:00:00Z"


def test_first_fitbit_data_date_none_for_unknown_account(s3):
    from registration_service import first_fitbit_data_date

    assert first_fitbit_data_date(s3, RAW_BUCKET, "NEVERSEEN") is None
