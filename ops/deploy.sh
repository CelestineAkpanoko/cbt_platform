#!/usr/bin/env bash
# One-shot, idempotent rollout of the identity/attribution overhaul plus the
# pull-reliability fixes. Safe to re-run: every step either detects it is
# already done or performs an in-place update.
#
#   ops/deploy.sh check      what is deployed vs not (read-only, do this first)
#   ops/deploy.sh plan       everything that would change (read-only)
#   ops/deploy.sh apply      do it, in dependency order, stopping on failure
#   ops/deploy.sh apply 3    resume from step 3
#
# Steps must run in this order. Ordering is not cosmetic: the backfill (2)
# writes attributes the materializer (3) reads, and the pullers (5,6) read
# an artifact the materializer produces.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ORG="${CBT_ORG_ID:-org1}"
REGION="${AWS_REGION:-us-east-1}"
command -v aws >/dev/null || {
  printf '\033[31m✗\033[0m aws CLI not found on PATH.\n' >&2
  printf '   Install it (brew install awscli) or add it to PATH.\n' >&2
  exit 1
}
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)" || {
  printf '\033[31m✗\033[0m aws sts get-caller-identity failed — no valid credentials.\n' >&2
  printf '   Run `aws configure` or set AWS_PROFILE, then retry.\n' >&2
  exit 1
}
RAW_BUCKET="${RAW_BUCKET:-raw-data-all-sensors-782329476642-us-east-1-an}"
USERS_BUCKET="${USERS_BUCKET:-users-heat-stress}"
PYPATH="PYTHONPATH=shared-lib:services/assignment-ledger:services/participant-registration:."

# --- Python interpreter -----------------------------------------------------
# Resolved explicitly rather than trusting `python` to be on PATH. In an
# interactive zsh with pyenv, `python` is often a shell function or an alias,
# which does not exist in this bash subprocess — the script would then die
# with "python: command not found" even though the venv is activated.
# Order: an explicit override, the active venv, the repo's venv, then
# whatever python3/python the PATH offers.
resolve_python() {
  local candidates=(
    "${CBT_PYTHON:-}"
    "${VIRTUAL_ENV:+$VIRTUAL_ENV/bin/python}"
    "$ROOT/.venv/bin/python"
    "$(command -v python3 2>/dev/null || true)"
    "$(command -v python 2>/dev/null || true)"
  )
  for c in "${candidates[@]}"; do
    [ -n "$c" ] && [ -x "$c" ] && { echo "$c"; return 0; }
  done
  return 1
}
PYTHON="$(resolve_python)" || {
  printf '\033[31m✗\033[0m No usable Python interpreter found.\n' >&2
  printf '   Tried: $CBT_PYTHON, $VIRTUAL_ENV/bin/python, %s/.venv/bin/python,\n' "$ROOT" >&2
  printf '   python3, python. Activate the venv or set CBT_PYTHON=/path/to/python.\n' >&2
  exit 1
}

MODE="${1:-check}"
FROM_STEP="${2:-1}"

case "$MODE" in
  check|plan|apply) ;;
  *) echo "usage: $0 {check|plan|apply} [start-step 1-7]" >&2
     echo "  (there is no --commit flag; 'apply' IS the commit)" >&2
     exit 2 ;;
esac
case "$FROM_STEP" in
  ''|*[!0-9]*) echo "start-step must be a number 1-7, got: $FROM_STEP" >&2
               echo "usage: $0 {check|plan|apply} [start-step 1-7]" >&2
               exit 2 ;;
