# assignment-ledger

SCD Type 2 device→participant and entity→site ledgers (Section 4).

- `tables.py` — table/GSI specs (spec of record; CFN in `infra/ledger-tables`).
- `writes.py` — the only write path: one `TransactWriteItems` per
  reassignment, conditioned on a per-device `#HEAD` mutex row plus the open
  window, with a deterministic `ClientRequestToken`. Losing a race raises
  `DeviceJustReassignedError` ("refresh and retry") — never a silent retry.
- `queries.py` — current-assignment lookups (sparse `Current` GSI),
  participant history (`ByParticipant`), and the full-history
  point-in-time `participant_at(device, timestamp)` that offline retraining
  will consume.

Tested in `tests/test_ledger.py`, including the two-concurrent-writers race
(exactly one wins) and attribution across a simulated reassignment.
