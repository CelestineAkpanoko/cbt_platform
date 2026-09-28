"""DoD: both enrollment modes work, duplicate rapid submissions rejected,
returning participants relinked, legacy migration (Section 3b) — and a
participant can never be enrolled onto someone else's Fitbit account.

The identity model these tests pin down: fitbit_id is PROVEN (only the
account holder can complete OAuth), while user_id and email are typed and
therefore merely claimed. A claimed identifier must never override a
proven one.
"""

import pytest

from botocore.exceptions import ClientError

from registration_service import (
    DuplicateSubmissionError,
    FitbitAccountInUseError,
    IdentityConflictError,
    RegistrationRequest,
    ValidationError,
    find_participant,
    migrate_legacy_users,
    register,
    unassigned_devices,
)
from assignment_ledger import assignments_for_participant, participant_by_fitbit_id

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


# --- enrollment shape --------------------------------------------------


def test_research_enrollment_binds_fitbit_and_assigns_cosinuss_and_site(
        dynamo_client, participants, devices, sites):
    result = register(dynamo_client, participants, devices, sites, _req())
    assert result.created_new_participant

    # The Fitbit account is NOT an assignment window — it is bound to the
    # person. Only the shared in-ear receiver is a ledger assignment.
    kinds = {(a.device_type, a.device_id) for a in result.device_assignments}
    assert kinds == {("cosinuss", "ZC5C5W")}

    person = participants.get_item("pk", participants.scoped(result.participant_id))
    assert person["fitbit_id"] == "D58MBD"
    assert person["identity_source"] == "native"
    assert float(person["bmi"]) == pytest.approx(24.2, abs=0.1)
    assert int(person["age"]) == 34  # NIOSH HR check needs this downstream


def test_fitbit_id_is_indexed_for_direct_lookup(
        dynamo_client, participants, devices, sites):
    result = register(dynamo_client, participants, devices, sites, _req())
    found = participant_by_fitbit_id(participants, "D58MBD")
    assert found["participant_id"] == result.participant_id


def test_production_enrollment_has_no_cosinuss(dynamo_client, participants,
                                               devices, sites):
    result = register(dynamo_client, participants, devices, sites,
                      _req(enrollment_mode="production", cosinuss_id=None))
    assert result.device_assignments == []
    assert result.fitbit_id == "D58MBD"
    # the production flow must not even accept a cosinuss pick
    with pytest.raises(ValidationError):
        register(dynamo_client, participants, devices, sites,
                 _req(user_id="user16", email="b@example.com",
                      fitbit_id="D2", enrollment_mode="production",
                      cosinuss_id="ZC5C5W"))


def test_a_connected_fitbit_is_required(dynamo_client, participants,
                                        devices, sites):
    with pytest.raises(ValidationError, match="connected Fitbit"):
        register(dynamo_client, participants, devices, sites, _req(fitbit_id=""))


def test_age_is_required_for_new_enrollments(dynamo_client, participants,
                                             devices, sites):
    with pytest.raises(ValidationError):
        register(dynamo_client, participants, devices, sites, _req(age=None))
    with pytest.raises(ValidationError):
        register(dynamo_client, participants, devices, sites, _req(age=11))


# --- account-takeover guardrails ---------------------------------------


def test_cannot_enroll_a_second_person_on_an_already_linked_fitbit(
        dynamo_client, participants, devices, sites):
    """The failure this refactor exists to prevent.

    Under the old model this succeeded silently: the Fitbit's assignment
    window was closed and reopened under the second participant, moving
    that account's raw data — and every prediction keyed off it — from
    the first person to the second, with no error anywhere.
    """
    first = register(dynamo_client, participants, devices, sites, _req())

    with pytest.raises(FitbitAccountInUseError) as exc:
        register(dynamo_client, participants, devices, sites,
                 _req(user_id="user16", email="bob@example.com",
                      cosinuss_id="9G27HD"))  # same fitbit_id
    assert exc.value.owner_user_id == "user15"
    assert exc.value.owner_participant_id == first.participant_id

    # and nothing was written for the would-be second participant
    assert participants.query("user_id_pk", participants.scoped("user16"),
                              index_name="ByUserId") == []


def test_cannot_register_a_linked_fitbit_under_a_brand_new_identity(
        dynamo_client, participants, devices, sites):
    """No existing record contradicts the typed identifiers — they are
    simply new. Accepting this would fork one person into two records, or
    attach a stranger to someone else's Fitbit data."""
    register(dynamo_client, participants, devices, sites, _req())
    with pytest.raises(FitbitAccountInUseError, match="already enrolled as user15"):
        register(dynamo_client, participants, devices, sites,
                 _req(user_id="user20", email="new@example.com",
                      cosinuss_id="9G27HD"))


