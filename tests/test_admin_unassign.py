"""admin_service.unassign_participant: the "no longer in the study" path.

Distinct from delete_participant — this closes the current Cosinuss/Clarity
windows so the hardware is reassignable, while leaving the participant's
profile, calibration history, and identity markers alone. Regression
coverage for FKCWHM-style stuck receivers: deactivating/reactivating a
device in the Registry catalog never touched the assignment ledger, so a
receiver whose previous wearer never had their window closed stayed
permanently unassignable. unassign_participant is the fix.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "services", "admin-portal"))

from admin_service import (  # noqa: E402
    ParticipantNotFoundError,
    current_assignments,
    unassign_participant,
)
from assignment_ledger.queries import current_device_assignment  # noqa: E402
from registration_service import RegistrationRequest, register  # noqa: E402
from registration_service.service import unassigned_devices  # noqa: E402

T0 = "2026-07-01T00:00:00Z"
T1 = "2026-08-01T00:00:00Z"


def _req(**overrides):
    base = dict(
        user_id="user19", email="user19@example.com", display_name="A-19",
        sex="female", age=34, height_in=66.0, weight_lbs=150.0, race="white",
        enrollment_mode="research", consent_given=True, site_id="DGFVZ0274",
        fitbit_id="D58MBD", cosinuss_id="FKCWHM", effective_from=T0,
    )
    base.update(overrides)
    return RegistrationRequest(**base)


def test_unassign_frees_device_for_the_next_participant(
        dynamo_client, participants, devices, sites, calibration_table):
    first = register(dynamo_client, participants, devices, sites, _req())
    pid = first.participant_id

    # before: exactly the stuck-FKCWHM symptom — the device is taken and
    # does not show up in the enrollment pool.
    assert "FKCWHM" not in unassigned_devices(devices, "cosinuss", ["FKCWHM"])

    result = unassign_participant(dynamo_client, participants, devices,
                                  sites, pid, T1)
    assert result["devices"] == [{"device_type": "cosinuss",
                                  "device_id": "FKCWHM"}]
    assert result["site"] == "DGFVZ0274"

    # after: free, and a new participant can take it over
    assert "FKCWHM" in unassigned_devices(devices, "cosinuss", ["FKCWHM"])
    second = register(dynamo_client, participants, devices, sites, _req(
        user_id="user20", email="user20@example.com", fitbit_id="D58XYZ",
        effective_from=T1))
    row = current_device_assignment(devices, "cosinuss", "FKCWHM")
    assert row["participant_id"] == second.participant_id

    # the departing participant's profile and history are untouched
    person = participants.get_item("pk", participants.scoped(pid))
    assert person is not None
    assert person["participant_id"] == pid


def test_unassign_is_idempotent_when_nothing_is_open(
        dynamo_client, participants, devices, sites, calibration_table):
    first = register(dynamo_client, participants, devices, sites, _req())
    pid = first.participant_id
    unassign_participant(dynamo_client, participants, devices, sites, pid, T1)

    # calling again finds nothing open — no error, empty result
    again = unassign_participant(dynamo_client, participants, devices,
                                 sites, pid, T1)
    assert again == {"devices": [], "site": None}


def test_unassign_missing_participant_raises(
        participants, devices, sites, calibration_table, dynamo_client):
    with pytest.raises(ParticipantNotFoundError):
        unassign_participant(dynamo_client, participants, devices, sites,
                             "nope", T1)


def test_unassign_after_device_already_reassigned_elsewhere_is_a_noop_for_it(
        dynamo_client, participants, devices, sites, calibration_table):
    """If FKCWHM was already handed to someone else (e.g. via the normal
    enrollment flow) before an admin gets around to unassigning the
    original participant, that participant no longer holds a current
    device window — unassign_participant must not touch the new wearer's
    window, and must still close the departing participant's site window."""
    first = register(dynamo_client, participants, devices, sites, _req())
    pid = first.participant_id

    second = register(dynamo_client, participants, devices, sites, _req(
        user_id="user20", email="user20@example.com", fitbit_id="D58XYZ",
        effective_from=T1))  # takes over FKCWHM from `first`

    result = unassign_participant(dynamo_client, participants, devices,
                                  sites, pid, T1)
    assert result["devices"] == []  # nothing of pid's own left to close
    assert result["site"] == "DGFVZ0274"  # pid's own site window still closes
    row = current_device_assignment(devices, "cosinuss", "FKCWHM")
    assert row["participant_id"] == second.participant_id  # untouched


def test_current_assignments_matches_what_unassign_would_close(
        dynamo_client, participants, devices, sites, calibration_table):
    first = register(dynamo_client, participants, devices, sites, _req())
    pid = first.participant_id
    held = current_assignments(devices, sites, pid)
    assert held == {"devices": [{"device_type": "cosinuss",
                                 "device_id": "FKCWHM"}],
                    "site": "DGFVZ0274"}
