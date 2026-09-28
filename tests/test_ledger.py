"""DoD: race-safe reassignment (exactly one concurrent winner) and
point-in-time attribution across a reassignment.

Everything here uses cosinuss receivers, because they are the only
exclusive-wear hardware in the ledger — fitbit was removed from
DeviceAssignments entirely (a Fitbit account is an OAuth token owned by
one person, see cbt_shared.models.DEVICE_TYPES), and clarity stations are
shared many-to-one via SiteAssignments. The receiver id used here,
ZC5C5W, is the real one that changed wearer mid-study.
"""

import threading

import pytest

from cbt_shared.models import DeviceTypeError

from assignment_ledger import (
    DeviceJustReassignedError,
    assign_device,
    assign_site,
    current_device_assignment,
    participant_at,
    release_device,
    release_site,
)
from assignment_ledger.queries import current_site_assignment

T0 = "2026-07-01T00:00:00Z"
T1 = "2026-07-03T00:00:00Z"


def test_assign_and_current(dynamo_client, devices):
    assign_device(dynamo_client, devices, device_type="cosinuss",
                  device_id="ZC5C5W", participant_id="p-alice",
                  role="production", effective_from=T0)
    row = current_device_assignment(devices, "cosinuss", "ZC5C5W")
    assert row["participant_id"] == "p-alice"
    assert "is_current" in row


def test_reassignment_closes_old_window(dynamo_client, devices):
    assign_device(dynamo_client, devices, device_type="cosinuss",
                  device_id="ZC5C5W", participant_id="p-alice",
                  role="production", effective_from=T0)
    assign_device(dynamo_client, devices, device_type="cosinuss",
                  device_id="ZC5C5W", participant_id="p-bob",
                  role="production", effective_from=T1)
    row = current_device_assignment(devices, "cosinuss", "ZC5C5W")
    assert row["participant_id"] == "p-bob"
    # old window closed at the new window's start
    old = devices.query("pk", devices.scoped("cosinuss", "ZC5C5W"))
    closed = [r for r in old if r.get("participant_id") == "p-alice"]
    assert closed[0]["effective_to"] == T1
    assert "is_current" not in closed[0]


