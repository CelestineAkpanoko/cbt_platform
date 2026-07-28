"""Canonical record shapes shared across all in-scope services.

These mirror the DynamoDB item layouts in docs/data-model.md. Keep field
names in sync with infra/ledger-tables — the materializer's users.json
output shape depends on Participant + DeviceAssignment + SiteAssignment.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

# Served for predictions requested before a participant's first calibration
# completes. Always paired with provisional=True downstream.
POPULATION_BASELINE = {
    "resting_hr": 62.0,
    "baseline_skin_temp": 33.5,
}

# Device types that live in the DeviceAssignments ledger: physically
# scarce hardware handed from one wearer to the next, so "who is wearing
# device X right now" is a real question with a time-varying answer.
#
# Fitbit is deliberately NOT here. A fitbit_id is Fitbit's OAuth account
# identifier — a token minted against one person's email, proven by that
# person completing the OAuth flow. It is not a limited pool, it is never
# handed to the next participant, and modelling it as reassignable created
# a silent account-takeover path (enrolling B with A's already-linked
# Fitbit closed A's window and moved A's data to B). It now lives as
# Participant.fitbit_id, uniqueness-enforced. See docs/data-model.md.
DEVICE_TYPES = ("cosinuss",)

# Clarity environmental stations are also a limited pool, but shared:
# many participants can be covered by one station at the same time. They
# are therefore tracked in SiteAssignments (many-to-one SCD2), not
# DeviceAssignments (one-to-one). site_id IS the Clarity device id.
SITE_DEVICE_TYPE = "clarity"

# Everything with a bounded, enumerable id space that enrollment has to
# pick from — the union of both ledgers' device kinds.
POOLED_DEVICE_TYPES = DEVICE_TYPES + (SITE_DEVICE_TYPE,)

ENROLLMENT_MODES = ("research", "production")


class DeviceTypeError(ValueError):
    """A device type was used with the wrong ledger."""


@dataclass
class Participant:
    """SCD Type 1 person-level record. Overwritten in place; the durable
    identity is participant_id, never a device serial.

    consent_status: a consent-withdrawal / right-to-erasure pipeline is out
    of scope for this phase and needs legal review before a deletion
    workflow is built. The field exists so that workflow has somewhere to
    hang off later.
    """

    org_id: str
    participant_id: str
    user_id: str  # legacy-compatible "user{number}" identifier
    email: Optional[str]
    display_name: str
    # Fitbit's OAuth account id, captured from the OAuth exchange — the
    # third natural identifier for a participant, alongside user_id and
    # email, and the strongest of the three because holding it proves the
    # person controls that Fitbit account. Unique per participant
    # (org_id#uniq#fitbit_id#<id> marker + ByFitbitId GSI). Optional
    # because legacy-migrated rows predate it and because a participant
    # can exist before connecting a Fitbit.
    fitbit_id: Optional[str]
    sex: str
    height_in: float
    weight_lbs: float
    bmi: float
    race: str
    enrollment_mode: str  # research | production
    consent_status: str
    enrolled_at: str  # ISO 8601
    identity_source: str  # "legacy_migrated" | "native"
    # Needed by the feedback layer's NIOSH heart-rate check
    # (fitbit_hr > 180 - age). Optional because pre-2026-07 records were
    # created before the field existed; ops/backfill_participant_age.py
    # backfills those from the legacy users.json demographics.
    age: Optional[int] = None
    # Fast-read cache of the most recent completed calibration. Source of
    # truth is the CalibrationHistory log, not this field.
    current_baseline: Optional[dict] = None

    def pk(self) -> str:
        return f"{self.org_id}#{self.participant_id}"

    def to_item(self) -> dict:
        item = {k: v for k, v in asdict(self).items() if v is not None}
        item["pk"] = self.pk()
        # Hash key of the ByOrg GSI. Constant per org, so "every
        # participant in this org" is one indexed query rather than a
        # Scan — and unlike a Scan it still carries the org_id, which is
        # what tests/test_tenancy.py enforces. Uniqueness-marker items
        # (pk = org#uniq#...) deliberately omit it, so the index holds
        # exactly the person records.
        item["org_pk"] = f"{self.org_id}#participant"
        return item


@dataclass
class DeviceAssignment:
    """SCD Type 2 row: one wearer per device at any point in time.

    Only DEVICE_TYPES belong here — exclusive, physically-shared hardware.
    Passing "fitbit" raises: a Fitbit account is a participant attribute,
    not a device on loan (see DEVICE_TYPES). Passing "clarity" raises too:
    stations are shared by many participants at once, so they live in
    SiteAssignment.

    effective_from/effective_to are *valid time* only. Transaction time
    (recorded_at bitemporal tracking) is deliberately deferred — see
    docs/data-model.md "Known limitations".
    """

    org_id: str
    device_type: str  # cosinuss (the only exclusive-wear device type)
    device_id: str
    participant_id: str
    role: str  # research | production
    effective_from: str  # ISO 8601
    effective_to: Optional[str] = None  # None = current
    is_current: Optional[bool] = None  # present only on the current row (sparse GSI)

    def __post_init__(self):
        if self.device_type == "fitbit":
            raise DeviceTypeError(
                "fitbit is not a ledger device — a Fitbit account id is "
                "Participant.fitbit_id (unique per person), not an "
                "assignment window. See docs/data-model.md."
            )
        if self.device_type == SITE_DEVICE_TYPE:
            raise DeviceTypeError(
                "clarity stations are shared by many participants at once — "
                "record them as a SiteAssignment, not a DeviceAssignment."
            )
        if self.device_type not in DEVICE_TYPES:
            raise DeviceTypeError(
                f"unknown device type {self.device_type!r}; expected one of "
                f"{DEVICE_TYPES}"
            )

    def pk(self) -> str:
        return f"{self.org_id}#{self.device_type}#{self.device_id}"

    def sk(self) -> str:
        return f"assign#{self.effective_from}"

    def to_item(self) -> dict:
        item = {
            "pk": self.pk(),
            "sk": self.sk(),
            "org_id": self.org_id,
            "device_type": self.device_type,
            "device_id": self.device_id,
            "participant_id": self.participant_id,
            "role": self.role,
            "effective_from": self.effective_from,
            "participant_pk": f"{self.org_id}#{self.participant_id}",
        }
        if self.effective_to is not None:
            item["effective_to"] = self.effective_to
        if self.is_current:
            # Sparse-GSI hash key. Holds the org_id (not a boolean) so the
            # current-view GSI is itself tenant-partitioned.
            item["is_current"] = self.org_id
        return item


@dataclass
class SiteAssignment:
    """SCD Type 2 row linking a participant to a site.

    site_id IS the currently-active Clarity device id — the research admin
    types it directly into the enrollment form (no separate station-to-site
    admin workflow, by design). Many participants can share one site_id
    concurrently (many-to-one). entity_kind is currently always
    "participant"; kept generic rather than dropped in case a future phase
    needs a second entity kind, but nothing constructs any other value today.
    """

    org_id: str
    entity_kind: str  # participant (only value in use)
    entity_id: str  # participant_id
    site_id: str
    effective_from: str
    effective_to: Optional[str] = None
    is_current: Optional[bool] = None

    def pk(self) -> str:
        return f"{self.org_id}#{self.entity_kind}#{self.entity_id}"

    def sk(self) -> str:
        return f"assign#{self.effective_from}"

    def to_item(self) -> dict:
        item = {
            "pk": self.pk(),
            "sk": self.sk(),
            "org_id": self.org_id,
            "entity_kind": self.entity_kind,
            "entity_id": self.entity_id,
            "site_id": self.site_id,
            "effective_from": self.effective_from,
            "site_pk": f"{self.org_id}#site#{self.site_id}",
        }
        if self.effective_to is not None:
            item["effective_to"] = self.effective_to
        if self.is_current:
            item["is_current"] = self.org_id  # sparse-GSI hash; see DeviceAssignment
        return item


@dataclass
class CalibrationRecord:
    """Append-only CalibrationHistory entry. Never overwritten; the
    Participants.current_baseline cache points at the newest completed one.
    """

    org_id: str
    participant_id: str
    computed_at: str  # ISO 8601 — sort key
    device_type: str
    device_id: str
    assignment_effective_from: str
    algorithm_version: str
    window_start: str
    window_end: str
    qualifying_sample_count: int
    resting_hr: Optional[float]
    baseline_skin_temp: Optional[float]
    calibration_status: str  # pending | complete | extended
    provisional: bool
    consent_status: str = "consented"  # see Participant.consent_status note

    def pk(self) -> str:
        return f"{self.org_id}#{self.participant_id}"

    def to_item(self) -> dict:
        item = {k: v for k, v in asdict(self).items() if v is not None}
        item["pk"] = self.pk()
        return item
