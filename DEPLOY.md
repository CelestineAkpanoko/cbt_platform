# Phase 1 — Deployment Guide

Full path from empty AWS account state to testing with real, previously
signed-in participants. Follow in order — each step depends on the last.

---

## 0. Prerequisites checklist

- [ ] AWS CLI v2 configured (`aws sts get-caller-identity` works) with an
      IAM principal that can create DynamoDB tables, Lambda functions,
      IAM roles, API Gateway, EventBridge rules, and CloudWatch alarms.
- [ ] AWS SAM CLI installed (`sam --version`) — all templates in `infra/`
      are SAM templates.
- [ ] Python 3.12 available locally (matches the Lambda runtime) — needed
      for `sam build` to produce compatible layer/function artifacts.
- [ ] You know the **name of the existing raw bucket**
      (`raw-data-all-sensors-*`) and the **existing config bucket**
      (`users-heat-stress`) that `heat-stress-predict` reads from today.
      Every command below assumes you'll substitute the real names.
- [ ] You have (or can export) the current `users.json` / legacy roster —
      needed for the migration step so existing participants aren't
      treated as new people.
- [ ] Decide your `org_id` for now. If you only have one organization,
      use `org1` (the default baked into every template) — it's just a
      partition-key prefix, not a real AWS concept, so this costs nothing
      to get "wrong" early; it only needs to be consistent across every
      stack parameter below.

---

## 1. Run the test suite locally (sanity check before touching AWS)

```bash
cd /Users/ca/najma/cbt_platform
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```
Expect `33 passed`. If this doesn't pass, nothing below will work correctly
either — stop and fix first.

---

## 2. Build the shared Lambda layer

None of `cbt_shared`, `assignment_ledger`, `registration_service`,
`calibration_service`, `ingestion_resolver`, or `materializer` are PyPI
packages — every Lambda function needs them bundled as a Layer.

