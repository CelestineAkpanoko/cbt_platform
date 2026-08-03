"""Grant the IAM permissions the rebuilt pullers need.

Every one of these was missing on first deploy, and every one fails
*softly* — which is worse than failing loudly, because the puller keeps
running and looks healthy while doing the wrong thing:

  s3:GetObject on <raw>/config/*
      Reads the generated user_mapping.json to pull only enrolled
      participants. Without it the puller logs a warning and falls back to
      "pull every token in the bucket" — 12 accounts instead of 7, burning
      wall-clock time in a run that has to finish inside its schedule
      interval. Deliberately a soft failure (coverage matters more than
      cost), which is exactly why it needs checking rather than trusting.

  s3:DeleteObject on the token prefixes
      Completes the dead-grant quarantine. The copy to
      <key>.dead-grant.bak succeeds without it, but the original stays put,
      so the puller re-discovers the dead token and retries it on every
      single run — forever. Observed: C2W5PD failing every 2 minutes with
      the .bak already sitting next to it.

Read-modify-write on the existing inline policy: statements are matched by
Sid so re-running replaces rather than duplicates, and nothing already in
the policy is dropped.

    python -m ops.grant_puller_permissions
    python -m ops.grant_puller_permissions --commit
"""

from __future__ import annotations

import argparse
import json

import boto3

RAW_BUCKET = "raw-data-all-sensors-782329476642-us-east-1-an"
TOKEN_BUCKET = "fitbit-study-tokens-stored"
ACCOUNT = "782329476642"
REGION = "us-east-1"

FITBIT_ROLE = "FitbitLambdaExecutionRole"
FITBIT_POLICY = "FitbitLambdaInlinePolicy"
COSINUSS_ROLE = "cosinuss-lambda-s3-role"
COSINUSS_POLICY = "cosinuss-s3-read-write-policy"


def _table(name: str) -> str:
    return f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{name}"


COSINUSS_STATEMENTS = [
    {
        "Sid": "ReadAssignmentLedger",
        "Effect": "Allow",
        "Action": ["dynamodb:Query", "dynamodb:GetItem"],
        "Resource": [
            _table("DeviceAssignments"),
            _table("DeviceAssignments") + "/index/*",
            _table("Participants"),
            _table("Participants") + "/index/*",
        ],
    },
    {
        "Sid": "ReadGeneratedConfig",
        "Effect": "Allow",
        "Action": ["s3:GetObject"],
        "Resource": [f"arn:aws:s3:::{RAW_BUCKET}/config/*"],
    },
]

FITBIT_STATEMENTS = [
    {
        "Sid": "ReadGeneratedConfig",
        "Effect": "Allow",
        "Action": ["s3:GetObject"],
        "Resource": [f"arn:aws:s3:::{RAW_BUCKET}/config/*"],
    },
    {
        "Sid": "QuarantineDeadGrants",
        "Effect": "Allow",
        "Action": ["s3:DeleteObject"],
        "Resource": [
            f"arn:aws:s3:::{TOKEN_BUCKET}/fitbit_tokens/*",
            f"arn:aws:s3:::{TOKEN_BUCKET}/fitbit-tokens/*",
        ],
    },
]


def merge(document: dict, statements: list) -> dict:
    ours = {s["Sid"] for s in statements}
    kept = [s for s in document.get("Statement", []) if s.get("Sid") not in ours]
    return {"Version": document.get("Version", "2012-10-17"),
            "Statement": kept + statements}


def apply_to(iam, role: str, policy: str, statements: list, commit: bool):
    current = iam.get_role_policy(RoleName=role, PolicyName=policy)["PolicyDocument"]
    updated = merge(current, statements)

    before = len(current.get("Statement", []))
    after = len(updated["Statement"])
    if after < before:
        raise SystemExit(f"refusing: merge would drop statements on {role}")

    print(f"\n{role}/{policy}: {before} statement(s) -> {after}")
    for s in statements:
        print(f"  + {s['Sid']}: {', '.join(s['Action'])}")
        for r in s["Resource"]:
            print(f"      {r}")
    if commit:
        iam.put_role_policy(RoleName=role, PolicyName=policy,
                            PolicyDocument=json.dumps(updated))


def main():  # pragma: no cover - CLI
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    iam = boto3.client("iam")
    apply_to(iam, FITBIT_ROLE, FITBIT_POLICY, FITBIT_STATEMENTS, args.commit)
    apply_to(iam, COSINUSS_ROLE, COSINUSS_POLICY, COSINUSS_STATEMENTS, args.commit)

    if not args.commit:
        print("\nDRY RUN — re-run with --commit to apply.")
        return
    print("\nApplied. IAM changes can take a few seconds to propagate.")


if __name__ == "__main__":  # pragma: no cover
    main()
