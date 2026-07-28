"""Participant registration & enrollment.

Two modes:
  - research:   demographics + consent + Cosinuss pick + Clarity site
  - production: demographics + consent + Clarity site (no Cosinuss — the
                field is absent from the flow entirely, not disabled)

Identity — three natural identifiers, one of them proven
--------------------------------------------------------
Every registration carries a legacy user_id ("user{number}"), an email,
and a fitbit_id. They are NOT equal in weight:

  fitbit_id  PROVEN. Fitbit only hands out this account id after the person
             completes the OAuth flow, so possession of it demonstrates
             control of that Fitbit account. It is minted against one
             email and belongs to exactly one person forever — it is not a
             device on loan, and it is stored on the participant record
             (never as a DeviceAssignment window).
  user_id    CLAIMED. Typed into the form; anyone can type anyone's.
  email      CLAIMED. Same.

So the rule is: **a claimed identifier may never override a proven one.**
If the connected Fitbit account already belongs to a participant, the
registration must resolve to *that* participant or be refused. This is
what stops one person enrolling under another person's account — either
by typing someone else's user_id, or (the far likelier accident) by
completing OAuth in a browser still logged into a previous participant's
Fitbit, which Fitbit silently re-approves without a login prompt.

Returning participants: a match on any identifier resolves to the existing
participant_id and we open new assignment windows under it rather than
creating a duplicate person. A legacy-migrated record (no email, no
fitbit_id on file) picks both up here.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Optional

from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from cbt_shared.models import (
    DEVICE_TYPES,
    ENROLLMENT_MODES,
    Participant,
)
from cbt_shared.tenancy import ScopedTable, _dynamo_safe

from assignment_ledger import (
    assign_device,
    assign_site,
    all_current_device_assignments,
    participant_by_fitbit_id,
)

USER_ID_RE = re.compile(r"^user\d+$")

_SERIALIZER = TypeSerializer()


def _serialize(d: dict) -> dict:
    """dict -> low-level DynamoDB AttributeValue map.

    NB: transactions must use the plain low-level client — a boto3
    resource's meta.client applies the document transform and corrupts
    TransactItems' AttributeValue maps.
    """
    return {k: _SERIALIZER.serialize(v) for k, v in _dynamo_safe(d).items()}


class ValidationError(Exception):
    pass


class IdentityConflictError(ValidationError):
    """The identifiers on this submission point at two different people.

    Subclasses ValidationError so existing `except ValidationError`
    handlers keep working, but callers (the enrollment form) catch it
    separately: this is a "stop, someone is about to be enrolled under the
    wrong account" message, not a "you mistyped a field" message.
    """


class FitbitAccountInUseError(IdentityConflictError):
    """The connected Fitbit account is already enrolled to someone else.

    Carries the owning participant's user_id so the form can tell the
    person exactly whose account they are connected to — that is almost
    always the fastest way for them to realise the browser reused a
    previous participant's Fitbit session.
    """

    def __init__(self, message: str, owner_user_id: Optional[str] = None,
                 owner_participant_id: Optional[str] = None):
        super().__init__(message)
        self.owner_user_id = owner_user_id
        self.owner_participant_id = owner_participant_id


class DuplicateSubmissionError(Exception):
    """A concurrent/rapid duplicate submission lost the conditional write."""


@dataclass
class RegistrationRequest:
    user_id: str
    email: str
    display_name: str
    sex: str
    height_in: float
    weight_lbs: float
    race: str
    enrollment_mode: str  # research | production
    consent_given: bool
    site_id: str  # IS the Clarity station id covering this participant
    fitbit_id: str  # from the OAuth exchange — proven, never typed
    effective_from: str  # ISO 8601
    cosinuss_id: Optional[str] = None  # research mode only
    # Required for new enrollments (the NIOSH HR check needs it); Optional
    # in the dataclass only so pre-existing callers/tests fail validation,
    # not construction.
    age: Optional[int] = None


@dataclass
class RegistrationResult:
    participant_id: str
    created_new_participant: bool
    # which identifier(s) resolved an existing person: "fitbit_id",
    # "user_id", "email", a "+"-joined combination, or None for a new one
    matched_on: Optional[str]
    fitbit_id: Optional[str] = None
    # Cosinuss windows only — a Fitbit account is not an assignment
    device_assignments: list = field(default_factory=list)


def _validate(req: RegistrationRequest):
    if not USER_ID_RE.match(req.user_id):
        raise ValidationError("user_id must match 'user{number}', e.g. user15")
    if "@" not in (req.email or ""):
        raise ValidationError("a valid email is required")
    if req.enrollment_mode not in ENROLLMENT_MODES:
        raise ValidationError(f"enrollment_mode must be one of {ENROLLMENT_MODES}")
    if not req.consent_given:
        raise ValidationError("consent is required to enroll")
    if req.age is None or not (18 <= int(req.age) <= 100):
        raise ValidationError("age is required (18-100)")
    if not req.site_id:
        raise ValidationError("a site pick is required")
    if not req.fitbit_id:
        # Not a "pick" — it comes from the OAuth exchange. Its absence
        # means the form was submitted without a connected Fitbit account,
        # which would leave the participant with no proven identifier.
        raise ValidationError("a connected Fitbit account is required")
    if req.enrollment_mode == "research" and not req.cosinuss_id:
        raise ValidationError("research enrollment requires a Cosinuss device pick")
    if req.enrollment_mode == "production" and req.cosinuss_id:
        # Production flow never presents a Cosinuss field; receiving one
        # means the caller bypassed the form contract.
        raise ValidationError("production enrollment does not accept a Cosinuss device")


def _fitbit_marker_owner(participants: ScopedTable,
                         fitbit_id: str) -> Optional[str]:
    """participant_id that has ever claimed this Fitbit account, or None.

    Read off the uniqueness marker rather than the ByFitbitId GSI: the
    index tracks each participant's *current* account, so it forgets one
    they replaced, while the marker is a permanent claim. That difference
    is the point — a retired Fitbit account must not become available for
    someone else to enroll under, because its historical raw data is still
    the original participant's.
    """
    marker = participants.get_item(
        "pk", participants.scoped("uniq", "fitbit_id", fitbit_id))
    return (marker or {}).get("participant_id")


def find_participant(participants: ScopedTable, user_id: Optional[str],
                     email: Optional[str],
                     fitbit_id: Optional[str] = None,
                     ) -> tuple[Optional[dict], Optional[str]]:
    """Resolve an existing participant from the submitted identifiers.

    Returns (item, matched_on) where matched_on names the identifier(s)
    that hit: "fitbit_id", "user_id", "email", or a "+"-joined combination.

    Raises IdentityConflictError / FitbitAccountInUseError when the
    identifiers disagree. There is deliberately no "pick the best guess"
    branch: every disagreement here is either a typo or one person about
    to be enrolled onto another person's record, and both want a human to
    look before any window is opened.
    """
    by_user = by_email = by_fitbit = None
    if user_id:
        rows = participants.query("user_id_pk", participants.scoped(user_id),
                                  index_name="ByUserId")
        by_user = rows[0] if rows else None
    if email:
        rows = participants.query("email_pk", participants.scoped(email.lower()),
                                  index_name="ByEmail")
        by_email = rows[0] if rows else None
    if fitbit_id:
        by_fitbit = participant_by_fitbit_id(participants, fitbit_id)

    # --- The proven identifier wins, or nobody enrolls -------------------
    # Checked BEFORE the user_id/email disagreement below, deliberately.
    # When all three are present and they disagree, the Fitbit account is
    # the one fact we can verify, so its message ("this account belongs to
    # X") is both more accurate and more actionable than "your two typed
    # fields disagree".
    if by_fitbit is not None:
        owner_uid = by_fitbit.get("user_id")
        owner_pid = by_fitbit["participant_id"]

        for claimed, label in ((by_user, "User ID"), (by_email, "email")):
            if claimed is not None and claimed["participant_id"] != owner_pid:
                raise FitbitAccountInUseError(
                    f"the connected Fitbit account is already enrolled to "
                    f"{owner_uid}, but the {label} entered belongs to a "
                    f"different participant. Nothing was saved. Connect your "
                    f"own Fitbit account (log out at fitbit.com or use a "
                    f"private window first), or contact the research admin.",
                    owner_user_id=owner_uid, owner_participant_id=owner_pid,
                )

        # No contradicting record exists — but the typed identifiers must
        # still be the ones on file for this Fitbit's owner. Otherwise this
        # is the same Fitbit account being registered under a brand-new
        # identity, which would either fork one person into two records or
        # quietly attach a stranger to someone else's data.
        if user_id and owner_uid and user_id != owner_uid:
            raise FitbitAccountInUseError(
                f"the connected Fitbit account is already enrolled as "
                f"{owner_uid}, so it cannot be registered as {user_id}. "
                f"Nothing was saved. If you are {owner_uid}, re-enter that "
                f"User ID; if this is not your Fitbit account, connect your "
                f"own (log out at fitbit.com or use a private window first).",
                owner_user_id=owner_uid, owner_participant_id=owner_pid,
            )
        on_file_email = by_fitbit.get("email")
        if email and on_file_email and email.lower() != on_file_email.lower():
            raise FitbitAccountInUseError(
                f"the connected Fitbit account is already enrolled as "
                f"{owner_uid} under a different email address. Nothing was "
                f"saved. Re-enter the email this participant enrolled with, "
                f"or contact the research admin to change it.",
                owner_user_id=owner_uid, owner_participant_id=owner_pid,
            )

        matched = "+".join(
            part for part, hit in (("fitbit_id", True),
                                   ("user_id", by_user is not None),
                                   ("email", by_email is not None)) if hit
        )
        return by_fitbit, matched

    # --- No current binding: check for a RETIRED one ---------------------
    # The ByFitbitId index only holds each participant's *current* account,
    # so a participant who replaced their watch leaves their old account
    # unindexed. The uniqueness marker survives, and it is what stops that
    # retired account being claimed by someone else — read it here so the
    # refusal comes with the proper message instead of surfacing later as
    # an opaque transaction failure.
    if fitbit_id:
        owner_pid = _fitbit_marker_owner(participants, fitbit_id)
        resolved_pid = (by_user or by_email or {}).get("participant_id")
        if owner_pid and owner_pid != resolved_pid:
            owner = participants.get_item("pk", participants.scoped(owner_pid))
            owner_uid = (owner or {}).get("user_id")
            raise FitbitAccountInUseError(
                f"the connected Fitbit account was previously enrolled to "
                f"{owner_uid} and stays reserved to them. Nothing was saved. "
                f"Connect your own Fitbit account, or contact the research "
                f"admin if you believe this account is yours.",
                owner_user_id=owner_uid, owner_participant_id=owner_pid,
            )

    if by_user and by_email and by_user["participant_id"] != by_email["participant_id"]:
        raise IdentityConflictError(
            "user_id and email belong to two different existing participants — "
            "resolve manually before enrolling"
        )

    # --- No Fitbit binding yet: claimed identifiers resolve as before ----
    if by_user and by_email:
        return by_user, "user_id+email"
    if by_user:
        return by_user, "user_id"
    if by_email:
        return by_email, "email"
    return None, None


def unassigned_devices(device_table: ScopedTable, device_type: str,
                       inventory: list[str]) -> list[str]:
    """Devices from `inventory` with no active wearer (the enrollment pick
    pool).

    Only meaningful for exclusive-wear hardware — DEVICE_TYPES, i.e.
    Cosinuss. Fitbit accounts are not a pool (one per person, captured via
    OAuth) and Clarity stations are shared by many participants at once, so
    neither has an "unassigned" state; asking for either is a bug, not an
    empty list.
    """
    if device_type not in DEVICE_TYPES:
        raise ValidationError(
            f"{device_type!r} has no assignment pool. Valid pooled device "
            f"types: {DEVICE_TYPES}. Fitbit accounts come from OAuth "
            f"(Participant.fitbit_id); Clarity stations are shared "
            f"(SiteAssignments) — use the station inventory instead."
        )
    taken = {
        row["device_id"]
        for row in all_current_device_assignments(device_table)
        if row["device_type"] == device_type
    }
    return [d for d in inventory if d not in taken]


def _identity_markers(participants: ScopedTable, participant_id: str,
                      *, user_id: Optional[str] = None,
                      email: Optional[str] = None,
                      fitbit_id: Optional[str] = None) -> list[dict]:
    """TransactItems that claim the natural identifiers for one person.

    Each marker item carries the participant_id that owns it, so the
    condition is "unclaimed, or already mine" — that makes a re-submission
    by the same participant idempotent while still refusing a claim from
    anyone else. The fitbit_id marker is the transactional backstop behind
    find_participant()'s ByFitbitId read: the index is eventually
    consistent, so two simultaneous enrollments could both see the account
    as free. Exactly one of them commits.
    """
    claims = [("user_id", user_id), ("email", email), ("fitbit_id", fitbit_id)]
    return [
        {"Put": {
            "TableName": participants.name,
            "Item": _serialize({
                "pk": participants.scoped("uniq", kind, value),
                "participant_id": participant_id,
            }),
            "ConditionExpression": (
                "attribute_not_exists(pk) OR participant_id = :pid"
            ),
            "ExpressionAttributeValues": {":pid": {"S": participant_id}},
        }}
        for kind, value in claims if value
    ]


def _create_participant(participants: ScopedTable, req: RegistrationRequest,
                        dynamo_client) -> dict:
    height_m = req.height_in * 0.0254
    weight_kg = req.weight_lbs * 0.453592
    p = Participant(
        org_id=participants.org_id,
        participant_id=f"p-{uuid.uuid4().hex[:12]}",
        user_id=req.user_id,
        email=req.email.lower(),
        fitbit_id=req.fitbit_id,
        display_name=req.display_name,
        sex=req.sex,
        height_in=req.height_in,
        weight_lbs=req.weight_lbs,
        bmi=round(weight_kg / (height_m ** 2), 1),
        race=req.race,
        enrollment_mode=req.enrollment_mode,
        consent_status="consented",
        enrolled_at=req.effective_from,
        identity_source="native",
        age=int(req.age),
    )
    item = p.to_item()
    item["user_id_pk"] = participants.scoped(req.user_id)
    item["email_pk"] = participants.scoped(req.email.lower())
    item["fitbit_id_pk"] = participants.scoped(req.fitbit_id)

    # Idempotency: the participant row's pk is a random participant_id, so a
    # duplicate rapid submission would happily create a second row. Real
    # uniqueness lives in marker items on the natural identifiers, written
    # atomically with the participant record.
    try:
        dynamo_client.transact_write_items(TransactItems=[
            {"Put": {"TableName": participants.name, "Item": _serialize(item)}},
            *_identity_markers(participants, p.participant_id,
                               user_id=req.user_id, email=req.email.lower(),
                               fitbit_id=req.fitbit_id),
        ])
    except ClientError as e:
        if e.response["Error"]["Code"] in ("TransactionCanceledException",
                                           "ConditionalCheckFailedException"):
            # Either a rapid duplicate of this same submission, or another
            # participant holds one of these identifiers. find_participant()
            # already screened for the latter against the GSIs; reaching
            # here means the two raced. Re-read to tell them apart, so a
            # racing takeover attempt still gets the security message
            # rather than a benign "already submitted".
            owner_pid = _fitbit_marker_owner(participants, req.fitbit_id)
            if owner_pid:
                owner = participants.get_item(
                    "pk", participants.scoped(owner_pid))
                raise FitbitAccountInUseError(
                    f"the connected Fitbit account is claimed by "
                    f"{(owner or {}).get('user_id')}. Nothing was saved.",
                    owner_user_id=(owner or {}).get("user_id"),
                    owner_participant_id=owner_pid,
                ) from e
            raise DuplicateSubmissionError(
                "a registration for this user_id, email or Fitbit account "
                "was just submitted"
            ) from e
        raise
    return item


def _relink_existing(participants: ScopedTable, existing: dict,
                     req: RegistrationRequest, dynamo_client) -> dict:
    """New enrollment period under an existing participant_id.

    Backfills the email and fitbit_id on legacy-migrated records and
    refreshes mutable demographics (SCD Type 1 — overwrite in place).

    The whole update is one transaction with the identity-marker claims, so
    a participant can only pick up an email or Fitbit account that is
    unclaimed or already theirs. A returning participant who connects a
    *different* Fitbit account keeps their old marker (it still points at
    them), which retires that account rather than releasing it for someone
    else to claim.
    """
    pid = existing["participant_id"]
    updates = {
        "email": req.email.lower(),
        "email_pk": participants.scoped(req.email.lower()),
        "fitbit_id": req.fitbit_id,
        "fitbit_id_pk": participants.scoped(req.fitbit_id),
        "display_name": req.display_name,
        "sex": req.sex,
        "height_in": req.height_in,
        "weight_lbs": req.weight_lbs,
        "race": req.race,
        "enrollment_mode": req.enrollment_mode,
        "consent_status": "consented",
        "age": int(req.age),
    }
    if existing.get("identity_source") == "legacy_migrated":
        updates["identity_source"] = "legacy_migrated_email_attached"

    expr = "SET " + ", ".join(f"#a{i} = :v{i}" for i in range(len(updates)))
    names = {f"#a{i}": k for i, k in enumerate(updates)}
    values = _serialize({f":v{i}": v for i, v in enumerate(updates.values())})
    try:
        dynamo_client.transact_write_items(TransactItems=[
            {"Update": {
                "TableName": participants.name,
                "Key": _serialize({"pk": existing["pk"]}),
                "UpdateExpression": expr,
                "ExpressionAttributeNames": names,
                "ExpressionAttributeValues": values,
            }},
            *_identity_markers(participants, pid,
                               email=req.email.lower(), fitbit_id=req.fitbit_id),
        ])
    except ClientError as e:
        if e.response["Error"]["Code"] in ("TransactionCanceledException",
                                           "ConditionalCheckFailedException"):
            owner_pid = _fitbit_marker_owner(participants, req.fitbit_id)
            if owner_pid and owner_pid != pid:
                owner = participants.get_item(
                    "pk", participants.scoped(owner_pid))
                raise FitbitAccountInUseError(
                    f"the connected Fitbit account belongs to "
                    f"{(owner or {}).get('user_id')}, not to "
                    f"{existing['user_id']}. Nothing was saved.",
                    owner_user_id=(owner or {}).get("user_id"),
                    owner_participant_id=owner_pid,
                ) from e
            raise DuplicateSubmissionError(
                "this email or Fitbit account was just claimed by another "
                "registration"
            ) from e
        raise
    return {**existing, **updates}


def register(dynamo_client, participants: ScopedTable, device_table: ScopedTable,
             site_table: ScopedTable, req: RegistrationRequest) -> RegistrationResult:
    """Full enrollment: resolve/create the person, then open the pooled
    device + site assignment windows through the race-safe ledger path.

    The Fitbit account is bound on the participant record itself (see the
    module docstring), not opened as an assignment window — so
    `device_assignments` contains the Cosinuss window only, and only in
    research mode.
    """
    _validate(req)

    existing, matched_on = find_participant(participants, req.user_id,
                                            req.email, req.fitbit_id)
    if existing is not None:
        record = _relink_existing(participants, existing, req, dynamo_client)
        created = False
    else:
        record = _create_participant(participants, req, dynamo_client)
        created = True
    pid = record["participant_id"]

    assignments = []
    if req.enrollment_mode == "research":
        assignments.append(
            assign_device(dynamo_client, device_table, device_type="cosinuss",
                          device_id=req.cosinuss_id, participant_id=pid,
                          role="research", effective_from=req.effective_from)
        )
    assign_site(dynamo_client, site_table, entity_kind="participant",
                entity_id=pid, site_id=req.site_id,
                effective_from=req.effective_from)

    return RegistrationResult(
        participant_id=pid,
        created_new_participant=created,
        matched_on=matched_on,
        fitbit_id=req.fitbit_id,
        device_assignments=assignments,
    )
