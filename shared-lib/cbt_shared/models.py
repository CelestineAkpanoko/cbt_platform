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

DEVICE_TYPES = ("fitbit", "cosinuss")
ENROLLMENT_MODES = ("research", "production")


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
    sex: str
    height_in: float
    weight_lbs: float
    bmi: float
    race: str
    enrollment_mode: str  # research | production
    consent_status: str
    enrolled_at: str  # ISO 8601
    identity_source: str  # "legacy_migrated" | "native"
    # Fast-read cache of the most recent completed calibration. Source of
    # truth is the CalibrationHistory log, not this field.
    current_baseline: Optional[dict] = None

    def pk(self) -> str:
        return f"{self.org_id}#{self.participant_id}"

    def to_item(self) -> dict:
        item = {k: v for k, v in asdict(self).items() if v is not None}
        item["pk"] = self.pk()
        return item


@dataclass
class DeviceAssignment:
    """SCD Type 2 row: one wearer per device at any point in time.

    effective_from/effective_to are *valid time* only. Transaction time
    (recorded_at bitemporal tracking) is deliberately deferred — see
    docs/data-model.md "Known limitations".
    """

    org_id: str
    device_type: str  # fitbit | cosinuss
    device_id: str
    participant_id: str
    role: str  # research | production
    effective_from: str  # ISO 8601
    effective_to: Optional[str] = None  # None = current
    is_current: Optional[bool] = None  # present only on the current row (sparse GSI)

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
