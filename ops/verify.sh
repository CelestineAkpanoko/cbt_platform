#!/usr/bin/env bash
# End-to-end health check. Read-only — safe to run any time, and the right
# thing to run after each ops/deploy.sh step and once a day afterwards.
#
# Every check answers a question that has actually gone wrong here before,
# rather than just confirming a resource exists.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT"
ORG="${CBT_ORG_ID:-org1}"
RAW_BUCKET="${RAW_BUCKET:-raw-data-all-sensors-782329476642-us-east-1-an}"
USERS_BUCKET="${USERS_BUCKET:-users-heat-stress}"
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

command -v aws >/dev/null || { echo "aws CLI not found on PATH."; exit 1; }

FAIL=0

ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; FAIL=1; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
hdr()  { printf '\n\033[1m%s\033[0m\n' "$*"; }

hdr "1. Ledger indexes"
G="$(aws dynamodb describe-table --table-name Participants \
     --query 'Table.GlobalSecondaryIndexes[].[IndexName,IndexStatus]' --output text 2>/dev/null)"
for idx in ByOrg ByFitbitId ByUserId ByEmail; do
  L="$(echo "$G" | grep -w "$idx" || true)"
  if [ -z "$L" ]; then bad "$idx missing"
  elif [[ "$L" != *ACTIVE* ]]; then warn "$idx $(echo "$L" | awk '{print $2}')"
  else ok "$idx ACTIVE"; fi
done
[ "$(aws dynamodb describe-table --table-name Participants \
     --query 'Table.StreamSpecification.StreamEnabled' --output text 2>/dev/null)" = "True" ] \
  && ok "Participants stream enabled" || bad "Participants stream NOT enabled"

hdr "2. Backfill — every participant must carry org_pk"
# A participant without org_pk is absent from the ByOrg index, so the
# materializer omits them from every artifact with no error raised.
aws dynamodb scan --table-name Participants \
  --filter-expression "attribute_exists(user_id) AND attribute_not_exists(org_pk)" \
  --query 'Items[].user_id.S' --output text 2>/dev/null | tr '\t' '\n' | grep -v '^$' \
  | while read -r u; do echo "MISSING:$u"; done > /tmp/_nopk
if [ -s /tmp/_nopk ]; then
  bad "participants missing org_pk (invisible to the materializer): $(tr '\n' ' ' < /tmp/_nopk)"
  echo "      fix: $PYTHON -m ops.migrate_fitbit_to_participants --org $ORG --commit"
else ok "all participants carry org_pk"; fi

hdr "3. Generated artifacts"
for k in config/user_mapping.json config/assignments.json; do
  if aws s3api head-object --bucket "$RAW_BUCKET" --key "$k" >/dev/null 2>&1; then
    # LastModified is UTC — `date -j -f` without -u would parse it as
    # local time and report a negative age.
    AGE=$(( $(date -u +%s) - $(date -u -j -f "%Y-%m-%dT%H:%M:%S" \
          "$(aws s3api head-object --bucket "$RAW_BUCKET" --key "$k" \
             --query LastModified --output text | cut -d+ -f1 | cut -d. -f1)" +%s 2>/dev/null || echo 0) ))
    ok "$k present (${AGE}s old)"
  else
    bad "$k MISSING — run: $PYTHON -m ops.touch_participants --org $ORG"
  fi
done
# The count that matters: the mapping must cover every enrolled participant,
# because batch-clean's fallback for an unmapped device is to use the device
# id AS the participant id.
LEDGER_N=$(aws dynamodb scan --table-name Participants \
  --filter-expression "attribute_exists(user_id)" --select COUNT --query Count --output text 2>/dev/null)
MAP_N=$(aws s3 cp "s3://$RAW_BUCKET/config/user_mapping.json" - 2>/dev/null \
  | "$PYTHON" -c "import json,sys;print(len(json.load(sys.stdin).get('fitbit',{})))" 2>/dev/null || echo 0)
if [ "$MAP_N" -ge "$LEDGER_N" ] 2>/dev/null; then
  ok "user_mapping.json covers $MAP_N/$LEDGER_N participants"
else
  bad "user_mapping.json has $MAP_N fitbit entries for $LEDGER_N participants"
fi