esac

ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
todo() { printf '  \033[33m•\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; }
step() { printf '\n\033[1m[%s] %s\033[0m\n' "$1" "$2"; }

run() {  # run, or just print, depending on MODE
  if [ "$MODE" = "apply" ]; then eval "$@"; else echo "      would run: $*"; fi
}

should_run() { [ "$1" -ge "$FROM_STEP" ]; }

# ---------------------------------------------------------------------------
step 0 "Preflight"
# ---------------------------------------------------------------------------
echo "  account=$ACCOUNT region=$REGION org=$ORG"
echo "  raw bucket=$RAW_BUCKET"
ok "python $("$PYTHON" -V 2>&1 | awk '{print $2}') at $PYTHON"
# Fail here, not four steps in: the ops modules all need boto3, and a
# resolved-but-wrong interpreter (system python3 instead of the venv) would
# otherwise surface as an ImportError halfway through a deploy.
"$PYTHON" -c 'import boto3' 2>/dev/null || {
  bad "$PYTHON cannot import boto3 — wrong interpreter, or deps not installed"
  echo "     activate the venv, or: $PYTHON -m pip install -r requirements.txt"
  exit 1
}
ok "boto3 importable"
command -v sam >/dev/null || { bad "aws-sam-cli not installed (brew install aws-sam-cli)"; exit 1; }
ok "sam $(sam --version 2>&1 | awk '{print $4}')"
if [ "$MODE" = "apply" ]; then
  "$PYTHON" -m pytest tests/ -q >/dev/null || { bad "tests failing — not deploying"; exit 1; }
  ok "test suite passes"
fi

# ---------------------------------------------------------------------------
step 1 "Ledger tables — ByOrg + ByFitbitId GSIs, Participants stream"
# ---------------------------------------------------------------------------
# DynamoDB allows only ONE GSI to be added per UpdateTable call, and the
# table must be ACTIVE between them. CloudFormation handles the sequencing;
# it just takes a few minutes per index while DynamoDB backfills.
gsi_names() {
  aws dynamodb describe-table --table-name Participants \
    --query 'Table.GlobalSecondaryIndexes[].IndexName' --output text 2>/dev/null || echo ""
}
wait_indexes_active() {
  # A GSI backfills after UpdateTable returns; a second GSI addition while
  # one is CREATING fails the same way two-at-once does.
  for _ in $(seq 1 90); do
    local st
    st="$(aws dynamodb describe-table --table-name Participants \
          --query 'Table.GlobalSecondaryIndexes[].IndexStatus' --output text 2>/dev/null)"
    case "$st" in *CREATING*|*UPDATING*|*DELETING*) sleep 20 ;; *) return 0 ;; esac
  done
  bad "indexes did not reach ACTIVE in 30 minutes"; return 1
}
deploy_ledger() {  # $1 = stage1 | complete
  run "aws cloudformation deploy \
        --template-file infra/ledger-tables/tables.yaml \
        --stack-name cbt-ledger-tables \
        --parameter-overrides IndexRollout=$1 \
        --capabilities CAPABILITY_IAM \
        --region $REGION"
}

HAVE_GSI="$(gsi_names)"
if [[ "$HAVE_GSI" == *ByFitbitId* && "$HAVE_GSI" == *ByOrg* ]]; then
  ok "ByOrg + ByFitbitId already present"
elif should_run 1; then
  # DynamoDB allows ONE GSI creation per UpdateTable, and CloudFormation
  # does not sequence them — a single pass adding both fails with
  # "Cannot perform more than one GSI creation or deletion in a single
  # update" and rolls the whole stack back. Hence two passes with a wait
  # between, driven by the IndexRollout parameter in the template.
  if [[ "$HAVE_GSI" != *ByOrg* ]]; then
    todo "pass 1/2 — adding ByOrg (backfills; several minutes)"
    deploy_ledger stage1
    [ "$MODE" = "apply" ] && { wait_indexes_active && ok "ByOrg ACTIVE"; }
  else
    ok "ByOrg already present — skipping pass 1"
  fi
  todo "pass 2/2 — adding ByFitbitId"
  deploy_ledger complete
  if [ "$MODE" = "apply" ]; then
    wait_indexes_active && ok "ByOrg + ByFitbitId ACTIVE"
    HAVE_GSI="$(gsi_names)"
    [[ "$HAVE_GSI" == *ByFitbitId* && "$HAVE_GSI" == *ByOrg* ]] \
      || { bad "indexes still missing after both passes: $HAVE_GSI"; exit 1; }
  fi
fi

