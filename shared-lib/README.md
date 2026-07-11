# shared-lib

Shared types and the tenant-scoping data-access layer used by every
in-scope service.

- `cbt_shared/models.py` — `Participant`, `DeviceAssignment`,
  `SiteAssignment`, `CalibrationRecord`, `POPULATION_BASELINE`. Field names
  are the schema of record (mirrored by `infra/ledger-tables`).
- `cbt_shared/tenancy.py` — `ScopedTable`: all DynamoDB access goes through
  it; every key value must lead with the constructing `org_id` or it raises
  `TenantScopeError`. Enforced repo-wide by `tests/test_tenancy.py`.