hdr "4. Pull schedules actually firing"
for f in fitbit-pull-to-s3 cosinuss-pull-to-s3 clarity-pull-to-s3; do
  N=$(aws cloudwatch get-metric-statistics --namespace AWS/Lambda \
      --metric-name Invocations --dimensions Name=FunctionName,Value=$f \
      --start-time "$(date -u -v-1H +%Y-%m-%dT%H:%M:%SZ)" \
      --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --period 3600 --statistics Sum \
      --query 'Datapoints[0].Sum' --output text 2>/dev/null)
  E=$(aws cloudwatch get-metric-statistics --namespace AWS/Lambda \
      --metric-name Errors --dimensions Name=FunctionName,Value=$f \
      --start-time "$(date -u -v-1H +%Y-%m-%dT%H:%M:%SZ)" \
      --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --period 3600 --statistics Sum \
      --query 'Datapoints[0].Sum' --output text 2>/dev/null)
  # Also look at the last 10 minutes. A one-hour window keeps reporting
  # errors from before a fix landed, which reads as "still broken" long
  # after it is fixed — and the reverse, a failure that started 2 minutes
  # ago barely moves the hourly count.
  E10=$(aws cloudwatch get-metric-statistics --namespace AWS/Lambda \
      --metric-name Errors --dimensions Name=FunctionName,Value=$f \
      --start-time "$(date -u -v-10M +%Y-%m-%dT%H:%M:%SZ)" \
      --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --period 600 --statistics Sum \
      --query 'Datapoints[0].Sum' --output text 2>/dev/null)
  N=${N%.*}; E=${E%.*}; E10=${E10%.*}
  N=${N:-0}; E=${E:-0}; E10=${E10:-0}
  [ "$N" = "None" ] && N=0; [ "$E" = "None" ] && E=0; [ "$E10" = "None" ] && E10=0
  if [ "$N" -lt 2 ]; then
    bad "$f: $N invocations in the last hour — the schedule is not firing"
    [ "$f" = "cosinuss-pull-to-s3" ] && \
      echo "      likely the scheduler role/function-name mismatch — ops/deploy.sh step 5"
  elif [ "$E10" -gt 0 ]; then
    bad "$f: $E10 error(s) in the last 10 min ($E in the hour, $N invocations)"
  elif [ "$E" -gt 0 ]; then
    ok "$f: $N invocations, 0 errors in the last 10 min ($E earlier in the hour — likely pre-fix)"
  else
    ok "$f: $N invocations, 0 errors in the last hour"
  fi
done

hdr "5. Fitbit request budget"
"$PYTHON" -m ops.fitbit_budget --schedule-minutes "${FITBIT_SCHEDULE_MINUTES:-2}" \
  --slow-tier-minutes "${FITBIT_SLOW_TIER_MINUTES:-15}" 2>/dev/null | tail -3

hdr "6. Fitbit tokens: authorized but never enrolled"
# The gap that produced "new tokens aren't flowing". OAuth writes the token
# BEFORE the registration form is shown, so a form that errors leaves a
# valid token with no participant record. PULL_ENROLLED_ONLY then correctly
# skips it — no consent, no collection — but from the participant's side it
# looks identical to a broken pipeline. These people are mid-enrolment and
# need to finish the form; nothing else recovers them.
TOKEN_BUCKET="${TOKEN_BUCKET:-fitbit-study-tokens-stored}"
ENROLLED=$(aws s3 cp "s3://$RAW_BUCKET/config/user_mapping.json" - 2>/dev/null \
  | "$PYTHON" -c "import json,sys;print(' '.join(json.load(sys.stdin).get('fitbit',{})))" 2>/dev/null)
PENDING=""
for k in $(aws s3 ls "s3://$TOKEN_BUCKET/fitbit_tokens/" 2>/dev/null \
           | awk '{print $4}' | grep '\.json$' | sed 's/\.json$//'); do
  case " $ENROLLED " in *" $k "*) ;; *) PENDING="$PENDING $k" ;; esac
done
# Anything with a stray hyphenated prefix is a regression: the app pins its
# write prefix in code now, so a file here means something else is writing.
# NB: `grep -c` on empty input prints 0 AND exits 1, so a trailing
# `|| echo 0` appends a second 0 and the test sees "0\n0". Count with awk,
# which exits 0 either way.
STRAY=$(aws s3 ls "s3://$TOKEN_BUCKET/fitbit-tokens/" 2>/dev/null \
        | awk '/\.json$/{n++} END{print n+0}')
if [ "${STRAY:-0}" -gt 0 ]; then
  bad "$STRAY token(s) under the legacy fitbit-tokens/ prefix — nothing reads it"
  echo "      fix: $PYTHON -m ops.migrate_token_prefix --commit --delete-source"
else
  ok "no tokens under the legacy fitbit-tokens/ prefix"
fi
if [ -n "$PENDING" ]; then
  warn "authorized but NOT enrolled:$PENDING"
  echo "      these completed Fitbit OAuth but never finished the form, so no"
  echo "      participant record exists and no data is collected for them."
  echo "      They must re-open the enrollment link and submit the form."