# ---------------------------------------------------------------------------
step 2 "Backfill fitbit_id / fitbit_id_pk / org_pk onto existing participants"
# ---------------------------------------------------------------------------
# MUST come after step 1 (writes fitbit_id_pk, which needs the index to
# exist) and BEFORE step 3 (the materializer reads org_pk off the ByOrg
# index; a participant without it is silently omitted from every generated
# artifact). Idempotent — re-running reports "skipped".
if should_run 2; then
  if [ "$MODE" = "apply" ]; then
    eval "$PYPATH \"$PYTHON\" -m ops.migrate_fitbit_to_participants --org $ORG --commit"
    ok "backfill applied"
  else
    eval "$PYPATH \"$PYTHON\" -m ops.migrate_fitbit_to_participants --org $ORG" || true
  fi
fi

# ---------------------------------------------------------------------------
step 3 "Materializer — users.json + config/user_mapping.json + assignments.json"
# ---------------------------------------------------------------------------
if should_run 3; then
  run "infra/shared/build_layer.sh"
  run "sam deploy --template-file infra/materialize/template.yaml \
        --stack-name cbt-materialize \
        --capabilities CAPABILITY_IAM \
        --resolve-s3 --no-confirm-changeset \
        --region $REGION \
        --parameter-overrides UsersConfigBucket=$USERS_BUCKET RawBucket=$RAW_BUCKET"
  # `sam deploy` returns when CloudFormation reports the stack complete,
  # which can be BEFORE the DynamoDB event-source mappings finish becoming
  # Enabled. That matters because they use StartingPosition=LATEST: any
  # write that lands before a mapping exists is at a stream position the
  # mapping never reads, so it is skipped silently. (Observed: the touch
  # ran 12 seconds before the mappings were created and produced nothing.)
  if [ "$MODE" = "apply" ]; then
    echo "      waiting for stream event-source mappings to enable..."
    for _ in $(seq 1 60); do
      PENDING="$(aws lambda list-event-source-mappings \
                 --function-name ledger-materialize-users-json \
                 --query 'EventSourceMappings[?State!=`Enabled`].State' \
                 --output text 2>/dev/null)"
      COUNT="$(aws lambda list-event-source-mappings \
               --function-name ledger-materialize-users-json \
               --query 'length(EventSourceMappings)' --output text 2>/dev/null)"
      [ -z "$PENDING" ] && [ "${COUNT:-0}" -ge 3 ] && break
      sleep 10
    done
    ok "event-source mappings enabled ($COUNT)"
  fi
  # Streams only fire on NEW writes, so nothing exists until something
  # changes. Touch each participant to force a first materialization.
  run "$PYPATH \"$PYTHON\" -m ops.touch_participants --org $ORG"
  # And confirm the artifacts actually appeared — a touch that lands at the
  # wrong stream position fails silently, and everything downstream then
  # runs on a stale or absent mapping.
  if [ "$MODE" = "apply" ]; then
    for _ in $(seq 1 18); do
      aws s3api head-object --bucket "$RAW_BUCKET" \
        --key config/assignments.json >/dev/null 2>&1 && break
      sleep 10
    done
    if aws s3api head-object --bucket "$RAW_BUCKET" \
         --key config/assignments.json >/dev/null 2>&1; then
      ok "config/assignments.json + user_mapping.json generated"
    else
      bad "materializer produced nothing — check /aws/lambda/ledger-materialize-users-json"
      exit 1
    fi
  fi
fi

# ---------------------------------------------------------------------------
step 4 "Ingestion resolver — fixes the permanently-firing Clarity alarm"
# ---------------------------------------------------------------------------
if should_run 4; then
  run "infra/shared/build_layer.sh"
  run "sam deploy --template-file infra/ingestion-resolver/template.yaml \
        --stack-name cbt-ingestion-resolver \
        --capabilities CAPABILITY_IAM \
        --resolve-s3 --no-confirm-changeset \
        --region $REGION \
        --parameter-overrides RawBucket=$RAW_BUCKET"
  # S3 notification config is ONE document with no "add a rule" API, so
  # this merges rather than replaces — a naive put would delete the
  # fitbit-new-data and clarity-new-data triggers that feed
  # heat-stress-predict, stopping predictions with no error anywhere.
  if [ "$MODE" = "apply" ]; then
    eval "$PYPATH \"$PYTHON\" -m ops.wire_resolver_notifications --bucket $RAW_BUCKET --commit"
  else
    eval "$PYPATH \"$PYTHON\" -m ops.wire_resolver_notifications --bucket $RAW_BUCKET" || true
  fi
