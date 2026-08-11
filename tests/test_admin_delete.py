import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "services", "admin-portal"))

from admin_service import (  # noqa: E402
    ParticipantNotFoundError,
    delete_participant,
    list_participants,
)
from registration_service import RegistrationRequest, register

T0 = "2026-07-01T00:00:00Z"
T1 = "2026-08-01T00:00:00Z"


def _req(**overrides):
    base = dict(
        user_id="user15", email="alice@example.com", display_name="A-01",
        sex="female", age=34, height_in=66.0, weight_lbs=150.0, race="white",
        enrollment_mode="research", consent_given=True, site_id="DGFVZ0274",
        fitbit_id="D58MBD", cosinuss_id="ZC5C5W", effective_from=T0,
    )
    base.update(overrides)
    return RegistrationRequest(**base)


def test_delete_removes_every_row_and_frees_the_identity(
        dynamodb, dynamo_client, participants, devices, sites,
        calibration_table):
    result = register(dynamo_client, participants, devices, sites, _req())
    pid = result.participant_id
    assert len(list_participants(participants)) == 1

    counts = delete_participant(participants, devices, sites,
                                calibration_table, pid)
    assert counts["participant"] == 1
    assert counts["markers"] >= 3          # user_id, email, fitbit_id
    assert counts["device_assignments"] == 1
    assert counts["device_heads"] == 1     # device currently theirs -> freed
    assert counts["site_assignments"] >= 1

    for name in ("Participants", "DeviceAssignments", "SiteAssignments",
                 "CalibrationHistory"):
        assert dynamodb.Table(name).scan()["Count"] == 0

    # The regression that matters: the same fitbit/user/email register fresh.
    again = register(dynamo_client, participants, devices, sites, _req())
    assert again.created_new_participant


def test_delete_keeps_head_of_a_reassigned_device(
        dynamodb, dynamo_client, participants, devices, sites,
        calibration_table):
    first = register(dynamo_client, participants, devices, sites, _req())
    second = register(dynamo_client, participants, devices, sites, _req(
        user_id="user16", email="bob@example.com", fitbit_id="D58XYZ",
        effective_from=T1))  # takes over ZC5C5W

    counts = delete_participant(participants, devices, sites,
                                calibration_table,
                                first.participant_id)
    # The receiver now belongs to user16 — their HEAD mutex must survive.
    assert counts["device_heads"] == 0
    remaining = dynamodb.Table("DeviceAssignments").scan()["Items"]
    assert any(i["sk"] == "#HEAD" for i in remaining)
    assert any(i.get("participant_id") ==
               second.participant_id for i in remaining)


def test_delete_missing_participant_raises(participants, devices, sites,
                                           calibration_table):
    with pytest.raises(ParticipantNotFoundError):
        delete_participant(participants, devices, sites, calibration_table,
                           "nope")


def test_calibration_overview_reports_each_participant(
        dynamodb, dynamo_client, participants, devices, sites,
        calibration_table):
    from admin_service import calibration_overview

    result = register(dynamo_client, participants, devices, sites, _req())
    rows = calibration_overview(participants, calibration_table)
    assert len(rows) == 1
    assert rows[0]["status"] == "waiting for first sweep"
    assert rows[0]["enrolled_at"] == T0[:10]

    # A completed calibration for the current enrollment window shows up.
    calibration_table.raw.put_item(Item={
        "pk": f"org1#{result.participant_id}",
        "computed_at": "2026-07-05T00:00:00Z",
        "assignment_effective_from": T0,
        "calibration_status": "complete",
        "nights_used": 3,
    })
    rows = calibration_overview(participants, calibration_table)
    assert rows[0]["status"] == "complete"
    assert rows[0]["nights"] == 3