else
  ok "every stored token belongs to an enrolled participant"
fi

hdr "7. Data actually landing (freshness)"
TODAY=$(date -u +%Y-%m-%d)
for pid in $(aws s3 cp "s3://$RAW_BUCKET/config/user_mapping.json" - 2>/dev/null \
             | "$PYTHON" -c "import json,sys;print(' '.join(json.load(sys.stdin).get('fitbit',{})))" 2>/dev/null); do
  HR="fitbit/raw/$pid/$TODAY/heart_rate_intraday.json"
  if aws s3api head-object --bucket "$RAW_BUCKET" --key "$HR" >/dev/null 2>&1; then
    # HRV is the metric that was never being pulled at all
    if aws s3api head-object --bucket "$RAW_BUCKET" \
         --key "fitbit/raw/$pid/$TODAY/hrv_intraday.json" >/dev/null 2>&1; then
      ok "$pid: heart rate + HRV present for $TODAY"
    else
      warn "$pid: heart rate present, no HRV yet for $TODAY (slow tier, or no sleep record)"
    fi
  else
    warn "$pid: no heart-rate file for $TODAY (watch may not have synced)"
  fi
done

hdr "8. Ingestion resolver actually processing"
# A resolver that is invoked but logs nothing is the signature of deployed
# code that predates the EventBridge wiring: the old handler read only
# event["Records"], which EventBridge does not send, so every invocation
# succeeded in ~30ms having done nothing. Invocations alone are not proof.
RINV=$(aws cloudwatch get-metric-statistics --namespace AWS/Lambda \
    --metric-name Invocations --dimensions Name=FunctionName,Value=ingestion-resolver \
    --start-time "$(date -u -v-1H +%Y-%m-%dT%H:%M:%SZ)" \
    --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --period 3600 --statistics Sum \
    --query 'Datapoints[0].Sum' --output text 2>/dev/null)
RINV=${RINV%.*}; RINV=${RINV:-0}; [ "$RINV" = "None" ] && RINV=0
# filter-log-events paginates, and --query runs per page, so length(events)
# prints one number per page. Sum them rather than taking the first.
RLOG=$(aws logs filter-log-events --log-group-name /aws/lambda/ingestion-resolver \
    --start-time $(( ($(date +%s) - 3600) * 1000 )) --filter-pattern '"resolved"' \
    --query 'length(events)' --output text 2>/dev/null \
    | awk '{t+=$1} END{print t+0}')
RLOG=${RLOG:-0}
if [ "$RINV" -lt 1 ]; then
  bad "ingestion-resolver: 0 invocations this hour — EventBridge rule not delivering"
elif [ "$RLOG" -lt 1 ]; then
  bad "ingestion-resolver: $RINV invocations but 0 resolution log lines"
  echo "      the deployed code is probably older than the EventBridge wiring"
  echo "      (the old handler read event['Records'], which EventBridge never sends)"
  echo "      fix: sam deploy --template-file infra/ingestion-resolver/template.yaml ..."
else
  ok "ingestion-resolver: $RINV invocations, $RLOG file(s) resolved this hour"
fi

hdr "9. Quarantine / unattributed"
Q=$(aws s3 ls "s3://$RAW_BUCKET/unattributed/" --recursive 2>/dev/null | wc -l | tr -d ' ')
if [ "$Q" -gt 0 ]; then
  warn "$Q quarantined file(s) — breakdown:"
  aws s3 ls "s3://$RAW_BUCKET/unattributed/" --recursive 2>/dev/null \
    | awk '{print $4}' | cut -d/ -f2 | sort | uniq -c | sed 's/^/        /'
  echo "      Clarity entries dated before the resolver deploy are expected"
  echo "      (every Clarity file quarantined under the old key parser)."
else
  ok "no quarantined files"
fi

hdr "10. Cosinuss layout"
if aws s3 ls "s3://$RAW_BUCKET/cosinuss/raw/" 2>/dev/null | grep -qE 'PRE (user|_unassigned)'; then
  ok "user-scoped layout in use"
  aws s3 ls "s3://$RAW_BUCKET/cosinuss/raw/" 2>/dev/null | sed 's/^/        /'
else
  warn "still flat (receiver-keyed) — puller not yet cut over; both layouts are read"
fi

if [ "$FAIL" -eq 0 ]; then printf '\n\033[32mAll critical checks passed.\033[0m\n'
else printf '\n\033[31mSome critical checks failed — see above.\033[0m\n'; fi
exit $FAIL