fi

# ---------------------------------------------------------------------------
step 5 "Cosinuss puller — FIX THE IAM NAME MISMATCH, then user-scoped writes"
# ---------------------------------------------------------------------------
# The scheduler's execution role grants lambda:InvokeFunction on
#   cosinuss-pull-to-s3-account-2-username-vanderbilt-app
# but the schedule targets
#   cosinuss-pull-to-s3
# so every invocation is AccessDenied. With MaximumRetryAttempts=0 and no
# DLQ that failure is completely silent — which is why a rate(1 minute)
# schedule produced 2 invocations in 24 hours.
SCHED_ROLE="Amazon_EventBridge_Scheduler_LAMBDA_9e119926a0"
POLICY_ARN="arn:aws:iam::${ACCOUNT}:policy/service-role/Amazon-EventBridge-Scheduler-Execution-Policy-c026107f-ac23-425f-a739-ae1b99ec8ad2"
if should_run 5; then
  cat > /tmp/cosinuss-invoke-policy.json <<JSON
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
 "Action":["lambda:InvokeFunction"],
 "Resource":["arn:aws:lambda:${REGION}:${ACCOUNT}:function:cosinuss-pull-to-s3",
             "arn:aws:lambda:${REGION}:${ACCOUNT}:function:cosinuss-pull-to-s3:*"]}]}
JSON
  run "aws iam create-policy-version --policy-arn $POLICY_ARN \
        --policy-document file:///tmp/cosinuss-invoke-policy.json --set-as-default"
  # Needs the shared layer: attribution.py uses the same ScopedTable and
  # participant_at() query as every other service rather than duplicating them.
  run "infra/shared/build_layer.sh"
  # Capture the ARN — publishing a layer does nothing on its own. The
  # function must also be told to USE it, and forgetting that is silent
  # until the first invocation dies with
  #   Runtime.ImportModuleError: No module named 'assignment_ledger'
  # which is exactly what happened on the first run of this step.
  if [ "$MODE" = "apply" ]; then
    LAYER_ARN="$(aws lambda publish-layer-version --layer-name cbt-shared-layer \
                 --zip-file fileb://dist/cbt-shared-layer.zip \
                 --compatible-runtimes python3.13 --region $REGION \
                 --query LayerVersionArn --output text)"
    [ -n "$LAYER_ARN" ] || { bad "layer publish returned no ARN"; exit 1; }
    ok "published $LAYER_ARN"
  else
    echo "      would run: aws lambda publish-layer-version --layer-name cbt-shared-layer ..."
    LAYER_ARN="<layer-arn>"
  fi
  run "(cd services/cosinuss-puller && zip -qr /tmp/cosinuss-pull.zip lambda_function.py attribution.py)"
  run "aws lambda update-function-code --function-name cosinuss-pull-to-s3 \
        --zip-file fileb:///tmp/cosinuss-pull.zip --region $REGION --no-cli-pager"
  run "aws lambda wait function-updated --function-name cosinuss-pull-to-s3"
  # Rendered to a JSON file in its own step so a failure here aborts the
  # script. Inlining it as $(...) inside the command would let a failed
  # render pass an EMPTY map, and --environment is a full replace: that
  # would delete COSINUSS_PASSWORD, which exists nowhere else.
  if [ "$MODE" = "apply" ]; then
    ENVFILE="$("$PYTHON" ops/render_env.py cosinuss)"
    run "aws lambda update-function-configuration --function-name cosinuss-pull-to-s3 \
          --timeout 55 --memory-size 512 \
          --layers $LAYER_ARN \
          --environment file://$ENVFILE \
          --region $REGION --no-cli-pager"
    run "aws lambda wait function-updated --function-name cosinuss-pull-to-s3"
    # attribution.py imports cbt_shared + assignment_ledger from the layer,
    # so a missing layer breaks every invocation. Prove it can start.
    if aws lambda invoke --function-name cosinuss-pull-to-s3 --payload '{}' \
         --cli-binary-format raw-in-base64-out /tmp/cbt-cosinuss-probe.json \
         --region $REGION --query FunctionError --output text 2>/dev/null \
         | grep -q "None"; then
      ok "cosinuss puller invokes cleanly"
    else
      bad "cosinuss puller failed its post-deploy probe:"
      head -c 400 /tmp/cbt-cosinuss-probe.json; echo
      exit 1
    fi
  else
    "$PYTHON" ops/render_env.py cosinuss >/dev/null && \
      ok "cosinuss env renders cleanly (secrets preserved)"
  fi
