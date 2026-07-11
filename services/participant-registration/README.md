# participant-registration

Enrollment (Section 3) + legacy migration (Section 3b).

- `registration_service/service.py` — `register()`: dual-identifier match
  (`user_id` OR `email` → same existing `participant_id`), transactional
  create with uniqueness markers (duplicate rapid submissions create at
  most one person), then device/site windows via the ledger write path.
  Production mode has no Cosinuss field anywhere in the flow.
- `registration_service/migration.py` — one-time import of the legacy
  `users.json` roster, flagged `identity_source: legacy_migrated`, no
  synthetic emails; run before general rollout (see docs/runbook.md).
- `registration_service/s3_inventory.py` — device dropdowns derived live
  from the raw sensor bucket's prefixes (`fitbit/raw/<id>/`,
  `cosinuss/raw/<id>/`, `clarity/raw/<date>/<id>_*.json`) — the bucket IS
  the inventory, nothing to maintain. fitbit_id needs no pool: it's
  auto-captured from the OAuth step (same id the raw data lands under).
- `streamlit_app.py` — Streamlit UI: Fitbit OAuth connection + registration form in
  one flow (thin; all business logic is in the tested service).
- `api_handler.py` — Lambda for scripted/bulk enrollment (`POST /register`).