```bash
./infra/shared/build_layer.sh
```
This writes `infra/shared/layer/python/...` (the directory each SAM
template's `SharedLayer` resource points at via `ContentUri: ../shared/layer`)
and `dist/cbt-shared-layer.zip` (only needed if you ever deploy a function
outside SAM). Re-run this any time you change code in those packages —
`sam deploy` will not pick up source edits otherwise.

---

## 3. Deploy the ledger tables

```bash
cd infra/ledger-tables
sam deploy --template-file tables.yaml \
  --stack-name cbt-ledger-tables \
  --capabilities CAPABILITY_IAM \
  --no-confirm-changeset
```
This creates `Participants`, `DeviceAssignments`, `SiteAssignments`,
`CalibrationHistory` with the GSIs and DynamoDB Streams described in
`docs/data-model.md`.

**Export the stream ARNs** — later stacks (`materialize`, `calibration`)
import them by name (`!ImportValue DeviceAssignmentsStreamArn` /
`SiteAssignmentsStreamArn`). `tables.yaml` as written does not yet export
them, so add this before deploying, or deploy once and patch:

```yaml
# add to infra/ledger-tables/tables.yaml, under each table resource
Outputs:
  DeviceAssignmentsStreamArn:
    Value: !GetAtt DeviceAssignmentsTable.StreamArn
    Export: {Name: DeviceAssignmentsStreamArn}
  SiteAssignmentsStreamArn:
    Value: !GetAtt SiteAssignmentsTable.StreamArn
    Export: {Name: SiteAssignmentsStreamArn}
```
Redeploy after adding this (`sam deploy` again — DynamoDB tables aren't
replaced by adding Outputs, this is a safe no-downtime update).

**Verify:**
```bash
aws dynamodb list-tables | grep -E "Participants|DeviceAssignments|SiteAssignments|CalibrationHistory"
aws cloudformation describe-stacks --stack-name cbt-ledger-tables \
  --query "Stacks[0].Outputs"
```

---

## 4. Run the legacy migration (before anything else touches the tables)

This is the step that matters for testing with real, previously-signed-in
users — it imports every existing `user{number}` participant from the
current `users.json` so re-registration/device reassignment recognizes
them instead of creating duplicates.

```bash
aws s3 cp s3://<existing-config-bucket>/users.json ./legacy-users.json

.venv/bin/python -m registration_service.migration ./legacy-users.json \
  --org org1
# optional, if you have emails from an external roster:
#   --roster ./email-roster.json   (JSON: {"user1": "a@x.com", ...})
```
(Run this with `PYTHONPATH` set so imports resolve, or from repo root:)
```bash
PYTHONPATH=shared-lib:services/assignment-ledger:services/participant-registration \
  .venv/bin/python -m registration_service.migration ./legacy-users.json --org org1
```

**Verify every legacy user landed:**
```bash
aws dynamodb scan --table-name Participants \
  --filter-expression "identity_source = :v" \
  --expression-attribute-values '{":v":{"S":"legacy_migrated"}}' \
  --select COUNT
```
Compare the count to the number of entries in your original `users.json`.

⚠️ This step only creates **Participant** records — it does not create
`DeviceAssignments` rows, because the legacy file doesn't carry effective
dates. Real users won't show up in the materialized `users.json` (and
won't get predictions) until each one goes through registration again
(Section 3b's intended flow) to open a device-assignment window. Decide
now: either (a) have each returning participant re-register once through
the form/API with their existing `user_id`, which the matching logic will
correctly relink to the migrated record, or (b) if you have the historical
device↔participant↔date mapping in hand, script a one-time batch of
`assign_device()` calls with the correct `effective_from` dates for each.
Option (a) is simpler and is what Section 3b was designed for.

---

## 5. Deploy the registration API

```bash
cd infra/registration-api
sam build --use-container
sam deploy \
  --stack-name cbt-registration-api \
  --capabilities CAPABILITY_IAM \
  --no-confirm-changeset \
  --resolve-s3
```
This deploys `POST /register` (API-key protected) plus the
`cbt-unattributed-raw-file` CloudWatch alarm. Grab the API URL and key:
```bash
aws cloudformation describe-stacks --stack-name cbt-registration-api \
  --query "Stacks[0].Outputs"
aws apigateway get-api-keys --query "items[?name=='<key-name>'].id" --output text
aws apigateway get-api-key --api-key <id> --include-value --query value --output text
```

**Deploy the Streamlit enrollment portal** (the primary field UI — Fitbit
OAuth + registration in one flow) on Streamlit Community Cloud, same
platform as the existing standalone auth portal:

1. **IAM user** for the app (Streamlit Cloud has no ambient AWS role):
   DynamoDB CRUD on `Participants`/`DeviceAssignments`/`SiteAssignments`
   (+ their indexes), `s3:PutObject` on the tokens bucket, `s3:ListBucket`
   on the raw sensor bucket. Create an access key for it.
2. **Push this repo to GitHub** — Streamlit Cloud deploys from a repo.
   `.streamlit/secrets.toml` (real one) must NOT be committed; only the
   `.example` is.
3. **Create the app** at share.streamlit.io → New app → this repo/branch →
   main file path `services/participant-registration/streamlit_app.py`.
   Pick the app URL/subdomain now — you need it for the next step.
4. **Fitbit dev console** (dev.fitbit.com → your app): add the Streamlit
   app's URL as a Redirect URL (keep the old portal's URL too during
   transition).
5. **Secrets**: paste `.streamlit/secrets.toml.example`'s contents, filled
   in with real values — `REDIRECT_URI` must be the app's own URL from
   step 3, exactly matching the Fitbit console entry. Both
   `FITBIT_CLIENT_ID` and `REDIRECT_URI` are required with no fallback —
   the app now refuses to render the Fitbit connect button and shows a
   clear error listing exactly what's missing if either is unset, rather
   than silently sending Fitbit a broken `client_id=None` request.
6. Deploy; run the smoke test below.

Local test alternative (uses your local AWS credentials, no PYTHONPATH
needed — the app bootstraps its own import path):
```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m streamlit run services/participant-registration/streamlit_app.py
```

**Token-file compatibility check (one-time):** the app writes
`tokens/{fitbit_id}.json` with `access_token`, `refresh_token`, `user_id`,
`token_type`, `expires_at` (+ `participant_id`, `org_id` — additive).
Compare one existing token file the current `fitbit-pull-to-s3` reads
against this shape before switching the study over:
```bash
aws s3 cp s3://<tokens-bucket>/tokens/<some-id>.json - | python -m json.tool
```

**Test it end to end:** register one real returning participant using
their known `user_id` (e.g. `user15`) and a new email. Confirm:
```bash
aws dynamodb query --table-name Participants \
  --index-name ByUserId \
  --key-condition-expression "user_id_pk = :v" \
  --expression-attribute-values '{":v":{"S":"org1#user15"}}'
```
`identity_source` should now read `legacy_migrated_email_attached` (not a
second participant record) and `email` should be populated.

---

## 6. Deploy the materializer

```bash
cd infra/materialize
sam build --use-container
sam deploy \
  --stack-name cbt-materialize \
  --capabilities CAPABILITY_IAM \
  --no-confirm-changeset \
  --resolve-s3 \
  --parameter-overrides UsersConfigBucket=<existing-config-bucket-name>
```
This subscribes to the DeviceAssignments/SiteAssignments streams and
regenerates `users.json` in the **existing** config bucket on every
change — same shape `heat-stress-predict` already reads.

**Verify:** after the Step 5 test registration, within ~a minute:
```bash
aws s3 cp s3://<existing-config-bucket>/users.json - | python -m json.tool
```
Confirm the registered Fitbit device_id key appears with the right
`participant_id`, and `provisional_baseline: true` (no calibration has run
yet).

⚠️ **Before this goes live for real inference**, confirm the field set in
`materializer.build_users_json()` (`services participant_id, user_id, sex,
height_in, weight_lbs, bmi, race, baseline_rhr, baseline_skin_temp,
provisional_baseline, site_id, clarity_id`) actually matches every field
`heat-stress-predict`'s feature-reconstruction code reads by name — the
snapshot test only checks internal consistency, not the real Lambda's
source. Diff this against the real Lambda's `users.json`-parsing code
once, by hand, before pointing it at production traffic.

---

## 7. Deploy calibration

```bash
cd infra/calibration
sam build --use-container
sam deploy \
  --stack-name cbt-calibration \
  --capabilities CAPABILITY_IAM \
  --no-confirm-changeset \
  --resolve-s3 \
  --parameter-overrides RawBucket=<existing-raw-bucket-name> OrgIds=org1
```

⚠️ **Stop and check one thing before relying on this in production:**
`services/calibration/handler.py`'s `_load_fitbit_samples()` assumes a raw
JSON schema (`timestamp`/`datetime`, `heart_rate`/`bpm`, `hr_confidence`,
`steps_nearby`/`steps`, `skin_temp` fields) that was **not** verified
against the real `fitbit-pull-to-s3` output, because that Lambda's code
wasn't available to inspect while building this. Before deploying:
```bash
aws s3 cp s3://<raw-bucket>/fitbit/raw/<a-real-device-id>/<a-real-date>/ . --recursive
cat <one-of-the-files> | python -m json.tool | head -40
```
Compare field names to `_load_fitbit_samples` in
`services/calibration/handler.py` and adjust the parsing there if they
differ (they very likely will need at least minor renaming). Re-run
`./infra/shared/build_layer.sh` and redeploy after any change.

**Test it:** the sweep runs hourly by default
(`SweepScheduleExpression`), or invoke it directly to force a run:
```bash
aws lambda invoke --function-name calibration-sweep out.json && cat out.json
```
For an assignment younger than 24h this will correctly no-op ("window not
elapsed yet"). To test the full completion path without waiting a day,
temporarily register a participant with `effective_from` backdated
36+ hours in the past (only for testing — a real registration should use
`now`), then re-invoke the sweep and check:
```bash
aws dynamodb query --table-name CalibrationHistory \
  --key-condition-expression "pk = :p" \
  --expression-attribute-values '{":p":{"S":"org1#<participant_id>"}}'
```

---

## 8. Deploy the ingestion resolver

```bash
cd infra/ingestion-resolver
sam build --use-container
sam deploy \
  --stack-name cbt-ingestion-resolver \
  --capabilities CAPABILITY_IAM \
  --no-confirm-changeset \
  --resolve-s3 \
  --parameter-overrides RawBucket=<existing-raw-bucket-name>
```

This stack deliberately does **not** own the raw bucket (it's managed by
the existing pull-Lambda stack) — grab the function ARN from the output
and wire the S3 event subscription onto the existing bucket out-of-band:

```bash
FN_ARN=$(aws cloudformation describe-stacks --stack-name cbt-ingestion-resolver \
  --query "Stacks[0].Outputs[?OutputKey=='ResolverFunctionArn'].OutputValue" --output text)

# merge with any existing notification config on the bucket — don't
# overwrite notifications the pull-Lambda stack already owns; fetch first:
aws s3api get-bucket-notification-configuration --bucket <raw-bucket> > existing-notif.json
# hand-edit existing-notif.json to add a new LambdaFunctionConfigurations entry:
#   {"LambdaFunctionArn": "$FN_ARN", "Events": ["s3:ObjectCreated:*"]}
aws s3api put-bucket-notification-configuration \
  --bucket <raw-bucket> --notification-configuration file://existing-notif.json
```

**Test it:** drop a file under a path with no matching ledger assignment
and confirm quarantine + alarm:
```bash
echo '{}' | aws s3 cp - s3://<raw-bucket>/fitbit/raw/GHOST01/2026-07-09/test.json
aws s3 ls s3://<raw-bucket>/unattributed/fitbit/GHOST01/
aws cloudwatch get-metric-statistics --namespace CBTPlatform/Ingestion \
  --metric-name UnattributedRawFile --start-time $(date -u -v-1H +%FT%TZ) \
  --end-time $(date -u +%FT%TZ) --period 300 --statistics Sum \
  --dimensions Name=DeviceType,Value=fitbit Name=OrgId,Value=org1
```
Then drop one under a real, currently-assigned device_id and confirm it
does **not** land in `unattributed/`.

---

## 9. End-to-end smoke test with a real returning participant

1. Confirm the participant exists post-migration (`identity_source:
   legacy_migrated`) — Step 4.
2. Re-register them through the Streamlit form using their known
   `user_id`, a real Fitbit pick, and their real site — Step 5. Confirm
   they relink (`created_new_participant: false`), not duplicate.
3. Confirm `users.json` in the config bucket reflects them within ~a
   minute, `provisional_baseline: true` — Step 6.
4. Confirm calibration completes once real Fitbit data accumulates (or
   fake a backdated window to test sooner) and `provisional_baseline`
   flips to `false` with a real `baseline_rhr` — Step 7.
5. Drop/observe a real raw file landing for their Fitbit and confirm it
   resolves cleanly (no quarantine entry) — Step 8.
6. At this point `heat-stress-predict` (existing, untouched) should be
   able to read the same `users.json` it always has and produce a
   prediction for this participant — verify via its existing output path
   (`cbt-predictions/`) exactly as before this phase existed.

---

## Rollback / safety notes

- Every stack is independent (`sam delete --stack-name <name>` per stack)
  — you can tear down Phase 1 without touching the existing
  pull-Lambdas, `heat-stress-predict`, or the dashboard.
- The materializer only **writes additively** to the existing config
  bucket key (`users.json`); it never deletes the bucket or other keys.
  If something looks wrong, the fastest safe rollback is to stop the
  materializer stack (`sam delete --stack-name cbt-materialize` or disable
  its stream event sources) and manually restore the last-known-good
  `users.json` from S3 versioning if the bucket has it enabled — enable
  versioning on that bucket first if it isn't already, before Step 6.
- Don't skip Step 4 before Step 5 for any real user — registering a
  legacy participant fresh (rather than migrating first) will create a
  genuine duplicate `participant_id`, which is exactly the failure mode
  Section 3b exists to prevent, and merging two participant records
  after the fact is a manual, unscripted operation this phase does not
  provide tooling for.