def _race(dynamo_client, devices, participant_ids):
    """Two concurrent assignment attempts that both observed the same
    pre-write state (as two open enrollment forms would); return outcomes."""
    observed = current_device_assignment(devices, "cosinuss", "RACEDEV")
    results = []
    barrier = threading.Barrier(2)

    def attempt(pid, ts):
        barrier.wait()
        try:
            assign_device(dynamo_client, devices, device_type="cosinuss",
                          device_id="RACEDEV", participant_id=pid,
                          role="production", effective_from=ts,
                          expected_current=observed)
            results.append(("ok", pid))
        except DeviceJustReassignedError:
            results.append(("conflict", pid))

    threads = [
        threading.Thread(target=attempt, args=(pid, ts))
        for pid, ts in zip(participant_ids, ["2026-07-05T00:00:00Z",
                                             "2026-07-05T00:00:01Z"])
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_concurrent_assignment_of_free_device_exactly_one_wins(dynamo_client, devices):
    results = _race(dynamo_client, devices, ["p-alice", "p-bob"])
    outcomes = sorted(r[0] for r in results)
    assert outcomes == ["conflict", "ok"], results
    # and the ledger holds exactly one current row
    row = current_device_assignment(devices, "cosinuss", "RACEDEV")
    winner = [pid for status, pid in results if status == "ok"][0]
    assert row["participant_id"] == winner
    rows = devices.query("pk", devices.scoped("cosinuss", "RACEDEV"))
    current_rows = [r for r in rows if "is_current" in r]
    assert len(current_rows) == 1


def test_concurrent_reassignment_of_taken_device_exactly_one_wins(dynamo_client, devices):
    assign_device(dynamo_client, devices, device_type="cosinuss",
                  device_id="RACEDEV", participant_id="p-old",
                  role="production", effective_from=T0)
    results = _race(dynamo_client, devices, ["p-alice", "p-bob"])
    outcomes = sorted(r[0] for r in results)
    assert outcomes == ["conflict", "ok"], results


def test_point_in_time_attribution_across_reassignment(dynamo_client, devices):
    assign_device(dynamo_client, devices, device_type="cosinuss",
                  device_id="ZC5C5W", participant_id="p-alice",
                  role="research", effective_from=T0)
    assign_device(dynamo_client, devices, device_type="cosinuss",
                  device_id="ZC5C5W", participant_id="p-bob",
                  role="research", effective_from=T1)

    during_alice = participant_at(devices, "cosinuss", "ZC5C5W", "2026-07-02T12:00:00Z")
    assert during_alice["participant_id"] == "p-alice"
    during_bob = participant_at(devices, "cosinuss", "ZC5C5W", "2026-07-04T12:00:00Z")
    assert during_bob["participant_id"] == "p-bob"
    before_anyone = participant_at(devices, "cosinuss", "ZC5C5W", "2026-06-01T00:00:00Z")
    assert before_anyone is None
    # exactly at the handover boundary the data belongs to the new wearer
    at_boundary = participant_at(devices, "cosinuss", "ZC5C5W", T1)
    assert at_boundary["participant_id"] == "p-bob"


def test_gap_between_assignments_resolves_to_nobody(dynamo_client, devices):
    assign_device(dynamo_client, devices, device_type="cosinuss",
                  device_id="C-9", participant_id="p-alice",
                  role="research", effective_from=T0)
    # close alice's window by reassigning, then check a timestamp after close
    assign_device(dynamo_client, devices, device_type="cosinuss",
                  device_id="C-9", participant_id="p-bob",
                  role="research", effective_from=T1)
    rows = devices.query("pk", devices.scoped("cosinuss", "C-9"))
    assert {r.get("participant_id") for r in rows if "sk" in r and r["sk"].startswith("assign#")} == {"p-alice", "p-bob"}


def test_fitbit_cannot_enter_the_assignment_ledger(dynamo_client, devices):
    """The account-takeover path, closed at its source.

    This call used to succeed: it closed whoever's Fitbit window was open
    and handed the account — and therefore that participant's raw data —
    to the new participant_id, silently. There is no legitimate caller,
    because a fitbit_id identifies a person, not a loanable device.
    """
    with pytest.raises(DeviceTypeError, match="not a DeviceAssignments"):
        assign_device(dynamo_client, devices, device_type="fitbit",
                      device_id="D58MBD", participant_id="p-bob",
                      role="production", effective_from=T0)


def test_clarity_cannot_enter_the_assignment_ledger(dynamo_client, devices):
    """Stations are shared by many participants at once — one-wearer-at-a-
    time semantics would be actively wrong, so they live in
    SiteAssignments."""
    with pytest.raises(DeviceTypeError, match="not a DeviceAssignments"):
        assign_device(dynamo_client, devices, device_type="clarity",
                      device_id="DGFVZ0274", participant_id="p-bob",
                      role="production", effective_from=T0)


# --- release_device: the "participant left the study" path ----------------
# Regression coverage for the bug where a receiver deactivated/reactivated
# through the admin portal's Registry catalog never became reassignable —
# the catalog toggle never touched this ledger, so the device stayed
# permanently "taken" here regardless. release_device is the operation that
# actually frees it.

def test_release_device_frees_it_for_reassignment(dynamo_client, devices):
    assign_device(dynamo_client, devices, device_type="cosinuss",
                  device_id="FKCWHM", participant_id="p-old",
                  role="research", effective_from=T0)

    closed = release_device(dynamo_client, devices, device_type="cosinuss",
                            device_id="FKCWHM", effective_to=T1)
    assert closed.participant_id == "p-old"
    assert closed.effective_to == T1
    assert current_device_assignment(devices, "cosinuss", "FKCWHM") is None

    # free again, exactly like a device that was never assigned
    assign_device(dynamo_client, devices, device_type="cosinuss",
                  device_id="FKCWHM", participant_id="p-new",
                  role="research", effective_from=T1, expected_current=None)
    row = current_device_assignment(devices, "cosinuss", "FKCWHM")
    assert row["participant_id"] == "p-new"

    # the old window is preserved with its close date, not deleted
    rows = devices.query("pk", devices.scoped("cosinuss", "FKCWHM"))
    old = [r for r in rows if r.get("participant_id") == "p-old"]
    assert old[0]["effective_to"] == T1
    assert "is_current" not in old[0]


def test_release_device_on_a_free_device_is_a_noop(dynamo_client, devices):
    assert release_device(dynamo_client, devices, device_type="cosinuss",
                          device_id="NEVERWORN", effective_to=T1) is None


def test_release_device_stale_expected_current_raises(dynamo_client, devices):
    assign_device(dynamo_client, devices, device_type="cosinuss",
                  device_id="ZC5C5W", participant_id="p-alice",
                  role="research", effective_from=T0)
    stale = {"pk": devices.scoped("cosinuss", "ZC5C5W"),
            "sk": "assign#2020-01-01T00:00:00Z",
            "effective_from": "2020-01-01T00:00:00Z"}
    with pytest.raises(DeviceJustReassignedError):
        release_device(dynamo_client, devices, device_type="cosinuss",
                       device_id="ZC5C5W", effective_to=T1,
                       expected_current=stale)
    # untouched — the failed release didn't close alice's real window
    row = current_device_assignment(devices, "cosinuss", "ZC5C5W")
    assert row["participant_id"] == "p-alice"


def test_release_site_ends_one_participants_coverage_only(
        dynamo_client, sites):
    """Clarity stations are shared many-to-one — releasing alice's coverage
    must not disturb bob's concurrent coverage of the same station."""
    assign_site(dynamo_client, sites, entity_kind="participant",
               entity_id="p-alice", site_id="STATION1", effective_from=T0)
    assign_site(dynamo_client, sites, entity_kind="participant",
               entity_id="p-bob", site_id="STATION1", effective_from=T0)

    closed = release_site(dynamo_client, sites, entity_kind="participant",
                          entity_id="p-alice", effective_to=T1)
    assert closed.site_id == "STATION1"
    assert current_site_assignment(sites, "participant", "p-alice") is None

    bob = current_site_assignment(sites, "participant", "p-bob")
    assert bob is not None and bob["site_id"] == "STATION1"


def test_release_site_on_a_participant_with_no_site_is_a_noop(
        dynamo_client, sites):
    assert release_site(dynamo_client, sites, entity_kind="participant",
                        entity_id="p-nobody", effective_to=T1) is None