fi

# ---------------------------------------------------------------------------
step 6 "Fitbit puller — HRV/SpO2/breathing rate, tiered cadence, dead grants"
# ---------------------------------------------------------------------------
if should_run 6; then
  # Two permissions the rebuilt puller needs. Both fail SOFTLY without
  # them — the puller keeps running and looks healthy while pulling 12
  # accounts instead of 7 and retrying a dead token forever.
  if [ "$MODE" = "apply" ]; then
    eval "$PYPATH \"$PYTHON\" -m ops.grant_puller_permissions --commit"
  else
    eval "$PYPATH \"$PYTHON\" -m ops.grant_puller_permissions" || true
  fi
  "$PYTHON" -m ops.fitbit_budget --schedule-minutes "${FITBIT_SCHEDULE_MINUTES:-2}" \
    --slow-tier-minutes "${FITBIT_SLOW_TIER_MINUTES:-15}"
  run "(cd services/fitbit-puller && zip -qr /tmp/fitbit-pull.zip lambda_function.py)"
  run "aws lambda update-function-code --function-name fitbit-pull-to-s3 \
        --zip-file fileb:///tmp/fitbit-pull.zip --region $REGION --no-cli-pager"
  run "aws lambda wait function-updated --function-name fitbit-pull-to-s3"
  # Timeout BELOW the schedule interval so a slow run is killed rather than
  # allowed to overlap the next one and compete for the same per-user quota.
  if [ "$MODE" = "apply" ]; then
    ENVFILE="$("$PYTHON" ops/render_env.py fitbit)"
    run "aws lambda update-function-configuration --function-name fitbit-pull-to-s3 \
          --timeout 100 --memory-size 512 \
          --environment file://$ENVFILE \
          --region $REGION --no-cli-pager"
  else
    "$PYTHON" ops/render_env.py fitbit >/dev/null && \
      ok "fitbit env renders cleanly (secrets preserved)"
  fi
  # update-schedule is a full replace; ops/set_schedule.py reads the
  # existing schedule and changes only the rate, so the target's role,
  # input and retry policy survive.
  if [ "$MODE" = "apply" ]; then
    eval "$PYPATH \"$PYTHON\" -m ops.set_schedule fitbit-collector-every-1-minute \
          'rate(${FITBIT_SCHEDULE_MINUTES:-2} minutes)' --commit"
  else
    eval "$PYPATH \"$PYTHON\" -m ops.set_schedule fitbit-collector-every-1-minute \
          'rate(${FITBIT_SCHEDULE_MINUTES:-2} minutes)'" || true
  fi
  # The schedule name says "every-1-minute" and no longer will; renaming a
  # schedule means delete + recreate, which is a worse trade than a
  # misleading name. docs/deployment.md records the discrepancy.
fi

# ---------------------------------------------------------------------------
step 7 "Registration form (Streamlit Cloud) — manual, see docs/deployment.md"
# ---------------------------------------------------------------------------
if should_run 7; then
  todo "push to the branch Streamlit Cloud tracks, then reboot the app"
  todo "no secret changes needed: CLARITY_ID is now a supplement, not the source"
fi

printf '\n\033[1mDone (%s).\033[0m Verify with: ops/verify.sh\n' "$MODE"