def test_cannot_change_the_email_on_a_linked_fitbit_by_re_registering(
        dynamo_client, participants, devices, sites):
    register(dynamo_client, participants, devices, sites, _req())
    with pytest.raises(FitbitAccountInUseError, match="different email"):
        register(dynamo_client, participants, devices, sites,
                 _req(email="someone.else@example.com", cosinuss_id="9G27HD"))


def test_someone_elses_user_id_with_your_own_fitbit_is_refused(
        dynamo_client, participants, devices, sites):
    """Typing another participant's user_id while connected to your own
    Fitbit account. The proven identifier says one person, the claimed one
    says another — refuse rather than pick."""
    register(dynamo_client, participants, devices, sites, _req())
    register(dynamo_client, participants, devices, sites,
             _req(user_id="user16", email="bob@example.com",
                  fitbit_id="D2", cosinuss_id="9G27HD"))
    with pytest.raises(FitbitAccountInUseError):
        # bob's Fitbit, alice's user_id
        register(dynamo_client, participants, devices, sites,
                 _req(user_id="user15", email="bob@example.com",
                      fitbit_id="D2", cosinuss_id="GFW7P7"))


def test_same_person_resubmitting_is_idempotent_not_a_conflict(
        dynamo_client, participants, devices, sites):
    first = register(dynamo_client, participants, devices, sites, _req())
    again = register(dynamo_client, participants, devices, sites, _req())
    assert again.participant_id == first.participant_id
    assert not again.created_new_participant
    assert "fitbit_id" in again.matched_on
    rows = participants.query("user_id_pk", participants.scoped("user15"),
                              index_name="ByUserId")
    assert len(rows) == 1


def test_returning_participant_may_connect_a_new_fitbit_account(
        dynamo_client, participants, devices, sites):
    """A replacement watch means a new Fitbit account, which is legitimate
    — the person is identified by their user_id/email, and the new account
    is unclaimed."""
    first = register(dynamo_client, participants, devices, sites, _req())
    again = register(dynamo_client, participants, devices, sites,
                     _req(fitbit_id="DNEW01", effective_from=T1))
    assert again.participant_id == first.participant_id
    person = participants.get_item("pk", participants.scoped(first.participant_id))
    assert person["fitbit_id"] == "DNEW01"
    # the retired account still points at them, so nobody else can claim it
    with pytest.raises(FitbitAccountInUseError):
        register(dynamo_client, participants, devices, sites,
                 _req(user_id="user16", email="bob@example.com",
                      fitbit_id="D58MBD", cosinuss_id="9G27HD"))


# --- returning participants --------------------------------------------


def test_returning_participant_matches_on_user_id_or_email(
        dynamo_client, participants, devices, sites):
    first = register(dynamo_client, participants, devices, sites, _req())

    by_user, how = find_participant(participants, "user15", "unknown@x.com")
    assert by_user["participant_id"] == first.participant_id and how == "user_id"
    by_email, how = find_participant(participants, "user999", "alice@example.com")
    assert by_email["participant_id"] == first.participant_id and how == "email"
    by_fitbit, how = find_participant(participants, None, None, "D58MBD")
    assert by_fitbit["participant_id"] == first.participant_id and how == "fitbit_id"


def test_re_registration_opens_a_new_cosinuss_window_for_the_same_person(
        dynamo_client, participants, devices, sites):
    first = register(dynamo_client, participants, devices, sites, _req())
    result2 = register(dynamo_client, participants, devices, sites,
                       _req(cosinuss_id="9G27HD", effective_from=T1))
    assert result2.participant_id == first.participant_id
    windows = assignments_for_participant(devices, first.participant_id)
    # only receivers — the Fitbit account was never a window
    assert {w["device_id"] for w in windows} == {"ZC5C5W", "9G27HD"}


def test_unknown_identifiers_create_a_fresh_participant(
        dynamo_client, participants, devices, sites):
    r1 = register(dynamo_client, participants, devices, sites, _req())
    r2 = register(dynamo_client, participants, devices, sites,
                  _req(user_id="user77", email="carol@example.com",
                       fitbit_id="D2", cosinuss_id="9G27HD"))
    assert r2.created_new_participant
    assert r2.participant_id != r1.participant_id


def test_user_id_and_email_pointing_at_two_people_is_refused(
        dynamo_client, participants, devices, sites):
    register(dynamo_client, participants, devices, sites, _req())
    register(dynamo_client, participants, devices, sites,
             _req(user_id="user16", email="bob@example.com",
                  fitbit_id="D2", cosinuss_id="9G27HD"))
    with pytest.raises(IdentityConflictError):
        # user15's id + bob's email — two different people
        find_participant(participants, "user15", "bob@example.com")


def test_conditional_write_blocks_double_create(dynamo_client, participants):
    """The raw idempotency guard: same user_id put twice, second one fails."""
    from registration_service.service import _create_participant
    _create_participant(participants, _req(), dynamo_client)
    with pytest.raises((DuplicateSubmissionError, FitbitAccountInUseError)):
        _create_participant(participants, _req(email="other@example.com"),
                            dynamo_client)


