"""Participant registration & enrollment.

Two modes:
  - research:   demographics + consent + Fitbit pick + Cosinuss pick + site
  - production: demographics + consent + Fitbit pick + site (no Cosinuss —
                the field is absent from the flow entirely, not disabled)

Returning-participant handling: every registration collects BOTH the legacy
user_id ("user{number}") and an email. A match on either resolves to the
same existing participant_id; we then open new assignment windows under
that participant instead of creating a duplicate person. A legacy-migrated
record (no email on file) picks up its email here.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Optional

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
)

USER_ID_RE = re.compile(r"^user\d+$")


class ValidationError(Exception):
    pass


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
    site_id: str
    fitbit_id: str
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
    matched_on: Optional[str]  # "user_id" | "email" | "both" | None
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
        raise ValidationError("a Fitbit device pick is required")
    if req.enrollment_mode == "research" and not req.cosinuss_id:
        raise ValidationError("research enrollment requires a Cosinuss device pick")
    if req.enrollment_mode == "production" and req.cosinuss_id:
        # Production flow never presents a Cosinuss field; receiving one
        # means the caller bypassed the form contract.
        raise ValidationError("production enrollment does not accept a Cosinuss device")


def find_participant(participants: ScopedTable, user_id: Optional[str],
                     email: Optional[str]) -> tuple[Optional[dict], Optional[str]]:
    """Resolve an existing participant by user_id OR email.

    Returns (item, matched_on). A hit on either field alone is a full match
    to that participant_id.
    """
    by_user = by_email = None
    if user_id:
        rows = participants.query("user_id_pk", participants.scoped(user_id),
                                  index_name="ByUserId")
        by_user = rows[0] if rows else None
    if email:
        rows = participants.query("email_pk", participants.scoped(email.lower()),
                                  index_name="ByEmail")
        by_email = rows[0] if rows else None
    if by_user and by_email:
        if by_user["participant_id"] != by_email["participant_id"]:
            raise ValidationError(
                "user_id and email belong to two different existing participants — "
                "resolve manually before enrolling"
            )
        return by_user, "both"
    if by_user:
        return by_user, "user_id"
    if by_email:
        return by_email, "email"
    return None, None


def unassigned_devices(device_table: ScopedTable, device_type: str,
                       inventory: list[str]) -> list[str]:
    """Devices from `inventory` with no active assignment (the pick pool)."""
    if device_type not in DEVICE_TYPES:
        raise ValidationError(f"unknown device type {device_type!r}")
    taken = {
        row["device_id"]
        for row in all_current_device_assignments(device_table)
        if row["device_type"] == device_type
    }
    return [d for d in inventory if d not in taken]


def _create_participant(participants: ScopedTable, req: RegistrationRequest,
                        dynamo_client) -> dict:
    height_m = req.height_in * 0.0254
    weight_kg = req.weight_lbs * 0.453592
    p = Participant(
        org_id=participants.org_id,
        participant_id=f"p-{uuid.uuid4().hex[:12]}",
        user_id=req.user_id,
        email=req.email.lower(),
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

    # Idempotency: the participant row's pk is a random participant_id, so a
    # duplicate rapid submission would happily create a second row. Real
    # uniqueness lives in marker items on the natural identifiers, written
    # atomically with the participant record.
    from boto3.dynamodb.types import TypeSerializer

    ser = TypeSerializer()
    serialize = lambda d: {k: ser.serialize(v) for k, v in _dynamo_safe(d).items()}
    markers = [
        {"pk": participants.scoped("uniq", "user_id", req.user_id)},
        {"pk": participants.scoped("uniq", "email", req.email.lower())},
    ]
    try:
        # NB: must be the plain low-level client — a boto3 resource's
        # meta.client applies the document transform and corrupts
        # TransactItems' AttributeValue maps.
        dynamo_client.transact_write_items(TransactItems=[
            {"Put": {"TableName": participants.name, "Item": serialize(item)}},
            *[
                {"Put": {
                    "TableName": participants.name,
                    "Item": serialize(m),
                    "ConditionExpression": "attribute_not_exists(pk)",
                }}
                for m in markers
            ],
        ])
    except ClientError as e:
        if e.response["Error"]["Code"] in ("TransactionCanceledException",
                                           "ConditionalCheckFailedException"):
            raise DuplicateSubmissionError(
                "a registration for this user_id or email was just submitted"
            ) from e
        raise
    return item


def _relink_existing(participants: ScopedTable, existing: dict,
                     req: RegistrationRequest) -> dict:
    """New enrollment period under an existing participant_id. Backfills the
    email on legacy-migrated records and refreshes mutable demographics
    (SCD Type 1 — overwrite in place)."""
    updates = {
        "email": req.email.lower(),
        "email_pk": participants.scoped(req.email.lower()),
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
    # claim the email's uniqueness marker so a racing fresh-create can't
    # take the same email (idempotent overwrite: same person, same marker)
    participants.put_item({"pk": participants.scoped("uniq", "email", req.email.lower())})
    expr = "SET " + ", ".join(f"#a{i} = :v{i}" for i in range(len(updates)))
    names = {f"#a{i}": k for i, k in enumerate(updates)}
    values = {f":v{i}": v for i, v in enumerate(updates.values())}
    participants.update_item(
        key={"pk": existing["pk"]},
        UpdateExpression=expr,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )
    return {**existing, **updates}


def register(dynamo_client, participants: ScopedTable, device_table: ScopedTable,
             site_table: ScopedTable, req: RegistrationRequest) -> RegistrationResult:
    """Full enrollment: resolve/create the person, then open device + site
    assignment windows through the race-safe ledger write path."""
    _validate(req)

    existing, matched_on = find_participant(participants, req.user_id, req.email)
    if existing is not None:
        record = _relink_existing(participants, existing, req)
        created = False
    else:
        record = _create_participant(participants, req, dynamo_client)
        created = True
    pid = record["participant_id"]

    assignments = [
        assign_device(dynamo_client, device_table, device_type="fitbit",
                      device_id=req.fitbit_id, participant_id=pid,
                      role=req.enrollment_mode, effective_from=req.effective_from)
    ]
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
        device_assignments=assignments,
    )
