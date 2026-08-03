"""Render a Lambda environment-variable map for `update-function-configuration`.

`aws lambda update-function-configuration --environment` REPLACES the whole
map. Passing only the new keys would wipe `COSINUSS_PASSWORD`,
`FITBIT_CLIENT_SECRET`, `CLARITY_API_KEY` and the bucket names — the
function would come back broken and the credentials would be unrecoverable
from AWS (they exist nowhere else; see the note at the bottom).

So: read what is deployed, merge the new keys over it, write the result to
a JSON file, and print that file's path for the caller to pass as
`--environment file://<path>`. Existing values always win unless this
script explicitly overrides them.

JSON, not the `Key=Value,Key=Value` shorthand: the shorthand has no
quoting, so it cannot express a value containing a comma — and
`TOKEN_PREFIX` is exactly that (`fitbit_tokens/,fitbit-tokens/`). Rendering
it as shorthand truncates the map, and a truncated map passed to
update-function-configuration deletes every variable it omits.

    python ops/render_env.py cosinuss   # -> /tmp/cbt-env-cosinuss.json
    python ops/render_env.py fitbit
"""

from __future__ import annotations

import json
import sys

import boto3

FUNCTIONS = {
    "cosinuss": "cosinuss-pull-to-s3",
    "fitbit": "fitbit-pull-to-s3",
}

# Keys this rollout adds or changes. Anything not listed is carried through
# from the deployed configuration untouched.
OVERRIDES = {
    "cosinuss": {
        # Attribution at write time needs the ledger.
        "CBT_ORG_ID": "org1",
        "DEVICE_TABLE": "DeviceAssignments",
        "PARTICIPANTS_TABLE": "Participants",
        "ASSIGNMENTS_KEY": "config/assignments.json",
        # 72h of lookback re-lists three days of sessions every single
        # minute. Once the schedule actually fires (see the IAM fix in
        # ops/deploy.sh step 5) that is wasted work on all but the first
        # run of a session; 6h still comfortably covers a shift plus any
        # catch-up after a brief outage.
        "COSINUSS_LOOKBACK_HOURS": "6",
    },
    "fitbit": {
        # Tiering — the change that brings the request budget back under
        # Fitbit's 150/hour/participant ceiling. Verify any change with
        # `python -m ops.fitbit_budget` before applying it.
        "SLOW_TIER_MINUTES": "15",
        # Must match the EventBridge Scheduler rate. Tier selection is
        # derived from the wall clock, so the function has to know how
        # wide its own slot is — see the slow-tier comment in
        # lambda_function.lambda_handler.
        "SCHEDULE_MINUTES": "2",
        "GRACE_WINDOW_DAYS": "1",
        "MAX_RETRIES_429": "1",
        # Pull only enrolled participants, from the generated mapping.
        "PULL_ENROLLED_ONLY": "true",
        "USER_MAPPING_KEY": "config/user_mapping.json",
        # DELIBERATELY still reads both prefixes.
        #
        # fitbit_tokens/ is canonical and the Streamlit app now pins it in
        # code — but that pin only takes effect once the app is pushed AND
        # rebooted. Until then the deployed app still honours its
        # S3_TOKEN_PREFIX secret and keeps writing to fitbit-tokens/.
        # Narrowing the reader before the writer is fixed makes every newly
        # authorized token invisible: exactly what happened at 03:21 on
        # 2026-08-03, when DDVGQ4 landed in the hyphenated prefix two
        # minutes after it had been narrowed away.
        #
        # Narrow this to "fitbit_tokens/" ONLY after ops/verify.sh reports
        # no tokens under the legacy prefix across a full day of new
        # registrations.
        "TOKEN_PREFIX": "fitbit_tokens/,fitbit-tokens/",
    },
}


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in FUNCTIONS:
        sys.exit(f"usage: render_env.py {{{'|'.join(FUNCTIONS)}}}")
    which = sys.argv[1]

    client = boto3.client("lambda")
    current = client.get_function_configuration(
        FunctionName=FUNCTIONS[which]
    ).get("Environment", {}).get("Variables", {})

    if not current:
        sys.exit(
            f"{FUNCTIONS[which]} reported no environment variables. Refusing "
            f"to write a config that would drop its credentials — check the "
            f"function name and your AWS profile."
        )

    merged = {**current, **OVERRIDES[which]}
    # Sanity: never emit fewer keys than are deployed. The only way that
    # happens is a bug here, and the cost is the function losing its
    # credentials for good.
    missing = set(current) - set(merged)
    if missing:
        sys.exit(f"refusing to drop deployed variables: {sorted(missing)}")

    path = f"/tmp/cbt-env-{which}.json"
    with open(path, "w") as fh:
        json.dump({"Variables": {k: str(v) for k, v in sorted(merged.items())}},
                  fh, indent=2)
    print(path)


if __name__ == "__main__":
    main()

# NOTE, out of scope for this rollout but worth scheduling: the pullers hold
# COSINUSS_PASSWORD, FITBIT_CLIENT_SECRET and CLARITY_API_KEY as plaintext
# Lambda environment variables. They are readable by anyone with
# lambda:GetFunctionConfiguration, they appear in CloudTrail, and this
# script has to defensively round-trip them precisely because they exist
# nowhere else. They belong in Secrets Manager.