def test_unassigned_pool_excludes_taken_receivers(dynamo_client, participants,
                                                  devices, sites):
    register(dynamo_client, participants, devices, sites, _req())
    pool = unassigned_devices(devices, "cosinuss", ["ZC5C5W", "9G27HD", "GFW7P7"])
    assert pool == ["9G27HD", "GFW7P7"]


# ---- Section 3b: legacy migration -----------------------------------------

# Mirrors the real schema_version 2 export: keyed by user_id, a "_meta"
# entry to skip, nested nullable demographics/baselines, race as a list.
LEGACY_USERS_JSON = {
    "_meta": {"schema_version": 2},
    "user1": {
        "user_id": "user1", "display_name": "user1",
        "fitbit_id": "D58MBD", "cosinuss_id": None,
        "demographics": {"age": 40, "sex": "male", "height_in": 70,
                        "weight_lbs": 180, "bmi": 25.8, "race": ["Black"]},
        "baselines": None,
    },
    "user2": {
        "user_id": "user2", "display_name": "user2",
        "fitbit_id": "DAAAAA", "cosinuss_id": "COSIN2",
        "demographics": {"age": 29, "sex": "female", "height_in": 64,
                        "weight_lbs": 130, "bmi": 22.3, "race": ["Asian"]},
        "baselines": {"resting_hr": 61.4, "baseline_skin": 33.0},
    },
    "user3": {
        # demographics/baselines not yet collected at export time
        "user_id": "user3", "display_name": "user3",
        "fitbit_id": "D3", "cosinuss_id": None,
        "demographics": None, "baselines": None,
    },
}


def test_legacy_migration_flags_and_is_idempotent(participants):
    migrated = migrate_legacy_users(participants, LEGACY_USERS_JSON)
    assert sorted(migrated) == ["user1", "user2", "user3"]  # _meta skipped
    row = participants.query("user_id_pk", participants.scoped("user1"),
                             index_name="ByUserId")[0]
    assert row["identity_source"] == "legacy_migrated"
    assert row["race"] == "Black"  # list flattened to a string
    assert "email" not in row and "email_pk" not in row  # no synthetic email

    with_baseline = participants.query("user_id_pk", participants.scoped("user2"),
                                       index_name="ByUserId")[0]
    assert float(with_baseline["current_baseline"]["resting_hr"]) == pytest.approx(61.4)
    assert float(with_baseline["current_baseline"]["baseline_skin_temp"]) == pytest.approx(33.0)

    no_demographics = participants.query("user_id_pk", participants.scoped("user3"),
                                         index_name="ByUserId")[0]
    assert no_demographics["sex"] == "unknown"
    assert "current_baseline" not in no_demographics

    # second run is a no-op
    assert migrate_legacy_users(participants, LEGACY_USERS_JSON) == []


def test_legacy_migration_binds_the_fitbit_account_directly(participants):
    """users.json already recorded each person's Fitbit account. It binds
    onto the participant with no assignment window and no backfill step —
    the runbook's device-reassignment chore now covers cosinuss only."""
    migrate_legacy_users(participants, LEGACY_USERS_JSON)
    found = participant_by_fitbit_id(participants, "D58MBD")
    assert found["user_id"] == "user1"


def test_legacy_participant_reregistering_updates_not_duplicates(
        dynamo_client, participants, devices, sites):
    """Section 3b test: legacy record + new registration with that user_id
    and a new email → same participant, email attached."""
    migrate_legacy_users(participants, LEGACY_USERS_JSON)
    legacy = participants.query("user_id_pk", participants.scoped("user1"),
                                index_name="ByUserId")[0]

    result = register(dynamo_client, participants, devices, sites,
                      _req(user_id="user1", email="dave@example.com"))
    assert not result.created_new_participant
    assert result.participant_id == legacy["participant_id"]
    # matched on both the legacy user_id and the Fitbit account already on
    # file for them
    assert "user_id" in result.matched_on and "fitbit_id" in result.matched_on

    updated = participants.query("user_id_pk", participants.scoped("user1"),
                                 index_name="ByUserId")
    assert len(updated) == 1
    assert updated[0]["email"] == "dave@example.com"
    assert updated[0]["identity_source"] == "legacy_migrated_email_attached"
    # email lookup now resolves too
    by_email, _ = find_participant(participants, None, "dave@example.com")
    assert by_email["participant_id"] == legacy["participant_id"]


def test_legacy_participant_cannot_be_hijacked_via_their_migrated_fitbit(
        dynamo_client, participants, devices, sites):
    """The migrated fitbit_id is a real binding, not a placeholder — a
    stranger connecting that account is refused exactly like a natively
    enrolled one."""
    migrate_legacy_users(participants, LEGACY_USERS_JSON)
    with pytest.raises(FitbitAccountInUseError, match="user1"):
        register(dynamo_client, participants, devices, sites,
                 _req(user_id="user88", email="mallory@example.com",
                      fitbit_id="D58MBD"))
